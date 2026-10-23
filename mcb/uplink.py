r"""仅点名触发模型调用，其余聊天排队；回复按来源回游戏，自身发言不唤醒，避免自激。依赖 AstrBot 内部 API。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from pathlib import Path

from astrbot.api import logger

from . import protocol

MAX_GAME_SAY_LEN = 200

MAX_BACKOFF_SECONDS = 30.0

MAX_GAME_SAY_LEN = 120

_SENTENCE_END = "。！？!?…"

# 第一句太短时继续保留正文，避免只发语气词。
_SENTENCE_MIN = 8

MAX_FOLLOWUP_WAKES = 3


def provider_settings_to_build_kwargs(cfg: dict) -> dict:
    r"""字段须与 pipeline 映射对齐，否则静默回退构建默认值。"""
    ps = (cfg or {}).get("provider_settings") or {}
    file_extract = ps.get("file_extract") or {}
    proactive = ps.get("proactive_capability") or {}

    max_ctx = int(ps.get("max_context_length", 20))
    # 与 pipeline 的负数上下文边界保持一致。
    deq = min(max(1, int(ps.get("dequeue_context_length", 1))), max_ctx - 1)
    if deq <= 0:
        deq = 1

    return {
        "tool_call_timeout": int(ps.get("tool_call_timeout", 120)),
        "tool_schema_mode": str(ps.get("tool_schema_mode", "full")),
        "sanitize_context_by_modalities": bool(ps.get("sanitize_context_by_modalities", False)),
        "kb_agentic_mode": bool((cfg or {}).get("kb_agentic_mode", False)),
        "file_extract_enabled": bool(file_extract.get("enable", False)),
        "file_extract_prov": str(file_extract.get("provider", "moonshotai")),
        "file_extract_msh_api_key": str(file_extract.get("moonshotai_api_key", "")),
        "context_limit_reached_strategy": str(
            ps.get("context_limit_reached_strategy", "truncate_by_turns")
        ),
        "llm_compress_instruction": str(ps.get("llm_compress_instruction", "") or ""),
        "llm_compress_keep_recent_ratio": float(ps.get("llm_compress_keep_recent_ratio", 0.15)),
        "llm_compress_provider_id": str(ps.get("llm_compress_provider_id", "") or ""),
        "max_context_length": max_ctx,
        "dequeue_context_length": deq,
        "fallback_max_context_tokens": int(ps.get("fallback_max_context_tokens", 128000)),
        "llm_safety_mode": bool(ps.get("llm_safety_mode", True)),
        "safety_mode_strategy": str(ps.get("safety_mode_strategy", "system_prompt")),
        "computer_use_runtime": ps.get("computer_use_runtime"),
        "sandbox_cfg": ps.get("sandbox") or {},
        "add_cron_tools": bool(proactive.get("add_cron_tools", True)),
        "subagent_orchestrator": (cfg or {}).get("subagent_orchestrator") or {},
        "timezone": (cfg or {}).get("timezone"),
        "max_quoted_fallback_images": int(ps.get("max_quoted_fallback_images", 20)),
    }


class ChatUplink:

    def __init__(
        self,
        bridge,
        context,
        *,
        umo: str,
        poll_interval: float = 1.5,
        wake_keywords: list[str] | None = None,
        require_wake_keyword: bool = True,
        ambient_limit: int = 20,
        self_name: str = "Nanako",
        max_steps: int = 30,
        state_path: str | Path | None = None,
    ) -> None:
        self.bridge = bridge
        self.context = context
        self.umo = umo
        self.poll_interval = max(0.5, float(poll_interval))
        self.require_wake_keyword = require_wake_keyword
        self.wake_keywords = [str(k).strip() for k in (wake_keywords or []) if str(k).strip()]
        self.ambient_limit = max(0, int(ambient_limit))
        self.self_name = self_name
        self.max_steps = max(1, int(max_steps))

        self._task: asyncio.Task | None = None
        self._last_seq = 0
        self._ambient: list[str] = []       
        self._ambient_dropped = 0            
        self._ambient_trimmed = 0
        self._recent_self: list[str] = []   
        self._busy = asyncio.Lock()         
        self._pending: deque[tuple[str, str | None]] = deque()
        self._failed: list[tuple[str, str | None]] = []
        self._active = False
        self._retry_at = 0.0
        self._wake_task: asyncio.Task | None = None
        self._stopping = False
        self._state_path = Path(state_path) if state_path is not None else None
        self._load_state()
        self._fail_streak = 0
        self.hold_reason: str | None = None
        self.hold_detail = ""
        # 闲逛晚于 uplink 构造，回调需支持后绑定。
        self.on_activity = None

    def render(self) -> str:
        queued = len(self._pending) - int(self._active and bool(self._pending))
        text = (f"[mc:uplink] active={str(self._active).lower()} queued={queued} "
                f"failed={len(self._failed)}")
        if self._failed:
            text += " | 有失败或中断轮次待人工核对，未自动重试；不要据此重跑旧指令"
        return text

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        if (not isinstance(raw, dict) or raw.get("umo") != self.umo
                or type(raw.get("last_seq")) is not int or raw["last_seq"] < 0
                or type(raw.get("dropped")) is not int or raw["dropped"] < 0
                or not isinstance(raw.get("ambient"), list)
                or not all(isinstance(v, str) for v in raw["ambient"])
                or type(raw.get("active")) is not bool
                or not isinstance(raw.get("pending"), list)
                or not isinstance(raw.get("failed"), list)
                or not all(isinstance(v, list) and len(v) == 2
                           and isinstance(v[0], str)
                           and (v[1] is None or isinstance(v[1], str))
                           for v in raw["pending"] + raw["failed"])):
            raise ValueError("聊天消费状态类型或会话不匹配，拒绝覆盖")
        self._last_seq = raw["last_seq"]
        self._ambient = raw["ambient"]
        self._ambient_dropped = raw["dropped"]
        self._pending.extend((v[0], v[1]) for v in raw["pending"])
        self._failed = [(v[0], v[1]) for v in raw["failed"]]
        if raw["active"] and self._pending:
            self._failed.append(self._pending.popleft())
            logger.warning("[mc_body] 发现中断轮次，留存为待人工核对，不自动重跑工具")

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        state = {"umo": self.umo, "last_seq": self._last_seq,
                 "ambient": self._ambient, "dropped": self._ambient_dropped,
                 "pending": list(self._pending), "failed": self._failed,
                 "active": self._active}
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._state_path)


    def start(self) -> None:
        if self._task is not None:
            return
        if self.require_wake_keyword and not self.wake_keywords:
            logger.warning(
                "[mc_body] 未配置唤醒词 —— 游戏里说话永远不会唤醒白。"
                "请在插件配置的 wake_keywords 里填上（例如 Nanako、白）。"
            )
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="mc_body_uplink")
        logger.info(
            f"[mc_body] 聊天上行已启动：每 {self.poll_interval:g} 秒拉一次，"
            f"需要唤醒词 {self.require_wake_keyword}，唤醒词 {self.wake_keywords}，会话 {self.umo}"
        )

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._wake_task is not None:
            # 重载等待在途轮结束，避免重复有副作用的工具。
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._wake_task
        self._save_state()
        logger.info("[mc_body] 聊天上行已停止")


    async def _loop(self) -> None:
        while True:
            try:
                await self._poll_once()
                self._fail_streak = 0
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 隧道断/服务端忙 都走这里
                self._fail_streak += 1
                delay = min(
                    MAX_BACKOFF_SECONDS,
                    self.poll_interval * (2 ** min(self._fail_streak, 5)),
                )
                if self._fail_streak in (1, 5, 20):
                    logger.warning(
                        f"[mc_body] 聊天上行第 {self._fail_streak} 次失败"
                        f"（{exc}），退避 {delay:.0f} 秒"
                    )
                await asyncio.sleep(delay)

    async def _poll_once(self) -> None:
        reply = await self.bridge.call(f"mcb chat {self._last_seq}")
        if not reply.get("ok") or reply.get("action") != "chat":
            raise RuntimeError(reply.get("error") or "mcb chat 响应动作不匹配，未推进游标")

        data = reply.get("data") or {}
        lines = data.get("lines") or []
        max_seq = _as_int(data.get("max"))

        if max_seq < self._last_seq:
            logger.warning(f"[mc_body] 服务端聊天 seq 从 {self._last_seq} 退回 {max_seq}，重新取聊天")
            self._last_seq = 0
            self._save_state()
            self._kick_wakes()
            return

        wake_triggers: list[str] = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            seq = _as_int(line.get("seq"))
            if seq <= self._last_seq:
                continue
            self._last_seq = seq

            who = str(line.get("who") or "")
            text = str(line.get("text") or "").strip()
            if not text:
                continue

            if who == self.self_name:
                # 自身发言不得唤醒，防止自激。
                self._recent_self.append(text)
                _trim_head(self._recent_self, 2)
                continue

            head = _speaker_line(who, text, line)
            if self._is_wake(text):
                wake_triggers.append(head)
            else:
                # 点名单独排队，不能提前当旁听消费后重复执行。
                self._ambient.append(head)
                dropped = _trim_head(self._ambient, self.ambient_limit)
                self._ambient_dropped += dropped
                self._ambient_trimmed += dropped

        self._last_seq = max(self._last_seq, max_seq)
        if wake_triggers:
            if self.on_activity is not None:
                with contextlib.suppress(Exception):
                    self.on_activity()
            # 同页可合并唤醒，但不能只保留最后一条点名。
            self._pending.append(("\n".join(wake_triggers), None))
        self._save_state()
        self._kick_wakes()


    def _is_wake(self, text: str) -> bool:
        low = text.strip().lower()
        if not low:
            return False
        return not self.require_wake_keyword or any(kw.lower() in low for kw in self.wake_keywords)


    def _kick_wakes(self) -> None:
        if (self._stopping or not self._pending
                or asyncio.get_running_loop().time() < self._retry_at
                or (self._wake_task is not None and not self._wake_task.done())):
            return
        self._wake_task = asyncio.create_task(self._drain_wakes(), name="mc_body_wakes")
        self._wake_task.add_done_callback(self._wake_done)

    def _wake_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error(f"[mc_body] 唤醒队列执行失败，保留待处理项：{task.exception()}")
            self._retry_at = asyncio.get_running_loop().time() + MAX_BACKOFF_SECONDS

    async def _wake(self, trigger: str, prompt: str | None = None) -> None:
        self._pending.append((trigger, prompt))
        self._save_state()
        busy = self._busy.locked()
        self._kick_wakes()
        if not busy and self._wake_task is not None:
            await asyncio.shield(self._wake_task)

    async def _drain_wakes(self) -> None:
        async with self._busy:
            rounds = 0
            while self._pending and rounds < MAX_FOLLOWUP_WAKES and not self._stopping:
                if self._active:
                    self._failed.append(self._pending.popleft())
                    self._active = False
                    self._save_state()
                    logger.warning("[mc_body] 上轮结果未确认，留存待核对，不自动重跑")
                    continue
                current = self._pending[0]
                self._active = True
                self._save_state()
                if not await self._one_wake(current[0], current[1]):
                    # 工具可能已有副作用，失败不自动重跑整轮。
                    self._failed.append(self._pending.popleft())
                    self._active = False
                    self._save_state()
                    break
                self._pending.popleft()
                self._active = False
                self._save_state()
                rounds += 1
                if self._stopping:
                    break
            if self._pending:
                logger.info(f"[mc_body] 待处理唤醒剩余 {len(self._pending)} 项，后续轮询继续")

    async def wake_with_events(self, prompt: str) -> None:
        await self._wake("[mc:events]", prompt=prompt)

    async def _one_wake(self, trigger: str, prompt: str | None = None) -> bool:
        ambient_count = len(self._ambient)
        trimmed_before = self._ambient_trimmed
        dropped_before = self._ambient_dropped
        self.hold_reason = None
        self.hold_detail = ""
        if prompt is None:
            prompt = self._build_prompt(trigger)
        else:
            prompt = self._with_ambient(prompt)
        try:
            from astrbot.core.utils.session_lock import session_lock_manager

            self.hold_reason = "external"
            self.hold_detail = "waiting_for_session_lock"
            async with session_lock_manager.acquire_lock(self.umo):
                self.hold_reason = None
                self.hold_detail = ""
                reply_text = await self._run_white(prompt)
        except Exception as exc:  # noqa: BLE001
            self.hold_reason = "failed"
            self.hold_detail = "game_turn_failed"
            logger.error(f"[mc_body] 唤醒白失败：{exc}", exc_info=True)
            return False

        if self.hold_reason is not None:
            return False
        consumed = max(0, ambient_count - (self._ambient_trimmed - trimmed_before))
        del self._ambient[:consumed]
        self._ambient_dropped -= dropped_before

        if not reply_text:
            logger.info("[mc_body] 白这次没有输出文本，不往游戏里发")
            return True
        try:
            await self._say_in_game(reply_text)
        except Exception as exc:
            # 公屏发送失败不能重跑已经执行的工具。
            logger.warning(f"[mc_body] 游戏回复发送失败，不重跑已完成轮次：{exc}")
        return True

    def _with_ambient(self, text: str) -> str:
        if not self._ambient:
            return text
        parts = [text, "", "最近的游戏内聊天（供你参考上下文）："]
        if self._ambient_dropped:
            parts.append(f"  （更早的约 {self._ambient_dropped} 条已经挤掉了，你只看到最近这几条）")
        parts += [f"  {line}" for line in self._ambient]
        return "\n".join(parts)

    def _build_prompt(self, trigger: str) -> str:
        # 游戏数据带 [mc:*]，避免混入现实处境。
        parts = [
            "[mc:chat] 来自 Minecraft 游戏内聊天（是游戏世界里的事，与用户现实处境无关）",
            "",
            f"游戏里有人对你说话了（你在游戏里的名字是 {self.self_name}）：",
            trigger,
        ]
        if self._ambient:
            parts += ["", "最近的游戏内聊天（供你参考上下文）："]
            if self._ambient_dropped:
                parts += [f"  （更早的约 {self._ambient_dropped} 条聊天已经挤掉了，你只看到最近这几条）"]
            parts += [f"  {line}" for line in self._ambient]
        if self._recent_self:
            parts += ["", "你刚才在游戏里说过："]
            parts += [f"  {line}" for line in self._recent_self]
        parts += [
            "",
            "请用你自己的身份回应。**你的回答会被自动打到游戏公屏上**，"
            "所以直接说话就行 —— 不需要（也不能）调用 `mc_say`，系统已经替你发了。",
            # 提示词之外仍需硬截断，避免长回复流入公屏。
            "⚠️ **公屏只发一句话**：一句话、20 字上下，像在游戏里打字聊天那样。"
            "**不要分点、不要换行、不要旁白、不要括号里加解释、不要一口气说三件事**。"
            "想说的多就挑最要紧的那一句。",
            "",
            "⚠️ **说话和执行是两件事。** 有人让你做事（放方块 / 合成 / 走过去 / 打怪 / 查东西…），"
            "**必须真的调用工具去做**，不能只在公屏上答应一句。"
            "做不到就直说做不到，**绝对不许用「已经做好了」来圆场** —— "
            "你说了什么都会被人看见，做没做也瞒不住。",
        ]
        return "\n".join(parts)

    async def _run_white(self, prompt: str) -> str:
        from astrbot.core.astr_main_agent import (
            MainAgentBuildConfig,
            _get_session_conv,
            build_main_agent,
        )
        from astrbot.core.cron.events import CronMessageEvent
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.provider.entities import ProviderRequest

        session = MessageSession.from_str(self.umo)
        # event 与 req.prompt 保持一致，兼容框架回退。
        event = CronMessageEvent(
            context=self.context,
            session=session,
            message=prompt,
            extras={"mc_body_uplink": True},
            message_type=session.message_type,
        )
        # CronMessageEvent 的 sender 使用用户会话 ID，沿用原授权检查。

        cfg = self.context.get_config(umo=self.umo) or {}
        provider_settings = cfg.get("provider_settings") or {}
        # 构建参数须对齐 pipeline，否则静默回退默认值。
        build_cfg = MainAgentBuildConfig(
            **provider_settings_to_build_kwargs(cfg),
            provider_settings=provider_settings,
            # 禁用流式，避免公屏收到分段回复。
            streaming_response=False,
        )

        req = ProviderRequest()
        req.prompt = prompt
        conv = await _get_session_conv(event=event, plugin_context=self.context)
        req.conversation = conv

        # 显式 req 会跳过框架历史填充；conv.history 是 JSON 文本，禁止 list()。
        try:
            req.contexts = json.loads(conv.history or "[]")
        except (TypeError, ValueError) as exc:
            logger.warning(f"[mc_body] 读会话历史失败，这一轮不带历史：{exc}")
            req.contexts = []
        if not isinstance(req.contexts, list):
            logger.warning(
                f"[mc_body] 会话历史不是 list（拿到 {type(req.contexts).__name__}），"
                "这一轮不带历史"
            )
            req.contexts = []
        # 此处条数是截断前数量，不等于实际模型上下文。
        logger.info(f"[mc_body] 游戏内这一轮读入历史 {len(req.contexts)} 条（截断前）")

        result = await build_main_agent(
            event=event,
            plugin_context=self.context,
            config=build_cfg,
            req=req,
            apply_reset=False,
        )
        if not result:
            self.hold_reason = "blocked"
            self.hold_detail = "agent_build_unavailable"
            return ""

        reset_coro = result.reset_coro
        try:
            return await self._run_built_turn(event, req, result, reset_coro)
        finally:
            if reset_coro.cr_frame is not None:
                reset_coro.close()

    async def _run_built_turn(self, event, req, result, reset_coro) -> str:
        # 钩子和净化后再建 runner 快照；工具去重须在 build 合并后做。
        for owner in (req, getattr(result, "provider_request", None)):
            tool_set = getattr(owner, "func_tool", None) if owner is not None else None
            if tool_set is not None:
                with contextlib.suppress(Exception):
                    tool_set.remove_tool("mc_say")

        # 绕过 pipeline 后必须补调请求钩子，返回 True 即终止。
        try:
            from astrbot.core.pipeline.context_utils import call_event_hook
            from astrbot.core.star.star_handler import EventType

            if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
                logger.info(
                    "[mc_body] 有 on_llm_request 钩子终止了这一轮 —— 照 pipeline 的语义不往下跑"
                )
                self.hold_reason = "blocked"
                self.hold_detail = "request_hook_blocked"
                return ""
        except Exception as exc:  # noqa: BLE001 - 钩子是加分项，不该让整轮挂掉
            logger.warning(f"[mc_body] 跑 on_llm_request 钩子失败（这一轮当没有它）：{exc}")

        # 这里只净化截断前视图，框架后续仍可能再截断。
        try:
            issues = protocol.audit(req.contexts)
            if issues:
                logger.warning(
                    f"[mc_body] 出站历史有 {len(issues)} 处不合法（净化前）：{issues[:3]}"
                )
            cleaned = protocol.for_wire(req.contexts)
            if len(cleaned) != len(req.contexts):
                logger.info(f"[mc_body] 出站净化：{len(req.contexts)} -> {len(cleaned)} 条")
            req.contexts = cleaned
        except Exception as exc:  # noqa: BLE001 - 净化失败不该让这一轮发不出去
            logger.warning(f"[mc_body] 出站净化失败（这一轮按原样发）：{exc}")

        await reset_coro
        runner = result.agent_runner
        initial_messages = runner.run_context.messages
        turn_start = next((m for m in reversed(initial_messages)
                           if getattr(m, "role", "") == "user"), None)
        async for _ in runner.step_until_done(self.max_steps):
            pass
        # 绕过 pipeline 时需显式保存本轮历史。
        await self._save_turn(req, runner, turn_start=turn_start)

        final = runner.get_final_llm_resp()
        if final is None:
            return ""
        return (getattr(final, "completion_text", "") or "").strip()

    async def _load_stored(self, conv) -> list:
        r"""写回前重新读取完整历史；读取失败时禁止以空历史覆盖。"""
        cid = getattr(conv, "cid", None)
        if not cid:
            raise ValueError("会话ID缺失，拒绝写回历史")
        stored = await self.context.conversation_manager.get_conversation(
            self.umo, cid, create_if_not_exists=False
        )
        if stored is None:
            raise ValueError("会话不存在，拒绝写回历史")
        raw = getattr(stored, "history", None)
        if not isinstance(raw, str):
            raise TypeError("会话history不是JSON字符串，拒绝写回历史")
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or any(not isinstance(m, dict) for m in parsed):
            raise TypeError("会话历史不是list[dict]，拒绝写回历史")
        return parsed

    async def _save_turn(self, req, runner, *, turn_start=None) -> None:
        r"""update_conversation 是替换语义，须先核类型再合并，并检查只增不减及异常膨胀；不等同于 JSONL 追加。"""
        try:
            from astrbot.core.agent.message import dump_messages_with_checkpoints

            if turn_start is not None and getattr(runner, "_mcb_saved_turn", None) is turn_start:
                return
            conv = getattr(req, "conversation", None)
            if conv is None:
                return
            stored = await self._load_stored(conv)

            # 先校验元素类型；字符串拆成字符后会骗过长度检查。
            if not isinstance(stored, list) or any(not isinstance(m, dict) for m in stored):
                bad = type(stored).__name__
                logger.error(
                    f"[mc_body] 🔴 拒绝写回：读到的历史不是 list[dict]（顶层是 {bad}）。"
                    " 这一步如果继续走下去就是 2026-10-10 那次 478→18 万的重演。"
                )
                return

            run_msgs = list(getattr(getattr(runner, "run_context", None), "messages", []) or [])
            boundary = next((i for i, message in enumerate(run_msgs)
                             if message is turn_start), None) if turn_start is not None else None
            if boundary is None:
                logger.error("[mc_body] 拒绝写回：本轮起点缺失或被上下文处理移除，历史保持不变")
                return
            fresh = [m for m in run_msgs[boundary:]
                     if getattr(m, "role", "") != "system" and not getattr(m, "_no_save", False)]
            appended = dump_messages_with_checkpoints(fresh)
            combined = stored + appended

            if len(combined) < len(stored):
                logger.error(
                    f"[mc_body] 🔴 拒绝写回：{len(stored)} 条历史会变成 {len(combined)} 条。"
                    "（这不该发生，是 bug —— 见 _save_turn 的说明）"
                )
                return

            bad_roles = [
                m.get("role") for m in appended
                if m.get("role") not in ("user", "assistant", "tool", "_checkpoint")
            ]
            if bad_roles:
                logger.error(
                    f"[mc_body] 🔴 拒绝写回：追加段里出现非法 role {bad_roles[:5]}。"
                    "（合法只有 user/assistant/tool）"
                )
                return

            # 限制追加膨胀，防止重复追加。
            if len(combined) > max(40, len(stored) * 2):
                logger.error(
                    f"[mc_body] 🔴 拒绝写回：{len(stored)} 条 → {len(combined)} 条，涨得离谱。"
                    f"（追加了 {len(appended)} 条，多半是重复追加）"
                )
                return

            # 只记录形状摘要，不输出整份历史。
            logger.info(
                f"[mc_body] 落库前：stored={len(stored)} 追加={len(appended)} "
                f"→ {len(combined)} 条（{_shape_digest(combined)}）"
            )

            await self.context.conversation_manager.update_conversation(
                self.umo, conv.cid, history=combined
            )
            runner._mcb_saved_turn = turn_start
            logger.info(
                f"[mc_body] 游戏内这一轮已写回会话：历史 {len(stored)} → {len(combined)} 条"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 游戏内对话落库失败（不影响她这次的回应）：{exc}")

    async def _say_in_game(self, text: str) -> None:
        clean = _clean_for_game_chat(text)
        if not clean:
            return
        reply = await self.bridge.call(f"mcb say {clean}")
        if reply.get("ok"):
            logger.info(f"[mc_body] 已把白的回复打到游戏里：{clean}")
        else:
            logger.warning(
                f"[mc_body] 白的回复没打进游戏：{reply.get('error')}（内容：{clean}）"
            )




def _shape_digest(msgs: list) -> str:
    r"""仅记录角色计数和首尾，避免泄露或输出整份历史。"""
    counts: dict[str, int] = {}
    for m in msgs or []:
        r = m.get("role") if isinstance(m, dict) else type(m).__name__
        counts[str(r)] = counts.get(str(r), 0) + 1
    parts = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    head = msgs[0].get("role") if msgs and isinstance(msgs[0], dict) else "?"
    tail = msgs[-1].get("role") if msgs and isinstance(msgs[-1], dict) else "?"
    return f"{parts} 首={head} 尾={tail}"


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _trim_head(items: list[str], limit: int) -> int:
    dropped = 0
    while len(items) > limit:
        items.pop(0)
        dropped += 1
    return dropped


def _speaker_line(who: str, text: str, raw: dict) -> str:
    r"""距离按拉取时计算，不是说话时。"""
    pos = raw.get("pos")
    dist = raw.get("dist")
    if not (isinstance(pos, list) and len(pos) == 3):
        return f"<{who}> {text}"
    where = f"（他在 ({_g(pos[0])},{_g(pos[1])},{_g(pos[2])})"
    if isinstance(dist, (int, float)) and not isinstance(dist, bool):
        where += f"，离你 {dist:g} 格"
    where += "）"
    return f"<{who}> {text}{where}"


def _g(v: object) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:g}"
    return "?"


def _clean_for_game_chat(text: str) -> str:
    r"""提示词是软约束，发送前仍须单句与长度硬截断。"""
    flat = " ".join(str(text).split())
    for i, ch in enumerate(flat):
        # 短句门槛避免把语气词误当完整回复。
        if ch in _SENTENCE_END and i + 1 >= _SENTENCE_MIN:
            flat = flat[: i + 1]
            break
    return flat[:MAX_GAME_SAY_LEN]
