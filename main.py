"""让白（Minecraft 里的 Nanako）长出手脚。

数据流：
    QQ ── ->  白 ── ->  本插件的 @filter.llm_tool ── ->  RCON 隧道 ── ->  R820 服务端桥
                                                              ── ->  Nanako 的客户端
                                                              ── ->  Baritone

这一版就这些工具，全部走已经跑通的客户端动作（say / baritone / stop），
不需要改客户端、不需要重启 Nanako 的客户端。

注意： 动作类工具只能报告"已下发"，不能报告"完成了"。 服务端只知道动作发给了她的客户端；
客户端到底执行到哪一步，得靠 mc_state 复核坐标。所以动作类工具的返回值里都带着
"过一会儿用 mc_state 确认"的提示 —— 她也得像人一样先做、再去看结果。

授权：默认拒绝。必须在插件配置的 allowed_sender_ids 里列出允许的发送者 ID。
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
from .mcb.arbiter import LEVEL_REFLEX, LEVEL_TASK, LEVEL_USER, Arbiter
from .mcb import containers as mcb_containers
from .mcb.containers import ContainerIO
from .mcb.craft import CraftRunner, Crafter
from .mcb.events import EventFeed
from .mcb.journal import Journal
from .mcb.places import PlaceBook
from .mcb.reflex import ReflexGuard
from .mcb import reflexes
from .mcb.sight import Sight
from .mcb.smelt import Smelter
from .mcb.tasks import TaskRunner
from .mcb.uplink import ChatUplink
from .mcb import render

PLUGIN_NAME = "mc_body"

# ---- 自主心跳（没人叫她的时候自己醒来）--------------------------------------
#
# 注意： 这会花钱 —— 每次唤醒都是一整轮 agent。见 _start_self_loop 的说明。
SELF_LOOP_JOB = "mc_body_self_loop"

# 唤醒时喂给她的那条"给未来自己的指令"。
#
# 注意： 它是她的输入，会以 user 消息的身份进会话 —— 所以：
#   · 前缀用 [mc:alert]，按 docs/12 §1.4 那条规范（从游戏来的数据必须自报家门）
#   · 话要短、要中性，写清"该干什么"和"可以不干" ——
#     不给她"必须做点什么"的压力，否则她会为了交差而瞎折腾（还白烧 token）。
SELF_LOOP_NOTE = (
    "[mc:alert] 自主心跳：现在没人叫你。看一眼自己现在的处境"
    "（mc_state 看身体、mc_around 看周围），想接着做什么就去做；"
    "**没事可做就什么都别做，也别发消息** —— 沉默是允许的，不用每次都汇报。"
)

# Minecraft 的世界边界
COORD_LIMIT = 30_000_000
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
MAX_SAY_LEN = 200
MAX_RAW_LEN = 200

# ---- ⭐ 抄 Numen 的纪律：Baritone 原始命令走白名单，不走黑名单 ------------
#
# 重要： 这是我们现在最大的安全敞口（2026-10-10 核实）：
#    mc_baritone_raw 把模型给的字符串原样下发，客户端
#    （bridge/client/mcbridge.js 的 action === 'baritone'）直接
#    getCommandManager().execute(arg)，零检查。
#     ->  模型出一条 build / clearArea 就能一次抹掉一片建筑。
#
# 注意： 白名单而不是黑名单 —— 黑名单死在"没想到"，白名单死在"不让你做"。
#
# 注意： mine 必须留着：它是她现在唯一的挖矿方式（我们还没 mc_mine）。
#    挖矿那边的粒度保护在客户端：Baritone 的 blocksToDisallowBreaking
#    （一次设置，护住箱子/熔炉这类"里面装着东西的"）—— 见 docs\24。
BARITONE_VERBS = frozenset({
    # 移动
    "goto", "follow", "come", "thisway", "explore", "path", "surface",
    # 干活（mine 是唯一允许的"改世界"动词，理由见上）
    "mine", "farm",
    # 控制
    "stop", "cancel", "pause", "resume", "proc", "axis", "version", "help",
})

# ---- 技能（Skill）正文读取 ---------------------------------------------------
#
# 注意： AstrBot 自己已经有完整的 skill 机制（astrbot/core/skills/skill_manager.py）：
#    每轮把 ## Skills 索引（名字 + 描述 + SKILL.md 路径）拼进系统提示词，
#    并且自动扫描 data/plugins/*/skills/<名>/SKILL.md —— 所以我们只要把技能
#    放进插件目录下的 skills/ 就行，不用自己造一套。
#
# 重要： 但它有个洞：提示词里说的是"运行一条 shell 命令去读"（cat / type），
#    而那个文件工具被 provider_settings.computer_use_runtime ∈ {local, sandbox} 门控
#    （astrbot/core/tools/computer_tools/fs.py:69）—— 我们的配置是 "none"，
#    ⇒ 她看得见技能索引，读不到技能正文。（mc_skill 补的就是这一环。）
#
# 注意： 为什么不直接把 computer_use_runtime 改成 local：那一开就是
#    读/写/编辑/grep 整个本机文件系统。为了读一个 SKILL.md 不值得。
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_SKILL_CHARS = 24000   # SKILL.md 是给人读的说明书，超长的多半是写错了


@register(
    "astrbot_plugin_mc_body",
    "Moebius1453",
    "让 AstrBot 的智能体在 Minecraft 里长出手脚：查询角色状态、说话、移动。",
    "0.39.0",
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
        # 注意： 存内存（用户 2026-10-09 拍板）：插件热重载会丢，任务本身也一样。
        self.journal = Journal(capacity=int(self._cfg("journal_capacity", 400)))

        # 地点簿 —— 「哪儿有什么、我在那儿干过什么」（见 mcb/places.py）。
        # 注意： 这个写磁盘：跟日志不一样，地图丢了代价太大。
        places_path = str(self._cfg("places_file", "") or "").strip()
        if not places_path:
            places_path = str(Path(__file__).resolve().parent / "data" / "places.json")
        self.places = PlaceBook(places_path)

        # 眼睛 —— 截图  ->  识图转述成文字（见 mcb/sight.py）。
        # 注意： 图片永远不进白的大脑：走"截图  ->  API  ->  文字  ->  上下文"（用户 2026-10-08 定的）。
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

        # 仲裁层 —— 谁在写 walk 通道（见 mcb/arbiter.py）。
        # 注意： 从这里开始，任何模块都不许再直接调 mcb baritone / mcb stop。
        #    反射要保命、任务在走路、用户喊她过来 —— 三方都只能"声明"，
        #    由这层按优先级（反射 > 用户 > 任务）决定谁生效。
        #    这条是"解耦功能配合不好"的根上解法（main/docs/16）。
        self.arbiter = Arbiter(self._call)
        self.tasks.bind_arbiter(self.arbiter)

        # 事件流 —— 「刚才都发生了什么」（挨打/死亡/进出服/背包变动）。
        # 注意： 和 uplink.py 的聊天分工不同、两张表：那张管"人说的话"，
        #    这张管"聊天以外的事"。合表就是同一件事记两遍（见 mcb/events.py 头部）。
        self.events = EventFeed(self.bridge, self.journal,
                                on_permission=self._permission_denied)

        self.reflex = ReflexGuard(
            self.bridge,
            interval=float(self._cfg("reflex_interval_seconds", 1)),
            hp_low=float(self._cfg("hp_low", 12)),
            hp_critical=float(self._cfg("hp_critical", 6)),
            flee_distance=int(self._cfg("flee_distance", 32)),
            hunger_low=float(self._cfg("hunger_low", 16)),
            stuck_seconds=float(self._cfg("stuck_seconds", 18)),
            scan_range=int(self._cfg("reflex_scan_range", 24)),
            stance=str(self._cfg("default_stance", "defend")),
            owner_name=str(self._cfg("owner_player_name", "")),
            # 注意： 代码兜底默认值要和 _conf_schema.json 一致（现在是 auto）。
            #    auto = 按主人远近自己选（24 格内往他那儿跑，否则慌不择路）。
            #    一路演化的理由见 docs\17 §九 F：safe 在地下会挑到实心石头里的随机点，
            #    而 Baritone 的 allowBreak 是开的  ->  她一路把地板凿穿（"抽风挖地板"）。
            #    但用户也说了 "慌不择路 实际上怪可爱的" —— 所以不是删掉它，
            #    而是只在主人离得远时才用。
            flee_toward=str(self._cfg("retreat_toward", "auto")),
            notify=self._notify,
            journal=self.journal,
        )
        # 反射要能抢占任务（保命 > 干活）。挂起不是停止 —— 事完会自己接着做。
        self.reflex.bind_tasks(self.tasks)
        # 反射也要走仲裁层（它的 walk 声明优先级最高）
        self.reflex.bind_arbiter(self.arbiter)
        # 通知去重（见 _notify）
        self._last_notify_text = ""
        self._last_notify_at = 0.0
        # 数她经历了多少次 LLM 请求 —— 用来"每 N 次附一次状态包"（见 inject_body_state）
        self._llm_calls = 0
        # 游戏公屏的已读游标 —— 和 uplink 的 _last_seq 故意分开：
        # 那个管"要不要唤醒她"，这个管"她看没看见"，共用一个会互相吃消息。
        self._chat_seen_seq = 0

    async def _notify(self, facts: str) -> None:
        """把一个处境事实推到白的会话里（她/用户能看见）。

        注意：注意： 只传事实，不许传句子（用户 2026-10-09 拍板）：
        > "感知我是没法接受程序文本的。"
        已完成： food=6/20 food_items=0 ／ 不成立或禁止： 我饿了，但身上没有食物。
        —— 后者是程序替她写的台词。她想怎么讲是她的事，程序只负责把状态摆出来。

        注意： 而且只给"需要用户知道、需要用户动手"的事用。
        自动反射的状态翻转（进战/脱战/逃跑/卡住）一律只写状态日志，不许占聊天正文 ——
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

        # 注意： 前缀必须是 [mc:...] —— 规范：凡是从 Minecraft 来的数据都带 [mc:*] 标签。
        #    这样她（和模型）一眼能分清"游戏世界"和"现实对话"，不会把游戏里的下雨
        #    当成用户现实里在下雨（2026-10-10 实际发生过，见 docs/17 P2）。
        await self.context.send_message(umo, MessageChain([Plain(f"[mc:alert] {facts}")]))

    async def _held_count(self) -> int | None:
        """手上（快捷栏当前格）那个东西有几个。读不到回 None，不假装是 0。

        用途：mc_use_on 靠"手上的数量少没少"来判定"到底放没放成" ——
        这是唯一能自动验证的实据（见那个工具里的说明）。

        注意：注意： 2026-10-10 订正（这就是 docs/18 A2 那个"held 对不上"的真因）：
        老代码拿 data['held'] 去比 hotbar 项里的 slot 字段 ——
        而那个字段在 hotbar 里压根不存在（服务端只给 main 的项加 slot），
        于是循环永远匹配不上、恒返回 None  ->  mc_use_on 永远只会说"确认不了"。

        真相：held 是快捷栏的序号（0~8），物品是 hotbar[held]。
        实测 "held":2.0, "via":"inv.selected" —— 访问器一直是好的，是我们读错了。
        """
        data, err = await self._call("mcb inventory")
        if err or not isinstance(data, dict):
            return None
        sel = data.get("held")
        if not isinstance(sel, (int, float)) or isinstance(sel, bool):
            return None
        idx = int(sel)
        hotbar = data.get("hotbar") or []
        if not 0 <= idx < len(hotbar):
            return None
        it = hotbar[idx]
        if not isinstance(it, dict):
            # 注意： 空槽位是 null（不是缺字段）。这是"手上是空的"，不是"读不到" ——
            #    空手就是 0 个，如实报 0。"读不到"才回 None。
            return 0
        try:
            return int(it.get("c"))
        except (TypeError, ValueError):
            return None

    async def _menu_snapshot(self) -> dict | None:
        """取一次"她当前开着的界面"。读不到返回 None（不是空 dict）。

        注意： 索引类的东西（格号）用之前要核身份 —— 快照过期/界面被顶掉就会点错格
        （docs\\04 坑 13）。
        """
        data, err = await self._call("mcb state")
        if err or not isinstance(data, dict):
            return None
        task = data.get("task")
        menu = task.get("menu") if isinstance(task, dict) else None
        return menu if isinstance(menu, dict) else None

    async def _permission_denied(self, raw: dict) -> None:
        if raw.get("who") != str(self._cfg("character_name", "Nanako")):
            return
        token = str(raw.get("walkToken") or "")
        claim = self.arbiter.rejected_claim(token)
        if claim is None:
            return
        detail = str(raw.get("text") or "权限拒绝")
        if claim.owner == "task":
            await self.tasks.fail_permission(detail)
        else:
            await self.arbiter.reject_walk(token)
        self.journal.add("body", f"[mc:permission] 已终止 {claim.owner} 的对应意图：{detail}")

    async def _claim_walk_user(self, cmd: str, note: str) -> tuple[dict, str | None]:
        """用户级地声明 walk 通道。返回值和 _call 同形，方便原地替换。

        注意： 别再直接 mcb baritone ... —— 那会绕过仲裁、把任务/反射的路线踩掉。
        """
        await self.arbiter.claim_walk(LEVEL_USER, "llm", cmd, note)
        return {"ok": self.arbiter.last_error is None}, self.arbiter.last_error

    async def _release_walk_user(self) -> None:
        await self.arbiter.release_walk("llm")


    @filter.on_llm_request(priority=100)
    async def inject_body_state(self, event: AstrMessageEvent, req) -> None:
        """每 N 次 LLM 请求，把状态数据包附在她这条用户消息的末尾。

        用户 2026-10-09 定："状态最少得三到四轮对话主动告诉她一次"，
        并且不接受程序文本 —— 所以这里只挂 §render.state_packet 那份纯数据。

        注意： 挂 extra_user_content_parts（用户消息侧）而不是 system_prompt ——
        状态每轮都在变，塞进 system 会把提示词缓存全打掉。
        （先例：astrbot_plugin_dafeiyu_pet 也在 on_llm_request 里注身体状态，
          但它挂的是 system_prompt；我们数据更碎，走用户侧更划算。）
        """
        if not self._cfg("enable_body_state", True):
            return
        # 注意： 聊天是"新的就立刻给"，不受 every 节流（用户 2026-10-10）：
        #    状态晚三轮无所谓（血还在掉），但有人跟你说话晚三轮就蠢了。
        #    先看有没有新聊天，有就这一轮一定注。
        chat_lines, has_new_chat = await self._pull_chat()

        self._llm_calls += 1
        every = max(1, int(self._cfg("body_state_every", 3)))
        # 第一次就给 —— 她一开始就该知道自己站在哪、什么状态
        due = (self._llm_calls == 1 or self._llm_calls % every == 0)
        if not due and not has_new_chat:
            return
        try:
            data, err = await self._call("mcb state")
            if err:
                return
            if not data.get("online"):
                from astrbot.core.agent.message import TextPart
                req.extra_user_content_parts.append(TextPart(
                    text="[mc:body] offline\n" + render.describe_hold(data)
                ))
                return
            # 注意： 聊天只在她"被唤醒的那一刻"由 uplink 注入是不够的 ——
            #    实测（2026-10-10）她在 QQ 会说"我听不见游戏聊天"，
            #    因为那条通路不在她的自我模型里。所以：聊天也当上下文数据包的一部分，
            #    和 [body]/[log] 一起给，并配一段接线说明（见 render.WIRING_NOTE）。
            packet = render.state_packet(
                data, self.reflex.stance, self.tasks.status(), self.journal, self.places,
                chat_lines if has_new_chat else None,
                wiring=self._cfg("enable_wiring_note", True),
                instincts=reflexes.overview(),
                hold=render.describe_hold(data, self.uplink.hold_reason,
                                          self.uplink.hold_detail),
            )
            from astrbot.core.agent.message import TextPart
            req.extra_user_content_parts.append(TextPart(text=packet))
        except Exception as exc:  # noqa: BLE001 - 注入失败绝不能让她这条消息也发不出去
            logger.warning(f"[{PLUGIN_NAME}] 状态注入失败（不影响对话）：{exc}")

    async def _pull_chat(self) -> tuple[list[dict], bool]:
        """拉新的游戏公屏消息。返回 (新行, 有没有新的)。

        注意： 用一个独立的游标 _chat_seen_seq，不复用 uplink 的 _last_seq ——
        uplink 关心的是"要不要唤醒她"，我们关心的是"她看没看见"，
        两者语义不同，共用一个游标会互相吃消息。
        """
        try:
            data, err = await self._call(f"mcb chat {self._chat_seen_seq}")
            if err:
                return [], False
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 拉聊天失败：{exc}")
            return [], False
        lines = [x for x in (data.get("lines") or []) if isinstance(x, dict)]
        if not lines:
            return [], False
        newest = max(
            (int(float(x.get("seq") or 0)) for x in lines), default=self._chat_seen_seq
        )
        if newest > self._chat_seen_seq:
            self._chat_seen_seq = newest
        return lines, True

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

        # 事件流跟着反射一起开关 —— 两者都是"常驻在跑的后台感知"，没必要拆成两个开关
        if self._cfg("enable_reflex", True):
            self.events.start()

        # 自主心跳 —— 默认关，因为它按轮烧 token（见 _start_self_loop）
        if self._cfg("enable_self_loop", False):
            await self._start_self_loop()
        else:
            logger.info(
                f"[{PLUGIN_NAME}] 自主心跳已关闭（enable_self_loop=false）—— "
                "她自己那个 future_task 工具仍然可用"
            )

        logger.info(
            f"[{PLUGIN_NAME}] 已加载，RCON 目标 {self.bridge.host}:{self.bridge.port}"
        )

    async def terminate(self) -> None:
        # 注意： 先停任务再停反射 —— 反过来的话任务可能正在等反射放开它，
        #    会卡在这个 await 上。停任务顺带把她的寻路也取消掉。
        await self.tasks.stop(quiet=True)
        await self.reflex.stop()
        await self.events.stop()
        await self.uplink.stop()
        await self._drop_self_loop()
        await self.bridge.close()
        logger.info(f"[{PLUGIN_NAME}] 已卸载，RCON 连接已关闭")

    # ---- 自主心跳 --------------------------------------------------------
    #
    # 注意：注意： 这个会花钱，默认关着。
    #     每次唤醒 = 一整轮 agent（30 个工具描述 + 记忆注入 + 状态包），
    #     一次几千到上万 token。每 20 分钟醒一次 ≈ 一天 72 轮。
    #
    # 机制用的是 AstrBot 原生的 CronJobManager 的 active_agent 任务
    # （astrbot/core/cron/manager.py:172  ->  _woke_main_agent）——
    # 它会重新构造事件跑满一轮 agent，并注入 SendMessageToUserTool 让她能开口。
    # 别自己造轮子（asyncio 定时器伪造唤醒那条路，框架已经替我们做好了）。
    #
    # 注意： 另一条免费的路：她自己的 future_task 工具（AstrBot 默认就注入给她了）。
    #     接线说明里已经交代过。先试那条 —— 她自己排的班比我们替她排的更合身。

    async def _drop_self_loop(self) -> None:
        """删掉同名的自主心跳任务。加载和卸载都要调 —— 反复重载不能越积越多。"""
        mgr = getattr(self.context, "cron_manager", None)
        if mgr is None:
            return
        try:
            for job in await mgr.list_jobs():
                if getattr(job, "name", "") == SELF_LOOP_JOB:
                    await mgr.delete_job(job.job_id)
                    logger.info(f"[{PLUGIN_NAME}] 已清掉旧的自主心跳任务")
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[{PLUGIN_NAME}] 清理自主心跳时出错（可忽略）：{exc}")

    async def _start_self_loop(self) -> None:
        """注册"没人叫她的时候自己醒来接着玩"的定时任务。失败不影响插件其它部分。"""
        umo = str(self._cfg("white_session", "") or "")
        if not umo:
            logger.warning(
                f"[{PLUGIN_NAME}] 开了自主心跳但没配 white_session —— **不启动**。"
                "（cron 唤醒必须知道把消息投到哪个会话，缺了它发不出去）"
            )
            return
        mgr = getattr(self.context, "cron_manager", None)
        if mgr is None:
            logger.warning(
                f"[{PLUGIN_NAME}] 这个 AstrBot 版本没有 context.cron_manager —— 自主心跳不可用"
            )
            return

        minutes = int(self._cfg("self_loop_minutes", 20) or 20)
        minutes = max(5, min(240, minutes))
        await self._drop_self_loop()          # 注意： 先删同名的，别越积越多
        try:
            await mgr.add_active_job(
                name=SELF_LOOP_JOB,
                cron_expression=f"*/{minutes} * * * *",
                payload={"session": umo, "note": SELF_LOOP_NOTE, "origin": "plugin"},
                description="自主心跳：没人叫她的时候自己醒来看看该做什么",
                # 注意： 不带进 DB —— 插件每次加载自己重建，避免她删了插件还留着孤儿任务
                persistent=False,
            )
            logger.info(f"[{PLUGIN_NAME}] 自主心跳已开：每 {minutes} 分钟唤醒一次")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 自主心跳注册失败：{exc}")

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
            return render.fail(
                render.Kind.DENIED, "没有配置允许调用者，MC 工具对所有人关闭",
                usage="管理员在 AstrBot 插件配置的 allowed_sender_ids 里填入允许的发送者 ID",
                hint="这是我的配置问题，不是你的问题——照实告诉用户，别自己重试",
            )
        if sender not in allowed:
            logger.info(f"[{PLUGIN_NAME}] 拒绝未授权调用 sender={sender}")
            return render.fail(
                render.Kind.DENIED, "你没有调用 Minecraft 工具的权限",
                detail=f"你的发送者 ID：{sender}",
                hint="让主人在插件配置的 allowed_sender_ids 里加上这个 ID；别自己重试",
            )
        return None

    def _guard(self, event: AstrMessageEvent) -> str | None:
        """工具的统一前置检查。返回拒绝理由；None = 放行。

        注意： 每个工具都必须走这里，别自己抄一遍授权逻辑 ——
        曾经 18 个工具抄了 18 遍，改一个公共行为要动 18 处。
        要加"逐工具开关"之类的公共检查，只改这一个地方。
        """
        return self._authorize(event)

    # ---- 与桥对话 -------------------------------------------------------

    async def _call(self, command: str) -> tuple[dict | None, str | None]:
        """发一条桥命令。返回 (data, 错误文案)，两者恰有一个非 None。"""
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
        """Your own state in MC: online / coords / health / hunger / dimension /
        what you are doing / what just happened.
        
        The result has both "what you are doing" (your task progress) and "pathing" (Baritone's
        walking state) -- do not confuse the two.
        After any ACTION tool, wait a moment and use this to confirm the result: those tools can
        only report "dispatched", never "it worked".
        "offline" means the character is not connected to the server -- no action is possible."""
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb state")
        if err:
            return render.reword(err, "查不到 Nanako 的状态", kind=render.Kind.BRIDGE,
                                hint="过几秒重试一次；一直这样就说明桥断了（不是她不在线）")
        return render.describe_state(
            data, self.reflex.stance, self.tasks.status(), self.journal,
            hold=render.describe_hold(data, self.uplink.hold_reason, self.uplink.hold_detail),
        )

    @filter.llm_tool(name="mc_say")
    async def mc_say(self, event: AstrMessageEvent, text: str):
        """Say something on the in-game public chat using Nanako's body.
        This is in-game chat, NOT a reply to QQ.
        
        Args:
            text(string): What to say. Goes to the public chat; everyone can see it."""
        if (deny := self._guard(event)):
            return deny
        clean = self._clean_text(text, MAX_SAY_LEN)
        if clean is None:
            return "要说的话是空的，或者只包含换行 —— 没发出去。"
        _, err = await self._call(f"mcb say {clean}")
        if err:
            return render.reword(err, "没能在游戏里说话", kind=render.Kind.BRIDGE,
                                hint="过几秒重试一次")
        return f"已经用 Nanako 的身体在游戏里说了：{clean}"

    @filter.llm_tool(name="mc_goto")
    async def mc_goto(self, event: AstrMessageEvent, near: str = "", x: float = 0, z: float = 0):
        """Walk somewhere. ASYNC -- it dispatches and she walks on her own;
        check coordinates later with mc_state.
        
        Three ways to specify; give just one:
        - near="crafting_table" / near="minecraft:furnace" -- walk next to that thing.
          Looks in your PLACE BOOK first (names you recorded), then treats it as a block id and
          scans nearby. This is the easiest form -- no coordinates needed.
        - x + z -- walk to exact coordinates (only when you read F3 or someone told you).
        
        WARNING: searching by block name only covers ~16 blocks around her (server scan limit).
        If it is further away it will not be found -- walk closer first, or record it in the place
        book (mc_place) and then use near="name".
        
        Common block ids: minecraft:crafting_table, minecraft:furnace, minecraft:chest,
        minecraft:farmland, simpletomb:grave_cross.
        
        Args:
            near(string): A place name you recorded, or a block id; walks to the nearest one.
            x(number): Target X (used together with z).
            z(number): Target Z."""
        if (deny := self._guard(event)):
            return deny

        want = str(near or "").strip()
        if want:
            # 1 先查地点簿 —— "家""田"这种名字比方块名好用得多
            hit = self.places.match(want)
            if hit is not None:
                name, place = hit
                tx, tz = int(float(place["x"])), int(float(place["z"]))
                # 注意： 顺手做一次指纹自检 —— 去一个很久没去的地方，
                #    最该知道的就是"那地方还在不在"。一次 RCON 往返，很便宜。
                #    抄 mcpfabric memory.ts 的 valid/changed/gone。
                status = self.places.check(
                    name, await self._block_at(place.get("x"), place.get("y"), place.get("z"))
                )
                _, err = await self._claim_walk_user(f"goto {tx} {tz}", f"去「{name}」")
                if err:
                    return render.reword(err, "没能让她出发", kind=render.Kind.BRIDGE,
                                        hint="先用 mc_state 看她在哪、在不在线")
                warn = {
                    "changed": "⚠️ 不过**那地方已经和记的时候不一样了** —— 到那儿先看一眼再说。",
                    "gone": "❌ 而且**那儿已经空了** —— 多半被挖掉或炸没了，可能白跑一趟。",
                }.get(status, "")
                return (f"出发去「{name}」（{tx}, {tz}）"
                        + (f"，那儿是{place['what']}" if place.get("what") else "")
                        + "。过会儿用 mc_state 看坐标确认到没到。"
                        + ("\n" + warn if warn else ""))

            # 2 不是地点名  ->  当方块注册名在附近扫
            block_id = want if ":" in want else f"minecraft:{want}"
            pos = await mcb_containers.find_block(self.bridge, block_id)
            if pos is None:
                return (
                    f"附近 16 格内没有「{want}」这种方块，地点簿里也没这个名字。\n"
                    "要么走近点再试，要么直接给坐标（x/z），"
                    "要么先过去一趟再用 mc_place 把它记下来。"
                )
            tx, tz = int(pos[0]), int(pos[2])
            _, err = await self._claim_walk_user(f"goto {tx} {tz}", f"扫到的 {block_id}")
            if err:
                return render.reword(err, "没能让她出发", kind=render.Kind.BRIDGE,
                                    hint="先用 mc_state 看她在哪、在不在线")
            return (f"附近找到了 {block_id}（{pos[0]},{pos[1]},{pos[2]}），"
                    "已让她出发。过会儿用 mc_state 看坐标确认。")

        tx, tz = self._clean_coord(x), self._clean_coord(z)
        if tx is None or tz is None:
            return (
                "要么给 `near`（地点名或方块名），要么给 `x` 和 `z` 坐标 —— "
                f"现在给的是 near={near!r} x={x} z={z}。"
            )
        _, err = await self._claim_walk_user(f"goto {tx} {tz}", "mc_goto x/z")
        if err:
                return render.reword(err, "没能让她出发", kind=render.Kind.BRIDGE,
                                    hint="先用 mc_state 看她在哪、在不在线")
        return (
            f"已让她出发前往 x={tx} z={tz}。**她现在还在路上** —— "
            "过一会儿调 mc_state 看坐标，确认她是不是真的到了。"
        )

    @filter.llm_tool(name="mc_place")
    async def mc_place(self, event: AstrMessageEvent, action: str = "list",
                       name: str = "", what: str = "", note: str = ""):
        """PLACE BOOK -- remember / list / self-check / forget "what is where".
        
        Your scan only covers ~16 blocks around you; outside that you are blind. Record the
        important places, and mc_goto near="name" will take you there without anyone reporting
        coordinates.
        
        - action="remember" -- record where you are standing right now (needs a name).
          Also write what (what this place is) so it makes sense when you read it later.
          It automatically stores a FINGERPRINT of the block under your feet.
        - action="list" -- see what you have recorded
        - action="check" -- is that place still the way it was? Compares fingerprints and answers
          unchanged / changed / gone / unknown. Give name to check only that one.
          WARNING: worth checking for outposts you have not visited in a long time, places that got
          blown up, or someone else's territory.
        - action="forget" -- forget one (give name)
        
        When to record: you built an outpost, found a field, placed a chest, found a cave entrance,
        where a grave is... The path you walked gets forgotten; what you recorded does not.
        
        Args:
            action(string): "remember" / "list" / "check" / "forget".
            name(string): The place name, one you will actually remember ("home", "wheat field",
                "cave mouth"). For check you may give just one.
            what(string): What this place is (a bit more than the name).
            note(string): Free-form note (optional)."""
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
                return render.reword(err, "读不到坐标，记不了", kind=render.Kind.BRIDGE,
                                    hint="先用 mc_state 确认她在哪")
            if not data.get("online"):
                return "她不在线，拿不到坐标。"
            px, py, pz = data.get("x") or 0, data.get("y") or 0, data.get("z") or 0
            # 顺手把脚下方块的指纹也记上 —— 以后就能知道"这地方变了没"。
            # 注意： 读脚底下一格（y-1），不是脚下那格 —— 站的地方永远是空气，没有信息量。
            fp = await self._block_at(px, float(py) - 1, pz)
            got = self.places.remember(
                name, px, py, pz,
                dim=str(data.get("dim") or ""), what=what, note=note,
                fp=fp or "",
            )
            self.journal.add("body", f"记了个地方：{got}")
            return got

        if act in ("check", "verify", "valid", "diff"):
            return await self._check_places(name)

        return f"不认得 action={action!r}。只认 remember / list / check / forget。"

    async def _block_at(self, x, y, z) -> str | None:
        """读某个坐标上的方块 id。读不到回 None（不是空串 —— 空串会被当成"没了"）。"""
        try:
            tx, ty, tz = int(float(x)), int(float(y)), int(float(z))
        except (TypeError, ValueError):
            return None
        try:
            data, err = await self._call(f"mcb blockinfo {tx} {ty} {tz}")
        except Exception:  # noqa: BLE001
            return None
        if err:
            return None
        got = data.get("id")
        return str(got) if got else None

    async def _check_places(self, name: str = "") -> str:
        """比对地点簿里的指纹 —— 那地方还是原来的样子吗。

        注意： 每个地方要一次 RCON 往返，所以封顶（12 个）。不做事前缓存：
        这个功能的意义就是"现在去看一眼"，缓存等于自欺。
        """
        rows = self.places.list()
        if not rows:
            return "地点簿是空的 —— 还没记过任何地方。"
        if name.strip():
            hit = self.places.match(name)
            if hit is None:
                return f"地点簿里没有「{name}」。"
            rows = [dict(hit[1], name=hit[0])]

        lines: list[str] = []
        for r in rows[:12]:
            key = str(r.get("name") or "?")
            status = self.places.check(key, await self._block_at(r.get("x"), r.get("y"), r.get("z")))
            mark = {"ok": "✅ 没变", "changed": "⚠️ **变了**", "gone": "❌ **没了**",
                    "unknown": "❓ 说不准"}.get(status, status)
            lines.append(f"· 「{key}」({r.get('x'):g},{r.get('y'):g},{r.get('z'):g}) —— {mark}"
                         + (f"（记的时候是 {r.get('fp')}）" if status in ("changed", "gone") and r.get("fp") else ""))
        if len(rows) > 12:
            lines.append(f"…（还有 {len(rows) - 12} 个没查 —— 一次只查 12 个，每个要一次往返）")
        lines.append("⚠️ 「说不准」= 当初没记指纹、或现在读不到那个坐标 —— **不等于没变**。")
        return "地点自检：\n" + "\n".join(lines)

    @filter.llm_tool(name="mc_follow")
    async def mc_follow(self, event: AstrMessageEvent, player: str):
        """Follow a player.
        
        WARNING: this means "stay within a few blocks", NOT "stick to them" -- if the target is
        right next to her, her not moving is normal.
        
        Args:
            player(string): The player name to follow (in-game id; letters, digits, underscore)."""
        if (deny := self._guard(event)):
            return deny
        name = self._clean_player(player)
        if name is None:
            return f"玩家名不合法：{player!r}。只允许 1-16 位字母、数字、下划线。"
        _, err = await self._claim_walk_user(f"follow player {name}", f"跟随 {name}")
        if err:
            return render.reword(err, "没能让她跟随", kind=render.Kind.BRIDGE,
                                hint="先用 mc_state 看她在不在线")
        return (
            f"已让她开始跟随 {name}。记住这是「跟到几格以内」不是贴身，"
            "目标就在旁边时她不动是正常的。"
        )

    @filter.llm_tool(name="mc_stop")
    async def mc_stop(self, event: AstrMessageEvent):
        """Emergency stop: CANCEL the current task, and stop all pathing / walking /
        following / using actions.
        
        WARNING: the task is DISCARDED (cancelled, not paused). If you want her to "do something
        else first and come back to it later", just give her the new instruction -- suspension is
        handled by the reflex layer, not by this."""
        if (deny := self._guard(event)):
            return deny
        stopped = await self.tasks.stop()
        # 注意： 不是无脑 mcb stop —— 那会把反射的逃跑路线也撤了（保命优先）。
        #    仲裁层的语义：撤掉用户级和任务级的声明，反射级的不动。
        #    （mc_arbiter 那条描述里也这么说；见 mcb/arbiter.py）
        await self.arbiter.clear_user_and_task()
        err = self.arbiter.last_error
        # 顺手松开"使用键" —— 万一吃东西时出了岔子，按键卡住会让她一直重复动作
        await self._call("mcb release")
        if err:
            return render.reword(err, "没能让她停下", kind=render.Kind.BRIDGE,
                                hint="过几秒再用 mc_state 看她还在不在走")
        return f"已下发停止指令，她应该会停下来（惯性可能还会滑一小段）。{stopped}"

    @filter.llm_tool(name="mc_baritone_raw")
    async def mc_baritone_raw(self, event: AstrMessageEvent, command: str):
        """Escape hatch: send a raw Baritone command. Only use it when the
        fixed tools cannot do the job.
        
        Common ones: mine diamond_ore (mine), explore, farm, goto 100 64 -200
        (goto with Y), come, thisway 100.
        
        Args:
            command(string): The Baritone command itself, WITHOUT the leading #."""
        if (deny := self._guard(event)):
            return deny
        clean = self._clean_text(command, MAX_RAW_LEN)
        if clean is None:
            return "命令是空的。"
        clean = clean.lstrip("#").strip()
        if not clean:
            return "命令是空的。"
        # 注意： 白名单（见 BARITONE_VERBS 的注释）—— 别改成黑名单。
        verb = clean.split()[0].lower()
        if verb not in BARITONE_VERBS:
            return (
                f"error: 不允许「{verb}」这个 Baritone 动词 —— 它可能有能力一次改掉一大片方块。\n"
                f"usage: 只允许 {' / '.join(sorted(BARITONE_VERBS))}\n"
                "hint: 挖东西用 `mine <方块>`（例如 `mine oak_log`）；"
                "想去某个地方用 `mc_goto`；想看周围用 `mc_around`。"
            )
        _, err = await self._claim_walk_user(clean, "mc_baritone_raw")
        if err:
            return render.reword(err, "没能执行", kind=render.Kind.BRIDGE,
                                hint="先用 mc_state 看她在不在线")
        return f"已把 Baritone 命令下发出去：{clean}。过一会儿用 mc_state 看效果。"

    @filter.llm_tool(name="mc_skill")
    async def mc_skill(self, event: AstrMessageEvent, name: str):
        """Read the FULL text of one of your skills.

        WARNING: your instructions have a ## Skills section listing your skills, and it tells
        you to open the SKILL.md file with a shell command (cat / type). You have no
        shell -- that command will fail. Use THIS tool instead, with the skill NAME.

        Read a skill's SKILL.md BEFORE you use it. Never assume what is inside it.

        WARNING: this is for YOUR OWN instruction bundles, not for game files.

        Args:
            name(string): the skill name exactly as listed in your ## Skills section."""
        if (deny := self._guard(event)):
            return deny
        key = str(name or "").strip()
        if not SKILL_NAME_RE.match(key):
            return f"技能名不合法：{key!r}（只能用字母数字下划线短横线）。"
        path = SKILLS_DIR / key / "SKILL.md"
        try:
            if not path.is_file():
                have = [p.name for p in SKILLS_DIR.iterdir()
                        if p.is_dir()] if SKILLS_DIR.is_dir() else []
                return (
                    f"没有叫「{key}」的技能。现有："
                    + ("、".join(sorted(have)) if have else "（一个都还没有）")
                )
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"读不到技能正文：{exc}"
        if len(text) > MAX_SKILL_CHARS:
            text = text[:MAX_SKILL_CHARS] + f"\n\n…（只显示了前 {MAX_SKILL_CHARS} 字）"
        return f"技能「{key}」的正文：\n\n{text}"

    # ---- 背包与交互（生存必需）-------------------------------------------

    @filter.llm_tool(name="mc_inventory")
    async def mc_inventory(self, event: AstrMessageEvent):
        """Look at your inventory: what you have, which hotbar slot is in hand,
        and your hunger. Check it before taking / eating / crafting anything."""
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb inventory")
        if err:
            return render.reword(err, "读不到背包", kind=render.Kind.BRIDGE,
                                hint="先用 mc_state 确认她在线")
        return render.describe_inventory(data)

    @filter.llm_tool(name="mc_hold")
    async def mc_hold(self, event: AstrMessageEvent, slot: int):
        """Move HOTBAR slot 0-8 into your hand.
        If the item is somewhere in the backpack (unknown slot), use mc_equip instead.
        
        Args:
            slot(number): Hotbar slot; 0 is leftmost, 8 is rightmost."""
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
            return render.reword(err, "切换快捷栏失败", kind=render.Kind.BRIDGE,
                                hint="先用 mc_state 确认她在线；格号是 0~8")
        return f"已经把手换到快捷栏第 {n} 格。"

    @filter.llm_tool(name="mc_equip")
    async def mc_equip(self, event: AstrMessageEvent, item: str, where: str = "hand"):
        """Move an item you carry to a specific place -- just give the item name,
        no need to find which slot it is in first.
        
        Possible values of where:
        - hand (default) -- hold it. Use this for placing blocks, eating, using tools.
        - off -- offhand. Hold a shield, carry a torch as a light source.
        - head / chest / legs / feet -- wear armor.
        - hotbar0 .. hotbar8 -- put it in a specific HOTBAR slot (swaps with whatever is there).
        - backpack -- move it from the hotbar into the backpack (finds an empty slot).
        
        WARNING: use the REGISTRY id, not a translated display name --
        minecraft:iron_helmet is right, a localized name is not.
        
        Args:
            item(string): The item's registry id, e.g. "minecraft:iron_helmet", "minecraft:shield".
            where(string): "hand" (default) / "off" / "head" / "chest" / "legs" / "feet"
                / "hotbar0"~"hotbar8" / "backpack"."""
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
        """Use what is in your HAND: eat food, drink a potion, place a block, shoot a bow.
        
        To use something ON A BLOCK (open a chest, place on the ground) use mc_use_on instead."""
        if (deny := self._guard(event)):
            return deny
        _, err = await self._call("mcb use")
        if err:
            return render.reword(err, "使用失败", kind=render.Kind.BRIDGE,
                                hint="先用 mc_state 看她在不在线、手上是什么")
        return "已经用了一次手上的东西。过一会儿用 mc_inventory 或 mc_state 看效果。"

    @filter.llm_tool(name="mc_use_on")
    async def mc_use_on(
        self, event: AstrMessageEvent, x: float, y: float, z: float, keep_open: bool = False
    ):
        """Right-click the BLOCK at the given coordinates: place a block, press a
        button, open a chest / crafting table.
        
        WARNING: reach is about 4.5 blocks; if it is out of reach, mc_goto there first.
        By default any screen that opens is closed again. To work with things INSIDE the screen
        (take from a chest, craft), set keep_open=true, then use mc_menu + mc_click.
        
        Args:
            x(number): X of the target block.
            y(number): Y of the target block.
            z(number): Z of the target block.
            keep_open(bool): true = leave the opened screen open for mc_menu / mc_click."""
        if (deny := self._guard(event)):
            return deny
        coords = self._clean_coords3(x, y, z)
        if coords is None:
            return f"坐标不合法：({x}, {y}, {z})。"
        tx, ty, tz = coords
        # 注意： 先拍一张"手上的东西"的快照 —— 放方块/用物品成功了的话，
        #    手上的数量会少。这是唯一能自动判定"到底放没放成"的实据。
        #
        #    踩过（2026-10-10，用户在公屏让她放熔炉）：工具只回了一句
        #    "已对着 (…) 右键…过一会儿用 mc_inventory 看手上东西少没少"，
        #    她没看，转头就在公屏上说"熔炉放地上啦！" —— 其实熔炉还在包里。
        #    ⭐ 教训：把验证甩给一个不会去验证的人，等于没有验证。
        #    （和 docs\11 那条"成功判据用世界真的变了，不用命令发出去了"是同一条。）
        before = await self._held_count()
        # 走 useOnAt（直接给坐标构造命中），不依赖准星射线 —— 实测射线经常 MISS
        _, err = await self._call(f"mcb useOnAt {tx} {ty} {tz}")
        if err:
            return render.reword(
                err, f"对着 ({tx},{ty},{tz}) 右键没成功",
                kind=render.Kind.OUT_OF_REACH,
                hint=f"先 `mc_goto x={tx} z={tz}` 走到 4 格以内，再重试这条",
            )
        if keep_open:
            await asyncio.sleep(0.6)   # 等界面开起来
            data, merr = await self._call("mcb state")
            if merr:
                return render.reword(merr, f"已对着 ({tx},{ty},{tz}) 右键，但读不到界面",
                                     kind=render.Kind.BRIDGE, hint="过几秒再用 mc_menu 看一次")
            return "已对着 ({tx},{ty},{tz}) 右键，界面留着没关。\n" + render.describe_menu(data)
        # 不操作界面  ->  关掉，免得她动不了
        await self._call("mcb closeGui")
        after = await self._held_count()
        if before is None or after is None:
            return (
                f"已对着 ({tx},{ty},{tz}) 右键，界面也关掉了。"
                "（手上数量没读到，**这次没法自动确认生效** —— 要用 mc_inventory 自己看。）"
            )
        if after < before:
            return (
                f"✅ 已在 ({tx},{ty},{tz}) 用掉了 1 个手上的东西（{before} → {after}）"
                "—— **这回是真的生效了**。"
            )
        return (
            f"⚠️ **没生效**：对着 ({tx},{ty},{tz}) 右键了，"
            f"但手上的东西**一个没少**（还是 {before} 个）。"
            "多半是**那里放不下**（目标格不是空气 / 被占着），或者**够不着**。"
            "**别跟用户说放下了** —— 换个地方再试，或者直说没放成。"
        )

    @filter.llm_tool(name="mc_menu")
    async def mc_menu(self, event: AstrMessageEvent):
        """See what is inside the CURRENTLY OPEN screen (chest / crafting table /
        inventory crafting grid): each slot's SLOT NUMBER plus its contents.
        
        Those slot numbers belong to THAT screen, not to your backpack.
        When opening a container with mc_use_on, remember keep_open=true."""
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb state")
        if err:
            return render.reword(err, "读不到界面", kind=render.Kind.BRIDGE,
                                hint="先用 mc_state 确认她在线")
        return render.describe_menu(data)

    @filter.llm_tool(name="mc_click")
    async def mc_click(self, event: AstrMessageEvent, slot: int, mode: int = 1):
        """Click slot number N of the currently open screen -- this is how you take
        things out of a chest and how crafting happens. Use mc_menu first to see the slot numbers.
        
        - mode=1 (default) shift quick-move: chest slot -> backpack, or CRAFT RESULT -> craft immediately
        - mode=0 plain left click (pick up / put down); mode=6 double-click to gather the same kind
        
        Args:
            slot(number): Slot number (look it up with mc_menu; NOT a backpack slot).
            mode(number): 1=shift quick-move (default, most common); 0=plain left click;
                6=double-click gather."""
        if (deny := self._guard(event)):
            return deny
        try:
            n = int(slot)
        except (TypeError, ValueError):
            return render.fail(
                render.Kind.BAD_ARGUMENT, "格子号不合法", detail=f"给的是 {slot!r}",
                usage="slot=整数（先用 mc_menu 看格号）",
                hint="先调 `mc_menu`，照着它列出来的格号原样传",
            )
        if n < 0:
            return render.fail(
                render.Kind.BAD_ARGUMENT, "格子号不能是负数", detail=f"给的是 {n}",
                usage="slot=0 或更大的整数",
                hint="先调 `mc_menu` 看真实格号",
            )
        try:
            m = int(mode)
        except (TypeError, ValueError):
            m = 1
        if m not in (0, 1, 2, 3, 4, 5, 6):
            return render.fail(
                render.Kind.BAD_ARGUMENT, "mode 只能是 0~6", detail=f"给的是 {m}",
                usage="mode=1 是 shift 整体移动（最常用）；0 是普通左键；6 是双击聚拢",
                hint="不确定就用默认的 mode=1，别自己填数字",
            )
        before = await self._menu_snapshot()
        _, err = await self._call(f"mcb clickSlot {n} 0 {m}")
        if err:
            return render.reword(err, "点格子失败", kind=render.Kind.REFUSED,
                                hint="先用 mc_menu 看一次界面 id，确认还是同一个容器再重点")
        await asyncio.sleep(0.4)
        # ⭐ 当场对账 —— 点之前那张快照和点之后比（docs\25 §三 1.8）
        after = await self._menu_snapshot()
        return render.describe_click_result(n, m, before, after)

    @filter.llm_tool(name="mc_craft")
    async def mc_craft(self, event: AstrMessageEvent, item: str, count: int = 1):
        """MAKE something: give the item name and she works out the whole chain and
        does it step by step (including finding a crafting table, walking there, opening it).
        
        Example: "make a wooden pickaxe" -> she derives log -> planks -> stick -> pickaxe herself.
        Recipes come from the server, so MOD items work too.
        WARNING: she must already have the materials (she will NOT go mining for missing ones --
        she will tell you exactly what is missing). Only crafting table / inventory crafting is
        supported; anything that needs a furnace goes through mc_smelt.
        
        Args:
            item(string): What to make, e.g. "wooden_pickaxe", "crafting_table", "oak_planks".
                The minecraft: prefix is optional; MOD items need their prefix.
            count(number): How many to make, default 1."""
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
        """SMELT something: put raw material into a furnace and get the product
        (raw iron -> iron ingot, sand -> glass, raw meat -> cooked meat).
        
        She picks the recipe herself (the same product often has several), walks over, loads
        material and fuel, WAITS for it to finish, and takes the result.
        WARNING: needs FUEL (coal / charcoal) and a furnace within 16 blocks. She loads the WHOLE
        stack of material -- you get however much comes out.
        If you are unsure whether to use this or mc_craft, try mc_craft first: it will say
        "this has to be smelted".
        
        Args:
            item(string): The thing to produce, e.g. "iron_ingot", "glass", "copper_ingot".
                The minecraft: prefix is optional."""
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
        """Attack whatever entity your CROSSHAIR is on (fight mobs, hit animals).
        
        It first checks what the crosshair points at. If that is not an entity, it tells you clearly.
        To aim at something first, use mc_aim(x, y, z) to lock your view onto it.
        (Usually you walk closer first with mc_goto / mc_follow to get the target into view.)"""
        if (deny := self._guard(event)):
            return deny
        _, err = await self._call("mcb attack")
        if err:
            return render.reword(err, "攻击失败", kind=render.Kind.BRIDGE,
                                hint="先用 mc_state 看她在不在线、手上是不是武器")
        return "已攻击准星指着的实体。过一会儿用 mc_state 看血量/效果。"

    @filter.llm_tool(name="mc_threats")
    async def mc_threats(self, event: AstrMessageEvent):
        """See whether there are mobs nearby, how far, what kind, how much health,
        and whether they are aiming at you (the server reads the world directly, so this is accurate).
        
        Use it to judge "is it safe around here", "should I fight", "which way do I run".
        targeting=true means that mob is staring at you."""
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb threats 24")
        if err:
            return render.reword(err, "查不到周围情况", kind=render.Kind.BRIDGE,
                                hint="过几秒重试")
        return render.describe_threats(data)

    @filter.llm_tool(name="mc_stance")
    async def mc_stance(self, event: AstrMessageEvent, mode: str):
        """Set your COMBAT STANCE -- whether to hunt mobs is your decision.
        
        - defend (default) passive: only fights back when HIT or when a mob is AIMING at you;
          does not CHASE things out of reach
        - hunt actively clear mobs: attacks any hostile within 5 blocks, chases ones out of reach
        
        WARNING: under BOTH stances, being hit triggers an immediate automatic counterattack
        (a reflex, it does not go through you), so defend will not get you killed.
        Set defend to mine in peace; set hunt to clear an area.
        
        Args:
            mode(string): Either "defend" (passive) or "hunt" (actively clear)."""
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
    async def mc_retreat(self, event: AstrMessageEvent, toward: str = "auto"):
        """BREAK OFF combat: stop fighting immediately and back away toward safety.
        Works even at full health (you are losing, do not want to fight, or want to come back and
        find someone).

        - auto (default) -- decide by how far the user is: run TO him if he is within ~24
          blocks, otherwise just run away from the mobs
        - owner -- always run toward the user (falls back to safe if he is offline or in
          another dimension)
        - safe -- always run away from the nearest mob for a while

        Args:
            toward(string): "auto" (default) / "owner" (run toward the user) / "safe"
                (run away from mobs)."""
        if (deny := self._guard(event)):
            return deny
        try:
            what = await self.reflex.retreat(str(toward or "auto"))
        except ValueError as exc:
            return str(exc)
        return f"已脱战，{what}。她不会再自己冲回去打。"

    # ---- 任务层（多步行为）-----------------------------------------------

    @filter.llm_tool(name="mc_task")
    async def mc_task(self, event: AstrMessageEvent, task: str, at: str = ""):
        """Send her off to do a MULTI-STEP job; the program carries it out step by step.
        Other tools are ONE ACTION; this is ONE JOB.
        
        Currently available:
        - torch path lighting -- walk in one direction, placing a torch every so often.
          Needs torches in her inventory.
        - farm farming -- walk to the field, harvest and replant. A hoe / seeds help but are not
          required (harvest-only takes up to 3 minutes).
        
        She does it on her own; you do not have to watch. Progress shows up under "what you are
        doing" in mc_state; stop it with mc_task_stop.
        WARNING: being hit / nearly dying interrupts her (survival first), but that is a SUSPENSION,
        not a cancellation -- she picks it back up once the danger passes.
        
        WARNING: a field further than ~16 blocks is OUT OF SCAN RANGE and farm will come up empty --
        use at to tell her where the field is.
        
        Args:
            task(string): The task name. An unknown name is rejected and the options are listed.
            at(string): Optional location for the task, format "x z" (e.g. "-50 120").
                For farming, use it to point at the field."""
        if (deny := self._guard(event)):
            return deny
        if not self._cfg("enable_mc_task", True):
            return "派任务的功能被管理员关掉了。"
        name = str(task or "").strip()
        if not name:
            return "要派哪件事？现在能派的有：\n" + render.describe_catalog(self.tasks.catalog())
        params: dict = {}
        # at = "x z"。解析失败就忽略 —— 任务自己会退化成"就地干"。
        parts = str(at or "").replace(",", " ").split()
        if len(parts) >= 2:
            px, pz = self._clean_coord(parts[0]), self._clean_coord(parts[1])
            if px is not None and pz is not None:
                params["x"], params["z"] = px, pz
        return await self.tasks.start(name, **params)

    @filter.llm_tool(name="mc_task_stop")
    async def mc_task_stop(self, event: AstrMessageEvent):
        """Throw away the current task and go back to idling.
        
        WARNING: this is a CANCEL, not a pause -- progress is lost. To "do something else first and
        come back to it later" do NOT use this; just give the new instruction."""
        if (deny := self._guard(event)):
            return deny
        return await self.tasks.stop()

    @filter.llm_tool(name="mc_journal")
    async def mc_journal(self, event: AstrMessageEvent, count: int = 20):
        """Browse the STATE LOG: what just happened, by category
        (task / reflex / craft / body / error).
        
        Use it when the user asks "what were you just doing" and you cannot remember, or to confirm
        whether something succeeded.
        WARNING: mc_state already carries the last 10 entries; this one goes further back.
        The log lives IN MEMORY ONLY -- a plugin reload clears it.
        
        Args:
            count(number): How many entries back, default 20, max 100."""
        if (deny := self._guard(event)):
            return deny
        try:
            n = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            n = 20
        if len(self.journal) == 0:
            return "日志是空的 —— 要么刚重启过，要么真的什么都还没发生。"
        return f"最近 {min(n, len(self.journal))} 条：\n{self.journal.render(n)}"

    @filter.llm_tool(name="mc_events")
    async def mc_events(self, event: AstrMessageEvent, count: int = 20):
        """What just happened -- damage taken / deaths / who logged in or out /
        what appeared in or left your inventory.
        
        When to use it:
        - Your inventory suddenly changed and you want to know WHO gave you something
          ("got diamond_sword x1")
        - You lost health but did not see what hit you ("hurt <- zombie -3.5hp left 12.7")
        - You want to know WHO IS ONLINE, or who just logged in
        - The user asks "has anyone been around"
        
        WARNING: how this differs from mc_journal:
        - mc_events = things happening OUT THERE (what others did, what the world did)
        - mc_journal = things YOU did (how many torches you placed, what you smelted)
        
        WARNING: inventory changes are BATCHED -- mining does not spam one line per cobblestone, it
        comes as one line "got cobblestone x23". So what you see is AGGREGATED, not a per-event
        timeline. For precise current state use mc_inventory.
        
        Args:
            count(number): How many entries back, default 20, max 60."""
        if (deny := self._guard(event)):
            return deny
        try:
            n = max(1, min(int(count), 60))
        except (TypeError, ValueError):
            n = 20
        return "最近发生的事：\n" + self.events.render(n)

    async def _around_t2(self, data: dict, force: bool = False) -> dict:
        """mc_around 的第二层：T2 跨 tick 全分辨率扫描。

        它是个后台任务（服务端每 tick 推进一小块，约 6ms）：第一次调用开一个，
        之后每次来看进度，扫完就读结果。所以"叫一次拿不全"是正常的 —— 再叫一次。

        注意： 这一层失败不影响 T1 —— 它只是"更细的一层"，拿不到就算了，
        绝不能让整个 mc_around 报错。
        """
        status, err = await self._call("mcb t2 status")
        if err or not isinstance(status, dict):
            return {"job": "error", "err": err or "拿不到状态"}
        job = status.get("job")

        # 位置挪远了就重扫 —— 缓存是以某个点为中心的，人走了就没意义
        if job == "done" and not force:
            tried = status.get("center") or []
            here = data.get("center") or []
            if len(tried) == 3 and len(here) == 3:
                d2 = sum((float(tried[i]) - float(here[i])) ** 2 for i in range(3))
                if d2 > 64 * 64:
                    job = "none"          # 装成"没扫过"，下面会重开一个

        if job == "none" or force:
            started, err2 = await self._call("mcb t2 start 6")
            if err2:
                return {"job": "error", "err": err2}
            return {"job": "running", "status": started, "justStarted": True}

        if job == "running":
            return {"job": "running", "status": status}

        got, err3 = await self._call("mcb t2 get")
        if err3:
            return {"job": "done", "status": status, "err": err3}
        if not isinstance(got, dict):
            return {"job": "done", "status": status, "err": "结果不是数据"}
        return got

    @filter.llm_tool(name="mc_around")
    async def mc_around(self, event: AstrMessageEvent, deep: str = ""):
        """Look at what is around you -- a wide overview: terrain + surface
        composition + nearby facilities.

        WARNING: how it differs from the other two:
        - "what is the environment like", "is there a chest nearby", "is the ground flat" -> THIS
        - "what is this block" (the one under the crosshair) -> mc_lookat (block-accurate, free)
        - "where is a specific block" (coords of a crafting table / furnace) -> mc_goto near

        WARNING: it comes in TWO layers and the second one takes time:
        - The FIRST layer answers right away: terrain + facilities that have a block entity
          (chest / furnace / barrel / bed / sign / spawner / portal).
        - The SECOND layer ("deep") is a background scan that also finds things WITHOUT a block
          entity (crafting table / anvil / stonecutter / loom / fletching table / composter /
          portal frame) plus a full-resolution block count. It runs at ~6ms per server tick and
          takes ~36 seconds for the full radius. Call this tool again later to collect it.
          You do NOT have to wait or poll.

        WARNING: slow -- about 50ms of server time per call (one tick); do not call it repeatedly.
          (The deep layer does NOT add to that; it is spread out in the background.)

        Args:
            deep(string): Pass "1" to force a FRESH deep scan centered where you stand now.
                Leave empty normally.
        """
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb around")
        if err:
            return render.reword(err, "看不了周围", kind=render.Kind.BRIDGE,
                                hint="过几秒重试")
        force = str(deep).strip().lower() in ("1", "true", "yes", "deep", "now")
        t2 = await self._around_t2(data if isinstance(data, dict) else {}, force)
        return render.describe_around(data, t2)

    @filter.llm_tool(name="mc_where")
    async def mc_where(self, event: AstrMessageEvent, player: str):
        """Look up WHERE a player is -- yourself, or anyone on the server.
        
        When to use it:
        - You want to go find someone -> get their coordinates, then mc_goto there
        - You want to know how far away they are
        
        WARNING: this reads the authoritative server value directly. It does not require them to
        speak or to agree. So be aware of what you are doing: you are asking "where is he", not
        "where did he say he was".
        
        Args:
            player(string): The player name (in-game id)."""
        if (deny := self._guard(event)):
            return deny
        who = " ".join(str(player or "").split())
        if not who:
            return "得说清楚找谁。"
        data, err = await self._call(f"mcb where {who}")
        if err:
            return render.reword(err, "查不到这个玩家", kind=render.Kind.NOT_FOUND,
                                hint="不带参数可以查主人；名字要用她的游戏 ID（不是显示名）")
        return render.describe_where(data, who)

    @filter.llm_tool(name="mc_chat_log")
    async def mc_chat_log(self, event: AstrMessageEvent, count: int = 20):
        """Browse the IN-GAME PUBLIC CHAT -- who said what, and where they were
        standing at the time.
        
        When to use it:
        - The user asks "did anyone talk in game", "what were they talking about"
        - You were called by name in game and want the surrounding context
        - You want to know WHERE someone was when they said it (every line carries their
          coordinates and how far they were from you)
        
        WARNING: you ALREADY HEAR the public chat -- someone calling your name wakes you directly,
        and new lines show up automatically in the [chat] section you receive each turn. This tool
        is for actively scrolling BACK.
        
        Args:
            count(number): How many entries back, default 20, max 50."""
        if (deny := self._guard(event)):
            return deny
        try:
            n = max(1, min(int(count), 50))
        except (TypeError, ValueError):
            n = 20
        data, err = await self._call("mcb chat 0")
        if err:
            return render.reword(err, "翻不到聊天记录", kind=render.Kind.BRIDGE,
                                hint="过几秒重试")
        lines = [x for x in (data.get("lines") or []) if isinstance(x, dict)]
        if not lines:
            return "游戏公屏是空的 —— 还没人说过话。"
        return f"游戏公屏最近 {min(n, len(lines))} 条：\n" + render.describe_chat(lines, n)

    @filter.llm_tool(name="mc_aim")
    async def mc_aim(self, event: AstrMessageEvent, x: float = 0, y: float = 0,
                     z: float = 0, release: bool = False):
        """LOCK YOUR LINE OF SIGHT onto something -- keep looking at it even while
        you are walking.
        
        This is the missing half of "walk over there WHILE WATCHING that chest": previously, as soon
        as you walked, Baritone twisted your head back to the direction of travel, so you could
        never walk and stare at something at the same time.
        
        - mc_aim(x, y, z) -- stare at those coordinates (RECOMPUTED EVERY TICK, so it stays on
          that thing no matter how far you walk)
        - mc_aim(release=True) -- release it and give your view back to the walking logic
          (REMEMBER to release when you are done)
        
        WARNING: when to release -- as soon as you have seen what you came for. If you leave it
        locked, you will no longer naturally face your direction of travel while walking; you will
        walk sideways, which looks very odd to other players.
        
        WARNING: how it differs from mc_lookat (the crosshair):
        - mc_lookat = ASK "what am I looking at" (read-only, instantaneous)
        - mc_aim = COMMAND "keep looking there from now on" (stays in effect until released)
        
        Args:
            x(number): Target X.
            y(number): Target Y.
            z(number): Target Z.
            release(bool): true = release the lock (then x/y/z are not needed)."""
        if (deny := self._guard(event)):
            return deny
        if release:
            _, err = await self._call("mcb releaseAim")
            if err:
                return render.reword(err, "松不开朝向", kind=render.Kind.BRIDGE,
                                    hint="过几秒重试")
            return "松开视线了 —— 现在走路时会自然朝向行进方向。"
        try:
            tx, ty, tz = int(float(x)), int(float(y)), int(float(z))
        except (TypeError, ValueError):
            return "坐标不合法 —— 要么给三个数，要么 release=true。"
        _, err = await self._call(f"mcb aimAt {tx} {ty} {tz}")
        if err:
                return render.reword(err, "锁不住朝向", kind=render.Kind.BRIDGE,
                                    hint="先用 mc_state 确认她在线")
        return (f"视线锁在 ({tx}, {ty}, {tz}) 了 —— 接下来**就算走路也会一直看着那儿**，"
                "而且是每 tick 重算，不会因为走远就偏。看完记得 `mc_aim(release=true)` 松开。")

    @filter.llm_tool(name="mc_screenshot")
    async def mc_screenshot(self, event: AstrMessageEvent, question: str = ""):
        """TAKE A LOOK AROUND YOU -- capture your game screen and have it
        described to you in words.
        
        Normally you can only "read data" and cannot see the picture. This tool gives you EYES.
        WARNING: for "what am I looking at" (the block / mob under the crosshair) use mc_lookat
        instead -- that one is instant and free; this one captures a screenshot and calls a vision
        API, so it is much slower.
        
        When to use it:
        - You want to know "what does this place look like", "is this a nice view" (SCENERY and
          overall impression -- something mc_lookat cannot give you)
        - The data does not tell you enough (mc_state only has coordinates, no scenery)
        - You want to confirm something worked (did the thing end up in the right place)
        
        WARNING: three things to know:
        1. SLOW -- capture + transfer + vision, several seconds to tens of seconds
        2. BLURRY -- your screen is only 640x360; you can make out outlines and colors, not small text
        3. It only shows what you are FACING, and distance fades into fog -- first-person view, you
           cannot see behind you; your render distance is only 32 blocks, beyond that is fog.
           To look somewhere else, walk or turn first, then call it again.
        
        Args:
            question(string): What you want to know (optional), e.g. "what block is in front of me",
                "are the crops ready". Leave it out for "describe what you see"."""
        if (deny := self._guard(event)):
            return deny
        if not self._cfg("enable_sight", True):
            return "看东西的功能被管理员关掉了。"
        return await self.sight.look(str(question or ""))

    @filter.llm_tool(name="mc_lookat")
    async def mc_lookat(self, event: AstrMessageEvent):
        """WHAT ARE YOU LOOKING AT RIGHT NOW -- the single thing under your
        crosshair, answered instantly.
        
        It reports the NEAREST thing at the end of your line of sight: a mob or dropped item if
        there is one, otherwise the block you are facing (including which face).
        It also returns your FACING (facing: south / northwest ...), which you can use when
        talking about directions.
        
        WARNING: how it differs from mc_screenshot:
        - "what is this block", "am I looking at a chicken or a stone" -> THIS (instant, free)
        - "what does this place look like", "what is the environment like" -> mc_screenshot
          (slow, calls an API)
        
        WARNING: your reach is only about 4.5 blocks. target=none means you are looking at air or
        at the sky -- to see something far away, mc_goto closer first, or use mc_screenshot for
        the wide view."""
        if (deny := self._guard(event)):
            return deny
        data, err = await self._call("mcb lookat")
        if err:
            return render.reword(err, "看不了准星", kind=render.Kind.BRIDGE,
                                hint="过几秒重试")
        return render.describe_look(data, self.places)

    # ---- 调试入口 -------------------------------------------------------

    @filter.command("mcbody")
    async def cmd_mcbody(self, event: AstrMessageEvent):
        """不带 LLM 的连通性自检：/mcbody 直接打一次 RCON。"""
        if (deny := self._guard(event)):
            yield event.plain_result(deny)
            return
        data, err = await self._call("mcb diag")
        if err:
            logger.warning(f"[{PLUGIN_NAME}] /mcbody 自检失败: {err}")
            yield event.plain_result(f"桥不通：{err}")
            return
        yield event.plain_result(f"桥是通的。自检：{data}")
