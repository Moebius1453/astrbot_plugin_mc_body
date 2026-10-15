r"""## 模型（抄原版 Brain 的 memory 抢占 + AltoClef 的 TaskChain）

续期的证据是身体的可观测状态，不是调用方的心情（renew_active 的注释里有理由）。
显式 release_walk 仍然是立刻生效 —— 不照 Numen 的"release 只是不再续命"，
那会让"停"慢一个 TTL 才停。"""

from __future__ import annotations

import json
import time
import uuid

from astrbot.api import logger

# 重要： 纪律（2026-10-11 抄 Numen 定，别破坏）：层是固定的，层内不许有分数。
#   Numen TaskSelector 的 javadoc 标题就是「为什么没有优先级数字」—— 原话：
LEVEL_REFLEX = 0     
LEVEL_USER = 10      
LEVEL_COMBAT = 15    
LEVEL_TASK = 20      
LEVEL_IDLE = 30      

_LEVEL_CN = {LEVEL_REFLEX: "反射", LEVEL_USER: "用户", LEVEL_COMBAT: "战斗",
             LEVEL_TASK: "任务", LEVEL_IDLE: "闲逛"}

# 保命可抢占用户；追怪只能抢占任务。

# TTL 要覆盖寻路暂歇和加工等待，避免正常任务被过早释放。
USER_TTL = 60.0
TASK_TTL = 60.0
# 目标消失后战斗位移需尽快到期。
COMBAT_TTL = 20.0

# following-idle 仍在跟随，必须续期。
ACTIVE_STATUSES = frozenset({"moving", "following-idle"})


class _Claim:
    __slots__ = ("level", "owner", "cmd", "note", "expire_at", "ttl", "token", "no_break")

    def __init__(self, level: int, owner: str, cmd: str | None, note: str,
                 expire_at: float | None = None, ttl: float | None = None) -> None:
        self.level = level
        self.owner = owner
        self.cmd = cmd
        self.note = note
        self.expire_at = expire_at
        self.ttl = ttl
        self.token = ""
        self.no_break = False


