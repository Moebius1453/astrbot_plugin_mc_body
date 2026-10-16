r"""动作下发不等于完成；授权默认拒绝。"""

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
from .mcb.hunt import HuntGuard
from .mcb.idle import IdleGuard
from .mcb.journal import Journal
from .mcb import permission
from .mcb import protocol
from .mcb.places import PlaceBook
from .mcb import ripeness
from .mcb.ripeness import RipenessDesk
from .mcb.reflex import ReflexGuard
from .mcb import reflexes
from .mcb.sight import Sight
from .mcb.smelt import Smelter
from .mcb.tasks import TaskRunner
from .mcb.uplink import ChatUplink
from .mcb import render

PLUGIN_NAME = "mc_body"

# 每次自主唤醒都会运行完整 agent，产生模型费用。
SELF_LOOP_JOB = "mc_body_self_loop"

SELF_LOOP_NOTE = (
    "[mc:alert] 自主心跳：现在没人叫你。看一眼自己现在的处境"
    "（mc_state 看身体、mc_around 看周围），想接着做什么就去做；"
    "**没事可做就什么都别做，也别发消息** —— 沉默是允许的，不用每次都汇报。"
)

COORD_LIMIT = 30_000_000
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
MAX_SAY_LEN = 200
MAX_RAW_LEN = 200

# Baritone 原始命令只允许白名单。
BARITONE_VERBS = frozenset({
    "goto", "follow", "come", "thisway", "explore", "path", "surface",
    "mine", "farm",
    "stop", "cancel", "pause", "resume", "proc", "axis", "version", "help",
})

# computer_use_runtime=none 时框架文件工具不可用，由 mc_skill 读取正文。
# 不开 local 文件工具，避免为读技能开放整个文件系统。
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_SKILL_CHARS = 24000   


