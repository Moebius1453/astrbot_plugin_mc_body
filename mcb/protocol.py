r"""抄 Numen 的切分（docs\21 §0）：记录事实 和 发给模型的样子 是两件事。
历史如实落盘，合法序列只在出口现算。我们两次血案（494 -> 1、478 -> 18 万）
都是把这两件事揉在一起 —— 这个模块只读不写，所以它不可能写坏任何账。

## 四条规则（照 Numen ProtocolView，去掉我们没有的那条）

| # | 规则 | 我们这边 |
|---|---|---|
| 1 | 悬空的 tool_call 补一条结果 | 有 —— 没有结果的 tool_call 上游会直接 400 |
| 2 | Halt 本身不发 | 不适用 —— 我们没有 Halt 这个消息类型（那是 Numen 的密封联合） |
| 3 | 相邻 user 合并 | 有 —— AstrBot 的 truncator 不做这条（见下） |
| 4 | 工具结果只发文字 | 有 —— 结构化数据不进请求 |"""

from __future__ import annotations

# 悬空调用补的那条结果正文。照 Numen 的 CLOSED_BEFORE_RESULT。
CLOSED_BEFORE_RESULT = "（这一轮在拿到结果之前就结束了）"

_ROLE = ("system", "user", "assistant", "tool")


def _role(msg) -> str:
    if not isinstance(msg, dict):
        return ""
    r = str(msg.get("role") or "").lower()
    return r if r in _ROLE else ""


def _tool_calls(msg) -> list:
    raw = msg.get("tool_calls") if isinstance(msg, dict) else None
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


def _call_id(call) -> str:
    return str(call.get("id") or "") if isinstance(call, dict) else ""


def _text_of(content) -> str:
    r"""结构化内容仅保留文字，缺失内容返回空文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return str(content)


def _copy(msg: dict) -> dict:
    r"""不修改入参。"""
    out = dict(msg)
    if "content" in out:
        out["content"] = _text_of(out.get("content"))
    return out


def for_wire(messages) -> list:
    r"""不改历史存储；补悬空结果、合并 user、工具结果仅文本。"""
    if not isinstance(messages, list):
        return []
    out: list = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        # 未知 role 的字典保留，避免丢弃框架新增消息类型。
        if not isinstance(msg, dict):
            i += 1
            continue
        role = _role(msg)

        if role == "assistant" and _tool_calls(msg):
            out.append(_copy(msg))
            seen: set[str] = set()
            j = i + 1
            while j < n and _role(messages[j]) == "tool":
                tid = str(messages[j].get("tool_call_id") or "")
                if tid and tid not in seen:
                    out.append(_copy(messages[j]))
                    seen.add(tid)
                j += 1              
            for call in _tool_calls(msg):
                cid = _call_id(call)
                if cid and cid not in seen:
                    out.append({"role": "tool", "tool_call_id": cid,
                                "content": CLOSED_BEFORE_RESULT})
                    seen.add(cid)
            i = j
            continue

        if role == "tool":
            i += 1
            continue

        if role == "user" and out and _role(out[-1]) == "user":
            prev = out[-1]
            a, b = _text_of(prev.get("content")), _text_of(msg.get("content"))
            merged = dict(prev)
            merged["content"] = (a + "\n\n" + b) if (a and b) else (a or b)
            out[-1] = merged
            i += 1
            continue

        out.append(_copy(msg))
        i += 1
    return out


def audit(messages) -> list[str]:
    problems: list[str] = []
    if not isinstance(messages, list):
        return [f"历史不是 list（是 {type(messages).__name__}）"]
    pending: dict[str, int] = {}
    last_role = ""
    for idx, msg in enumerate(messages):
        role = _role(msg)
        if not role:
            problems.append(f"第 {idx} 条没有可识别的 role")
            continue
        if role == "tool":
            tid = str(msg.get("tool_call_id") or "")
            if tid in pending:
                pending.pop(tid)
            else:
                problems.append(f"第 {idx} 条 tool 结果没有对应的调用（孤立）")
        elif role == "assistant":
            for call in _tool_calls(msg):
                cid = _call_id(call)
                if cid:
                    pending[cid] = idx
        if role == "user" and last_role == "user":
            problems.append(f"第 {idx} 条与上一条都是 user（相邻 user）")
        if role in ("user", "assistant", "tool"):
            last_role = role
    for cid, idx in pending.items():
        problems.append(f"第 {idx} 条 assistant 的调用 {cid[:12]}… 没有结果（悬空）")
    return problems