class Arbiter:

    def __init__(self, call) -> None:
        self._call = call
        self._walk: list[_Claim] = []
        self._walk_applied: str | None = None
        self._walk_applied_claim: _Claim | None = None
        self._ttl_clock = time.monotonic()
        # 保留下发错误，供工具返回失败原因。
        self.last_error: str | None = None


    async def claim_walk(self, level: int, owner: str, cmd: str | None,
                         note: str = "", ttl: float | None = None, *,
                         no_break: bool = False) -> None:
        r"""cmd=None 是抢占并停下，不是释放；反射必须显式传 ttl。"""
        if ttl is None:
            if level <= LEVEL_REFLEX:
                raise ValueError(
                    "反射级的 walk 声明必须显式带 ttl（FLEE_TTL / STOP_TTL）—— "
                    "见 mcb/arbiter.py 的模块注释"
                )
            if level <= LEVEL_USER:
                ttl = USER_TTL
            elif level <= LEVEL_COMBAT:
                ttl = COMBAT_TTL
            else:
                ttl = TASK_TTL
        expire_at = time.monotonic() + float(ttl)
        self._walk = [c for c in self._walk if c.owner != owner]
        claim = _Claim(level, owner, cmd, note, expire_at, float(ttl))
        claim.no_break = no_break
        self._walk.append(claim)
        self._walk.sort(key=lambda c: c.level)
        await self._sync_walk("claim:" + owner)

    async def touch_walk(self, owner: str, ttl: float) -> bool:
        r"""仅续有路线的声明，不能把短暂停止续成永久锁死。"""
        hit = False
        for c in self._walk:
            if c.owner == owner and c.expire_at is not None and c.cmd is not None:
                c.expire_at = time.monotonic() + float(ttl)
                hit = True
        return hit

    def holds_route(self, owner: str) -> bool:
        return any(c.owner == owner and c.cmd is not None for c in self._walk)

    async def renew_active(self, status: str | None) -> None:
        r"""只为活动的非反射声明续期；被抢占声明冻结 TTL。"""
        now = time.monotonic()
        dt = now - self._ttl_clock
        self._ttl_clock = now
        if dt < 0:                       
            dt = 0.0
        active = str(status or "") in ACTIVE_STATUSES
        for i, c in enumerate(self._walk):
            if c.expire_at is None or c.level < LEVEL_USER:
                continue
            if i == 0:
                if active and c.cmd is not None:
                    c.expire_at = now + (c.ttl or USER_TTL)
            else:
                c.expire_at += dt        

    async def expire_stale(self) -> list[str]:
        r"""调用方须周期调用，不在此另起定时器。"""
        now = time.monotonic()
        stale = [c.owner for c in self._walk
                 if c.expire_at is not None and c.expire_at <= now]
        if not stale:
            return []
        self._walk = [c for c in self._walk
                      if not (c.expire_at is not None and c.expire_at <= now)]
        logger.info(f"[mc_body] walk 声明到期自动让位：{stale}")
        await self._sync_walk("ttl-expired")
        return stale

    async def release_walk(self, owner: str) -> None:
        before = len(self._walk)
        self._walk = [c for c in self._walk if c.owner != owner]
        if len(self._walk) == before:
            return
        await self._sync_walk("release:" + owner)

    def rejected_claim(self, token: str) -> _Claim | None:
        return next((c for c in self._walk
                     if c.token == token and token and c.level >= LEVEL_USER), None)

    async def reject_walk(self, token: str) -> str | None:
        claim = self.rejected_claim(token)
        if claim is None:
            return None
        await self.release_walk(claim.owner)
        return claim.owner

    def walk_holder(self) -> _Claim | None:
        return self._walk[0] if self._walk else None

    def walk_stack(self) -> list[_Claim]:
        return list(self._walk)

    async def _sync_walk(self, why: str) -> None:
        top = self.walk_holder()
        want = top.cmd if top is not None else None
        if want == self._walk_applied and top is self._walk_applied_claim:
            return
        cmd = "mcb stop"
        if want:
            top.token = uuid.uuid4().hex
            payload = {"token": top.token, "owner": top.owner, "cmd": want,
                       "noBreak": top.no_break}
            cmd = "mcb walk " + json.dumps(payload, ensure_ascii=True)
        self.last_error = None
        try:
            _, err = await self._call(cmd)
        except Exception as exc:  # noqa: BLE001 - 下发出错不该炸掉调用方
            self.last_error = str(exc)
            logger.warning(f"[mc_body] 仲裁下发失败（{why}）：{exc}")
            return
        if err:
            self.last_error = str(err)
            logger.warning(f"[mc_body] 仲裁下发被拒（{why}）：{err}")
            return
        self._walk_applied = want
        self._walk_applied_claim = top
        held = f"{_LEVEL_CN.get(top.level, top.level)}:{top.owner}" if top else "空闲"
        logger.info(f"[mc_body] walk 通道 -> {held}"
                    + (f"（{top.cmd}）" if top and top.cmd else "（停）")
                    + f"  [{why}]")


    def render(self) -> str:
        top = self.walk_holder()
        if top is None:
            return "walk=idle"
        held = f"{_LEVEL_CN.get(top.level, top.level)}:{top.owner}"
        waiting = len(self._walk) - 1
        return f"walk={held}" + (f"(+{waiting})" if waiting > 0 else "")

    def describe(self) -> str:
        if not self._walk:
            return "walk 通道：空闲"
        now = time.monotonic()
        out = []
        for i, c in enumerate(self._walk):
            mark = "← 生效" if i == 0 else ""
            lv = _LEVEL_CN.get(c.level, str(c.level))
            ttl = ""
            if c.expire_at is not None:
                ttl = f"（还剩 {max(0.0, c.expire_at - now):.0f}s）"
            out.append(f"  [{lv}] {c.owner}：{c.cmd or '（停）'}{ttl}"
                       + (f" —— {c.note}" if c.note else "") + f" {mark}")
        return "walk 通道：\n" + "\n".join(out)


    async def clear_user_and_task(self) -> int:
        r"""不撤保命声明，避免用户停止指令中断逃跑。"""
        keep = [c for c in self._walk if c.level < LEVEL_USER]
        dropped = len(self._walk) - len(keep)
        self._walk = keep
        if dropped:
            await self._sync_walk("clear user+task")
        return dropped
