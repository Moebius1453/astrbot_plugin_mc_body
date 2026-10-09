"""让白（Minecraft 里的 `Nanako`）长出手脚。

数据流：
    QQ ──→ 白 ──→ 本插件的 @filter.llm_tool ──→ RCON 隧道 ──→ R820 服务端桥
                                                              ──→ Nanako 的客户端
                                                              ──→ Baritone

**这一版就这些工具，全部走已经跑通的客户端动作**（`say` / `baritone` / `stop`），
不需要改客户端、不需要重启 Nanako 的客户端。

⚠️ **动作类工具只能报告"已下发"，不能报告"完成了"。** 服务端只知道动作发给了她的客户端；
客户端到底执行到哪一步，得靠 `mc_state` 复核坐标。所以动作类工具的返回值里都带着
"过一会儿用 mc_state 确认"的提示 —— 她也得像人一样先做、再去看结果。

授权：**默认拒绝**。必须在插件配置的 `allowed_sender_ids` 里列出允许的发送者 ID。
这是刻意的 —— 这组工具能让一个角色在世界里走动、说话、挖东西。
"""

from __future__ import annotations

import math
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .mc_rcon import BridgeError, RconBridge
from .mc_uplink import ChatUplink

PLUGIN_NAME = "mc_body"

# Minecraft 的世界边界
COORD_LIMIT = 30_000_000
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
MAX_SAY_LEN = 200
MAX_RAW_LEN = 200