@register(
    "astrbot_plugin_mc_body",
    "Moebius1453",
    "需要独立 Minecraft 客户端，通过配套桥接实现聊天、感知、移动、交互和合成。",
    "0.40.0",
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
            require_wake_keyword=bool(self._cfg("require_wake_keyword", True)),
            ambient_limit=int(self._cfg("ambient_context_lines", 20)),
            self_name=str(self._cfg("character_name", "Nanako")),
            state_path=Path(__file__).resolve().parent / "data" / "uplink_state.json",
        )
        # 日志仅存内存，重载丢失。
        self.journal = Journal(capacity=int(self._cfg("journal_capacity", 400)))

        # 地点簿持久化，避免重载丢失地图。
        places_path = str(self._cfg("places_file", "") or "").strip()
        if not places_path:
            places_path = str(Path(__file__).resolve().parent / "data" / "places.json")
        self.places = PlaceBook(places_path)

        # 图片只交视觉 provider，主会话只收转述文字。
        self.sight = Sight(
            ssh_host=str(self._cfg("client_ssh_host", "")),
            container=str(self._cfg("client_container", "mc-brain")),
            sudo_password=str(self._cfg("client_sudo_password", "")),
            context=context,
            provider_id=str(self._cfg("vision_provider_id", "")),
            vision_prompt=str(self._cfg("vision_prompt", "")),
        )

        self.io = ContainerIO(self.bridge)
        self.tasks = TaskRunner(self.bridge, self.journal, io=self.io)

        # 所有 walk 写入必须经过仲裁，避免互相覆盖路线。
        self.arbiter = Arbiter(self._call)
        self.tasks.bind_arbiter(self.arbiter)

        self.events = EventFeed(self.bridge, self.journal,
                                on_permission=self._permission_denied,
                                on_event=self._on_world_event,
                                on_tick=self._maybe_wake_by_events)

        self.ripeness = RipenessDesk(
            level=int(self._cfg("event_wake_level", 3)),
            owner_name=str(self._cfg("owner_player_name", "") or ""),
            char_name=str(self._cfg("character_name", "Nanako")),
            hurt_hp_line=float(self._cfg("owner_hurt_hp_line", 8)),
            cooldown=float(self._cfg("event_wake_cooldown_seconds", 60)),
        )

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
            # 默认值必须与 _conf_schema.json 一致。
            flee_toward=str(self._cfg("retreat_toward", "auto")),
            combat_interval=float(self._cfg("reflex_combat_interval_seconds", 0.25)),
            notify=self._notify,
            journal=self.journal,
        )
        # 保命抢占只挂起任务，不取消。
        self.reflex.bind_tasks(self.tasks)
        self.reflex.bind_arbiter(self.arbiter)

        # 闲逛复用反射节拍和状态，不另起循环。
        self.idle = IdleGuard(
            self.bridge,
            self.arbiter,
            enabled=bool(self._cfg("enable_idle_wander", True)),
            after_seconds=float(self._cfg("idle_wander_after_seconds", 25)),
            radius=float(self._cfg("idle_wander_radius", 16)),
            owner_name=str(self._cfg("owner_player_name", "") or ""),
            journal=self.journal,
        )
        self.reflex.bind_idle(self.idle)

        # 主动追怪走 LEVEL_COMBAT，不能压过用户指令。
        self.hunt = HuntGuard(
            self.bridge,
            self.arbiter,
            enabled=bool(self._cfg("enable_hunt", True)),
            journal=self.journal,
        )
        self.reflex.bind_hunt(self.hunt)
        self.uplink.on_activity = self._on_user_activity
        self._last_notify_text = ""
        self._last_notify_at = 0.0
        self._llm_calls = 0
        # 聊天已读与唤醒游标独立，避免互相吞消息。
        self._chat_seen_seq = 0

    async def _notify(self, facts: str) -> None:
        r"""只通知需用户介入的事实；自动反射只记日志。"""
        umo = str(self._cfg("white_session", "") or "")
        if not umo:
            return

        now = time.monotonic()
        if facts == self._last_notify_text and (now - self._last_notify_at) < 60:
            logger.info(f"[mc_body] 通知去重（60 秒内已发过同样的）：{facts}")
            return
        self._last_notify_text = facts
        self._last_notify_at = now

        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain

        # Minecraft 数据必须带 [mc:*]，防止混入现实处境。
        await self.context.send_message(umo, MessageChain([Plain(f"[mc:alert] {facts}")]))

    async def _held_stack(self) -> tuple[str | None, int | None]:
        r"""注册名用于权限；空手为 (None, 0)，未知为 (None, None)。"""
        data, err = await self._call("mcb inventory")
        if err or not isinstance(data, dict):
            return None, None
        sel = data.get("held")
        if not isinstance(sel, (int, float)) or isinstance(sel, bool):
            return None, None
        idx = int(sel)
        hotbar = data.get("hotbar") or []
        if not 0 <= idx < len(hotbar):
            return None, None
        it = hotbar[idx]
        if not isinstance(it, dict):
            # 空槽位是 null，不是缺字段。
            return None, 0
        item_id = str(it.get("id") or "") or None
        try:
            return item_id, int(it.get("c"))
        except (TypeError, ValueError):
            return item_id, None

    async def _held_count(self) -> int | None:
        r"""held 是快捷栏下标；未知数量返回 None，不冒充 0。"""
        return (await self._held_stack())[1]

    async def _menu_snapshot(self) -> dict | None:
        r"""格号依赖菜单身份；过期快照不可复用。"""
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


    def _on_user_activity(self) -> None:
        r"""重置闲逛安静计时和熟度冷却。"""
        self.idle.note_activity()
        self.ripeness.note_turn()

    def _on_world_event(self, raw: dict) -> None:
        r"""同步回调，不可由调用方 await。"""
        if not self._cfg("enable_event_wake", True):
            return
        kind = str(raw.get("kind") or "")
        if not kind:
            return
        who = str(raw.get("who") or "")
        # hurt/death 的 victim 是受害者，who 是攻击者。
        victim = str(raw.get("victim") or "")
        if kind in ("join", "leave"):
            victim = victim or who
        if not victim:
            return
        self.ripeness.add(kind, victim, who, str(raw.get("text") or ""),
                          hp=raw.get("hp"))

    async def _maybe_wake_by_events(self) -> None:
        r"""复用事件轮询节拍，不另起循环。"""
        if not self._cfg("enable_event_wake", True):
            return
        # 主人在线不代表身体在线；身体离线时不唤醒。
        if not self.reflex.online:
            return
        why = self.ripeness.why()
        if why is None:
            return
        entries = self.ripeness.take()
        if not entries:
            return
        prompt = ripeness.render_prompt(entries, self.ripeness.clear_dropped())
        logger.info(
            f"[{PLUGIN_NAME}] 熟度到了（{why}）：自己醒一轮，{len(entries)} 条事件"
        )
        self.journal.add("ripeness", f"自己醒了（{why}）：{entries[-1].text}")
        try:
            await self.uplink.wake_with_events(prompt)
        except Exception as exc:  # noqa: BLE001 - 叫醒失败不该炸掉事件流那条循环
            logger.warning(f"[{PLUGIN_NAME}] 事件唤醒失败：{exc}")

    async def _claim_walk_user(self, cmd: str, note: str) -> tuple[dict, str | None]:
        r"""用户路线必须经仲裁，不能直接覆盖任务或反射。"""
        await self.arbiter.claim_walk(LEVEL_USER, "llm", cmd, note)
        return {"ok": self.arbiter.last_error is None}, self.arbiter.last_error

    async def _release_walk_user(self) -> None:
        await self.arbiter.release_walk("llm")


    @filter.on_llm_request(priority=999)
    async def sanitize_wire(self, event: AstrMessageEvent, req) -> None:
        r"""出站净化独立于状态包开关，排在其他请求钩子之后；仅处理当前视图，不覆盖框架后续截断。"""
        try:
            issues = protocol.audit(getattr(req, "contexts", None))
            if issues:
                logger.warning(
                    f"[{PLUGIN_NAME}] 出站历史有 {len(issues)} 处不合法（净化前）：{issues[:3]}"
                )
            before = getattr(req, "contexts", None)
            cleaned = protocol.for_wire(before)
            if isinstance(before, list) and len(cleaned) != len(before):
                logger.info(f"[{PLUGIN_NAME}] 出站净化：{len(before)} -> {len(cleaned)} 条")
            req.contexts = cleaned
        except Exception as exc:  # noqa: BLE001 - 净化失败不该让这一轮发不出去
            logger.warning(f"[{PLUGIN_NAME}] 出站净化失败（这一轮按原样发）：{exc}")

    @filter.on_llm_request(priority=100)
    async def inject_body_state(self, event: AstrMessageEvent, req) -> None:
        r"""按配置轮次在用户消息侧注入状态数据，避免动态状态破坏 system_prompt 缓存。来源先例：astrbot_plugin_dafeiyu_pet 的状态注入。"""
        if not self._cfg("enable_body_state", True):
            return
        # 新聊天不受状态包 every 节流，避免延迟对话。
        chat_lines, has_new_chat = await self._pull_chat()

        self._llm_calls += 1
        every = max(1, int(self._cfg("body_state_every", 3)))
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
            packet = render.state_packet(
                data, self.reflex.stance, self.tasks.status(), self.journal, self.places,
                chat_lines if has_new_chat else None,
                wiring=self._cfg("enable_wiring_note", True),
                instincts=reflexes.overview(),
                hold=render.describe_hold(data, self.uplink.hold_reason,
                                          self.uplink.hold_detail),
                hunt=self.hunt.render(),
                wander=self.idle.render(),
            )
            packet += "\n" + self.uplink.render()
            from astrbot.core.agent.message import TextPart
            req.extra_user_content_parts.append(TextPart(text=packet))
        except Exception as exc:  # noqa: BLE001 - 注入失败绝不能让她这条消息也发不出去
            logger.warning(f"[{PLUGIN_NAME}] 状态注入失败（不影响对话）：{exc}")

    async def _pull_chat(self) -> tuple[list[dict], bool]:
        r"""独立已读游标，避免与唤醒游标互相吞消息。"""
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

        if self._cfg("enable_reflex", True):
            self.events.start()

        if self._cfg("enable_self_loop", False):
            await self._start_self_loop()
        else:
            logger.info(
                f"[{PLUGIN_NAME}] 自主心跳已关闭（enable_self_loop=false）—— "
                "她自己那个 future_task 工具仍然可用"
            )

        # 启动时读取主人在线状态，之后由 join/leave 更新。
        owner = str(self._cfg("owner_player_name", "") or "").strip()
        if owner and self._cfg("enable_event_wake", True):
            data, err = await self._call(f"mcb where {owner}")
            online = bool(not err and isinstance(data, dict) and data.get("online"))
            self.ripeness.note_owner(online)
            logger.info(
                f"[{PLUGIN_NAME}] 熟度总闸初值：主人 {owner} "
                f"{'在线' if online else '不在线（只攒不醒）'}"
            )

        logger.info(
            f"[{PLUGIN_NAME}] 已加载，RCON 目标 {self.bridge.host}:{self.bridge.port}"
        )

    async def terminate(self) -> None:
        # 在途轮收尾前保留身体组件，避免工具访问半卸载状态。
        await self.events.stop()
        await self.uplink.stop()
        await self._drop_self_loop()
        await self.tasks.stop(quiet=True)
        await self.reflex.stop()
        await self.bridge.close()
        logger.info(f"[{PLUGIN_NAME}] 已卸载，RCON 连接已关闭")

    # 自主心跳每次运行完整 agent，默认关闭。

    async def _drop_self_loop(self) -> None:
        r"""加载与卸载均清理同名任务，避免重载叠加。"""
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
        await self._drop_self_loop()          # 重建前删除同名任务，防止重载叠加。
        try:
            await mgr.add_active_job(
                name=SELF_LOOP_JOB,
                cron_expression=f"*/{minutes} * * * *",
                payload={"session": umo, "note": SELF_LOOP_NOTE, "origin": "plugin"},
                description="自主心跳：没人叫她的时候自己醒来看看该做什么",
                # 不持久化定时任务，避免卸载留下孤儿。
                persistent=False,
            )
            logger.info(f"[{PLUGIN_NAME}] 自主心跳已开：每 {minutes} 分钟唤醒一次")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 自主心跳注册失败：{exc}")


    def _cfg(self, key: str, default):
        value = self.config.get(key) if hasattr(self.config, "get") else None
        return default if value is None else value

    def _allowed_ids(self) -> set[str]:
        raw = self._cfg("allowed_sender_ids", []) or []
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        return {str(item).strip() for item in raw if str(item).strip()}

    def _wake_keywords(self) -> list[str]:
        raw = self._cfg("wake_keywords", ["Nanako", "白"]) or []
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        return [str(item).strip() for item in raw if str(item).strip()]

    def _authorize(self, event: AstrMessageEvent) -> str | None:
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

    def _guard(self, event: AstrMessageEvent, *, observe: bool = False) -> str | None:
        deny = self._authorize(event)
        if deny:
            return deny
        if not observe and getattr(event, "_mcb_craft_failed", False):
            return "本轮合成已失败，后续动作已锁定；只可查询或停止，等待用户下一条指令，不自行走近或重试。"
        return None

    async def _halt_failed_craft(self, event: AstrMessageEvent) -> None:
        if event is not None:
            event._mcb_craft_failed = True
        await self.tasks.stop(quiet=True)
        await self.arbiter.clear_user_and_task()

    # 发送者授权与动作权限是两道独立门。

    def _owner_rules(self) -> permission.RuleSet:
        r"""缺失可忽略；错误规则必须抛出，不能静默失效。"""
        path = str(self._cfg("permission_rules_file", "") or "").strip()
        if not path:
            return permission.EMPTY
        try:
            return permission.load_owner_rules(path)
        except permission.RuleError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 主人权限规则写错了，这一层按空处理：{exc}")
            return permission.EMPTY
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 主人权限规则读不动，这一层按空处理：{exc}")
            return permission.EMPTY

    async def _permit(
        self,
        action: permission.Action,
        facts: permission.Facts | None = None,
    ) -> tuple[permission.Verdict, permission.Facts]:
        r"""缺失事实按 ask，不按 allow。"""
        mode = permission.Mode.by_name(self._cfg("permission_mode", permission.Mode.ASK))
        if mode == permission.Mode.BYPASS:
            return permission.ALLOWED, facts if facts is not None else permission.Facts()
        judge = permission.Judge(mode=mode, owner=self._owner_rules())
        if facts is None:
            facts = permission.Facts(pos=action.pos)
            if action.pos is not None:
                tx, ty, tz = action.pos
                data, err = await self._call(f"mcb placed {tx} {ty} {tz}")
                if err or not isinstance(data, dict):
                    return (
                        permission.Verdict(
                            permission.VerdictKind.ASK, "读不到这一格的放置记录"
                        ),
                        facts,
                    )
                facts = permission.Facts.from_placed_reply(data)
        return judge.decide(action, facts), facts

    async def _permit_or_refuse(
        self,
        action: permission.Action,
        what: str,
        facts: permission.Facts | None = None,
    ) -> str | None:
        r"""ask 暂降级为停止并通知，无答复回环。"""
        verdict, facts = await self._permit(action, facts)
        if verdict.allowed:
            return None
        if verdict.asks:
            await self._say_in_game("等一下，这个我拿不准，先问问主人再动。")
            where = ""
            if facts.pos is not None:
                where = f"（在 {facts.pos[0]},{facts.pos[1]},{facts.pos[2]} 那一格"
                where += f"，{facts.block}）" if facts.block else "）"
            return render.fail(
                render.Kind.DENIED,
                f"{what}被权限层拦下了",
                detail=verdict.reason() + where,
                usage="停下来告诉主人你想做什么，等他明确说可以做；别换个坐标或换种说法重试同一件事",
                hint="这是保护主人东西的规则，不是你做错了——但没有许可之前不要绕过它",
            )
        return render.fail(
            render.Kind.DENIED,
            f"{what}被权限层拒绝了",
            detail=verdict.reason(),
            usage="别再试这一下；把它当成「这个动作本身不被允许」",
            hint="如果确实需要，让主人在插件配置里改 permission_mode 或他那一层规则",
        )

    async def _say_in_game(self, text: str) -> None:
        try:
            await self._call(f"mcb say {text}")
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[{PLUGIN_NAME}] 公屏说一句失败（可忽略）：{exc}")


    async def _call(self, command: str) -> tuple[dict | None, str | None]:
        r"""data 与错误恰有一个非 None。"""
        try:
            reply = await self.bridge.call(command)
        except BridgeError as exc:
            return None, f"没送到桥上：{exc}"
        if not reply.get("ok"):
            return None, str(reply.get("error") or "桥报了失败但没说原因")
        data = reply.get("data")
        return (data if isinstance(data, dict) else {}), None


    @staticmethod
    def _clean_text(raw: str, limit: int = MAX_SAY_LEN) -> str | None:
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


    @filter.llm_tool(name="mc_state")
    async def mc_state(self, event: AstrMessageEvent):
        """Your own state in MC: online / coords / health / hunger / dimension /
        what you are doing / what just happened.
        
        The result has both "what you are doing" (your task progress) and "pathing" (Baritone's
        walking state) -- do not confuse the two.
        After any ACTION tool, wait a moment and use this to confirm the result: those tools can
        only report "dispatched", never "it worked".
        "offline" means the character is not connected to the server -- no action is possible."""
        if (deny := self._guard(event, observe=True)):
            return deny
        data, err = await self._call("mcb state")
        if err:
            return render.reword(err, "查不到 Nanako 的状态", kind=render.Kind.BRIDGE,
                                hint="过几秒重试一次；一直这样就说明桥断了（不是她不在线）")
        return render.describe_state(
            data, self.reflex.stance, self.tasks.status(), self.journal,
            hold=render.describe_hold(data, self.uplink.hold_reason, self.uplink.hold_detail),
        ) + "\n" + self.uplink.render()

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
                                hint="结果未知时先查公屏记录，不要自动重发")
        return f"客户端已执行 Nanako 的聊天发送：{clean}"

    @filter.llm_tool(name="mc_goto")
    async def mc_goto(self, event: AstrMessageEvent, near: str = "", x: float = 0, z: float = 0):
        """Walk somewhere. ASYNC -- it dispatches and she walks on her own;
        check coordinates later with mc_state.
        
        Three ways to specify; give just one:
        - near="crafting_table" / near="minecraft:furnace" -- walk next to that thing.
          Looks in your PLACE BOOK first (names you recorded), then treats it as a block id and
          scans nearby. This is the easiest form -- no coordinates needed.
        - x + z -- walk to exact coordinates (only when you read F3 or someone told you).
        
        WARNING: searching by block name only covers a SMALL patch around her -- the server
        caps the scan to about 11 blocks, not the whole area. If it is further away it will not
        be found -- walk closer first, or record it in the place book (mc_place) and then use
        near="name". NOT FINDING IT DOES NOT MEAN IT IS NOT THERE.
        
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
            hit = self.places.match(want)
            if hit is not None:
                name, place = hit
                tx, tz = int(float(place["x"])), int(float(place["z"]))
                
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
        
        Your scan only covers about 11 blocks around you (the server caps it); outside that you
        are blind. Record the important places, and mc_goto near="name" will take you there
        without anyone reporting coordinates.
        
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
            # 指纹读 y-1；玩家所在格通常是空气。
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
        r"""未知返回 None，空串会被当成方块消失。"""
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
        r"""每地点一次 RCON，限量且不缓存。"""
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

        WARNING: following does NOT stop on its own -- she keeps following until something
        takes over the walking. What DOES take over: any other walking instruction (mc_goto,
        mc_baritone_raw, a task that walks) or mc_stop. What does NOT: tools that do not walk
        (mc_craft, mc_use_on, mc_say, ...). So if she is following and you want her to go do
        something, give her a walking instruction or mc_stop first -- do not assume the follow
        will time out.

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
        if (deny := self._guard(event, observe=True)):
            return deny
        stopped = await self.tasks.stop()
        # 只撤用户和任务声明，不能取消保命路线。
        await self.arbiter.clear_user_and_task()
        err = self.arbiter.last_error
        # 同时释放使用键，防止异常后持续使用。
        await self._call("mcb release")
        if err:
            return render.reword(err, "没能让她停下", kind=render.Kind.BRIDGE,
                                hint="过几秒再用 mc_state 看她还在不在走")
        return f"已下发停止指令，她应该会停下来（惯性可能还会滑一小段）。{stopped}"

    @filter.llm_tool(name="mc_baritone_raw")
    async def mc_baritone_raw(self, event: AstrMessageEvent, command: str):
        """Escape hatch: send a raw Baritone command. Only use it when the
        fixed tools cannot do the job.
        
        Common ones: ``mine <block_id>``, explore, farm, goto 100 64 -200
        (goto with Y), come, thisway 100.

        NEVER guess a block id from memory. If you were asked to chop or mine
        something you can see, call mc_lookat FIRST to read which block your
        crosshair is on, then pass THAT id to mine. A guessed id sends you off
        hunting for a block that is not the one in front of you.

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
        verb = clean.split()[0].lower()
        if verb not in BARITONE_VERBS:
            return (
                f"error: 不允许「{verb}」这个 Baritone 动词 —— 它可能有能力一次改掉一大片方块。\n"
                f"usage: 只允许 {' / '.join(sorted(BARITONE_VERBS))}\n"
                "hint: 挖/砍东西用 `mine <方块注册名>` —— 方块名不许凭印象写，"
                "先 `mc_lookat` 看你准星指着的是什么方块，用它报的 id（去掉 `minecraft:` 前缀）；"
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


    @filter.llm_tool(name="mc_inventory")
    async def mc_inventory(self, event: AstrMessageEvent):
        """Look at your inventory: what you have, which hotbar slot is in hand,
        and your hunger. Check it before taking / eating / crafting anything."""
        if (deny := self._guard(event, observe=True)):
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
        # 动作前后比数量，不能把下发当成放置成功。
        before_item, before = await self._held_stack()
        # 动作前读取手持物，危险物品不能绕过权限门。
        what = f"对着 ({tx},{ty},{tz}) 放置"
        if (refusal := await self._permit_or_refuse(
            permission.Action(permission.Kind.PLACE, pos=(tx, ty, tz), item=before_item),
            what,
        )):
            return refusal
        # 直接构造坐标命中，避免准星射线 MISS。
        _, err = await self._call(f"mcb useOnAt {tx} {ty} {tz}")
        if err:
            return render.reword(
                err, f"对着 ({tx},{ty},{tz}) 右键没成功",
                kind=render.Kind.OUT_OF_REACH,
                hint=f"先 `mc_goto x={tx} z={tz}` 走到 4 格以内，再重试这条",
            )
        if keep_open:
            await asyncio.sleep(0.6)   
            data, merr = await self._call("mcb state")
            if merr:
                return render.reword(merr, f"已对着 ({tx},{ty},{tz}) 右键，但读不到界面",
                                     kind=render.Kind.BRIDGE, hint="过几秒再用 mc_menu 看一次")
            return "已对着 ({tx},{ty},{tz}) 右键，界面留着没关。\n" + render.describe_menu(data)
        # 不用界面时关闭，否则无法移动。
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
    async def mc_click(self, event: AstrMessageEvent, slot: int, mode: int = 1,
                       button: int = 0):
        """Click slot number N of the currently open screen -- this is how you take
        things out of a chest and how crafting happens. Use mc_menu first to see the slot numbers.
        
        - mode=1 (default) shift quick-move: chest slot -> backpack, or CRAFT RESULT -> craft immediately
        - mode=0 plain left click (pick up / put down); mode=6 double-click to gather the same kind
        - mode=4 drops items on the ground: button=0 drops ONE, button=1 drops the WHOLE STACK.
          Empty the cursor first. Use only your own inventory slots; never drop container contents.
          Dropping is not delivery to another player. Do not retry an uncertain result automatically.

        Args:
            slot(number): Slot number (look it up with mc_menu; NOT a backpack slot).
            mode(number): 1=shift quick-move (default); 0=plain click; 4=drop; 6=gather.
            button(number): For mode=4 only: 0=one item (default), 1=whole stack.
                Other modes require 0."""
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
        try:
            b = int(button)
        except (TypeError, ValueError):
            b = -1
        if b not in (0, 1) or (m != 4 and b != 0):
            return render.fail(render.Kind.BAD_ARGUMENT, "button 参数不合法",
                               usage="mode=4 时 0=丢一个，1=丢整叠；其他模式只能为 0")
        before = await self._menu_snapshot()
        if (not isinstance(before, dict) or not isinstance(before.get("generation"), int)
                or not isinstance(before.get("sample"), int)):
            return render.fail(render.Kind.REFUSED, "缺少菜单保护信息，未执行点击",
                               hint="需要配套客户端桥；更新并重启客户端后重新查看 mc_menu")
        click_guard = {"id": before.get("id"), "generation": before["generation"]}
        if m == 4:
            roles = str(before.get("roles") or "")
            if not (0 <= n < len(roles) and roles[n] == "Y"):
                return render.fail(render.Kind.REFUSED, "只能丢弃自己的库存物品",
                                   hint="先用 mc_menu 确认自己的库存槽，不要丢容器或合成槽物品")
            if before.get("cursorEmpty") is not True:
                return render.fail(render.Kind.REFUSED, "光标非空或状态未知，未执行丢物",
                                   hint="先把光标物品放回自己的库存，再查看 mc_menu")
            source = next((it for it in before.get("items", [])
                           if isinstance(it, dict) and it.get("i") == n), None)
            if not isinstance(source, dict) or not source.get("id") or not source.get("c"):
                return render.fail(render.Kind.REFUSED, "目标槽为空或未读到物品，未执行")
            click_guard.update(item=source["id"], count=source["c"])
            guard_arg = json.dumps(click_guard, ensure_ascii=True, separators=(",", ":"))
            _, err = await self._call(f"mcb clickSlot {n} {b} 4 {guard_arg}")
            if err:
                return render.reword(err, "丢物结果未知，不要自动重试",
                                     kind=render.Kind.BRIDGE, hint="先检查背包和地面物品")
            return (f"已下发第 {n} 格的丢物操作（{'整叠' if b else '一个'}）。"
                    "尚未确认客户端执行、库存减少或地面掉落；不要报告已交付，也不要自动重发。"
                    "请用 mc_inventory 检查数量，并核实地面物品；角色可能重新捡起。")
        # 读取手持物失败时按空手判，保守拦截取物。
        _, held_n = await self._held_stack()
        hand_empty = True if held_n is None else (held_n == 0)
        if mcb_containers.takes_from_container(before, n, m, hand_empty):
            refusal = await self._permit_or_refuse(
                permission.Action(permission.Kind.TAKE),
                f"从容器第 {n} 格拿东西",
                facts=permission.Facts(),
            )
            if refusal:
                return refusal
        guard_arg = json.dumps(click_guard, ensure_ascii=True, separators=(",", ":"))
        _, err = await self._call(f"mcb clickSlot {n} 0 {m} {guard_arg}")
        if err:
            return render.reword(err, "点击结果未知，不要自动重试", kind=render.Kind.BRIDGE,
                                hint="先检查当前菜单和库存")
        after = None
        for _ in range(5):
            await asyncio.sleep(0.3)
            candidate = await self._menu_snapshot()
            if isinstance(candidate, dict) and (
                candidate.get("generation") != before["generation"]
                or (isinstance(candidate.get("sample"), int)
                    and candidate["sample"] > before["sample"])
            ):
                after = candidate
                break
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
        runner = CraftRunner(self.bridge, arbiter=self.arbiter)
        try:
            plan = await crafter.plan(name, n)
        except BridgeError as exc:
            await self._halt_failed_craft(event)
            return f"算配方的时候没连上桥：{exc}"
        if not plan.ok:
            await self._halt_failed_craft(event)
            self.journal.add("craft", f"做 {name}×{n} 做不了：{plan.describe().splitlines()[0]}")
            return "做不了：\n" + plan.describe()
        if not plan.steps:
            return f"不用做，她手上已经有 {name} 了。"
        self.journal.add("craft", f"开始做 {name}×{n}")
        try:
            result = await runner.run(plan)
        except BridgeError as exc:
            await self._halt_failed_craft(event)
            self.journal.add("error", f"做 {name} 时桥断了")
            return f"做的过程中桥断了：{exc}"
        except Exception:
            await self._halt_failed_craft(event)
            raise
        if not runner.completed:
            await self._halt_failed_craft(event)
        self.journal.add("craft", f"做 {name}×{n} —— {result.splitlines()[0]}")
        return result

    @filter.llm_tool(name="mc_smelt")
    async def mc_smelt(self, event: AstrMessageEvent, item: str):
        """SMELT something: put raw material into a furnace and get the product
        (raw iron -> iron ingot, sand -> glass, raw meat -> cooked meat).
        
        She picks the recipe herself (the same product often has several), walks over, loads
        material and fuel, WAITS for it to finish, and takes the result.
        WARNING: needs FUEL (coal / charcoal) and a furnace nearby -- the server caps the search
        to about 11 blocks, so a furnace "16 blocks away" may not be found. She loads the WHOLE
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
            return await Smelter(self.bridge, self.journal, arbiter=self.arbiter).smelt(str(item or ""))
        except BridgeError as exc:
            return f"烧的过程中桥断了：{exc}"

    @filter.llm_tool(name="mc_attack")
    async def mc_attack(self, event: AstrMessageEvent):
        """Attack whatever entity your CROSSHAIR is on (fight mobs, hit animals).

        It first checks what the crosshair points at. If that is not an entity, it tells you clearly.
        To aim at something first, use mc_aim(x, y, z) to lock your view onto it.
        (Usually you walk closer first with mc_goto / mc_follow to get the target into view.)

        WARNING: this goes through a safety gate. Hitting a player's pet (a tamed wolf, a
        villager, someone's horse) may be REFUSED and you will be told so -- that is the gate
        working, not a bug. Automatic counter-attacks when something hits you do NOT go through
        this tool and do not ask.
        WARNING: this hits ENTITIES only. To break blocks, use mc_baritone_raw with
        "mine <block_id>"."""
        if (deny := self._guard(event)):
            return deny
        # 自动反击走独立保命路径，不经过工具权限门。
        data, err = await self._call("mcb attackTarget")
        if err or not isinstance(data, dict):
            return render.reword(err, "看不准星指着什么，这一下没打出去",
                                 kind=render.Kind.BRIDGE,
                                 hint="过几秒再试；一直这样的话用 mc_threats 看附近有什么")
        if not data.get("hit"):
            why = data.get("err")
            why = "；".join(str(x) for x in why) if isinstance(why, list) else str(why or "")
            return render.fail(
                render.Kind.NOT_FOUND, "准星没指着任何实体，这一下没打出去",
                detail=why,
                usage="先用 mc_aim(x=…, y=…, z=…) 把视线锁到目标身上，或者 mc_goto 走近一点再试",
                hint="要打某只东西，得先让准星对着它；挖方块不走这个工具",
            )
        facts = permission.Facts.from_attack_reply(data)
        who = str(data.get("name") or data.get("type") or "那只")
        refusal = await self._permit_or_refuse(
            permission.Action(permission.Kind.ATTACK, entity=str(data.get("uuid") or "") or None),
            f"攻击 {who}",
            facts=facts,
        )
        if refusal:
            return refusal
        _, err = await self._call("mcb attack")
        if err:
            return render.reword(err, "攻击失败", kind=render.Kind.BRIDGE,
                                hint="先用 mc_state 看她在不在线、手上是不是武器")
        return f"已攻击 {who}。过一会儿用 mc_state 看血量/效果。"

    @filter.llm_tool(name="mc_threats")
    async def mc_threats(self, event: AstrMessageEvent):
        """See whether there are mobs nearby, how far, what kind, how much health,
        and whether they are aiming at you (the server reads the world directly, so this is accurate).

        Covers about 24 blocks around you -- "nothing found" only means nothing within that
        range, not that the world is empty.

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
        """Set your COMBAT STANCE. This decides whether you go LOOKING for mobs.

        - defend (default) passive: you never go after anything. You still fight back
          automatically the moment something hits you or aims at you (that part is a
          reflex, it never goes through you), so defend will not get you killed.
        - hunt aggressive: you actively SEEK OUT nearby hostile mobs -- you walk to them
          and attack. Only real monsters are targeted (never tamed animals/pets).
          You stop seeking when a mob is already in reach, or when your body cannot
          get there safely (underground, or a big height difference).

        WARNING: do NOT tell the user "I will go hunt mobs" unless you have switched to
        hunt first -- under defend you stay put and only react. Also, hunt is NOT a
        guarantee you will find anything: it searches the mobs your senses can see
        (about 24 blocks), not the whole world.

        WARNING: hunt loses to the user. The moment the user tells you to do something
        else, you drop the chase and do that instead -- that is by design, not a bug.

        Args:
            mode(string): Either "defend" (passive) or "hunt" (actively seek mobs)."""
        if (deny := self._guard(event)):
            return deny
        if not self._cfg("enable_mc_stance_tool", True):
            return "切换战斗姿态的功能被管理员关掉了。"
        try:
            actual = self.reflex.set_stance(str(mode))
        except ValueError as exc:
            return str(exc)
        if actual == "hunt":
            return ("战斗姿态已设为 hunt：我会在附近（约 24 格）主动找怪走过去打，"
                    "只打真的怪，不碰驯服的动物。已经打起来的那种照旧自动还手。"
                    "注意：你（用户）一开口我就放下追怪做你的事。")
        return ("战斗姿态已设为 defend：我不主动找怪，只在挨打或怪瞄着我时还手。")

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


    @filter.llm_tool(name="mc_task")
    async def mc_task(self, event: AstrMessageEvent, task: str, at: str = "",
                      rounds: int = 6, spacing: int = 8, direction: str = "east",
                      no_break: bool = False):
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
        
        WARNING: a field further away than the scan can reach (about 11 blocks -- the server
        caps it) is OUT OF RANGE and farm will come up empty -- use at to tell her where the
        field is.
        
        Args:
            task(string): The task name. An unknown name is rejected and the options are listed.
            at(string): Optional location for the task, format "x z" (e.g. "-50 120").
                For farming, use it to point at the field.
            rounds(int): Torch placement attempts, 1 to 32, default 6. Each attempt places at
                most one torch; blocked positions are skipped, so this is an upper limit.
            spacing(int): Blocks between torch attempts, 3 to 32, default 8.
            direction(string): Torch route: east, west, south or north; default east.
                These route options apply only to torch.
            no_break(bool): For torch only, forbid Baritone block breaking on this task's routes.
                Default false. Does not restrict survival reflexes or other tasks."""
        if (deny := self._guard(event)):
            return deny
        if not self._cfg("enable_mc_task", True):
            return "派任务的功能被管理员关掉了。"
        name = str(task or "").strip()
        if not name:
            return "要派哪件事？现在能派的有：\n" + render.describe_catalog(self.tasks.catalog())
        params: dict = {}
        if type(no_break) is not bool or (no_break and name != "torch"):
            return render.fail(render.Kind.BAD_ARGUMENT, "不挖地参数不合法，未更换任务",
                               detail="no_break必须为布尔值，当前仅照明任务支持")
        if name == "torch":
            heading = str(direction or "").strip().lower()
            if (type(rounds) is not int or not 1 <= rounds <= 32
                    or type(spacing) is not int or not 3 <= spacing <= 32
                    or heading not in ("east", "west", "south", "north")):
                return render.fail(
                    render.Kind.BAD_ARGUMENT, "照明参数不合法，未启动或更换任务",
                    detail="rounds 必须是 1–32 的整数，spacing 必须是 3–32 的整数，"
                           "direction 必须是 east/west/south/north",
                )
            params.update(rounds=rounds, spacing=spacing, dir=heading)
            if no_break:
                params["no_break"] = True
        # at 解析失败时任务退化为就地执行。
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
        if (deny := self._guard(event, observe=True)):
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
          WARNING: it does NOT say WHO gave it to you -- the log only records "you gained X",
          never a source. If you need to know who, ask the user; do not guess.
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
        r"""跨 tick 扫描需稍后取结果；失败不影响 T1。"""
        status, err = await self._call("mcb t2 status")
        if err or not isinstance(status, dict):
            return {"job": "error", "err": err or "拿不到状态"}
        job = status.get("job")

        # 扫描中心移动过远时重建缓存。
        if job == "done" and not force:
            tried = status.get("center") or []
            here = data.get("center") or []
            if len(tried) == 3 and len(here) == 3:
                d2 = sum((float(tried[i]) - float(here[i])) ** 2 for i in range(3))
                if d2 > 64 * 64:
                    job = "none"          

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
