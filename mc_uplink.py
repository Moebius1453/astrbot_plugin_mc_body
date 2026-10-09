"""游戏内聊天 → 白的主会话（上行接入）。

轮询 RCON 的聊天增量，按规则决定要不要唤醒白；唤醒时走 AstrBot 自己的主动唤醒路径
（`CronMessageEvent` + `build_main_agent`）—— 白用的是**完整的人格、会话和工具循环**，
不是另开一条裸 LLM 通路。

规则（用户 2026-10-09 定）：
  * **被点名才唤醒** —— 游戏里的消息总是进白的环境感知，但只有含唤醒词才真跑一次 agent；
    其余的攒着当上下文，下次被唤醒时一并附上
  * **按来源路由（确定性代码）** —— 从游戏来的回复用 `mc_say` 打回游戏公屏，**不回 QQ**。
    用户原话："不太信任 AI 的判断，程序规范靠谱点"
  * **Nanako 自己的发言永不唤醒**（防自激），只作 `你刚才在游戏里说过` 前缀

⚠️ 依赖 AstrBot 核心内部 API（`CronMessageEvent` / `build_main_agent` / `_get_session_conv`）。
她自己的 cron 主动唤醒就用这套，但不是公开插件接口 —— **AstrBot 升级时可能断**。
断了的退路是装 QueQiao。
"""

from __future__ import annotations

import asyncio
import contextlib

from astrbot.api import logger

# 游戏内聊天单条上限约 256；留点余量
MAX_GAME_SAY_LEN = 200

# 唤醒失败时的退避上限
MAX_BACKOFF_SECONDS = 30.0


