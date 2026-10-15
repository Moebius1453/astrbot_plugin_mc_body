
from __future__ import annotations

import time
from collections import deque

# 日志会进入模型上下文，限制容量。
CAPACITY = 400

RENDER_DEFAULT = 10

KIND_LABEL = {
    "task": "任务",
    "reflex": "反射",
    "craft": "合成",
    "body": "身体",
    "chat": "聊天",
    "error": "出错",
}


class Journal:

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._buf: deque[dict] = deque(maxlen=max(20, int(capacity)))
        # 注意： 挤掉的条数要记账（抄 Numen EventQueue.flushDropped）。
        self._dropped = 0


    def add(self, kind: str, text: str) -> None:
        entry = {
            "t": time.strftime("%H:%M:%S"),
            "kind": str(kind),
            "text": str(text).replace("\n", " ").strip(),
        }
        if not entry["text"]:
            return
        if len(self._buf) == self._buf.maxlen:
            self._dropped += 1
        self._buf.append(entry)


    def tail(self, n: int = RENDER_DEFAULT) -> list[dict]:
        k = max(1, int(n))
        return list(self._buf)[-k:]

    def render(self, n: int = RENDER_DEFAULT) -> str:
        rows = self.tail(n)
        if not rows:
            return "（还没有发生什么事）"
        out = []
        if self._dropped:
            out.append(f"（更早的约 {self._dropped} 条已经翻过去了，这只是最近一段）")
        for e in rows:
            label = KIND_LABEL.get(e["kind"], e["kind"])
            out.append(f"[{e['t']}] {label} · {e['text']}")
        return "\n".join(out)

    def __len__(self) -> int:
        return len(self._buf)
