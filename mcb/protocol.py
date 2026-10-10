"""出口净化 —— 把"要发给模型的这一段历史"收成一个纯函数。

抄 Numen 的切分（docs\\21 §0）：记录事实 和 发给模型的样子 是两件事。
历史如实落盘，合法序列只在出口现算。我们两次血案（494 -> 1、478 -> 18 万）
都是把这两件事揉在一起 —— 这个模块只读不写，所以它不可能写坏任何账。

它治的是"发出去的序列不合法" -> 服务商 400，不是那两次血案（见 docs\\21 §1.1 末尾）。
两件事、两个函数，要分两刀切。

## 四条规则（照 Numen ProtocolView，去掉我们没有的那条）

| # | 规则 | 我们这边 |
|---|---|---|
| 1 | 悬空的 tool_call 补一条结果 | 有 —— 没有结果的 tool_call 上游会直接 400 |
| 2 | Halt 本身不发 | 不适用 —— 我们没有 Halt 这个消息类型（那是 Numen 的密封联合） |
| 3 | 相邻 user 合并 | 有 —— AstrBot 的 truncator 不做这条（见下） |
| 4 | 工具结果只发文字 | 有 —— 结构化数据不进请求 |

## 注意： 接在哪里（这条决定了它是"止血"还是"补强"）

AstrBot 自己已经做了半套配对：`core/agent/context/truncator.py::fix_messages`
（每个截断方法末尾都 return 它）会把"没有结果的 assistant(tool_calls)"整条丢掉、
把孤立的 tool 结果丢掉。所以规则 1 我们跟它重叠，区别只是取舍：
它是"把这次调用抹掉"，我们是"补一条结果说明"（更忠实，模型知道自己调过）。

它不做相邻 user 合并 —— 规则 3 是我们真加的那一条。

注意： 而它在 runner 里跑（拿到 provider 请求之前），我们的接入点在它之前。
所以按 docs\\25 §四 2.1 的原话："位置必须覆盖截断之后的最终请求，否则净化后又被框架截断仍会坏"。
我们把能做的做了，剩下那半（在截断之后）需要动框架内部 —— 按 docs\\25 11.2 的明确要求
（"不要更改框架文件或存储历史来修请求视图"）不做，改由 audit() 在真实流量里体检查着。
"""

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
    """assistant 身上的 tool_calls。读不到回空列表（不猜）。"""
    raw = msg.get("tool_calls") if isinstance(msg, dict) else None
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


def _call_id(call) -> str:
    return str(call.get("id") or "") if isinstance(call, dict) else ""


def _text_of(content) -> str:
    """把 content 压成纯文本。

    只发文字（规则 4）：结构化片段一律不进请求。
    content 可能是 str、可能是分片列表（AstrBot 的 TextPart 那类），
    也可能是 None（只有 tool_calls 的 assistant）—— 一律给出 str。
    """
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
    """浅拷贝 —— 绝不改入参（纯函数的底线）。"""
    out = dict(msg)
    if "content" in out:
        out["content"] = _text_of(out.get("content"))
    return out


def for_wire(messages) -> list:
    """把发给模型的那段历史净成合法序列。纯函数：不改入参、不碰存储。

    保证三件事：① 没有悬空的 tool_call ② 没有相邻 user ③ tool 结果只有文字。
    """
    if not isinstance(messages, list):
        return []
    out: list = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        # 不是 dict 的直接跳掉（历史里混进字符串/None 是可能的）。
        # 注意： role 认不出来但确实是 dict 的保留 —— 丢掉可能是丢内容，
        #    而我们不认识的 role 更可能是以后 AstrBot 新加的，不是垃圾。
        if not isinstance(msg, dict):
            i += 1
            continue
        role = _role(msg)

        # ---- assistant(tool_calls)：把它的结果收齐，缺的补上 ----
        if role == "assistant" and _tool_calls(msg):
            out.append(_copy(msg))
            seen: set[str] = set()
            j = i + 1
            while j < n and _role(messages[j]) == "tool":
                tid = str(messages[j].get("tool_call_id") or "")
                if tid and tid not in seen:
                    out.append(_copy(messages[j]))
                    seen.add(tid)
                j += 1              # 重复的 / 没有 id 的结果丢掉
            for call in _tool_calls(msg):
                cid = _call_id(call)
                if cid and cid not in seen:
                    out.append({"role": "tool", "tool_call_id": cid,
                                "content": CLOSED_BEFORE_RESULT})
                    seen.add(cid)
            i = j
            continue

        # ---- 孤立的 tool 结果：没有对应的调用，丢掉 ----
        if role == "tool":
            i += 1
            continue

        # ---- 相邻 user：合成一条，中间空一行 ----
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
    """只读体检：这份历史有哪些地方会不合法。返回给人看的问题列表。

    用途：在真实流量里看它到底会不会发生 —— 先有证据再决定要不要在更深的地方动手
    （docs\\25 11.6："没有任何证据我们撞过那个 400，等真见到再做"）。
    """
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
