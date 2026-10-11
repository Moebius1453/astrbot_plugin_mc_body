"""外脑通道：把话送到 Numen 同伴身上。

为什么要这个模块：白回话的出口原本是 `mcb say`，那条路是
RCON -> 服务端 KubeJS -> 转发给客户端脚本 -> sendChat。Numen 的同伴是
服务端假玩家，没有客户端脚本接这条转发，所以服务端照常回 sent:true，
而游戏里什么都不会发生。症状是"她答了，公屏上却没有"。

Numen 自带一个 MCP 端点，AstrBot 的 data/mcp_server.json 里已经配好了
它的 url 和 token。这里直接读那一份，不另立配置 —— 免得端口或 token
存两份、改一处忘一处。

只依赖标准库：插件声明的是零第三方依赖，MCP 的 HTTP 调用手写。
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path

from astrbot.api import logger

_TIMEOUT_SECONDS = 30.0


def _find_numen_server() -> tuple[str, str] | None:
    """从 AstrBot 自己的 MCP 配置里找 numen 的 url 和 token。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[mc_body] 拿不到 AstrBot 数据目录：{exc}")
        return None
    path = Path(get_astrbot_data_path()) / "mcp_server.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[mc_body] 读不到 {path}：{exc}")
        return None
    for name, cfg in (data.get("mcpServers") or {}).items():
        if "numen" not in str(name).lower():
            continue
        url = str((cfg or {}).get("url") or "")
        if not url:
            continue
        token = str(((cfg or {}).get("headers") or {}).get("Authorization") or "")
        return url, token
    return None


def _parse_body(raw: str) -> dict:
    """MCP 的响应可能是纯 JSON，也可能是 SSE（data: 开头的一行）。两种都收。"""
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                return json.loads(chunk)
    return {}


def _call_blocking(url: str, token: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
        return _parse_body(resp.read().decode("utf-8"))


async def _call_tool(name: str, arguments: dict) -> str | None:
    """调一个 Numen 的 MCP 工具，返回回执文本；失败返回 None。

    注意： 省掉了 MCP 的 initialize 握手 —— 实测这个服务端不需要先握手、
    也不校验 session id，直接 tools/call 就收。
    """
    found = _find_numen_server()
    if found is None:
        return None
    url, token = found
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    try:
        reply = await asyncio.to_thread(_call_blocking, url, token, payload)
    except Exception as exc:  # noqa: BLE001 —— 端点没起、超时，都算"没送到"
        logger.warning(f"[mc_body] 调 Numen 的 {name} 失败：{exc}")
        return None
    result = reply.get("result") or {}
    texts = [str(c.get("text", "")) for c in (result.get("content") or [])]
    if result.get("isError"):
        logger.warning(f"[mc_body] Numen 的 {name} 报错：{' '.join(texts)[:200]}")
        return None
    return texts[0] if texts else ""


async def say(companion: str, text: str) -> bool:
    """以同伴的身份开口（气泡 + 聊天行 + 绑定声线）。返回是否确实送到。"""
    if not companion or not text:
        return False
    receipt = await _call_tool("say", {"companion": companion, "text": text})
    if receipt is None:
        return False
    logger.info(f"[mc_body] 已通过 Numen 让 {companion} 开口：{text}")
    return True
