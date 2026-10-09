"""状态日志 —— 白「记得自己刚才干了什么」的那本账。

**为什么要它**：任务层跑起来之后，「她刚才做了什么」不能只活在服务器日志里 ——
**白自己要看得见**。否则用户问「你刚才在干嘛」，她只能瞎猜。

**这本账是给模型看的，不是给运维看的**：条目要短、中文、一眼看懂
「什么时候、干了什么、成没成」。所以**别往里塞大块数据**（整个背包、整张配方表）——
那是 `mc_inventory` / `mc_menu` 的活。

**环形缓冲，写满丢最旧的。** 不是持久化的 —— 用户 2026-10-09 拍板：先存内存。
这意味着**插件热重载会丢**（任务本身也存内存，两者一致）。要持久化是以后的事。
"""

from __future__ import annotations

import time
from collections import deque

# 攒多少条。别调太大 —— 渲染进上下文是要花 token 的。
CAPACITY = 400

# 默认渲染多少条给模型看
RENDER_DEFAULT = 10

# 分类标签。加新分类**必须**在这里登记，否则渲染出来是裸的 kind 名。
KIND_LABEL = {
    "task": "任务",
    "reflex": "反射",
    "craft": "合成",
    "body": "身体",
    "chat": "聊天",
    "error": "出错",
}


class Journal:
    """一条一条的「我干了什么」。**给模型看的那本账。**"""

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._buf: deque[dict] = deque(maxlen=max(20, int(capacity)))

    # ---- 写 -------------------------------------------------------------

    def add(self, kind: str, text: str) -> None:
        """记一条。`kind` 见 `KIND_LABEL`。"""
        entry = {
            "t": time.strftime("%H:%M:%S"),
            "kind": str(kind),
            "text": str(text).replace("\n", " ").strip(),
        }
        if not entry["text"]:
            return
        self._buf.append(entry)

    # ---- 读 -------------------------------------------------------------

    def tail(self, n: int = RENDER_DEFAULT) -> list[dict]:
        """最近 `n` 条，**时间正序**（最旧的在前 —— 读起来才像在讲一件事）。"""
        k = max(1, int(n))
        return list(self._buf)[-k:]

    def render(self, n: int = RENDER_DEFAULT) -> str:
        """渲染成给模型看的中文。空账返回一句人话，不返回空串。"""
        rows = self.tail(n)
        if not rows:
            return "（还没有发生什么事）"
        out = []
        for e in rows:
            label = KIND_LABEL.get(e["kind"], e["kind"])
            out.append(f"[{e['t']}] {label} · {e['text']}")
        return "\n".join(out)

    def __len__(self) -> int:
        return len(self._buf)
