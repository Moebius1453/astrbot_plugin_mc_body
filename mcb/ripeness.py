# SPDX-License-Identifier: GPL-3.0-only
r"""主人在线才允许事件唤醒；主人低血量受伤和死亡为急件，自身受伤交给反射，其余事件排队。"""
# 熟度策略根据 Numen EventQueue 改写，增加在线与冷却门控；上游 LGPL-3.0-only。
# 本地 Git 首次记录及事件判断调整：2026-10-11；来源通知更新：2026-10-16。
# 具体来源、修改和许可边界见 ../THIRD_PARTY_NOTICES.md；许可全文见 ../licenses/Numen-LGPL-3.0.txt 和 ../licenses/GPL-3.0.txt。

from __future__ import annotations

import time
from dataclasses import dataclass

from astrbot.api import logger

DEFAULT_HURT_HP_LINE = 8.0

# 默认熟度为 3。
DEFAULT_LEVEL = 3

# 队列满时优先丢非急件，并报告丢弃数。
KEEP = 40


def wait_seconds(level: int) -> float:
    r"""等待阈值为 60000*N^2/3 毫秒。"""
    lv = max(1, min(10, int(level)))
    return 60000.0 * lv * lv / 3.0 / 1000.0


def wakes(kind: str, victim: str, owner: str, char: str) -> bool:
    r"""victim 是受害者；who 是引发者，不能混用。"""
    if kind == "hurt":
        return bool(owner) and victim == owner
    if kind == "death":
        return bool(victim) and victim in (owner, char)
    if kind in ("join", "leave"):
        return bool(owner) and victim == owner
    return False


def is_urgent(kind: str, victim: str, hp, owner: str, char: str,
              hurt_hp_line: float = DEFAULT_HURT_HP_LINE) -> bool:
    if kind == "hurt":
        if not owner or victim != owner:
            return False
        return isinstance(hp, (int, float)) and float(hp) <= float(hurt_hp_line)
    if kind == "death":
        return bool(owner) and victim == owner
    return False


@dataclass
class Waker:
    kind: str
    victim: str        
    who: str           
    text: str
    at: float
    urgent: bool = False


class RipenessDesk:

    def __init__(self, *, level: int = DEFAULT_LEVEL, owner_name: str = "",
                 char_name: str = "Nanako", hurt_hp_line: float = DEFAULT_HURT_HP_LINE,
                 cooldown: float = 60.0, keep: int = KEEP) -> None:
        self.level = max(1, min(10, int(level)))
        self.owner_name = str(owner_name or "").strip()
        self.char_name = str(char_name or "").strip()
        self.hurt_hp_line = float(hurt_hp_line)
        self.cooldown = max(0.0, float(cooldown))
        self.keep = max(4, int(keep))

        self._pending: list[Waker] = []
        self._owner_online = False
        self._last_wake = 0.0
        self._dropped = 0           


    def note_owner(self, online: bool) -> None:
        self._owner_online = bool(online)

    @property
    def owner_online(self) -> bool:
        return self._owner_online


    def add(self, kind: str, victim: str, who: str, text: str, *,
            hp=None, at: float | None = None) -> bool:
        if not wakes(kind, victim, self.owner_name, self.char_name):
            return False
        if kind == "join" and victim == self.owner_name:
            self.note_owner(True)
        if kind == "leave" and victim == self.owner_name:
            self.note_owner(False)
        self._pending.append(Waker(
            kind=kind, victim=victim, who=str(who or ""), text=str(text or ""),
            at=time.monotonic() if at is None else float(at),
            urgent=is_urgent(kind, victim, hp, self.owner_name, self.char_name,
                             self.hurt_hp_line),
        ))
        self._trim()
        return True

    def _trim(self) -> None:
        while len(self._pending) > self.keep:
            idx = next((i for i, w in enumerate(self._pending) if not w.urgent), 0)
            self._pending.pop(idx)
            self._dropped += 1


    def why(self, now: float | None = None) -> str | None:
        if not self._owner_online:
            return None
        if not self._pending:
            return None
        t = time.monotonic() if now is None else float(now)
        if any(w.urgent for w in self._pending):
            # 急件也受冷却，防止每次受击触发一轮模型。
            if self.cooldown and (t - self._last_wake) < self.cooldown:
                return None
            return "urgent"
        if self.cooldown and (t - self._last_wake) < self.cooldown:
            return None
        if len(self._pending) >= self.level:
            return "enough"
        oldest = min(w.at for w in self._pending)
        if (t - oldest) >= wait_seconds(self.level):
            return "long_enough"
        return None


    def take(self) -> list[Waker]:
        out = self._pending
        self._pending = []
        self.note_turn()
        return out

    def note_turn(self) -> None:
        r"""点名与事件唤醒都要重置冷却，避免连续开轮。"""
        self._last_wake = time.monotonic()

    def dropped(self) -> int:
        return self._dropped
    def clear_dropped(self) -> int:
        n = self._dropped
        self._dropped = 0
        return n

    def render(self) -> str:
        if not self._owner_online:
            return f"ripeness=owner-offline(queued {len(self._pending)})"
        why = self.why()
        if why is None:
            left = wait_seconds(self.level)
            if self._pending:
                oldest = min(w.at for w in self._pending)
                left = max(0.0, wait_seconds(self.level) - (time.monotonic() - oldest))
            return f"ripeness=waiting({len(self._pending)}/{self.level}, {left:.0f}s)"
        return f"ripeness=ready({why})"


def render_prompt(entries: list[Waker], dropped: int = 0) -> str:
    lines = [
        "[mc:events] 游戏世界里发生了这些事（是游戏里的事，与用户现实处境无关）：",
        "",
    ]
    for w in entries:
        lines.append(f"  - {w.text}")
    if dropped:
        lines.append(f"  （更早的约 {dropped} 条已经挤掉了，你只看到这几条）")
    lines += [
        "",
        "这些是刚刚自己发生的，没有人对你说话。看一眼，判断要不要做点什么。",
        "没事可做就什么都别做 —— 不要为了交差而找事，也别为了汇报而说话。",
        "真要说的话，一句话就够（会被打到游戏公屏上）。",
    ]
    return "\n".join(lines)