class ChatUplink:
    """把游戏内聊天接进白的会话。"""

    def __init__(
        self,
        bridge,
        context,
        *,
        umo: str,
        poll_interval: float = 1.5,
        wake_keywords: list[str] | None = None,
        ambient_limit: int = 20,
        self_name: str = "Nanako",
        max_steps: int = 30,
    ) -> None:
        self.bridge = bridge
        self.context = context
        self.umo = umo
        self.poll_interval = max(0.5, float(poll_interval))
        self.wake_keywords = [str(k).strip() for k in (wake_keywords or []) if str(k).strip()]
        self.ambient_limit = max(0, int(ambient_limit))
        self.self_name = self_name
        self.max_steps = max(1, int(max_steps))

        self._task: asyncio.Task | None = None
        self._last_seq = 0
        self._ambient: list[str] = []       # 没被点名、攒着当上下文的话
        self._recent_self: list[str] = []   # Nanako 最近说的，最多 2 句
        self._busy = asyncio.Lock()         # 一次只唤醒一个，别叠
        self._fail_streak = 0

    # ---- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        if not self.wake_keywords:
            logger.warning(
                "[mc_body] 未配置唤醒词 —— 游戏里说话永远不会唤醒白。"
                "请在插件配置的 wake_keywords 里填上（例如 Nanako、白）。"
            )
        self._task = asyncio.create_task(self._loop(), name="mc_body_uplink")
        logger.info(
            f"[mc_body] 聊天上行已启动：每 {self.poll_interval:g} 秒拉一次，"
            f"唤醒词 {self.wake_keywords}，会话 {self.umo}"
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        logger.info("[mc_body] 聊天上行已停止")

    # ---- 轮询主循环 -----------------------------------------------------

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
                # 隧道断了千万别刷屏：指数退避，只在第 1/5/20 次报一下
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
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "mcb chat 返回失败")

        data = reply.get("data") or {}
        lines = data.get("lines") or []
        max_seq = _as_int(data.get("max"))

        wake_trigger: str | None = None
        for line in lines:
            if not isinstance(line, dict):
                continue
            seq = _as_int(line.get("seq"))
            if seq > self._last_seq:
                self._last_seq = seq

            who = str(line.get("who") or "")
            text = str(line.get("text") or "").strip()
            if not text:
                continue

            if who == self.self_name:
                # 自己说的永不唤醒 —— 这是防自激的硬门槛
                self._recent_self.append(text)
                _trim_head(self._recent_self, 2)
                continue

            head = f"<{who}> {text}"
            self._ambient.append(head)
            _trim_head(self._ambient, self.ambient_limit)
            if self._is_wake(text):
                wake_trigger = head

        if max_seq > self._last_seq:
            self._last_seq = max_seq

        if wake_trigger is not None:
            await self._wake(wake_trigger)

    # ---- 判定 -----------------------------------------------------------

    def _is_wake(self, text: str) -> bool:
        low = text.lower()
        return any(kw.lower() in low for kw in self.wake_keywords) if low else False

    # ---- 唤醒 -----------------------------------------------------------

    async def _wake(self, trigger: str) -> None:
        if self._busy.locked():
            logger.info("[mc_body] 上一次唤醒还没跑完，这条先只进上下文")
            return
        async with self._busy:
            prompt = self._build_prompt(trigger)
            try:
                reply_text = await self._run_white(prompt)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[mc_body] 唤醒白失败：{exc}", exc_info=True)
                return

            # 跑成功了才清上下文 —— 失败时留着，下次还能带上
            self._ambient.clear()

            if not reply_text:
                logger.info("[mc_body] 白这次没有输出文本，不往游戏里发")
                return
            await self._say_in_game(reply_text)

    def _build_prompt(self, trigger: str) -> str:
        parts = [
            "【来自 Minecraft 游戏内聊天】",
            "",
            f"游戏里有人对你说话了（你在游戏里的名字是 {self.self_name}）：",
            trigger,
        ]
        if self._ambient:
            parts += ["", "最近的游戏内聊天（供你参考上下文）："]
            parts += [f"  {line}" for line in self._ambient]
        if self._recent_self:
            parts += ["", "你刚才在游戏里说过："]
            parts += [f"  {line}" for line in self._recent_self]
        parts += [
            "",
            "请用你自己的身份回应。**你的回答会被自动打到游戏公屏上**，"
            "所以直接说话就行 —— 不需要（也不能）调用 `mc_say`，系统已经替你发了。"
            "回答要短，像在游戏里聊天那样，不要加旁白或括号说明。",
        ]
        return "\n".join(parts)

    async def _run_white(self, prompt: str) -> str:
        """把文本注入白的主会话，跑一次完整 agent 循环，返回她的最终文本。

        ⚠️ 用核心内部 API。见模块头部的说明。
        """
        from astrbot.core.astr_main_agent import (
            MainAgentBuildConfig,
            _get_session_conv,
            build_main_agent,
        )
        from astrbot.core.cron.events import CronMessageEvent
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.provider.entities import ProviderRequest

        session = MessageSession.from_str(self.umo)
        # 事件消息和 req.prompt 都设成同一段 —— build_main_agent 里 `req.prompt` 为空时
        # 会退回 `event.message_str`，两条路喂同一段内容，哪条赢都对。
        event = CronMessageEvent(
            context=self.context,
            session=session,
            message=prompt,
            extras={"mc_body_uplink": True},
            message_type=session.message_type,
        )
        # 这条很关键：CronMessageEvent 把 sender.user_id 设成 session_id（= 用户 QQ 号），
        # 所以白在循环里调 mc_state 之列的工具时，授权检查能过。不用另造后门。

        cfg = self.context.get_config(umo=self.umo) or {}
        provider_settings = cfg.get("provider_settings") or {}
        build_cfg = MainAgentBuildConfig(
            tool_call_timeout=provider_settings.get("tool_call_timeout", 120),
            streaming_response=False,
            llm_safety_mode=False,
            provider_settings=provider_settings,
        )

        req = ProviderRequest()
        req.prompt = prompt
        conv = await _get_session_conv(event=event, plugin_context=self.context)
        req.conversation = conv
        # 不用手工塞 req.contexts —— build_main_agent 看到 req.conversation 会自己从
        # 会话历史里取（astr_main_agent.py 里 `if req.conversation: req.contexts = ...`）。

        result = await build_main_agent(
            event=event,
            plugin_context=self.context,
            config=build_cfg,
            req=req,
        )
        if not result:
            raise RuntimeError("build_main_agent 返回空（会话或 provider 有问题？）")

        # ⚠️ 结构性防止"说两遍"：**必须在 build 之后摘** ——
        #    build_main_agent 会把人格式的工具集 merge 进 req.func_tool（同名覆盖），
        #    提前摘没用。而 runner 是在**工具执行时**才读 req.func_tool 的
        #    （tool_loop_agent_runner.py:1146），所以这时候摘才有效。
        for owner in (req, getattr(result, "provider_request", None)):
            tool_set = getattr(owner, "func_tool", None) if owner is not None else None
            if tool_set is not None:
                with contextlib.suppress(Exception):
                    tool_set.remove_tool("mc_say")

        runner = result.agent_runner
        async for _ in runner.step_until_done(self.max_steps):
            pass

        final = runner.get_final_llm_resp()
        if final is None:
            return ""
        return (getattr(final, "completion_text", "") or "").strip()

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


# ---- 小工具 -------------------------------------------------------------


def _as_int(value, default: int = 0) -> int:
    """Rhino 的 JSON.stringify 会把整数写成浮点（1.0），这里统一收一下。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _trim_head(items: list[str], limit: int) -> None:
    while len(items) > limit:
        items.pop(0)


def _clean_for_game_chat(text: str) -> str:
    """游戏公屏是单行，且不宜过长。"""
    flat = " ".join(str(text).split())
    return flat[:MAX_GAME_SAY_LEN]
