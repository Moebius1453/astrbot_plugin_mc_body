"""与 Minecraft 服务端 `mcbridge` 桥对话的最小 RCON 封装。

只干一件事：把一条 RCON 命令发出去、把文本收回来；失败时抛出**带原因**的错误。
不认识 Minecraft，也不认识 AstrBot —— 所以可以脱离插件单独测。

桥的命令契约见项目文档 `docs/09-桥接实现与AstrBot工具.md`。
"""

from __future__ import annotations

import asyncio
import contextlib

from astrbot.api import logger

try:  # 惰性降级：缺依赖时插件仍能加载，只是工具会报明确的错
    import aiomcrcon
except ImportError:  # pragma: no cover - 取决于运行环境
    aiomcrcon = None


class BridgeError(RuntimeError):
    """桥不可用：连不上、没响应、密码错。

    消息是给人**和模型**看的中文 —— 它会原样进到白的下一次请求里，
    所以要说清"是隧道断了"还是"命令本身失败了"。
    """


class RconBridge:
    """一条 RCON 连接，串行化访问，断了下次自动重连。"""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        timeout: float = 10.0,
        connect_timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._client = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def run(self, command: str) -> str:
        """发一条命令，返回去掉首尾空白的响应文本。

        任何失败都抛 `BridgeError`，绝不静默返回空串 ——
        "命令下发成功但没输出" 和 "压根没连上" 必须能区分开。
        """
        if aiomcrcon is None:
            raise BridgeError(
                "缺少依赖 aio-mc-rcon，请在 AstrBot 里重装插件依赖（pip install aio-mc-rcon）"
            )

        async with self._lock:
            await self._connect_locked()
            try:
                response, _req_id = await self._client.send_cmd(command, self.timeout)
            except asyncio.TimeoutError as exc:
                await self._drop_locked()
                raise BridgeError(
                    f"RCON 命令超时（{self.timeout:g} 秒）：{command}。"
                    "服务端可能在卡顿，或这条命令太重。"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 一律转成可读原因
                await self._drop_locked()
                raise BridgeError(
                    f"RCON 命令失败（{type(exc).__name__}）：{exc}"
                ) from exc
            return (response or "").strip()

    async def ping(self) -> str:
        """连通性探针。连不上会抛 BridgeError。"""
        return await self.run("mcb ping")

    async def close(self) -> None:
        async with self._lock:
            await self._drop_locked()

    # ---- 内部（调用方必须已持有 self._lock）----------------------------

    async def _connect_locked(self) -> None:
        if self._client is not None:
            return
        client = aiomcrcon.Client(self.host, self.port, self.password)
        try:
            await client.connect(self.connect_timeout)
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(
                f"连不上 RCON {self.host}:{self.port}（{type(exc).__name__}: {exc}）。"
                "最常见的原因是 SSH 隧道没开。"
            ) from exc
        self._client = client
        logger.debug(f"[mc_body] RCON 已连接 {self.host}:{self.port}")

    async def _drop_locked(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        # close() 内部会 wait_closed()，连接已断时可能卡住 —— 加超时兜底
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.close(), 2.0)
