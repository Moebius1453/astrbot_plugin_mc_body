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
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .mc_rcon import BridgeError, RconBridge
from .mc_reflex import ReflexGuard
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
        self.reflex = ReflexGuard(
            self.bridge,
            interval=float(self._cfg("reflex_interval_seconds", 2)),
            hp_low=float(self._cfg("hp_low", 12)),
            hp_critical=float(self._cfg("hp_critical", 6)),
            flee_distance=int(self._cfg("flee_distance", 32)),
            scan_range=int(self._cfg("reflex_scan_range", 24)),
            notify=self._notify,
        )
        # 通知去重（见 _notify）
        self._last_notify_text = ""
        self._last_notify_at = 0.0

    async def _notify(self, text: str) -> None:
        """把反射事件写到白的会话里（她/用户能看见）。

        ⚠️ **只给"需要用户知道、需要用户动手"的事用**（比如"我饿了但身上没吃的"）。
        **自动反射的状态翻转（进战/脱战/逃跑/卡住）一律只写日志**，不许占聊天正文 ——
        用户明确要求过（2026-10-09）："这种东西日志里面出现就好了"。
        """
        umo = str(self._cfg("white_session", "") or "")
        if not umo:
            return

        # 去重保险：同一条消息 60 秒内只发一次。防止任何意料之外的重复刷屏。
        now = time.monotonic()
        if text == self._last_notify_text and (now - self._last_notify_at) < 60:
            logger.info(f"[mc_body] 通知去重（60 秒内已发过同样的）：{text}")
            return
        self._last_notify_text = text
        self._last_notify_at = now

        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain

        await self.context.send_message(umo, MessageChain([Plain(f"[身体] {text}")]))

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

        if self._cfg("enable_reflex", True):
            self.reflex.start()
        else:
            logger.info(f"[{PLUGIN_NAME}] 防御反射已关闭（enable_reflex=false）")

        logger.info(
            f"[{PLUGIN_NAME}] 已加载，RCON 目标 {self.bridge.host}:{self.bridge.port}"
        )

    async def terminate(self) -> None:
        await self.reflex.stop()
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

    @classmethod
    def _clean_coords3(cls, x, y, z) -> tuple[int, int, int] | None:
        tx, ty, tz = cls._clean_coord(x), cls._clean_coord(y), cls._clean_coord(z)
        if None in (tx, ty, tz):
            return None
        return tx, ty, tz

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

    # ---- 背包与交互（生存必需）-------------------------------------------

    @filter.llm_tool(name="mc_inventory")
    async def mc_inventory(self, event: AstrMessageEvent):
        """查看你背包里有什么、手上拿着哪一格、以及饥饿度。

        想拿东西、想吃东西、想合成之前，**先调这个**看看自己有什么。
        返回快捷栏 0-8 格、背包其余格子、副手，以及当前选中的快捷栏槽位。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        data, err = await self._call("mcb inventory")
        if err:
            return f"读不到背包：{err}"
        return self._describe_inventory(data)

    @filter.llm_tool(name="mc_hold")
    async def mc_hold(self, event: AstrMessageEvent, slot: int):
        """把你快捷栏的第几格拿在手上（0 到 8）。

        吃东西、放方块之前要先把手里的东西换对。
        ⚠️ 只能选**快捷栏**（0-8）。如果东西在背包里，先用 `mc_inventory` 看看它在哪一格 ——
        目前还没有把背包物品移到快捷栏的能力。

        Args:
            slot(number): 快捷栏槽位，0 是最左边，8 是最右边。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        try:
            n = int(slot)
        except (TypeError, ValueError):
            return f"槽位不合法：{slot!r}。应该是 0 到 8 的整数。"
        if not 0 <= n <= 8:
            return f"槽位 {n} 超范围。快捷栏只有 0 到 8。"
        _, err = await self._call(f"mcb hotbar {n}")
        if err:
            return f"切换失败：{err}"
        return f"已经把手换到快捷栏第 {n} 格。"

    @filter.llm_tool(name="mc_use")
    async def mc_use(self, event: AstrMessageEvent):
        """使用**手上**拿着的东西：吃东西、喝药水、放方块、射箭……

        最常见的用法是**吃东西**：先用 `mc_inventory` 找到食物在哪一格，
        用 `mc_hold` 拿到手上，再调这个。

        ⚠️ 这是"对着空气用"（比如吃东西）。要对着某个方块用（开箱子、放置到地上），
        用 `mc_use_on`。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        _, err = await self._call("mcb use")
        if err:
            return f"使用失败：{err}"
        return "已经用了一次手上的东西。过一会儿用 mc_inventory 或 mc_state 看效果。"

    @filter.llm_tool(name="mc_use_on")
    async def mc_use_on(self, event: AstrMessageEvent, x: float, y: float, z: float):
        """对着指定坐标的**方块**右键：放置方块、按按钮拉杆、开箱子/工作台。

        会先转头看向那个坐标，再右键，**然后自动关掉弹出的界面**。

        ⚠️ **距离限制约 4.5 格** —— 够不着就是够不着。先用 `mc_goto` 走到附近再调这个。

        ⚠️ 现在**还不能操作箱子/工作台界面里的东西**（没有点击格子的能力），
        所以打开容器暂时拿不到里面的物品。这个工具目前真正的用途是：
        **放置方块**、按按钮/拉杆、以及"证明交互这条路是通的"。

        Args:
            x(number): 目标方块的 X 坐标
            y(number): 目标方块的 Y 坐标
            z(number): 目标方块的 Z 坐标
        """
        denied = self._authorize(event)
        if denied:
            return denied
        coords = self._clean_coords3(x, y, z)
        if coords is None:
            return f"坐标不合法：({x}, {y}, {z})。"
        tx, ty, tz = coords
        # 走 useOnAt（直接给坐标构造命中），不依赖准星射线 —— 实测射线经常 MISS
        _, err = await self._call(f"mcb useOnAt {tx} {ty} {tz}")
        # 不管成没成，都关一次界面 —— 开着界面她动不了，这是安全兜底
        await self._call("mcb closeGui")
        if err:
            return (
                f"对着 ({tx},{ty},{tz}) 右键没成功：{err}。"
                "最常见的原因是**够不着**（超过约 4.5 格）—— 先用 mc_goto 走到附近。"
            )
        return (
            f"已对着 ({tx},{ty},{tz}) 右键，并把可能弹出的界面关掉了。"
            "如果要确认放置生效，过一会儿用 mc_inventory 看手上东西少没少。"
        )

    @filter.llm_tool(name="mc_attack")
    async def mc_attack(self, event: AstrMessageEvent):
        """攻击你准星正指着的实体（打怪、打动物）。

        会先看看准星指着什么。如果没指着实体，会明确告诉你。
        想先转向某个目标，可以先调 `mc_use_on` 的同款思路 —— 但目前没有独立的转向工具，
        通常是先走过去（`mc_goto` / `mc_follow`）让目标进视野。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        _, err = await self._call("mcb attack")
        if err:
            return f"攻击失败：{err}"
        return "已攻击准星指着的实体。过一会儿用 mc_state 看血量/效果。"

    @filter.llm_tool(name="mc_threats")
    async def mc_threats(self, event: AstrMessageEvent):
        """看看附近有没有怪、离你多远、什么怪、还剩多少血。

        数据来自**服务端直接查询世界**（不靠客户端看，所以很准）。
        想知道"周围安不安全"、"该不该打"、"往哪跑"时用它。

        返回按距离排序的实体列表；其中 `hostile` 为真的才是敌对怪。
        """
        denied = self._authorize(event)
        if denied:
            return denied
        data, err = await self._call("mcb threats 24")
        if err:
            return f"查不到周围情况：{err}"
        return self._describe_threats(data)

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
        food = data.get("food")
        if isinstance(food, dict) and food.get("level") is not None:
            level = food["level"]
            warn = "（饿了）" if isinstance(level, (int, float)) and level <= 6 else ""
            parts.append(f"饥饿 {level}/20{warn}")
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

    @staticmethod
    def _describe_inventory(data: dict) -> str:
        lines: list[str] = []

        food = data.get("food") or {}
        if isinstance(food, dict) and food.get("level") is not None:
            level = food["level"]
            note = "（饿了，该吃东西了）" if isinstance(level, (int, float)) and level <= 6 else ""
            lines.append(f"饥饿度 {level}/20{note}")

        held = data.get("held")
        hotbar = data.get("hotbar") or []
        if isinstance(held, int) and 0 <= held < len(hotbar):
            item = hotbar[held]
            name = item.get("n") if isinstance(item, dict) else None
            lines.append(f"手上（第 {held} 格）：{name or '空手'}")

        def fmt_items(items, label: str) -> None:
            if not items:
                return
            rendered = [
                f"{it.get('n')}×{int(it.get('c') or 0)}"
                for it in items
                if isinstance(it, dict) and it.get("n")
            ]
            if rendered:
                lines.append(f"{label}：" + "，".join(rendered))

        # 快捷栏排除手上那格，免得重复
        hotbar_rest = [
            it for i, it in enumerate(hotbar) if isinstance(it, dict) and i != held
        ]
        fmt_items(hotbar_rest, "快捷栏其它格")
        fmt_items(data.get("main") or [], "背包")

        off = data.get("offhand")
        if isinstance(off, dict) and off.get("n"):
            lines.append(f"副手：{off['n']}×{int(off.get('c') or 0)}")

        errors = data.get("err") or []
        if errors:
            lines.append("（读取时的异常：" + "；".join(str(e) for e in errors) + "）")

        if not lines:
            return "背包是空的，什么都读不到。"
        return "\n".join(lines)

    @staticmethod
    def _describe_threats(data: dict) -> str:
        threats = data.get("threats") or []
        if not threats:
            return "附近没看到任何实体。"

        hostiles = [t for t in threats if isinstance(t, dict) and t.get("hostile")]
        lines: list[str] = []
        if hostiles:
            lines.append(f"⚠️ 附近有 {len(hostiles)} 只敌对：")
            for t in hostiles[:5]:
                hp = t.get("hp")
                lines.append(
                    f"  · {t.get('name')}（{t.get('type')}）"
                    f"距 {t.get('dist')} 格" + (f"，血量 {hp}" if hp is not None else "")
                )
        else:
            lines.append("附近没有敌对怪。")

        others = [t for t in threats if isinstance(t, dict) and not t.get("hostile")]
        if others:
            names = "，".join(f"{t.get('name')}({t.get('dist')}格)" for t in others[:6])
            lines.append(f"其它实体：{names}")

        err = data.get("err") or []
        if err:
            lines.append("（查询异常：" + "；".join(str(e) for e in err) + "）")
        return "\n".join(lines)

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
