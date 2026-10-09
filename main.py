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

import asyncio
import math
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .mcb.rcon import BridgeError, RconBridge
from .mcb import containers as mcb_containers
from .mcb.containers import ContainerIO
from .mcb.craft import CraftRunner, Crafter
from .mcb.journal import Journal
from .mcb.places import PlaceBook
from .mcb.reflex import ReflexGuard
from .mcb.sight import Sight
from .mcb.smelt import Smelter
from .mcb.tasks import TaskRunner
from .mcb.uplink import ChatUplink
from .mcb import render

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
        # 状态日志 —— 白「记得自己刚才干了什么」的那本账（见 mcb/journal.py）。
        # ⚠️ **存内存**（用户 2026-10-09 拍板）：插件热重载会丢，任务本身也一样。
        self.journal = Journal(capacity=int(self._cfg("journal_capacity", 400)))

        # 地点簿 —— 「哪儿有什么、我在那儿干过什么」（见 mcb/places.py）。
        # ⚠️ **这个写磁盘**：跟日志不一样，地图丢了代价太大。
        places_path = str(self._cfg("places_file", "") or "").strip()
        if not places_path:
            places_path = str(Path(__file__).resolve().parent / "data" / "places.json")
        self.places = PlaceBook(places_path)

        # 眼睛 —— 截图 → 识图转述成文字（见 mcb/sight.py）。
        # ⚠️ **图片永远不进白的大脑**：走"截图 → API → 文字 → 上下文"（用户 2026-10-08 定的）。
        self.sight = Sight(
            ssh_host=str(self._cfg("client_ssh_host", "")),
            container=str(self._cfg("client_container", "mc-brain")),
            sudo_password=str(self._cfg("client_sudo_password", "")),
            context=context,
            provider_id=str(self._cfg("vision_provider_id", "")),
            vision_prompt=str(self._cfg("vision_prompt", "")),
        )

        # 任务层 —— 把多步行为串成一个目标（见 mcb/tasks.py）
        self.io = ContainerIO(self.bridge)
        self.tasks = TaskRunner(self.bridge, self.journal, io=self.io)

        self.reflex = ReflexGuard(
            self.bridge,
            interval=float(self._cfg("reflex_interval_seconds", 1)),
            hp_low=float(self._cfg("hp_low", 12)),
            hp_critical=float(self._cfg("hp_critical", 6)),
            flee_distance=int(self._cfg("flee_distance", 32)),
            scan_range=int(self._cfg("reflex_scan_range", 24)),
            stance=str(self._cfg("default_stance", "defend")),
            owner_name=str(self._cfg("owner_player_name", "")),
            flee_toward=str(self._cfg("retreat_toward", "safe")),
            notify=self._notify,
            journal=self.journal,
        )
        # 反射要能**抢占**任务（保命 > 干活）。挂起不是停止 —— 事完会自己接着做。
        self.reflex.bind_tasks(self.tasks)
        # 通知去重（见 _notify）
        self._last_notify_text = ""
        self._last_notify_at = 0.0
        # 数她经历了多少次 LLM 请求 —— 用来"每 N 次附一次状态包"（见 inject_body_state）
        self._llm_calls = 0

    async def _notify(self, facts: str) -> None:
        """把一个**处境事实**推到白的会话里（她/用户能看见）。

        ⚠️⚠️ **只传事实，不许传句子**（用户 2026-10-09 拍板）：
        > "感知我是没法接受程序文本的。"
        ✅ `food=6/20 food_items=0` ／ ❌ `我饿了，但身上没有食物。`
        —— 后者是**程序替她写的台词**。她想怎么讲是她的事，程序只负责把状态摆出来。

        ⚠️ 而且**只给"需要用户知道、需要用户动手"的事用**。
        自动反射的状态翻转（进战/脱战/逃跑/卡住）一律**只写状态日志**，不许占聊天正文 ——
        用户明确要求过："这种东西日志里面出现就好了"。
        """
        umo = str(self._cfg("white_session", "") or "")
        if not umo:
            return

        # 去重保险：同一条消息 60 秒内只发一次。防止任何意料之外的重复刷屏。
        now = time.monotonic()
        if facts == self._last_notify_text and (now - self._last_notify_at) < 60:
            logger.info(f"[mc_body] 通知去重（60 秒内已发过同样的）：{facts}")
            return
        self._last_notify_text = facts
        self._last_notify_at = now

        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain

        await self.context.send_message(umo, MessageChain([Plain(f"[body] {facts}")]))

    # ---- 处境感知：每 N 次 LLM 请求附一次状态数据包 ------------------------

    @filter.on_llm_request(priority=100)
    async def inject_body_state(self, event: AstrMessageEvent, req) -> None:
        """每 N 次 LLM 请求，把**状态数据包**附在她这条用户消息的末尾。

        用户 2026-10-09 定："状态最少得三到四轮对话主动告诉她一次"，
        并且**不接受程序文本** —— 所以这里只挂 §`render.state_packet` 那份**纯数据**。

        ⚠️ 挂 `extra_user_content_parts`（用户消息侧）**而不是 system_prompt** ——
        状态每轮都在变，塞进 system 会把提示词缓存全打掉。
        （先例：`astrbot_plugin_dafeiyu_pet` 也在 `on_llm_request` 里注身体状态，
          但它挂的是 system_prompt；我们数据更碎，走用户侧更划算。）
        """
        if not self._cfg("enable_body_state", True):
            return
        self._llm_calls += 1
        every = max(1, int(self._cfg("body_state_every", 3)))
        # 第一次就给 —— 她一开始就该知道自己站在哪、什么状态
        if self._llm_calls != 1 and self._llm_calls % every != 0:
            return
        try:
            data, err = await self._call("mcb state")
            if err or not data.get("online"):
                return
            packet = render.state_packet(
                data, self.reflex.stance, self.tasks.status(), self.journal, self.places
            )
            from astrbot.core.agent.message import TextPart
            req.extra_user_content_parts.append(TextPart(text=packet))
            logger.debug(f"[mc_body] 已附状态包（第 {self._llm_calls} 次请求）")
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 附件失败**绝不能影响她这一轮对话** —— 感知是锦上添花，不是命脉
            logger.warning(f"[mc_body] 状态包注入失败（忽略）：{exc}")

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
        # ⚠️ **先停任务再停反射** —— 反过来的话任务可能正在等反射放开它，
        #    会卡在这个 await 上。停任务顺带把她的寻路也取消掉。
        await self.tasks.stop(quiet=True)
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

    def _guard(self, event: AstrMessageEvent) -> str | None:
        """工具的统一前置检查。返回拒绝理由；None = 放行。

        ⚠️ **每个工具都必须走这里**，别自己抄一遍授权逻辑 ——
        曾经 18 个工具抄了 18 遍，改一个公共行为要动 18 处。
        要加"逐工具开关"之类的公共检查，只改这一个地方。
        """
        return self._authorize(event)

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
        """查自己在 MC 里的状态：在线/坐标/血量/饥饿/维度/**正在做的事**/最近发生了什么。

        返回里有「正在做的事」（你自己的任务进度）和「寻路」（Baritone 走路状态），别搞混。
        **动作类工具下发后，隔一会儿用它确认结果** —— 那些工具只能报告"已下发"。
        "不在线"= 角色没连进服务器，什么动作都做不了。
        """
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb state")
        if err:
            return f"查不到 Nanako 的状态：{err}"
        return render.describe_state(
            data, self.reflex.stance, self.tasks.status(), self.journal
        )

    @filter.llm_tool(name="mc_say")
    async def mc_say(self, event: AstrMessageEvent, text: str):
        """用 Nanako 的身体在**游戏公屏**说话。这是游戏内聊天，不是回 QQ。

        Args:
            text(string): 要说的话。会发到游戏公屏，所有人都看得见。
        """
        if (deny := self._guard(event)):
            return deny
        clean = self._clean_text(text, MAX_SAY_LEN)
        if clean is None:
            return "要说的话是空的，或者只包含换行 —— 没发出去。"
        _, err = await self._call(f"mcb say {clean}")
        if err:
            return f"没能在游戏里说话：{err}"
        return f"已经用 Nanako 的身体在游戏里说了：{clean}"

    @filter.llm_tool(name="mc_goto")
    async def mc_goto(self, event: AstrMessageEvent, near: str = "", x: float = 0, z: float = 0):
        """走到一个地方。**异步** —— 下发后她自己走，过会儿用 mc_state 看坐标确认。

        三种给法，**给其一就行**：
        · `near="工作台"` / `near="minecraft:furnace"` —— **走到那个东西旁边**。
          先查**地点簿**（你以前记过的名字），查不到再当**方块注册名**在附近扫。
          **这是最省事的给法 —— 不用知道坐标。**
        · `x` + `z` —— 走到指定坐标（你看 F3 或者别人告诉你坐标时才用）

        ⚠️ 按方块名找**只在附近 16 格内**（服务端扫描上限）。太远就找不到 ——
        那就先走近点，或者**把它记进地点簿**（`mc_place`），以后直接 `near="名字"`。

        常找的方块名：`minecraft:crafting_table` 工作台、`minecraft:furnace` 熔炉、
        `minecraft:chest` 箱子、`minecraft:farmland` 耕地、`simpletomb:grave_cross` 墓碑。

        Args:
            near(string): 地点名（你记过的）或方块注册名，走到最近的那个旁边。
            x(number): 目标 X 坐标（跟 z 一起用）。
            z(number): 目标 Z 坐标。
        """
        if (deny := self._guard(event)):
            return deny

        want = str(near or "").strip()
        if want:
            # ① 先查**地点簿** —— "家""田"这种名字比方块名好用得多
            hit = self.places.match(want)
            if hit is not None:
                name, place = hit
                tx, tz = int(float(place["x"])), int(float(place["z"]))
                _, err = await self._call(f"mcb baritone goto {tx} {tz}")
                if err:
                    return f"没能让她出发：{err}"
                return (f"出发去「{name}」（{tx}, {tz}）"
                        + (f"，那儿是{place['what']}" if place.get("what") else "")
                        + "。过会儿用 mc_state 看坐标确认到没到。")

            # ② 不是地点名 → 当方块注册名在附近扫
            block_id = want if ":" in want else f"minecraft:{want}"
            pos = await mcb_containers.find_block(self.bridge, block_id)
            if pos is None:
                return (
                    f"附近 16 格内没有「{want}」这种方块，地点簿里也没这个名字。\n"
                    "要么走近点再试，要么直接给坐标（x/z），"
                    "要么先过去一趟再用 mc_place 把它记下来。"
                )
            tx, tz = int(pos[0]), int(pos[2])
            _, err = await self._call(f"mcb baritone goto {tx} {tz}")
            if err:
                return f"没能让她出发：{err}"
            return (f"附近找到了 {block_id}（{pos[0]},{pos[1]},{pos[2]}），"
                    "已让她出发。过会儿用 mc_state 看坐标确认。")

        tx, tz = self._clean_coord(x), self._clean_coord(z)
        if tx is None or tz is None:
            return (
                "要么给 `near`（地点名或方块名），要么给 `x` 和 `z` 坐标 —— "
                f"现在给的是 near={near!r} x={x} z={z}。"
            )
        _, err = await self._call(f"mcb baritone goto {tx} {tz}")
        if err:
            return f"没能让她出发：{err}"
        return (
            f"已让她出发前往 x={tx} z={tz}。**她现在还在路上** —— "
            "过一会儿调 mc_state 看坐标，确认她是不是真的到了。"
        )

    @filter.llm_tool(name="mc_place")
    async def mc_place(self, event: AstrMessageEvent, action: str = "list",
                       name: str = "", what: str = "", note: str = ""):
        """**地点簿** —— 记住 / 查看 / 忘掉「哪儿是什么」。

        你的扫描只有附近 16 格，出了这个圈你就是瞎的。**把重要的地方记下来**，
        以后 `mc_goto near="名字"` 就能直接过去，不用任何人报坐标。

        · `action="remember"` —— **把你现在站的地方记下来**（要起个 `name`）。
          顺手写 `what`（这儿是什么），以后翻到能看懂。
        · `action="list"` —— 看看记过哪些地方
        · `action="forget"` —— 忘掉一个（给 `name`）

        **该记的时候**：造了个据点、发现一块田、放了箱子、找到矿洞入口、
        墓碑在哪… **走过的路会忘，记下来才不会忘。**

        Args:
            action(string): "remember"（记下当前位置）/ "list"（列出来）/ "forget"（忘掉）。
            name(string): 地点名，起个你自己记得住的（"家"、"麦田"、"矿洞口"）。
            what(string): 这儿是什么（比名字多说一点）。
            note(string): 备注（可选）。
        """
        if (deny := self._guard(event)):
            return deny
        act = str(action or "list").strip().lower()

        if act in ("", "list", "ls", "all", "show"):
            return "地点簿：\n" + self.places.render()

        if act in ("forget", "delete", "del", "rm", "drop"):
            return self.places.forget(name)

        if act in ("remember", "save", "mark", "add", "note"):
            data, err = await self._call("mcb state")
            if err:
                return f"读不到坐标，记不了：{err}"
            if not data.get("online"):
                return "她不在线，拿不到坐标。"
            got = self.places.remember(
                name, data.get("x") or 0, data.get("y") or 0, data.get("z") or 0,
                dim=str(data.get("dim") or ""), what=what, note=note,
            )
            self.journal.add("body", f"记了个地方：{got}")
            return got

        return f"不认得 action={action!r}。只认 remember / list / forget。"

    @filter.llm_tool(name="mc_follow")
    async def mc_follow(self, event: AstrMessageEvent, player: str):
        """跟着某个玩家走。

        ⚠️ 是「跟到几格以内」**不是贴身** —— 目标就在旁边时她不动是正常的。

        Args:
            player(string): 要跟随的玩家名（游戏 ID，只允许字母数字下划线）。
        """
        if (deny := self._guard(event)):
            return deny
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
        """急停：**取消当前任务**，并停掉所有寻路/行走/跟随/使用动作。

        ⚠️ 任务会被**丢掉**（取消，不是暂停）。想让她"先去忙别的、回头接着做"，
        直接给她新指令就行 —— 反射抢占才是挂起。
        """
        if (deny := self._guard(event)):
            return deny
        stopped = await self.tasks.stop()
        _, err = await self._call("mcb stop")
        # 顺手松开"使用键" —— 万一吃东西时出了岔子，按键卡住会让她一直重复动作
        await self._call("mcb release")
        if err:
            return f"没能让她停下：{err}"
        return f"已下发停止指令，她应该会停下来（惯性可能还会滑一小段）。{stopped}"

    @filter.llm_tool(name="mc_baritone_raw")
    async def mc_baritone_raw(self, event: AstrMessageEvent, command: str):
        """应急口：直接给 Baritone 下原始命令。固定工具做不到时才用。

        常用：`mine diamond_ore`（挖矿）、`explore`（探索）、`farm`（种田）、
        `goto 100 64 -200`（带 Y 的 goto）、`come`、`thisway 100`。

        Args:
            command(string): Baritone 命令本体，**不带 `#` 前缀**。
        """
        if (deny := self._guard(event)):
            return deny
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
        """看背包：有什么、手上是哪一格、饥饿度。拿东西/吃东西/合成之前先看它。"""
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb inventory")
        if err:
            return f"读不到背包：{err}"
        return render.describe_inventory(data)

    @filter.llm_tool(name="mc_hold")
    async def mc_hold(self, event: AstrMessageEvent, slot: int):
        """把**快捷栏**第 0~8 格拿到手上。东西在背包里（不知道第几格）就用 mc_equip。

        Args:
            slot(number): 快捷栏槽位，0 是最左边，8 是最右边。
        """
        if (deny := self._guard(event)):
            return deny
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

    @filter.llm_tool(name="mc_equip")
    async def mc_equip(self, event: AstrMessageEvent, item: str, where: str = "hand"):
        """把身上某物品**挪到指定位置** —— 给物品名就行，不用先查它在第几格。

        where 可选：
        · `hand`（默认）—— 拿到手上。放方块、吃东西、用工具都用它
        · `off` —— 副手。举盾、放火把当光源
        · `head` / `chest` / `legs` / `feet` —— 穿护甲
        · **`hotbar0` ~ `hotbar8`** —— 放进快捷栏**某一格**（跟那格现有的对调）
        · **`backpack`** —— 从快捷栏**收进背包**（自动找空格）

        ⚠️ 物品名要用**注册名**不是中文名：`minecraft:iron_helmet` ✅ / `"铁头盔"` ❌。

        Args:
            item(string): 物品的注册名，例如 "minecraft:iron_helmet"、"minecraft:shield"。
            where(string): "hand"（默认）/ "off" / "head" / "chest" / "legs" / "feet"
                / "hotbar0"~"hotbar8" / "backpack"。
        """
        if (deny := self._guard(event)):
            return deny
        name = str(item or "").strip()
        if not name:
            return "没说装哪个物品。"
        if ":" not in name:
            name = f"minecraft:{name}"
        err = await mcb_containers.wear(self.io, name, str(where or "hand").strip().lower())
        if err:
            return err
        return f"已经把 {name} 装到「{where}」了。"

    @filter.llm_tool(name="mc_use")
    async def mc_use(self, event: AstrMessageEvent):
        """用**手上**的东西：吃东西、喝药水、放方块、射箭。

        对着**方块**用（开箱子、放地上）请走 mc_use_on。
        """
        if (deny := self._guard(event)):
            return deny
        _, err = await self._call("mcb use")
        if err:
            return f"使用失败：{err}"
        return "已经用了一次手上的东西。过一会儿用 mc_inventory 或 mc_state 看效果。"

    @filter.llm_tool(name="mc_use_on")
    async def mc_use_on(
        self, event: AstrMessageEvent, x: float, y: float, z: float, keep_open: bool = False
    ):
        """对指定坐标的**方块**右键：放方块、按按钮、开箱子/工作台。

        ⚠️ 距离约 4.5 格，够不着先 mc_goto。
        默认会自动关掉弹出的界面；要**操作界面里的东西**（拿箱子、合成）就设
        `keep_open=true`，再配 mc_menu + mc_click 用。

        Args:
            x(number): 目标方块的 X 坐标
            y(number): 目标方块的 Y 坐标
            z(number): 目标方块的 Z 坐标
            keep_open(bool): true = 打开后**不关界面**，留着给 mc_menu / mc_click 用。
        """
        if (deny := self._guard(event)):
            return deny
        coords = self._clean_coords3(x, y, z)
        if coords is None:
            return f"坐标不合法：({x}, {y}, {z})。"
        tx, ty, tz = coords
        # 走 useOnAt（直接给坐标构造命中），不依赖准星射线 —— 实测射线经常 MISS
        _, err = await self._call(f"mcb useOnAt {tx} {ty} {tz}")
        if err:
            return (
                f"对着 ({tx},{ty},{tz}) 右键没成功：{err}。"
                "最常见的原因是**够不着**（超过约 4.5 格）—— 先用 mc_goto 走到附近。"
            )
        if keep_open:
            await asyncio.sleep(0.6)   # 等界面开起来
            data, merr = await self._call("mcb state")
            if merr:
                return f"已对着 ({tx},{ty},{tz}) 右键，但读不到界面：{merr}"
            return "已对着 ({tx},{ty},{tz}) 右键，界面留着没关。\n" + render.describe_menu(data)
        # 不操作界面 → 关掉，免得她动不了
        await self._call("mcb closeGui")
        return (
            f"已对着 ({tx},{ty},{tz}) 右键，并把可能弹出的界面关掉了。"
            "如果要确认放置生效，过一会儿用 mc_inventory 看手上东西少没少。"
        )

    @filter.llm_tool(name="mc_menu")
    async def mc_menu(self, event: AstrMessageEvent):
        """看**当前打开的界面**里有什么（箱子/工作台/背包合成格）：每格的**格子号**+内容。

        格子号是**这套界面自己的编号**，不是背包格号。用 mc_use_on 开容器时记得 `keep_open=true`。
        """
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb state")
        if err:
            return f"读不到界面：{err}"
        return render.describe_menu(data)

    @filter.llm_tool(name="mc_click")
    async def mc_click(self, event: AstrMessageEvent, slot: int, mode: int = 1):
        """点当前界面的**第几号格子** —— 从箱子里拿东西、合成都靠它。先用 mc_menu 看格子号。

        · `mode=1`（默认）shift 快速移动：箱子格→背包；或**产物格→直接合成**
        · `mode=0` 普通左键（拿起/放下）；`mode=6` 双击收同种

        Args:
            slot(number): 格子号（用 mc_menu 查，不是背包格号）。
            mode(number): 1=shift 快速移动（默认，最常用）；0=普通左键；6=双击收同种。
        """
        if (deny := self._guard(event)):
            return deny
        try:
            n = int(slot)
        except (TypeError, ValueError):
            return f"格子号不合法：{slot!r}。"
        if n < 0:
            return f"格子号不能是负数（给的是 {n}）。"
        try:
            m = int(mode)
        except (TypeError, ValueError):
            m = 1
        if m not in (0, 1, 2, 3, 4, 5, 6):
            return f"mode 只能是 0~6（给的是 {m}）。"
        _, err = await self._call(f"mcb clickSlot {n} 0 {m}")
        if err:
            return f"点格子失败：{err}"
        await asyncio.sleep(0.4)
        data, merr = await self._call("mcb state")
        after = "" if merr else "\n" + render.describe_menu(data)
        return f"已点第 {n} 号格（mode={m}）。过一会儿用 mc_inventory / mc_menu 看结果。{after}"

    @filter.llm_tool(name="mc_craft")
    async def mc_craft(self, event: AstrMessageEvent, item: str, count: int = 1):
        """**做东西**：给物品名，她自己算整条链并一步步做出来（含自己找工作台、走过去、打开）。

        例："做把木镐" → 她自己推原木→木板→木棍→木镐。配方来自服务端，**mod 物品也认识**。
        ⚠️ 材料得她本来就有（缺料不会去挖，会如实说缺什么）；只支持工作台/背包合成，熔炉要烧的用 mc_smelt。

        Args:
            item(string): 要做的物品，例如 "wooden_pickaxe"、"crafting_table"、
                "oak_planks"。带不带 `minecraft:` 前缀都行；mod 物品要带前缀。
            count(number): 要做几个，默认 1。
        """
        if (deny := self._guard(event)):
            return deny
        name = str(item or "").strip()
        if not name:
            return "没说要做什么。"
        try:
            n = max(1, int(count))
        except (TypeError, ValueError):
            n = 1
        if not self._cfg("enable_craft", True):
            return "合成功能被管理员关掉了。"

        crafter = Crafter(self.bridge)
        runner = CraftRunner(self.bridge)
        try:
            plan = await crafter.plan(name, n)
        except BridgeError as exc:
            return f"算配方的时候没连上桥：{exc}"
        if not plan.ok:
            self.journal.add("craft", f"做 {name}×{n} 做不了：{plan.describe().splitlines()[0]}")
            return "做不了：\n" + plan.describe()
        if not plan.steps:
            return f"不用做，她手上已经有 {name} 了。"
        self.journal.add("craft", f"开始做 {name}×{n}")
        try:
            result = await runner.run(plan)
        except BridgeError as exc:
            self.journal.add("error", f"做 {name} 时桥断了")
            return f"做的过程中桥断了：{exc}"
        self.journal.add("craft", f"做 {name}×{n} —— {result.splitlines()[0]}")
        return result

    @filter.llm_tool(name="mc_smelt")
    async def mc_smelt(self, event: AstrMessageEvent, item: str):
        """**烧东西**：原料放进熔炉炼成成品（生铁→铁锭、沙子→玻璃、生肉→熟肉）。

        自己挑配方（同一产物常有多个）、走过去、放料放燃料、**等烧完**、取走。
        ⚠️ 要有**燃料**（煤/木炭）且熔炉在 16 格内；会把原料**整叠**放进去，出来多少是多少。
        分不清该用这个还是 mc_craft 就先试 mc_craft —— 它会说"这个得烧"。

        Args:
            item(string): 要炼出来的东西，例如 "iron_ingot"、"glass"、"copper_ingot"。
                带不带 `minecraft:` 前缀都行。
        """
        if (deny := self._guard(event)):
            return deny
        if not self._cfg("enable_craft", True):
            return "冶炼功能被管理员关掉了。"
        try:
            return await Smelter(self.bridge, self.journal).smelt(str(item or ""))
        except BridgeError as exc:
            return f"烧的过程中桥断了：{exc}"

    @filter.llm_tool(name="mc_attack")
    async def mc_attack(self, event: AstrMessageEvent):
        """攻击你准星正指着的实体（打怪、打动物）。

        会先看看准星指着什么。如果没指着实体，会明确告诉你。
        想先转向某个目标，可以先调 `mc_use_on` 的同款思路 —— 但目前没有独立的转向工具，
        通常是先走过去（`mc_goto` / `mc_follow`）让目标进视野。
        """
        if (deny := self._guard(event)):
            return deny
        _, err = await self._call("mcb attack")
        if err:
            return f"攻击失败：{err}"
        return "已攻击准星指着的实体。过一会儿用 mc_state 看血量/效果。"

    @filter.llm_tool(name="mc_threats")
    async def mc_threats(self, event: AstrMessageEvent):
        """看附近有没有怪、多远、什么怪、多少血、**是不是正瞄着你**（服务端直查世界，很准）。

        想判断"周围安不安全""该不该打""往哪跑"就用它。`targeting=true` 表示它正盯着你。
        """
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb threats 24")
        if err:
            return f"查不到周围情况：{err}"
        return render.describe_threats(data)

    @filter.llm_tool(name="mc_stance")
    async def mc_stance(self, event: AstrMessageEvent, mode: str):
        """设**战斗姿态** —— "要不要主动打怪"这件事由你自己决定。

        · `defend`（默认）被动：只在**挨打**或**怪正瞄着你**时才还手，够不着**不追**
        · `hunt` 主动清怪：5 格内有敌对就上去打，够不着会追过去

        ⚠️ 两种姿态下**挨打都会立刻自动还手**（反射，不经过你），所以 defend 不会被打死。
        想安静挖矿设 defend；想清场设 hunt。

        Args:
            mode(string): 只能是 "defend"（被动还手）或 "hunt"（主动清怪）。
        """
        if (deny := self._guard(event)):
            return deny
        if not self._cfg("enable_mc_stance_tool", True):
            return "切换战斗姿态的功能被管理员关掉了。"
        try:
            actual = self.reflex.set_stance(str(mode))
        except ValueError as exc:
            return str(exc)
        if actual == "hunt":
            return "战斗姿态已设为 **hunt**：我会主动打 5 格内的怪，够不着就追过去。"
        return "战斗姿态已设为 **defend**：我不主动挑事，只在挨打或怪瞄着我时才还手。"

    @filter.llm_tool(name="mc_retreat")
    async def mc_retreat(self, event: AstrMessageEvent, toward: str = "owner"):
        """**主动脱战**：立刻停手往安全方向撤。血量好好的也能用（打不过、不想打、想回来找人）。

        · `owner`（默认）朝用户跑，会合最安全；他不在线/不同维度则自动退化成 safe
        · `safe` 背离最近的怪跑一段

        Args:
            toward(string): "owner"（朝用户跑，默认）或 "safe"（背离怪跑）。
        """
        if (deny := self._guard(event)):
            return deny
        try:
            what = await self.reflex.retreat(str(toward or "owner"))
        except ValueError as exc:
            return str(exc)
        return f"已脱战，{what}。她不会再自己冲回去打。"

    # ---- 任务层（多步行为）-----------------------------------------------

    @filter.llm_tool(name="mc_task")
    async def mc_task(self, event: AstrMessageEvent, task: str, at: str = ""):
        """派她去做一件**多步的事**，程序自己一步步做完。别的工具是**一个动作**，这个是**一件事**。

        现在能派的：
        · `torch` 沿途照明 —— 沿一个方向走，路边隔一段插一根火把。需要身上有火把
        · `farm` 种田 —— 走到田边，收割+补种。有锄头/种子更好，没有也能只收（最多 3 分钟）

        派出去她自己在做，你不用盯着 —— 进度看 mc_state 的「正在做的事」，停用 mc_task_stop。
        ⚠️ **挨打/濒死会自动打断**她（保命优先），但那是**挂起**不是取消，危险过去她自己接着做。

        ⚠️ 田远了（超过 16 格）她**扫不到**，`farm` 会扑空 —— 那就用 `at` 告诉她田在哪。

        Args:
            task(string): 任务名。不认得的名字会被拒绝并列出可选项。
            at(string): 可选，任务的地点，格式 "x z"（例如 "-50 120"）。种田时用它指定田的位置。
        """
        if (deny := self._guard(event)):
            return deny
        if not self._cfg("enable_mc_task", True):
            return "派任务的功能被管理员关掉了。"
        name = str(task or "").strip()
        if not name:
            return "要派哪件事？现在能派的有：\n" + render.describe_catalog(self.tasks.catalog())
        params: dict = {}
        # `at` = "x z"。解析失败就忽略 —— 任务自己会退化成"就地干"。
        parts = str(at or "").replace(",", " ").split()
        if len(parts) >= 2:
            px, pz = self._clean_coord(parts[0]), self._clean_coord(parts[1])
            if px is not None and pz is not None:
                params["x"], params["z"] = px, pz
        return await self.tasks.start(name, **params)

    @filter.llm_tool(name="mc_task_stop")
    async def mc_task_stop(self, event: AstrMessageEvent):
        """丢掉当前任务，回到闲着。

        ⚠️ 是**取消**不是暂停，进度会丢。"先去做别的、回头接着做"不用这个，直接给新指令即可。
        """
        if (deny := self._guard(event)):
            return deny
        return await self.tasks.stop()

    @filter.llm_tool(name="mc_journal")
    async def mc_journal(self, event: AstrMessageEvent, count: int = 20):
        """翻**状态日志**：刚才都发生了什么（分类：任务/反射/合成/身体/出错）。

        用户问"你刚才在干嘛"而你记不清、或想确认某件事成没成时用它。
        ⚠️ mc_state 里已带最近 10 条，这个是往前多翻。日志**只在内存**，插件重载就清空。

        Args:
            count(number): 往回翻多少条，默认 20，最多 100。
        """
        if (deny := self._guard(event)):
            return deny
        try:
            n = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            n = 20
        if len(self.journal) == 0:
            return "日志是空的 —— 要么刚重启过，要么真的什么都还没发生。"
        return f"最近 {min(n, len(self.journal))} 条：\n{self.journal.render(n)}"

    @filter.llm_tool(name="mc_look")
    async def mc_look(self, event: AstrMessageEvent, question: str = ""):
        """**看一眼你周围** —— 截下你游戏画面，转述成文字告诉你。

        你平时只能"读数据"，看不见画面。这个工具给你**眼睛**。

        什么时候用：
        · 想知道"我面前是什么""这儿长什么样""那个方块是什么"
        · 数据说不清的时候（`mc_state` 只有坐标，看不见风景）
        · 想确认某件事成没成（东西放对地方了吗）

        ⚠️ 三件事你得知道：
        1. **慢** —— 截图 + 传 + 识图，好几秒到十几秒
        2. **糊** —— 你的画面只有 640×360，看清轮廓和颜色，认不清小字
        3. **只照到你正对着的** —— 第一人称视角，背后的看不见。
           想换个角度就先走过去或者转头，再调一次

        Args:
            question(string): 你想知道什么（可选），比如"我面前是什么方块""田里熟了没"。
                不填就是"描述一下你看到的东西"。
        """
        if (deny := self._guard(event)):
            return deny
        if not self._cfg("enable_sight", True):
            return "看东西的功能被管理员关掉了。"
        return await self.sight.look(str(question or ""))

    # ---- 调试入口 -------------------------------------------------------

    @filter.command("mcbody")
    async def cmd_mcbody(self, event: AstrMessageEvent):
        """不带 LLM 的连通性自检：`/mcbody` 直接打一次 RCON。"""
        if (deny := self._guard(event)):
            yield event.plain_result(deny)
            return
        data, err = await self._call("mcb diag")
        if err:
            logger.warning(f"[{PLUGIN_NAME}] /mcbody 自检失败: {err}")
            yield event.plain_result(f"桥不通：{err}")
            return
        yield event.plain_result(f"桥是通的。自检：{data}")
