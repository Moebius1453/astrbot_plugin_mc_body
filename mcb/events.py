r"""环形缓冲 + 单调 seq + 按 sinceId 增量拉取（抄 mcpfabric 的 EventBus）。
不用推送：推送没法重连、没法去重、断一次就丢；拉取式天然可重放。"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

from astrbot.api import logger

POLL_INTERVAL = 2.0

KEEP = 60

MAX_BACKOFF_SECONDS = 30.0

class Joins:
    r"""一条事件随哪次调用进上下文（抄 Numen EventTypes.Delivery.joins）。"""

    ANY = "any"      
    OWN = "own"      
    NONE = "none"    


@dataclass(frozen=True)
class KindSpec:
    """一种事件的四列声明（抄 Numen EventTypes：投递方式写进类型表）。

    注意： 为什么要有这张表（docs\\21 §1.4）：
        Numen 的 EventQueue 里没有一处按档名 switch ——
        "这条急不急、要不要叫醒她、进不进聊天流"全查声明。
        我们原来是散落的字符串判断，改一条要动好几处。
    """

    label: str                    
    wakes: bool                   
    joins: str                    
    cleared_by_interrupt: bool    
    to_model: bool = True         


# 后三列尚无消费者，只是声明，不改变投递行为。
_KINDS: dict[str, KindSpec] = {
    "hurt":  KindSpec("挨打", False, Joins.ANY,  False),
    "hit":   KindSpec("出手", False, Joins.ANY,  False),
    "death": KindSpec("死亡", False, Joins.ANY,  False),
    "join":  KindSpec("进服", False, Joins.OWN,  False),
    "leave": KindSpec("退服", False, Joins.OWN,  False),
    "inv":   KindSpec("背包", False, Joins.ANY,  False),
    "permission": KindSpec("权限", False, Joins.ANY, False),
}

KIND_LABEL = {k: v.label for k, v in _KINDS.items()}

STALE_AFTER = 600.0


class EventFeed:

    def __init__(self, bridge, journal=None, *, poll_interval: float = POLL_INTERVAL,
                 keep: int = KEEP, on_permission=None, on_event=None, on_tick=None) -> None:
        self.bridge = bridge
        self.journal = journal
        self.on_permission = on_permission
        self.on_event = on_event
        self.on_tick = on_tick
        self.poll_interval = max(0.5, float(poll_interval))
        self.keep = max(4, int(keep))

        self._task: asyncio.Task | None = None
        self._last_seq = 0
        self._recent: list[dict] = []
        # 注意： 挤掉的条数要记账（抄 Numen EventQueue.flushDropped）——
        self._dropped = 0
        self._fail_streak = 0
        self._inv_snap: dict[str, int] | None = None
        # 首次从当前 max 建游标，避免重灌历史。
        self._primed = False


    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="mc_body_events")
        logger.info(f"[mc_body] 事件流已启动：每 {self.poll_interval:g} 秒拉一次")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        logger.info("[mc_body] 事件流已停止")


    async def _loop(self) -> None:
        while True:
            try:
                await self._poll_once()
                self._fail_streak = 0
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 隧道断/服务端忙都走这里
                self._fail_streak += 1
                delay = min(MAX_BACKOFF_SECONDS,
                            self.poll_interval * (2 ** min(self._fail_streak, 5)))
                if self._fail_streak in (1, 5, 20):
                    logger.warning(
                        f"[mc_body] 事件流第 {self._fail_streak} 次失败（{exc}），"
                        f"退避 {delay:.0f} 秒"
                    )
                await asyncio.sleep(delay)

    async def _poll_once(self) -> None:
        if not self._primed:
            reply = await self.bridge.call("mcb events 0")
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error") or "mcb events 返回失败")
            self._last_seq = _as_int((reply.get("data") or {}).get("max"))
            self._primed = True
            return

        reply = await self.bridge.call(f"mcb events {self._last_seq}")
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "mcb events 返回失败")
        data = reply.get("data") or {}
        for raw in (data.get("lines") or []):
            fresh = isinstance(raw, dict) and _as_int(raw.get("seq")) > self._last_seq
            self._absorb(raw)
            if not fresh:
                continue
            if raw.get("kind") == "permission" and self.on_permission is not None:
                await self.on_permission(raw)
            if self.on_event is not None:
                with contextlib.suppress(Exception):
                    self.on_event(raw)
        max_seq = _as_int(data.get("max"))
        if max_seq < self._last_seq:
            # 服务端重启会归零 seq，必须允许游标回退。
            logger.warning(
                f"[mc_body] 服务端事件 seq 从 {self._last_seq} 退回 {max_seq}"
                "（服务端重启或 /reload），游标归零重新取"
            )
            self._last_seq = 0
        elif max_seq > self._last_seq:
            self._last_seq = max_seq

        await self._poll_inventory()

        # 熟度复用轮询心跳，不另起循环。
        if self.on_tick is not None:
            with contextlib.suppress(Exception):
                await self.on_tick()


    async def _poll_inventory(self) -> None:
        r"""Inventory.add 绕过槽位监听，需快照 diff；按物品 ID 聚合。"""
        try:
            reply = await self.bridge.call("mcb inventory")
        except Exception:  # noqa: BLE001 - 隧道抖一下不该让整个循环挂
            return
        if not reply.get("ok"):
            return
        data = reply.get("data") or {}
        # 不完整快照不能 diff，否则会虚报失去物品。
        if data.get("err"):
            return

        cur: dict[str, int] = {}
        for value in data.values():
            if not isinstance(value, list):
                continue
            for it in value:
                if not isinstance(it, dict):
                    continue
                iid = str(it.get("id") or "")
                if not iid:
                    continue
                try:
                    n = int(float(it.get("c") or 0))
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    cur[iid] = cur.get(iid, 0) + n

        if self._inv_snap is None:
            self._inv_snap = cur        
            return

        got: list[str] = []
        lost: list[str] = []
        for iid, n in cur.items():
            d = n - self._inv_snap.get(iid, 0)
            if d > 0:
                got.append(f"{iid}×{d}")
        for iid, n in self._inv_snap.items():
            d = n - cur.get(iid, 0)
            if d > 0:
                lost.append(f"{iid}×{d}")
        self._inv_snap = cur
        if not got and not lost:
            return

        bits = []
        if got:
            bits.append("得到 " + "、".join(sorted(got)))
        if lost:
            bits.append("失去 " + "、".join(sorted(lost)))
        self._absorb({"kind": "inv", "who": "", "text": " · ".join(bits)})


    def _absorb(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        seq = _as_int(raw.get("seq"))
        if seq and seq > self._last_seq:
            self._last_seq = seq

        kind = str(raw.get("kind") or "?")
        who = str(raw.get("who") or "?")
        text = str(raw.get("text") or "").strip()
        if not text:
            return

        line = _describe(kind, who, raw, text)
        self._recent.append({"seq": seq, "kind": kind, "who": who, "text": text,
                             "line": line, "t": time.monotonic()})
        while len(self._recent) > self.keep:
            self._recent.pop(0)
            self._dropped += 1
        if self.journal is not None:
            self.journal.add("body", line)


    def recent(self, n: int = 20) -> list[dict]:
        return self._recent[-max(1, int(n)):]

    def render(self, n: int = 20) -> str:
        rows = self.recent(n)
        if not rows:
            return "（还没有任何事件 —— 没人挨打、没人进出、背包也没动过）"
        now = time.monotonic()
        lines = [f"· {_age_note(r, now)}{r['line']}" for r in rows]
        if self._dropped:
            # 报告挤掉的条数，避免把片段误当完整历史。
            lines.insert(0, f"（更早的约 {self._dropped} 条已经挤掉了，这只是最近的一段）")
        return "\n".join(lines)




def _describe(kind: str, who: str, raw: dict, text: str) -> str:
    label = KIND_LABEL.get(kind, kind)
    bits = [f"[{label}]"]

    if kind == "hurt":
        # by 是伤害类型，不是攻击者名字。
        by = raw.get("by") or who
        dmg = raw.get("dmg")
        hp = raw.get("hp")
        victim = str(text).replace(" 挨打", "").strip() or str(text)
        bits.append(victim)
        if by and str(by) not in ("环境", "?", ""):
            bits.append(f"← {by}")
        if isinstance(dmg, (int, float)):
            bits.append(f"-{dmg:g}hp")
        if isinstance(hp, (int, float)):
            bits.append(f"剩 {hp:g}")
    elif kind == "hit":
        bits.append(str(text))
        dmg = raw.get("dmg")
        if isinstance(dmg, (int, float)):
            bits.append(f"-{dmg:g}hp")
    elif kind in ("join", "leave"):
        bits.append(str(text))
        pos = raw.get("pos")
        if kind == "join" and isinstance(pos, list) and len(pos) == 3:
            bits.append(f"@({_g(pos[0])},{_g(pos[1])},{_g(pos[2])})")
    elif kind == "inv":
        bits.append(str(text))
    else:
        bits.append(str(text))

    # 合并事件保留次数，不能显示成单次。
    n = raw.get("n")
    if isinstance(n, (int, float)) and n > 1:
        bits.append(f"×{n:g}")

    return " ".join(bits)


def _g(v: object) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:g}"
    return "?"


def _age_note(row: dict, now: float) -> str:
    r"""躺太久的旧事件前面标一句"多久以前"（抄 Numen EventQueue.annotateAge）。"""
    t = row.get("t")
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        return ""
    age = now - float(t)
    if age < STALE_AFTER:
        return ""
    if age < 3600:
        return f"[{age / 60:.0f} 分钟前] "
    return f"[{age / 3600:.1f} 小时前] "


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
