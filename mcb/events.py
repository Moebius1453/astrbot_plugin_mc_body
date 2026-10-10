"""事件流 —— 「刚才都发生了什么」（聊天**以外**的事）。

## 和 `uplink.py`（聊天）的分工

| | 管什么 | 谁在录 |
|---|---|---|
| `uplink.py` | **人说的话** | 服务端 `PlayerEvents.chat` → `mcbChatBuf` |
| **本文件** | **挨打 / 死亡 / 进出服 / 背包变动** | 服务端 `mcbEvBuf` |

⚠️ **两张表必须分开** —— 聊天已经在聊天缓冲里带 seq 了，再往事件流里录一份
就是**同一件事记两遍**，白的 `[log]` 里会出现重复。

## 为什么要它（用户 2026-10-10 提的）

> "我找她要东西、给东西，总得让她感知到吧？"

在此之前她是**瞎的**：你扔给她一把剑，她背包里多了东西，但她**不知道**。
现在 `inventoryChanged` 会把这件事记成一行 `得到 diamond_sword×1`。

## 机制

**环形缓冲 + 单调 seq + 按 `sinceId` 增量拉取**（抄 mcpfabric 的 `EventBus`）。
不用推送：推送没法重连、没法去重、断一次就丢；拉取式天然可重放。

⚠️ 服务端**只在拉取时才**把攒着的背包变动落成事件行（没有定时器）——
所以轮询间隔就是背包变动的聚合窗口。间隔越长，一行里的东西越多。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

from astrbot.api import logger

# 拉取间隔。**也是背包变动的聚合窗口** —— 挖矿时不会一条一块石头地刷屏
POLL_INTERVAL = 2.0

# 内存里留多少条给 `mc_events` 查（服务端那边留 600）
KEEP = 60

# 指数退避上限（隧道断了别刷屏）
MAX_BACKOFF_SECONDS = 30.0

class Joins:
    """一条事件**随哪次调用**进上下文（抄 Numen `EventTypes.Delivery.joins`）。"""

    ANY = "any"      # 随便哪次请求都捎上
    OWN = "own"      # 只在"点名她"的那一轮捎上
    NONE = "none"    # 从不随请求走（只躺在 `mc_events` 里等人查）


@dataclass(frozen=True)
class KindSpec:
    """一种事件的**四列声明**（抄 Numen `EventTypes`：**投递方式写进类型表**）。

    ⚠️ **为什么要有这张表**（`docs\\21` §1.4）：
        Numen 的 `EventQueue` 里**没有一处按档名 `switch`** ——
        "这条急不急、要不要叫醒她、进不进聊天流"**全查声明**。
        我们原来是散落的字符串判断，改一条要动好几处。
    """

    label: str                    # 中文标签（**现在就在用**）
    wakes: bool                   # 要不要因此叫醒她跑一轮 agent
    joins: str                    # 随哪次调用进上下文
    cleared_by_interrupt: bool    # 她被打断时，这条要不要清掉
    to_model: bool = True         # 进不进"发给模型看的内容"


# ⚠️⚠️ **本轮只加列、不改行为**（`docs\25` §三 1.7）——
#     后三列现在是**文档**，还没有任何代码读它们。
#     真要接线（把 `wakes` 接进 `uplink._is_wake`）是另一件事，要先过"她凭什么自己醒"那道判据
#     （`docs\21` §7.1.1），**别顺手改了**。
_KINDS: dict[str, KindSpec] = {
    #                      标签     叫醒   随谁走           打断清  进模型
    "hurt":  KindSpec("挨打", False, Joins.ANY,  False),
    "hit":   KindSpec("出手", False, Joins.ANY,  False),
    "death": KindSpec("死亡", False, Joins.ANY,  False),
    "join":  KindSpec("进服", False, Joins.OWN,  False),
    "leave": KindSpec("退服", False, Joins.OWN,  False),
    "inv":   KindSpec("背包", False, Joins.ANY,  False),
}

KIND_LABEL = {k: v.label for k, v in _KINDS.items()}

# 躺超过这个秒数的旧事件，在正文前标一句"这是多久以前的"
STALE_AFTER = 600.0


class EventFeed:
    """轮询服务端事件流，写进状态日志，并留一份给 `mc_events` 查。"""

    def __init__(self, bridge, journal=None, *, poll_interval: float = POLL_INTERVAL,
                 keep: int = KEEP) -> None:
        self.bridge = bridge
        self.journal = journal
        self.poll_interval = max(0.5, float(poll_interval))
        self.keep = max(4, int(keep))

        self._task: asyncio.Task | None = None
        self._last_seq = 0
        self._recent: list[dict] = []
        # ⚠️ 挤掉的条数**要记账**（抄 Numen `EventQueue.flushDropped`）——
        #    原来是 `pop(0)` 一扔了事，**一声不吭**。
        #    后果：她看到的是"最近 60 条"，但**不知道中间漏了**，会把不连续的两件事当因果。
        self._dropped = 0
        self._fail_streak = 0
        # 背包快照（物品 id → 总数）。None = 还没建基线。
        self._inv_snap: dict[str, int] | None = None
        # 第一次拉取**不要**用 0 —— 那会把缓冲里所有历史一次性灌进日志。
        # 先问一次"现在 max 是多少"，从那儿开始跟。
        self._primed = False

    # ---- 生命周期 -------------------------------------------------------

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

    # ---- 轮询 -----------------------------------------------------------

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
            # 只问 max，不取历史 —— 避免插件重载后把陈年旧事重灌一遍日志
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
            self._absorb(raw)
        max_seq = _as_int(data.get("max"))
        if max_seq > self._last_seq:
            self._last_seq = max_seq

        # 背包 diff 跟着同一个循环走 —— 多一次 RCON 调用而已（服务端不读方块，很便宜）
        await self._poll_inventory()

    # ---- 背包 diff（"谁给了我东西"）--------------------------------------

    async def _poll_inventory(self) -> None:
        """背包快照 diff。**这才是"谁给了我东西"的正解。**

        ⚠️⚠️ **为什么不用服务端的 `PlayerEvents.inventoryChanged`**（2026-10-10 实测）：
            那个事件**根本不 fire**。石板一样的证据：
            墓碑取物明明把背包改了（`grave_key×2 → ×1`、多了 `oak_log×3`），
            服务端日志里**一条都没有**，`mcb events` 是 0。
            原因：`/give` 和墓碑走的都是 `Inventory.add()`，
            **绕过 `AbstractContainerMenu` 的槽位监听器**（KubeJS 的钩子挂在后者上）。
            所以改回**拉取式 diff**（`docs/13` §4 原本就是这个方案）：
            **保证有效**，代价只是延迟一个轮询周期。

        ⚠️ 按**物品 id 聚合**，不按格子 —— 我们要回答的是"我多了什么、少了什么"，
        不是"哪个格子动了"。副作用是挖矿会聚成一行 `得到 cobblestone×23`，
        那反而更好读。
        """
        try:
            reply = await self.bridge.call("mcb inventory")
        except Exception:  # noqa: BLE001 - 隧道抖一下不该让整个循环挂
            return
        if not reply.get("ok"):
            return
        data = reply.get("data") or {}
        # ⚠️ 读不全就**别比** —— 半份快照会产生一堆假的"失去"
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
            self._inv_snap = cur        # 第一次只建基线，不报
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

    # ---- 吸收一条 -------------------------------------------------------

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

    # ---- 给她看 ---------------------------------------------------------

    def recent(self, n: int = 20) -> list[dict]:
        return self._recent[-max(1, int(n)):]

    def render(self, n: int = 20) -> str:
        rows = self.recent(n)
        if not rows:
            return "（还没有任何事件 —— 没人挨打、没人进出、背包也没动过）"
        now = time.monotonic()
        lines = [f"· {_age_note(r, now)}{r['line']}" for r in rows]
        if self._dropped:
            # ⭐ **丢弃不许无声** —— 不说的话，她会把"最近 60 条"当成全部，
            #    把中间漏掉的那段当成"什么都没发生"。
            lines.insert(0, f"（更早的约 {self._dropped} 条已经挤掉了，这只是最近的一段）")
        return "\n".join(lines)


# ---- 小工具 -------------------------------------------------------------


def _describe(kind: str, who: str, raw: dict, text: str) -> str:
    """把一条原始事件说成一行中文。**只陈述事实，不做解读。**"""
    label = KIND_LABEL.get(kind, kind)
    bits = [f"[{label}]"]

    if kind == "hurt":
        # ⚠️ 服务端给的 `by` 是**伤害类型**（`mob` / `arrow` / `fall`…），不是攻击者名字。
        #    2026-10-10 实测：KubeJS 递过来的 `DamageSource` **实体访问器一个都没有**
        #    （`getEntity`/`getDirectEntity` 全 notFn），只有 `String(src)` 能抠出类型。
        #    **那就报类型 —— 不编造攻击者。** "摔的"和"怪咬的"处理方式完全不同，类型本身就有用。
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

    # ⚠️ **合并过的事件要把次数说出来** —— 服务端会把"连续同类"的事件并成一条并累加 `n`。
    #    不说的话，一次 253 连击看起来跟"发生了一次"一模一样（溺水那次实测就是 253 条）。
    n = raw.get("n")
    if isinstance(n, (int, float)) and n > 1:
        bits.append(f"×{n:g}")

    return " ".join(bits)


def _g(v: object) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:g}"
    return "?"


def _age_note(row: dict, now: float) -> str:
    """躺太久的旧事件前面标一句"多久以前"（抄 Numen `EventQueue.annotateAge`）。

    ⚠️ 为什么要有：跨重载/断线补发的旧消息，**不标的话会被当成"刚刚发生"**，
    她会去回应十分钟前就结束的事。
    """
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
    """⚠️ Rhino 的 JSON.stringify 会把整数写成浮点（1.0），这里统一收一下。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
