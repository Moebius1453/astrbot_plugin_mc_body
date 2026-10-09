"""让白（Minecraft 里的 `Nanako`）长出手脚。

数据流：
    QQ ──→ 白 ──→ 本插件的 @filter.llm_tool ──→ RCON 隧道 ──→ R820 服务端桥
                                                              ──→ Nanako 的客户端
                                                              ──→ Baritone

本版本（v0.1.0）**只提供 `mc_state`** —— 纯只读，用来先把管线验通：
QQ 里问 → 白查到 Nanako 的坐标 → 回你。动作类工具等这一步验过再加。

授权：**默认拒绝**。必须在插件配置的 `allowed_sender_ids` 里列出允许的发送者 ID，
否则任何调用都会被挡回去。这是刻意的 —— 这个插件能让一个角色在世界里动，
不该因为"忘了配"就默认放行。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .mc_rcon import BridgeError, RconBridge

PLUGIN_NAME = "mc_body"


@register(
    "astrbot_plugin_mc_body",
    "Moebius1453",
    "让 AstrBot 的智能体在 Minecraft 里长出手脚：查询角色状态、说话、移动。",
    "0.1.0",
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

    # ---- 生命周期 -------------------------------------------------------

    async def initialize(self) -> None:
        if not self.bridge.password:
            logger.warning(
                f"[{PLUGIN_NAME}] 未配置 rcon_password，MC 工具会直接报错。"
                "请在插件配置里填上。"
            )
        if not self._allowed_ids():
            logger.warning(
                f"[{PLUGIN_NAME}] allowed_sender_ids 为空 —— MC 工具对所有人关闭。"
                "请在插件配置里填上允许的发送者 ID 才会生效。"
            )
        logger.info(
            f"[{PLUGIN_NAME}] 已加载，RCON 目标 {self.bridge.host}:{self.bridge.port}"
        )

    async def terminate(self) -> None:
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

    # ---- 工具 -----------------------------------------------------------

    @filter.llm_tool(name="mc_state")
    async def mc_state(self, event: AstrMessageEvent):
        """查询你在 Minecraft 里的状态：是否在线、坐标、血量、所在维度。

        当你想知道自己现在在哪、还活着没有、在哪个维度，或者想确认刚才的移动有没有
        生效时，用这个工具。它读的是服务端的即时数据，不依赖你的游戏客户端。

        如果返回"不在线"，说明 Nanako 的角色当前没有连进服务器，任何动作都做不了。
        """
        denied = self._authorize(event)
        if denied:
            return denied

        try:
            raw = await self.bridge.run("mcb state")
        except BridgeError as exc:
            return f"查不到 Nanako 的状态：{exc}"

        return self._format_state(raw)

    # ---- 输出整形 -------------------------------------------------------

    STATE_PREFIX = "MCB_STATE"
    FAIL_PREFIX = "MCB_FAIL:"

    @classmethod
    def _format_state(cls, raw: str) -> str:
        if raw.startswith(cls.STATE_PREFIX):
            body = raw[len(cls.STATE_PREFIX) :].strip()
            return f"Nanako 在线。{cls._explain_state(body)}"
        if raw.startswith(cls.FAIL_PREFIX):
            reason = raw[len(cls.FAIL_PREFIX) :].strip()
            return f"Nanako 当前不可用：{reason}"
        return (
            f"桥返回了意料之外的内容：{raw!r}。"
            "可能是服务端脚本版本不对，或 RCON 打到了别的服务。"
        )

    @staticmethod
    def _explain_state(body: str) -> str:
        """把 `x=.. y=.. z=.. hp=.. dim=.. name=..` 翻成人话。"""
        fields: dict[str, str] = {}
        for token in body.split():
            if "=" in token:
                key, _, value = token.partition("=")
                fields[key] = value

        parts: list[str] = []
        if {"x", "y", "z"} <= fields.keys() and "?" not in (
            fields["x"],
            fields["y"],
            fields["z"],
        ):
            parts.append(f"坐标 x={fields['x']} y={fields['y']} z={fields['z']}")
        if "hp" in fields:
            parts.append(f"血量 {fields['hp']}")
        if "dim" in fields:
            parts.append(f"维度 {fields['dim'].removeprefix('minecraft:')}")
        if not parts:
            return body

        text = "，".join(parts)
        if any("=ERR(" in value for value in fields.values()):
            text += "（部分字段读取失败，服务端脚本可能有问题）"
        return text

    # ---- 调试入口 -------------------------------------------------------

    @filter.command("mcbody")
    async def cmd_mcbody(self, event: AstrMessageEvent):
        """不带 LLM 的连通性自检：`/mcbody` 直接打一次 RCON，看隧道通不通。"""
        denied = self._authorize(event)
        if denied:
            yield event.plain_result(denied)
            return
        try:
            raw = await self.bridge.ping()
        except BridgeError as exc:
            logger.warning(f"[{PLUGIN_NAME}] /mcbody 自检失败: {exc}")
            yield event.plain_result(f"桥不通：{exc}")
            return
        yield event.plain_result(f"桥是通的。响应：{raw!r}")
