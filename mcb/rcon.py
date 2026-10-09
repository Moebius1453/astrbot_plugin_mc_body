"""与 Minecraft 服务端 `mcbridge` 桥对话的 RCON 客户端。

只干一件事：把一条 RCON 命令发出去、把文本收回来；失败时抛出**带原因**的错误。
不认识 Minecraft，也不认识 AstrBot —— 所以可以脱离插件单独测。

桥的命令契约见项目文档 `docs/09-桥接实现与AstrBot工具.md`。

## 为什么不用 `aiomcrcon`

**它有个会串包的硬 bug，我们踩过（2026-10-09）。**
它的 `_send_msg` **只读一个包就返回**，而 **Minecraft 的 RCON 会把超过 4096 字节的
响应拆成多个包**发出来（同一个 request id）。于是：

  · `mcb scan 48`（几万个方块）、`mcb chat`（最多 50 条）这种大响应 → 剩下的包
    **留在 socket 缓冲区里**，**下一条命令读到的就是上一轮的残渣**。
  · 症状：插件日志里出现
    `桥返回的信封不是合法 JSON：'{"ok":true,...}\nMCB {"ok":true,...}'`
    —— 两个信封粘在一起，第一个还缺了 `MCB ` 前缀。
  · `asyncio.wait_for` 超时也会**打断读到一半的包**，同样留残渣。

**正解：读到"安静"为止。** 先按 `self.timeout` 等第一个包，之后只要 0.15 秒内
还有包就继续收、拼起来；发命令前**先把残渣排空**。这是 rcon-cli / mcrcon 那类
成熟客户端的通行做法。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct

try:  # 在 AstrBot 里跑时用它的 logger；单独拿出来测时退回标准库
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 只在脱离 AstrBot 时走到
    logger = logging.getLogger(__name__)

ENVELOPE_PREFIX = "MCB "

# 包类型
_TYPE_RESPONSE = 0
_TYPE_COMMAND = 2
_TYPE_LOGIN = 3

# 第一个包之后的"安静判定"：这么久没有新包就当响应收完了。
_IDLE_GAP_SECONDS = 0.25

# MC 按 **4096 字符** 把响应拆包（vanilla `RconClient.sendCmdResponse`）。
# 所以"这一包接近 4096"= 后面还有；远小于 = 这是最后一包。
_SPLIT_HINT = 4000

# 包体读一半的兜底：头都到手了，身子不该等太久
_BODY_TIMEOUT = 5.0

# 连接后那一次"排空"用多长的静默窗口。
# ⚠️ 别用 `_IDLE_GAP_SECONDS`（250ms）—— 实测那会让**每条连接的头一条命令白等 250ms**
# （排空通常什么都排不到）。50ms 足够接住紧跟登录响应一起到的包。
_CONNECT_DRAIN_SECONDS = 0.05

# 单条命令最多收多少个包（防跑飞）。4KB 一包，128 包 = 512KB，远超任何正经响应。
_MAX_PACKETS = 128

# RCON 协议上限：命令正文不能超过 1446 字节
_MAX_COMMAND_BYTES = 1446


class BridgeError(RuntimeError):
    """桥不可用：连不上、没响应、密码错、返回了看不懂的东西。

    消息是给人**和模型**看的中文 —— 它会原样进到白的下一次请求里，
    所以要说清是"隧道断了"还是"命令本身失败了"。
    """


def parse_envelope(raw: str) -> dict:
    """把服务端返回的文本拆成信封字典。

    服务端契约（见项目文档 09）：
        MCB {"ok":true,  "action":"state", "data":{...}}
        MCB {"ok":false, "action":"state", "error":"Nanako 不在线"}

    解析不了就抛 `BridgeError` —— **绝不假装成功**。

    ⚠️ 容错：万一还是收到了多个信封粘在一起（理论上传输层已经排干净了），
    **取最后一个** —— 残渣一定在前，新响应在后。
    """
    text = (raw or "").strip()
    if not text:
        raise BridgeError(
            "桥返回了空响应。可能是 RCON 打到了别的服务，或服务端脚本没加载。"
        )

    if ENVELOPE_PREFIX not in text:
        raise BridgeError(
            f"桥返回了非信封内容：{text!r}。"
            "可能是 RCON 打到了别的服务，或服务端脚本版本不对。"
        )

    # 取最后一个 MCB 开头的那段
    idx = text.rfind(ENVELOPE_PREFIX)
    if idx > 0:
        logger.warning(f"[mc_body] RCON 响应里有粘包，取最后一段（前 {idx} 字节丢弃）")
    body = text[idx + len(ENVELOPE_PREFIX) :].strip()
    # 去掉可能尾随的下一个信封（理论上不该有）
    nl = body.find("\n")
    if nl >= 0:
        body = body[:nl].strip()

    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"桥返回的信封不是合法 JSON：{body!r}") from exc
    if not isinstance(obj, dict) or "ok" not in obj:
        raise BridgeError(f"桥返回的信封缺少 ok 字段：{obj!r}")
    return obj


def _looks_complete(text: str) -> bool:
    """这段文本里已经有一段**能解析**的信封了吗？

    用来做"收工"判据 —— 别靠等时间猜，能解析就是收全了。
    """
    idx = text.find(ENVELOPE_PREFIX)
    if idx < 0:
        return False
    body = text[idx + len(ENVELOPE_PREFIX) :].strip()
    nl = body.find("\n")
    if nl >= 0:
        body = body[:nl].strip()
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and "ok" in obj


class RconBridge:
    """一条 RCON 连接，串行化访问，断了下次自动重连。

    协议实现细节见模块开头。关键点：**响应按"读到安静为止"收全**。
    """

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
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buf = bytearray()   # 自己的收包缓冲（见 _read_packet 的说明）
        self._request_id = 0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def run(self, command: str) -> str:
        """发一条命令，返回收全了的响应文本。

        任何失败都抛 `BridgeError`，绝不静默返回空串 ——
        "命令下发成功但没输出" 和 "压根没连上" 必须能区分开。
        """
        if len(command.encode("utf-8")) > _MAX_COMMAND_BYTES:
            raise BridgeError(
                f"命令太长（超过 RCON 的 {_MAX_COMMAND_BYTES} 字节上限）：{command[:60]}…"
            )

        async with self._lock:
            await self._connect_locked()
            try:
                return await self._exchange_locked(command)
            except BridgeError:
                raise
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

    async def ping(self) -> str:
        """连通性探针。连不上会抛 BridgeError。"""
        return await self.run("mcb ping")

    async def call(self, command: str) -> dict:
        """发一条命令并把返回的信封解析成 dict。

        只管**传输与解析**：信封里 `ok:false` 也照样返回（那是桥的正常回复，
        比如"Nanako 不在线"）。只有连不上/解析不了才抛 `BridgeError`。
        """
        return parse_envelope(await self.run(command))

    async def close(self) -> None:
        async with self._lock:
            await self._drop_locked()

    # ---- 内部（调用方必须已持有 self._lock）----------------------------

    async def _connect_locked(self) -> None:
        if self._writer is not None:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.connect_timeout
            )
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(
                f"连不上 RCON {self.host}:{self.port}（{type(exc).__name__}: {exc}）。"
                "最常见的原因是 SSH 隧道没开。"
            ) from exc

        # 登录
        pid = self._next_id()
        await self._write_locked(pid, _TYPE_LOGIN, self.password)
        try:
            body, rid = await self._read_packet(self.connect_timeout)
        except Exception:
            await self._drop_locked()
            raise
        if rid == -1:
            await self._drop_locked()
            raise BridgeError(f"RCON 密码不对（{self.host}:{self.port}）")
        # ⚠️ **登录握手会留下多余的包** —— 实测：认证响应之后还跟着一个
        #    `rid=1, len=0` 的空包。不排掉的话，**每一条命令前面都会先读到它**。
        #    只在连接时排一次（一条连接的代价），之后每条命令就不用再等了。
        await self._drain_locked()
        logger.debug(f"[mc_body] RCON 已连接 {self.host}:{self.port}")

    async def _exchange_locked(self, command: str) -> str:
        # ⚠️ 这里**不做"排空"** —— 排空必须等满一个静默窗口才能断定"没东西"，
        #    每条命令白花 250ms。残渣靠下面的 `rid != pid` 丢掉就够了：
        #    同一条连接上 request id 单调递增，**不可能撞车**。
        #    半包也不会丢 —— `_fill` 的超时不动缓冲区，下次接着取。
        pid = self._next_id()
        await self._write_locked(pid, _TYPE_COMMAND, command)

        parts: list[str] = []
        wait = self.timeout   # 第一个包等满超时（`/reload` 这类慢命令靠它）
        while len(parts) < _MAX_PACKETS:
            try:
                body, rid = await self._read_packet(wait)
            except asyncio.TimeoutError:
                break
            wait = _IDLE_GAP_SECONDS
            if rid != pid:
                continue          # 上一轮的残渣，丢掉
            parts.append(body)
            # 收工判据（**确定性的，不靠猜时机**）：
            #   这一包不是"满包"（<4096 → MC 没在拆）**且**已经拼出一段完整信封。
            if len(body) < _SPLIT_HINT and _looks_complete("".join(parts)):
                break

        return "".join(parts).strip()

    async def _drain_locked(self) -> None:
        """清掉连接刚建立时可能紧跟登录响应一起到的包。

        ⚠️ **只在连接后调一次，别每条命令都调** —— 那样每条都要白等一个静默窗口
        （实测 **250ms/条**，全是浪费）。因为 `_exchange_locked` 靠 `rid != pid` 就已经
        能丢掉任何残渣，排空纯粹是"清干净一点"。
        """
        dropped = 0
        while dropped < _MAX_PACKETS:
            try:
                await self._read_packet(_CONNECT_DRAIN_SECONDS)
                dropped += 1
            except asyncio.TimeoutError:
                break
            except (BridgeError, ConnectionError, OSError):
                break
        if dropped:
            logger.info(f"[mc_body] 连接后清掉 {dropped} 个多余的包")

    def _next_id(self) -> int:
        self._request_id = (self._request_id % 2_000_000_000) + 1
        return self._request_id

    async def _write_locked(self, req_id: int, msg_type: int, payload: str) -> None:
        assert self._writer is not None
        data = payload.encode("utf-8")
        packet = struct.pack("<ii", req_id, msg_type) + data + b"\x00\x00"
        self._writer.write(struct.pack("<i", len(packet)) + packet)
        await self._writer.drain()

    async def _read_packet(self, timeout: float) -> tuple[str, int]:
        """读**一个** RCON 包。返回 (正文, request_id)。

        ⚠️⚠️ **走自己的缓冲区，绝不用 `wait_for` 包住 `readexactly`。**
        踩过的坑（2026-10-09）：`asyncio.wait_for` 超时会**取消**正在进行的
        `readexactly`，而它**已经把一部分字节从 StreamReader 里消费掉了** ——
        那部分**永久丢失，整条流从此错位**。症状就是"每个响应前面挂着上一轮的残尾"。

        自己维护 `self._buf`：超时只影响"等多久"，**已经收到的字节一个都不丢**，
        下次接着从缓冲区里取。
        """
        header = await self._fill(4, timeout)
        (length,) = struct.unpack("<i", header)
        if not 10 <= length <= 4_200_000:
            raise BridgeError(f"RCON 包长度不合理（{length}），流已经错位了")
        data = await self._fill(length, _BODY_TIMEOUT)
        req_id, _type = struct.unpack("<ii", data[:8])
        return data[8:-2].decode("utf-8", "replace"), req_id

    async def _fill(self, need: int, timeout: float) -> bytes:
        """凑够 `need` 个字节。超时抛 `asyncio.TimeoutError`，**已收的字节留在缓冲区**。"""
        assert self._reader is not None
        while len(self._buf) < need:
            try:
                chunk = await asyncio.wait_for(self._reader.read(65536), timeout)
            except asyncio.TimeoutError:
                raise
            if not chunk:
                raise BridgeError("RCON 连接被服务端关闭了")
            self._buf += chunk
            timeout = _BODY_TIMEOUT   # 已经在收了，后面的字节给足时间
        out = bytes(self._buf[:need])
        del self._buf[:need]
        return out

    async def _drop_locked(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        self._buf.clear()
        if writer is None:
            return
        with contextlib.suppress(Exception):
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2.0)
