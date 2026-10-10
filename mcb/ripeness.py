"""熟度 —— 她什么时候该自己醒一轮。

## 抄什么（Numen EventQueue.ripeness，依据 docs\\21 §四 / §7.1.1）

三种理由之一就开一轮，按顺序判：

    1 URGENT      有急件                  -> 立刻
    2 ENOUGH      叫醒她的条目攒够 N 条    -> N 就是档位
    3 LONG_ENOUGH 最老那条躺够 60000*N^2/3 毫秒
                                          1 档约 20 秒 · 3 档 3 分钟 · 10 档约 33 分钟

三条用的是**同一段条目**（还没交出去的那些），所以熟了开出来的那一轮一定取得到东西。

## 我们加的那条总闸：主人在线

用户 2026-10-11 定："进出服务器来裁定这个不停跑的进程应不应该关掉。"

这条不是照抄，是我们自己的立场，因为我们的定位是**陪玩**不是自主同伴
（docs\\21 §7.1.1：Numen 的纯计时判据成立的前提是"她自己在玩"）。
"用户不在场，她醒来说给谁听？" —— 所以主人在线才醒；不在就只进队列，
等他回来那一下（join 会翻转这道闸）积压的条目自己就熟了。

## 哪条算"叫醒她的条目"

`wakes()` 和 `is_urgent()` 是纯函数，判据只有一条：**"用户此刻不在场、或不关心，
这次唤醒还有意义吗？"** 没意义就不该醒（docs\\21 §7.1.1 原话）。

- 主人挨打 —— 有具体的事、而且天然"他在场"（他刚挨打）。够格。
  血线以上只是记一笔；掉到 `hurt_hp_line` 以下才算急件（Numen 的 owner_hurt "按血线"）。
- 她自己挨打 —— **不叫醒**。反射层管这个，而且不花 token。
  用 LLM 去做反射能做的事，是最贵的错（docs\\21 §7.1.1 那张表）。
- 死亡 / 进出服 —— 主人那几条够格；她自己的死亡记下但不急。
- 背包变动 / 权限拒绝 / 出手 —— 不叫醒，攒着等下次有人叫她时一起带上。

## 冷却

急件也不许连续炸。`cooldown` 秒内已经醒过就不再醒 —— 否则主人在怪堆里，
每一击都是一轮 agent。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from astrbot.api import logger

# 主人挨打掉到这个血线以下才算急件
DEFAULT_HURT_HP_LINE = 8.0

# 默认档位。和 Numen 一样是 3（≈ 攒 3 条或等 3 分钟）
DEFAULT_LEVEL = 3

# 队列上限。满了丢最老的**非急件**；全是急件就丢最老的。丢了要记账。
KEEP = 40


def wait_seconds(level: int) -> float:
    """这一档"最老一条躺够多久"就开一轮。抄 Numen：60000*N^2/3 毫秒。"""
    lv = max(1, min(10, int(level)))
    return 60000.0 * lv * lv / 3.0 / 1000.0


def wakes(kind: str, who: str, owner: str, char: str) -> bool:
    """这一条算不算"叫醒她的条目"（只有这些参与熟度）。"""
    if kind == "hurt":
        return bool(owner) and who == owner
    if kind == "death":
        return who in (owner, char) and bool(who)
    if kind in ("join", "leave"):
        return bool(owner) and who == owner
    return False


def is_urgent(kind: str, who: str, hp, owner: str, char: str,
              hurt_hp_line: float = DEFAULT_HURT_HP_LINE) -> bool:
    """急件 = 立刻叫醒她，不看熟度。"""
    if kind == "hurt":
        if not owner or who != owner:
            return False
        # 血线以上只是记一笔 —— 挨一下就叫醒会把 token 烧在"哦你被打了"上
        return isinstance(hp, (int, float)) and float(hp) <= float(hurt_hp_line)
    if kind == "death":
        # 只有主人死了算急件；她自己死了解不开（死着不能动），记下就行
        return bool(owner) and who == owner
    return False


@dataclass
class Waker:
    kind: str
    who: str
    text: str
    at: float
    urgent: bool = False


class RipenessDesk:
    """攒着"叫醒她的条目"，按熟度决定什么时候开一轮。"""

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
        self._dropped = 0           # 被挤掉的条数，要记账

    # ---- 状态 -----------------------------------------------------------

    def note_owner(self, online: bool) -> None:
        """主人上下线。这就是那道总闸 —— 用户 2026-10-11 定的。"""
        self._owner_online = bool(online)

    @property
    def owner_online(self) -> bool:
        return self._owner_online

    # ---- 收条目 ---------------------------------------------------------

    def add(self, kind: str, who: str, text: str, *, hp=None, at: float | None = None) -> bool:
        """记一条事件。返回"它够不够格叫醒她"（不管此刻醒不醒得了）。"""
        if not wakes(kind, who, self.owner_name, self.char_name):
            return False
        if kind == "join" and who == self.owner_name:
            # 主人回来了 —— 开闸。积压的条目马上会因为 LONG_ENOUGH 自己熟
            self.note_owner(True)
        if kind == "leave" and who == self.owner_name:
            self.note_owner(False)
        self._pending.append(Waker(
            kind=kind, who=who, text=str(text or ""),
            at=time.monotonic() if at is None else float(at),
            urgent=is_urgent(kind, who, hp, self.owner_name, self.char_name,
                             self.hurt_hp_line),
        ))
        self._trim()
        return True

    def _trim(self) -> None:
        while len(self._pending) > self.keep:
            # 先丢最老的非急件；全是急件才丢最老的
            idx = next((i for i, w in enumerate(self._pending) if not w.urgent), 0)
            self._pending.pop(idx)
            self._dropped += 1

    # ---- 判熟 -----------------------------------------------------------

    def why(self, now: float | None = None) -> str | None:
        """熟了返回理由（'urgent' / 'enough' / 'long_enough'），没熟返回 None。"""
        if not self._owner_online:
            return None
        if not self._pending:
            return None
        t = time.monotonic() if now is None else float(now)
        if any(w.urgent for w in self._pending):
            # 急件也要过冷却 —— 否则主人在怪堆里，每一击都是一轮 agent
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

    # ---- 取走 -----------------------------------------------------------

    def take(self) -> list[Waker]:
        """把攒着的条目全取走（取走即清）。"""
        out = self._pending
        self._pending = []
        self._last_wake = time.monotonic()
        return out

    def dropped(self) -> int:
        return self._dropped

    def clear_dropped(self) -> int:
        n = self._dropped
        self._dropped = 0
        return n

    def render(self) -> str:
        """一行摘要，给状态包看。只摆事实。"""
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
    """把攒着的事件拼成给她的那一段（纯函数，好单测）。"""
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
