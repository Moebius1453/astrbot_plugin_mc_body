
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import uuid

try:  
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 只在脱离 AstrBot 时走到
    logger = logging.getLogger(__name__)

ENVELOPE_PREFIX = "MCB "

_TYPE_RESPONSE = 0
_TYPE_COMMAND = 2
_TYPE_LOGIN = 3

_IDLE_GAP_SECONDS = 0.25

# Minecraft 会将大响应拆包，不能只读一个包。
_SPLIT_HINT = 4000

_BODY_TIMEOUT = 5.0

# 连接排空用短窗口，避免增加首条命令延迟。
_CONNECT_DRAIN_SECONDS = 0.05

_MAX_PACKETS = 128

# RCON 命令正文上限为 1446 字节。
_MAX_COMMAND_BYTES = 1446


class BridgeError(RuntimeError):
    """桥不可用：连不上、没响应、密码错、返回了看不懂的东西。

    消息是给人和模型看的中文 —— 它会原样进到白的下一次请求里，
    所以要说清是"隧道断了"还是"命令本身失败了"。
    """


def parse_envelope(raw: str, *, request: str | None = None,
                   action: str | None = None) -> dict:
    text = (raw or "").strip()
    objects = []
    decoder = json.JSONDecoder()
    offset = 0
    while True:
        index = text.find(ENVELOPE_PREFIX, offset)
        if index < 0:
            break
        start = index + len(ENVELOPE_PREFIX)
        body = text[start:].lstrip()
        try:
            obj, used = decoder.raw_decode(body)
        except json.JSONDecodeError as exc:
            raise BridgeError("桥回执不完整或不是合法JSON，未确认命令结果") from exc
        if not isinstance(obj, dict) or type(obj.get("ok")) is not bool:
            raise BridgeError("桥回执类型不合法，未确认命令结果")
        objects.append(obj)
        offset = len(text) - len(body) + used
    matches = [obj for obj in objects
               if (request is None or obj.get("request") == request)
               and (action is None or obj.get("action") == action)]
    if len(matches) != 1:
        raise BridgeError("桥回执缺失或不唯一，未确认命令结果；不自动重发")
    return matches[0]


def _looks_complete(text: str) -> bool:
    decoder = json.JSONDecoder()
    remaining = text.strip()
    found = False
    while remaining:
        if not remaining.startswith(ENVELOPE_PREFIX):
            return False
        try:
            obj, used = decoder.raw_decode(remaining[len(ENVELOPE_PREFIX):])
        except json.JSONDecodeError:
            return False
        if not isinstance(obj, dict) or type(obj.get("ok")) is not bool:
            return False
        found = True
        remaining = remaining[len(ENVELOPE_PREFIX) + used:].strip()
    return found


class RconBridge:

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
        self._buf = bytearray()   
        self._request_id = 0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def run(self, command: str) -> str:
        r"""传输失败抛 BridgeError，不以空输出冒充成功。"""
        if len(command.encode("utf-8")) > _MAX_COMMAND_BYTES:
            raise BridgeError(
                f"命令太长（超过 RCON 的 {_MAX_COMMAND_BYTES} 字节上限）：{command[:60]}…"
            )

        async with self._lock:
            await self._connect_locked()
            try:
                return await self._exchange_locked(command)
            except (BridgeError, asyncio.CancelledError):
                await self._drop_locked()
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
        return await self.run("mcb ping")

    async def call(self, command: str) -> dict:
        r"""ok:false 属正常桥回执；仅传输或解析失败抛异常。"""
        words = command.strip().split(maxsplit=2)
        if len(words) < 2 or words[0] != "mcb":
            raise BridgeError("call只接受mcb命令，原版命令请使用run")
        token = uuid.uuid4().hex
        tagged = f"mcb @{token} " + command.strip()[4:]
        reply = parse_envelope(await self.run(tagged), request=token, action=words[1])
        if words[1] != "say" or not reply.get("ok"):
            return reply
        receipt_id = (reply.get("data") or {}).get("receiptId")
        if receipt_id != token:
            raise BridgeError("说话已下发但缺少执行回执标识，不自动重发")
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            receipt = await self.call(f"mcb receipt {receipt_id}")
            data = receipt.get("data") or {}
            state = data.get("state")
            if not receipt.get("ok") or state in ("failed", "unknown"):
                return {"ok": False, "action": "say", "error": "说话执行未确认，不自动重发：" + str(data.get("error") or state)}
            if state == "executed":
                reply["data"]["executed"] = True
                return reply
            await asyncio.sleep(0.1)
        raise BridgeError("说话已下发，但客户端执行回执超时，结果未知，不自动重发")

    async def close(self) -> None:
        async with self._lock:
            await self._drop_locked()


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
        # 登录后可能跟随空包，仅在建连时排空。
        await self._drain_locked()
        logger.debug(f"[mc_body] RCON 已连接 {self.host}:{self.port}")

    async def _exchange_locked(self, command: str) -> str:
        # 逐命令排空会增加静默延迟；残包按 request id 丢弃。
        pid = self._next_id()
        await self._write_locked(pid, _TYPE_COMMAND, command)

        parts: list[str] = []
        wait = self.timeout   
        seen = 0
        deadline = asyncio.get_running_loop().time() + self.timeout
        while seen < _MAX_PACKETS:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                body, rid = await self._read_packet(min(wait, remaining))
            except asyncio.TimeoutError:
                if not parts or self._buf:
                    raise
                break
            seen += 1
            if rid != pid:
                continue
            wait = _IDLE_GAP_SECONDS
            parts.append(body)
            # 非满包且信封完整才确认收全。
            if len(body) < _SPLIT_HINT and _looks_complete("".join(parts)):
                break

        if seen >= _MAX_PACKETS:
            raise BridgeError("RCON响应包数超限，未确认命令结果")
        return "".join(parts).strip()

    async def _drain_locked(self) -> None:
        r"""仅连接后排空一次，逐命令排空会增加延迟。"""
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
        r"""自管缓冲；取消 readexactly 会丢失已消费的半包。"""
        await self._fill(4, timeout, consume=False)
        (length,) = struct.unpack("<i", self._buf[:4])
        if not 10 <= length <= 4_200_000:
            raise BridgeError(f"RCON 包长度不合理（{length}），流已经错位了")
        packet = await self._fill(length + 4, _BODY_TIMEOUT)
        data = packet[4:]
        req_id, _type = struct.unpack("<ii", data[:8])
        if data[-2:] != b"\x00\x00":
            raise BridgeError("RCON包终止符不合法")
        return data[8:-2].decode("utf-8", "replace"), req_id

    async def _fill(self, need: int, timeout: float, *, consume: bool = True) -> bytes:
        r"""超时保留已收字节，供下次继续。"""
        assert self._reader is not None
        while len(self._buf) < need:
            try:
                chunk = await asyncio.wait_for(self._reader.read(65536), timeout)
            except asyncio.TimeoutError:
                raise
            if not chunk:
                raise BridgeError("RCON 连接被服务端关闭了")
            self._buf += chunk
            timeout = _BODY_TIMEOUT   
        out = bytes(self._buf[:need])
        if consume:
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