@register(
    "astrbot_plugin_mc_body",
    "Moebius1453",
    "让 AstrBot 的智能体在 Minecraft 里长出手脚：查询角色状态、说话、移动。",
    "0.2.0",
)
class McBodyPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.bridge = RconBridge(
            host=str(self._cfg("rcon_host", "127.0.0.1")),
            port=int(self._cfg("rcon_port", 25575)),
            password=str(self._cfg("rcon_password", "")),
            timeout=float(self._cfg("rcon_timeout_seconds", 10)),
        )
        self.uplink = ChatUplink(
            self.bridge,
            context,
            umo=str(self._cfg("white_session", "") or ""),
            poll_interval=float(self._cfg("poll_interval_seconds", 2)),
            wake_keywords=self._wake_keywords(),
            ambient_limit=int(self._cfg("ambient_context_lines", 20)),
            self_name=str(self._cfg("character_name", "Nanako")),
        )

    # ---- 生命周期 -------------------------------------------------------

    async def initialize(self) -> None:
        if not self.bridge.password:
            logger.warning(
                f"[{PLUGIN_NAME}] 未配置 rcon_password，MC 工具会直接报错。"
            )
        if not self._allowed_ids():
            logger.warning(
                f"[{PLUGIN_NAME}] allowed_sender_ids 为空 —— MC 工具对所有人关闭。"
            )

        if self._cfg("enable_chat_uplink", True):
            if not self.uplink.umo:
                logger.warning(
                    f"[{PLUGIN_NAME}] 开了聊天上行但没配 white_session —— **上行不会启动**。"
                    "请在插件配置里填上白的会话（形如 平台名:FriendMessage:QQ号）。"
                )
            else:
                self.uplink.start()
        else:
            logger.info(f"[{PLUGIN_NAME}] 聊天上行已关闭（enable_chat_uplink=false）")

        logger.info(
            f"[{PLUGIN_NAME}] 已加载，RCON 目标 {self.bridge.host}:{self.bridge.port}"
        )

    async def terminate(self) -> None:
        await self.uplink.stop()
        await self.bridge.close()
        logger.info(f"[{PLUGIN_NAME}] 已卸载，RCON 连接已关闭")

    # ---- 配置与授权 -----------------------------------------------------

    def _cfg(self, key: str, default):
        value = self.config.get(key) if hasattr(self.config, "get") else None
        return default if value is None else value

    def _allowed_ids(self) -> set[str]:
        raw = self._cfg("allowed_sender_ids", []) or []
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        return {str(item).strip() for item in raw if str(item).strip()}

    def _wake_keywords(self) -> list[str]:
        """游戏里出现这些词才算"被点名"，才唤醒白。"""
        raw = self._cfg("wake_keywords", ["Nanako", "白"]) or []
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        return [str(item).strip() for item in raw if str(item).strip()]

    def _authorize(self, event: AstrMessageEvent) -> str | None:
        """返回拒绝理由；None 表示放行。"""
        sender = str(event.get_sender_id())
        allowed = self._allowed_ids()
        if not allowed:
            return (
                "MC 工具还没授权任何调用者，所以暂时对所有人关闭。"
                "（管理员请在 AstrBot 插件配置的 allowed_sender_ids 里填入允许的发送者 ID）"
            )
        if sender not in allowed:
            logger.info(f"[{PLUGIN_NAME}] 拒绝未授权调用 sender={sender}")
            return f"你没有调用 Minecraft 工具的权限（你的 ID：{sender}）。"
        return None

    # ---- 与桥对话 -------------------------------------------------------

    async def _call(self, command: str) -> tuple[dict | None, str | None]:
        """发一条桥命令。返回 `(data, 错误文案)`，两者恰有一个非 None。"""
        try:
            reply = await self.bridge.call(command)
        except BridgeError as exc:
            return None, f"没送到桥上：{exc}"
        if not reply.get("ok"):
            return None, str(reply.get("error") or "桥报了失败但没说原因")
        data = reply.get("data")
        return (data if isinstance(data, dict) else {}), None

    # ---- 参数校验（工具边界）---------------------------------------------

    @staticmethod
    def _clean_text(raw: str, limit: int = MAX_SAY_LEN) -> str | None:
        """剥掉换行（防注入）并限长。空则返回 None。"""
        if raw is None:
            return None
        text = str(raw).replace("\r", " ").replace("\n", " ").strip()
        if not text:
            return None
        return text[:limit]

    @staticmethod
    def _clean_player(raw: str) -> str | None:
        name = str(raw or "").strip()
        return name if PLAYER_NAME_RE.match(name) else None

    @staticmethod
    def _clean_coord(value) -> int | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or abs(number) > COORD_LIMIT:
            return None
        return int(round(number))

    # ---- 工具 -----------------------------------------------------------

    @filter.llm_tool(name="mc_state")
    async def mc_state(self, event: AstrMessageEvent):
        """查询你在 Minecraft 里的状态：是否在线、坐标、血量、维度、正在做什么。

        当你想知道自己现在在哪、还活着没有、在哪个维度、或者**刚才的动作有没有生效**时，
        用这个工具。它读的是服务端的即时数据，不依赖你的游戏客户端。

        如果返回"不在线"，说明 Nanako 的角色没连进服务器，任何动作都做不了。
        动作类工具（走路、跟随）下发之后，**必须**隔一会儿用它来确认结果。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        data, err = await self._call("mcb state")
        if err:
            return f"查不到 Nanako 的状态：{err}"
        return self._describe_state(data)

    @filter.llm_tool(name="mc_say")
    async def mc_say(self, event: AstrMessageEvent, text: str):
        """在 Minecraft 里说话 —— 用 Nanako 的身体把这句文本发到游戏聊天里。

        当你想对游戏里的人（包括你自己在游戏里的样子）说点什么时用这个。
        注意这是**游戏内聊天**，不是回复 QQ 消息；要在 QQ 里回话直接正常输出就行。

        Args:
            text(string): 要说的话。会发到游戏公屏，所有人都看得见。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        clean = self._clean_text(text, MAX_SAY_LEN)
        if clean is None:
            return "要说的话是空的，或者只包含换行 —— 没发出去。"
        _, err = await self._call(f"mcb say {clean}")
        if err:
            return f"没能在游戏里说话：{err}"
        return f"已经用 Nanako 的身体在游戏里说了：{clean}"

    @filter.llm_tool(name="mc_goto")
    async def mc_goto(self, event: AstrMessageEvent, x: float, z: float):
        """让 Nanako 走到指定坐标（她自己寻路过去）。

        当你想移动到某个地点时用这个。**这是异步的** —— 命令下发后她会自己走，
        不会立刻到。想确认到没到，过一会儿调 mc_state 看坐标。

        Args:
            x(number): 目标 X 坐标
            z(number): 目标 Z 坐标
        """
        denied = self._authorize(event)
        if denied:
            return denied
        tx, tz = self._clean_coord(x), self._clean_coord(z)
        if tx is None or tz is None:
            return f"坐标不合法（x={x} z={z}）。要在世界边界 ±{COORD_LIMIT} 以内的数字。"
        _, err = await self._call(f"mcb baritone goto {tx} {tz}")
        if err:
            return f"没能让她出发：{err}"
        return (
            f"已让她出发前往 x={tx} z={tz}。**她现在还在路上** —— "
            "这个工具只能报告「已下发」，不知道她到没到。"
            "过一会儿调 mc_state 看坐标，确认她是不是真的到了。"
        )

    @filter.llm_tool(name="mc_follow")
    async def mc_follow(self, event: AstrMessageEvent, player: str):
        """让 Nanako 跟着某个玩家走。

        当你想让她跟在你身边时用这个。

        ⚠️ 这是「跟到几格以内」，**不是贴身**。目标就站在旁边时她**不会动**，
        那是正常行为，不是坏了。

        Args:
            player(string): 要跟随的玩家名（游戏 ID，只允许字母数字下划线）。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        name = self._clean_player(player)
        if name is None:
            return f"玩家名不合法：{player!r}。只允许 1-16 位字母、数字、下划线。"
        _, err = await self._call(f"mcb baritone follow player {name}")
        if err:
            return f"没能让她跟随：{err}"
        return (
            f"已让她开始跟随 {name}。记住这是「跟到几格以内」不是贴身，"
            "目标就在旁边时她不动是正常的。"
        )

    @filter.llm_tool(name="mc_stop")
    async def mc_stop(self, event: AstrMessageEvent):
        """让 Nanako 立刻停下：取消当前所有寻路、行走、跟随。

        当你想让她别动了、或者发现她卡住/走错方向时用这个。
        这是最安全的"急停"。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        _, err = await self._call("mcb stop")
        if err:
            return f"没能让她停下：{err}"
        return "已下发停止指令，她应该会停下来（惯性可能还会滑一小段）。"

    @filter.llm_tool(name="mc_baritone_raw")
    async def mc_baritone_raw(self, event: AstrMessageEvent, command: str):
        """应急口：直接给 Baritone 下一条原始命令。

        只有在上面那些固定工具**做不到**你要的事时才用它。常见的：
        `goto 100 64 -200`（带 Y 坐标的 goto）、`mine diamond_ore`（挖矿）、
        `explore`（探索）、`farm`、`come`、`thisway 100`。

        Args:
            command(string): Baritone 命令本体，**不带 `#` 前缀**。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        clean = self._clean_text(command, MAX_RAW_LEN)
        if clean is None:
            return "命令是空的。"
        clean = clean.lstrip("#").strip()
        if not clean:
            return "命令是空的。"
        _, err = await self._call(f"mcb baritone {clean}")
        if err:
            return f"没能执行：{err}"
        return f"已把 Baritone 命令下发出去：{clean}。过一会儿用 mc_state 看效果。"

    # ---- 输出整形 -------------------------------------------------------

    @classmethod
    def _describe_state(cls, data: dict) -> str:
        if not data.get("online"):
            return (
                f"Nanako 现在**不在线** —— 角色没连进服务器，任何动作都做不了。"
            )

        parts: list[str] = []
        if data.get("x") is not None:
            parts.append(f"坐标 x={data['x']} y={data['y']} z={data['z']}")
        if data.get("hp") is not None:
            parts.append(f"血量 {data['hp']}")
        dim = data.get("dim")
        if dim:
            parts.append(f"维度 {str(dim).removeprefix('minecraft:')}")

        head = "Nanako 在线，" + "，".join(parts) if parts else "Nanako 在线。"
        return f"{head}\n{cls._describe_task(data.get('task'))}"

    @staticmethod
    def _describe_task(task: object) -> str:
        if not isinstance(task, dict) or not task.get("available"):
            reason = (task or {}).get("reason") if isinstance(task, dict) else None
            return f"任务状态：**未知** —— {reason or '客户端没有上报状态'}"

        bits: list[str] = []
        if task.get("status"):
            bits.append(f"状态 {task['status']}")
        if task.get("goal"):
            bits.append(f"目标 {task['goal']}")
        if task.get("dist") is not None:
            bits.append(f"还剩约 {task['dist']} 格")
        if task.get("eta") is not None:
            bits.append(f"预计 {task['eta']} 秒")

        text = "任务：" + ("，".join(bits) if bits else "无上报字段")
        if task.get("stale"):
            text += "（⚠️ 这份状态已过期，客户端可能卡住或掉线了）"
        return text

    # ---- 调试入口 -------------------------------------------------------

    @filter.command("mcbody")
    async def cmd_mcbody(self, event: AstrMessageEvent):
        """不带 LLM 的连通性自检：`/mcbody` 直接打一次 RCON。"""
        denied = self._authorize(event)
        if denied:
            yield event.plain_result(denied)
            return
        data, err = await self._call("mcb diag")
        if err:
            logger.warning(f"[{PLUGIN_NAME}] /mcbody 自检失败: {err}")
            yield event.plain_result(f"桥不通：{err}")
            return
        yield event.plain_result(f"桥是通的。自检：{data}")
