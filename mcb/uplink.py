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
import json

from astrbot.api import logger

# 游戏内聊天单条上限约 256；留点余量
MAX_GAME_SAY_LEN = 200

# 唤醒失败时的退避上限
MAX_BACKOFF_SECONDS = 30.0

# ⚠️ 游戏内**只回一句短的**（用户 2026-10-10："强制游戏内信息只允许回一条简短的"）。
#    公屏单条上限约 256，这里再收一道：**先按句号截一句，再按长度硬截**。
MAX_GAME_SAY_LEN = 120

# 句末标点 —— 撞上第一个就截住（**连同标点**，读起来才自然）
_SENTENCE_END = "。！？!?…"

# ⚠️ 但**太短的"第一句"不算** —— 实测踩到（2026-10-10 21:36）：
#    她回 `诶？我这不是正看着你呢嘛～`，第一句是 `诶？`（2 字），
#    硬截之后公屏上只剩一个 **`诶？`**，**正文全丢了**。
#    语气词开头的句子在中文里太常见，所以要求"到这里至少凑够 N 个字"才肯截。
_SENTENCE_MIN = 8


def provider_settings_to_build_kwargs(cfg: dict) -> dict:
    """把 AstrBot 的全局配置翻成 `MainAgentBuildConfig` 的字段。

    ⚠️⚠️ **必须和 pipeline 的映射逐字对齐**
    （`pipeline/process_stage/method/agent_sub_stages/internal.py:75-155`）。
    我们是**绕过 pipeline** 直接 `build_main_agent` 的，
    **少传一个字段 = 那个字段悄悄退回 dataclass 默认值**，而且**不报错、看不出来**。

    2026-10-10 实测踩到（用户一句"上下文 800 吗"引出来的）：我们原来只传了 4 个字段，
    其余全在用默认值 —— 于是游戏里那一轮和 QQ 那边**根本不是同一套设置**：

    | 字段 | 我们（默认值） | 用户实际配的 |
    |---|---|---|
    | `max_context_length` | 50 轮 | -1（当时不限；现改成 20） |
    | `kb_agentic_mode` | False | **True** |
    | `computer_use_runtime` | `"local"` | **`"none"`** |
    | `llm_safety_mode` | 硬写 False | **True** |
    | `add_cron_tools` | True（**恰好对**） | True |

    > 📌 和 `req.contexts` 那个 bug **同一类**：**绕过框架时，
    > "框架会自己处理"是假设，不是事实。** 要么读源码确认，要么自己显式做。
    """
    ps = (cfg or {}).get("provider_settings") or {}
    file_extract = ps.get("file_extract") or {}
    proactive = ps.get("proactive_capability") or {}

    max_ctx = int(ps.get("max_context_length", 20))
    # ⚠️ 这段和 pipeline 一模一样（`internal.py:106-112`）—— 包括 `max_ctx - 1` 那个
    #    在 `max_ctx == -1` 时会算出负数的边角，再被下面那句兜回 1。
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

            head = _speaker_line(who, text, line)
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
        # ⚠️ 统一用 `[mc:*]` 前缀（规范见 `docs\12`）：**凡是从 Minecraft 来的数据都带这个标签**，
        #    好和现实对话分开。这些是**游戏里**发生的事，和用户现实中的处境无关。
        parts = [
            "[mc:chat] 来自 Minecraft 游戏内聊天（是游戏世界里的事，与用户现实处境无关）",
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
            "所以直接说话就行 —— 不需要（也不能）调用 `mc_say`，系统已经替你发了。",
            # ⚠️ 用户 2026-10-10："**强制游戏内信息只允许回一条简短的**"。
            #    这条是**软约束**（模型可以不听话），所以 `_clean_for_game_chat` 还有一道
            #    程序性硬截断 —— 两层一起用才稳。
            "⚠️ **公屏只发一句话**：一句话、20 字上下，像在游戏里打字聊天那样。"
            "**不要分点、不要换行、不要旁白、不要括号里加解释、不要一口气说三件事**。"
            "想说的多就挑最要紧的那一句。",
            "",
            # ⚠️⚠️ 这一段是 2026-10-10 加的，因为实测她**只答应不动手**：
            #    用户在公屏说"放下熔炉"，她回"好嘞，熔炉放地上啦！" —— **其实根本没放**，
            #    一翻背包熔炉还在。她是在**演**，不是在**做**。
            #    原来这段提示词只说"回应"，**从没告诉她可以动手** —— 于是她把每次唤醒
            #    都当成一次"聊天回复"任务。工具一直都在（`build_main_agent` 会给全套），
            #    缺的是**让她知道该用**。
            "⚠️ **说话和执行是两件事。** 有人让你做事（放方块 / 合成 / 走过去 / 打怪 / 查东西…），"
            "**必须真的调用工具去做**，不能只在公屏上答应一句。"
            "做不到就直说做不到，**绝对不许用「已经做好了」来圆场** —— "
            "你说了什么都会被人看见，做没做也瞒不住。",
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
        # ⚠️ **字段逐个对齐 pipeline** —— 少一个就悄悄退回默认值。
        #    映射和理由都在 `provider_settings_to_build_kwargs` 里。
        build_cfg = MainAgentBuildConfig(
            **provider_settings_to_build_kwargs(cfg),
            provider_settings=provider_settings,
            # 这条和 pipeline 不同、是**故意**的：我们走的是"主动唤醒"那条路，
            # 流式会让她的话被拆成好几段往公屏上推。
            streaming_response=False,
        )

        req = ProviderRequest()
        req.prompt = prompt
        conv = await _get_session_conv(event=event, plugin_context=self.context)
        req.conversation = conv

        # ⚠️⚠️ **必须自己把历史读出来塞进 `req.contexts`**（2026-10-10 实测订正）。
        #
        #   **原来这里写的是"不用手工塞，build_main_agent 会自己取" —— 那是错的。**
        #   `build_main_agent` 里填 `contexts` 的两处（`astr_main_agent.py:1442` / `:1575`）
        #   **都在 `if req is None:` 这个分支里**；**我们直接传了 `req`**，
        #   所以那两处**一次都不会跑** → `req.contexts` 一直停在
        #   `ProviderRequest` 的默认值 `[]`（`provider/entities.py:104`）。
        #
        #   ⇒ 后果：**游戏里她看不到任何对话历史** —— QQ 那边看得见游戏里发生的事
        #     （因为都写进同一个会话），**反过来在游戏里却看不见 QQ 说过什么**。
        #     用户 2026-10-10 指出的就是这个不对称。
        #
        # ⚠️⚠️ `conv.history` 是 **JSON 文本（str）**，不是 list —— 必须 `json.loads`。
        #   **绝对不许写 `list(...)`**：`list("<json文本>")` 不抛异常，
        #   它把字符串**逐字符**拆开 —— 2026-10-10 那次把 478 条历史炸成 18 万条
        #   就是这么来的（见 `docs\17` §七）。
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
        # ⚠️ 这是**截断前**的原始条数 —— 真正发给模型的由
        #    `provider_settings.max_context_length`（轮数）在 runner 里再削一刀。
        #    所以这里数字大**不等于**发出去的多，两边要分开看。
        logger.info(f"[mc_body] 游戏内这一轮读入历史 {len(req.contexts)} 条（截断前）")

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

        # ⭐⭐ **补上 `on_llm_request` 钩子** —— 这一步决定"游戏里的她"和"QQ 里的她"
        #      是不是同一个白。
        #
        # ⚠️ 实测（2026-10-10）：`call_event_hook(..., OnLLMRequestEvent, ...)` 在
        #    **整个 AstrBot 里只有 pipeline 两处调**
        #    （`pipeline/.../agent_sub_stages/internal.py:269` 和 `third_party.py:335`）——
        #    `astr_main_agent.py` 里 **grep 零命中**。
        #    而我们这条路是 `build_main_agent` + `step_until_done`，**绕过了 pipeline**，于是：
        #        ❌ `livingmemory` 的**回忆注入**不生效
        #        ❌ 我们自己的**状态数据包**（`main.py` 的 `@filter.on_llm_request`）不生效
        #    用户 2026-10-10 问"游戏聊天是什么机制……让机器人依然接受那些注入回忆？"
        #    —— **答案就是这里断了。**
        #
        # ⚠️ 位置和 pipeline **一模一样**：`build_main_agent` 之后、`step_until_done` 之前。
        #    （人格/技能/提示词前缀是 `build_main_agent` 内部的 `_decorate_llm_request` 注的，
        #      那条路本来就通 —— **缺的只有钩子这一层**。）
        #
        # ⚠️ 返回值语义照抄 pipeline：**True = 有钩子把事件终止了** → 不再往下跑。
        try:
            from astrbot.core.pipeline.context_utils import call_event_hook
            from astrbot.core.star.star_handler import EventType

            if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
                logger.info(
                    "[mc_body] 有 on_llm_request 钩子终止了这一轮 —— 照 pipeline 的语义不往下跑"
                )
                return ""
        except Exception as exc:  # noqa: BLE001 - 钩子是加分项，不该让整轮挂掉
            logger.warning(f"[mc_body] 跑 on_llm_request 钩子失败（这一轮当没有它）：{exc}")

        runner = result.agent_runner
        async for _ in runner.step_until_done(self.max_steps):
            pass
        # ⚠️⚠️ **必须自己把这一轮存下来** —— 否则游戏里的对话**从来不进她的历史**。
        #
        #    会话**不是 agent 自己存的**，是 **pipeline 那一层**存的：
        #    `pipeline/.../agent_sub_stages/internal.py:333` 的 `_save_to_history`
        #    → `conv_manager.update_conversation`（`internal.py:486/532`）。
        #    我们直接调 `build_main_agent` + `step_until_done`，**绕过了那一层**。
        #
        #    后果（用户 2026-10-10 报的）："游戏里面的交流……**看上去就像是两个对话一样**"。
        #    **实测证据**：全库搜唤醒标记 `来自 Minecraft 游戏内聊天`，**0 次命中** ——
        #    她在游戏里说的每一句，都不在她的对话历史里。她记不住、也对不上。
        await self._save_turn(req, runner)

        final = runner.get_final_llm_resp()
        if final is None:
            return ""
        return (getattr(final, "completion_text", "") or "").strip()

    async def _load_stored(self, conv) -> list:
        """读出**真正的历史 list**。

        ⚠️⚠️ **绝不要写 `list(conv.history)`** —— `conv.history` 是 AstrBot v1 的
        legacy 字段，**类型是 `str`（JSON 文本）**：
        `conversation_mgr.py:84` → `history=json.dumps(conv_v2.content or [])`。

        `list("<JSON文本>")` **不会抛异常** —— 它把字符串**逐字符拆开**，
        返回 184467 个单字符。2026-10-10 那次事故就是这么来的：
        `stored` 成了 184467 个字符，"绝不减少"的闸比较字符数、永远通过，
        于是把垃圾写回了库。**不是偶尔写坏，是在这个版本下必然写坏。**

        → 所以一律走**原始源头** `ConversationV2.content`（数据库里那个 list[dict]）。
        """
        cid = getattr(conv, "cid", None)
        if cid:
            try:
                v2 = await self.context.conversation_manager.db.get_conversation_by_id(cid)
                if v2 is not None:
                    return list(v2.content or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[mc_body] 读会话原始 content 失败，退回 legacy 字段：{exc}")

        # 退路：legacy 字段是 JSON 文本 —— 必须 json.loads，**绝不能 list()**
        raw = getattr(conv, "history", None)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw) if raw.strip() else []
            except ValueError:
                return []
            return parsed if isinstance(parsed, list) else []
        return list(raw) if isinstance(raw, list) else []

    async def _save_turn(self, req, runner) -> None:
        """把这一轮写回会话 —— 照 pipeline 的做法，但**必须保证"只增不减 + 类型全对"**。

        ⚠️⚠️ **2026-10-10 血案（两次，同一个函数）**：
        ① 初版直接把 `runner.run_context.messages` 丢进去，自以为那是"完整上下文" ——
           **在我们这条绕过 pipeline 的路上，它只有本轮**（第一条就是 uplink 的 prompt，
           前面没有历史）。`update_conversation(history=...)` 是**替换**语义 → 494 条 → 1 条。
        ② "修好"之后仍然写坏：`stored = list(conv.history)` —— 见 `_load_stored` 的说明，
           `conv.history` 是 **str**，`list()` 把 478 条历史拆成了 184467 个单字符。

        ⭐ **2026-10-10 深夜按 Numen 的纪律加固**（方案：`docs\21` §2.2）。核心两处：

        1. **不再猜"谁更长"**。原来是
               `if len(fresh) >= len(stored): combined = fresh else: stored + fresh`
           —— 这是**"run 里含历史"这个假设**，一旦假设错就是 494→1。
           改成**只追加**：`stored + [历史上没有过的那些]`。
           两种情况都对：run 含历史时重复部分被滤掉；run 只有本轮时全部追加。
        2. **四道闸一起上**（原来是"长度"一道，而**类型错了时长度闸永远通过**）：
           ⓪ 类型闸：`stored` 里**每个元素都得是 dict**（血案②就是元素变成了 str）
           ① 只增不减
           ② 追加段的 role 只能是 `user/assistant/tool`
           ③ 膨胀闸：涨得太离谱就**拒写**（防"重复追加"这种慢性爆炸）

        > ⭐ 先天差距要说清：AstrBot 的 `update_conversation(history=…)` 是**替换语义**，
        > 我们**做不到真正的 append-only**（Numen 的 `ConvoLog` 是 JSONL 追加 + 派生视图）。
        > 所以这里只能把"读-改-写"这条路守得更死 —— **守不出追加，只能守出"不敢乱写"**。
        """
        try:
            from astrbot.core.agent.message import dump_messages_with_checkpoints

            conv = getattr(req, "conversation", None)
            if conv is None:
                return
            stored = await self._load_stored(conv)

            # ── 第⓪道闸：**类型闸** ──────────────────────────────────────
            # 血案②就是这里没查：`stored` 变成了 184467 个 **str**，
            # 而"长度不许变少"那道闸比的是**元素个数**，垃圾越多越容易通过。
            if not isinstance(stored, list) or any(not isinstance(m, dict) for m in stored):
                bad = type(stored).__name__
                logger.error(
                    f"[mc_body] 🔴 拒绝写回：读到的历史不是 list[dict]（顶层是 {bad}）。"
                    " 这一步如果继续走下去就是 2026-10-10 那次 478→18 万的重演。"
                )
                return

            run_msgs = list(getattr(getattr(runner, "run_context", None), "messages", []) or [])
            # 第一条 system 不要 —— 那是每轮重建的人格/提示词，存了会把历史撑爆
            fresh = [m for m in run_msgs if getattr(m, "role", "") != "system"]
            fresh_dicts = dump_messages_with_checkpoints(fresh)

            # ── 只追加：不做"谁更长"的猜测（见方法说明第 1 条）──────────
            appended = [m for m in fresh_dicts if m not in stored]
            combined = stored + appended

            # ── 第①道闸：只增不减 ────────────────────────────────────────
            if len(combined) < len(stored):
                logger.error(
                    f"[mc_body] 🔴 拒绝写回：{len(stored)} 条历史会变成 {len(combined)} 条。"
                    "（这不该发生，是 bug —— 见 _save_turn 的说明）"
                )
                return

            # ── 第②道闸：追加段的 role 只能是这三种 ──────────────────────
            bad_roles = [
                m.get("role") for m in appended
                if m.get("role") not in ("user", "assistant", "tool")
            ]
            if bad_roles:
                logger.error(
                    f"[mc_body] 🔴 拒绝写回：追加段里出现非法 role {bad_roles[:5]}。"
                    "（合法只有 user/assistant/tool）"
                )
                return

            # ── 第③道闸：膨胀闸（防"重复追加"这种慢性爆炸）──────────────
            # 正常一轮最多加几条到几十条；一次涨了一倍以上必然有问题。
            if len(combined) > max(40, len(stored) * 2):
                logger.error(
                    f"[mc_body] 🔴 拒绝写回：{len(stored)} 条 → {len(combined)} 条，涨得离谱。"
                    f"（追加了 {len(appended)} 条，多半是重复追加）"
                )
                return

            # 写前留一份**形状摘要** —— 出事了能一眼看出是第几步坏的。
            # ⚠️ 不打印全文（900 条 dict 灌进日志没意义也没人看）。
            logger.info(
                f"[mc_body] 落库前：stored={len(stored)} 追加={len(appended)} "
                f"→ {len(combined)} 条（{_shape_digest(combined)}）"
            )

            await self.context.conversation_manager.update_conversation(
                self.umo, conv.cid, history=combined
            )
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


# ---- 小工具 -------------------------------------------------------------


def _shape_digest(msgs: list) -> str:
    """一句话说清一批消息的**形状** —— 出事了能一眼看出是哪一步开始不对的。

    ⚠️ 为什么不直接把整批打印出来：900 条 dict 灌进日志既没人看、又会把日志撑爆。
    这里只给 **角色计数 + 首尾角色**，定位"从哪一步起变形"足够了。
    （2026-10-10 那两次血案的共同点就是**形状变了**：一次只剩 1 条，一次变成 18 万个单字符。）
    """
    counts: dict[str, int] = {}
    for m in msgs or []:
        r = m.get("role") if isinstance(m, dict) else type(m).__name__
        counts[str(r)] = counts.get(str(r), 0) + 1
    parts = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    head = msgs[0].get("role") if msgs and isinstance(msgs[0], dict) else "?"
    tail = msgs[-1].get("role") if msgs and isinstance(msgs[-1], dict) else "?"
    return f"{parts} 首={head} 尾={tail}"


def _as_int(value, default: int = 0) -> int:
    """Rhino 的 JSON.stringify 会把整数写成浮点（1.0），这里统一收一下。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _trim_head(items: list[str], limit: int) -> None:
    while len(items) > limit:
        items.pop(0)


def _speaker_line(who: str, text: str, raw: dict) -> str:
    """`<谁> 说了什么` —— **外加他在哪、离多远**。

    ⚠️ **坐标是这条的重点**（用户 2026-10-10 定的）：
    "**视距内自己看，视距外靠人告诉**" —— 她视距只有 2 chunk（32 格），
    出了这个圈她**什么都看不见**，只能听人报。真人也没有世界地图，
    是听别人说"山那边有个村"。

    ⚠️ 距离是**拉取时**算的（服务端 `mcbChatData`），不是说话时算的 ——
    她要的是"**现在**他离我多远"，不是"他说话那一刻"。
    """
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
    """游戏公屏是**单行 + 一句 + 短**。

    用户 2026-10-10："**强制游戏内信息只允许回一条简短的**"。

    ⚠️ **两层一起用才稳**：
      · **发请求时**（`_build_prompt` 末尾）写死"只准一句话" —— 让它**别生成**
      · **发出去之前**（这里）程序性截断 —— 生成长了也兜得住

    提示词是**软**的（模型可以不听话），这一步是**硬**的。只靠提示词，
    迟早会有一条三行带旁白的回复飘到公屏上。
    """
    flat = " ".join(str(text).split())
    for i, ch in enumerate(flat):
        # ⚠️ `i + 1 >= _SENTENCE_MIN` 这个门槛别去掉 —— 见 `_SENTENCE_MIN` 的注释：
        #    少了它，"诶？我这不是正看着你呢嘛～" 会被砍成一个光秃秃的"诶？"。
        if ch in _SENTENCE_END and i + 1 >= _SENTENCE_MIN:
            flat = flat[: i + 1]
            break
    return flat[:MAX_GAME_SAY_LEN]
