r"""闲逛由身体层执行，不消耗模型调用，且让位于其他通道声明。"""

from __future__ import annotations

import math
import random
import time

from astrbot.api import logger

from .arbiter import LEVEL_IDLE

OWNER = "idle"          

# TTL 覆盖寻路停顿；实际移动由 renew_active 续期。
WANDER_TTL = 20.0

# 目标过近会造成原地抖动。
MIN_STEP = 3.0


class IdleGuard:

    def __init__(
        self,
        bridge,
        arbiter=None,
        *,
        enabled: bool = True,
        after_seconds: float = 25.0,
        radius: float = 16.0,
        stay_near_owner: float = 10.0,
        near_owner_radius: float = 6.0,
        owner_name: str = "",
        journal=None,
    ) -> None:
        self.bridge = bridge
        self.arbiter = arbiter
        self.enabled = bool(enabled)
        self.after_seconds = max(3.0, float(after_seconds))
        self.radius = max(MIN_STEP + 1.0, float(radius))
        self.stay_near_owner = max(0.0, float(stay_near_owner))
        self.near_owner_radius = max(MIN_STEP + 1.0, float(near_owner_radius))
        self.owner_name = str(owner_name or "").strip()
        self.journal = journal

        self._quiet_since: float | None = None   
        self._target: tuple[int, int] | None = None
        self._rand = random.Random()


    def bind_arbiter(self, arbiter) -> None:
        self.arbiter = arbiter

    def note_activity(self) -> None:
        r"""聊天重置安静计时；走路指令由通道占用触发重置。"""
        self._quiet_since = None
        self._target = None

    def render(self) -> str:
        if not self.enabled:
            return "wander=off"
        if self.arbiter is None:
            return "wander=no-arbiter"
        top = self.arbiter.walk_holder()
        if top is not None and top.owner == OWNER:
            return "wander=walking"
        if self._quiet_since is None:
            return "wander=idle"
        waited = time.monotonic() - self._quiet_since
        left = max(0.0, self.after_seconds - waited)
        return f"wander=waiting({left:.0f}s)"


    async def tick(self, data: dict) -> None:
        if not self.enabled or self.arbiter is None:
            return
        if not isinstance(data, dict) or not data.get("online"):
            self._quiet_since = None
            return

        top = self.arbiter.walk_holder()
        mine = top is not None and top.owner == OWNER
        if top is not None and not mine:
            self._quiet_since = None
            self._target = None
            return

        if not self._can_wander(data):
            if mine:
                await self._release("环境不适合闲逛（水里/地下/在睡）")
            self._quiet_since = None
            return

        if not mine:
            if self._quiet_since is None:
                self._quiet_since = time.monotonic()
                return
            if time.monotonic() - self._quiet_since < self.after_seconds:
                return
            await self._start(data)
            return

        status = ""
        task = data.get("task")
        if isinstance(task, dict):
            status = str(task.get("status") or "")
        if status != "moving":
            await self._release("这一趟走完了")
            self._quiet_since = time.monotonic()
            return
        await self.arbiter.touch_walk(OWNER, WANDER_TTL)


    def _can_wander(self, data: dict) -> bool:
        if data.get("inWater") or data.get("underWater"):
            return False
        if data.get("sleeping"):
            return False
        y = data.get("y")
        surf = data.get("surfY")
        if isinstance(y, (int, float)) and isinstance(surf, (int, float)):
            # allowBreak 开启时地下闲逛可能凿穿地形。
            if float(y) < float(surf) - 1.5:
                return False
        return True

    def _pick_target(self, data: dict, owner_dist: float | None) -> tuple[int, int] | None:
        try:
            x = float(data.get("x"))
            z = float(data.get("z"))
        except (TypeError, ValueError):
            return None
        r = self.radius
        if owner_dist is not None and owner_dist <= self.stay_near_owner:
            r = self.near_owner_radius
        for _ in range(8):
            ang = self._rand.uniform(0, 2 * math.pi)
            dist = self._rand.uniform(MIN_STEP, r)
            tx = int(round(x + math.cos(ang) * dist))
            tz = int(round(z + math.sin(ang) * dist))
            if math.hypot(tx - x, tz - z) >= MIN_STEP:
                return (tx, tz)
        return None

    async def _owner_distance(self, data: dict) -> float | None:
        r"""未知距离返回 None，不冒充 0。"""
        if not self.owner_name:
            return None
        try:
            reply = await self.bridge.call(f"mcb where {self.owner_name}")
        except Exception:  # noqa: BLE001 - 查不到就当不知道
            return None
        if not reply.get("ok"):
            return None
        who = reply.get("data") or {}
        if not who.get("online"):
            return None
        try:
            dx = float(who.get("x")) - float(data.get("x"))
            dz = float(who.get("z")) - float(data.get("z"))
        except (TypeError, ValueError):
            return None
        return math.hypot(dx, dz)

    async def _start(self, data: dict) -> None:
        dist = await self._owner_distance(data)
        target = self._pick_target(data, dist)
        if target is None:
            self._quiet_since = time.monotonic()
            return
        self._target = target
        await self.arbiter.claim_walk(
            LEVEL_IDLE, OWNER, f"goto {target[0]} {target[1]}", "闲逛", ttl=WANDER_TTL
        )
        if self.arbiter.last_error is not None:
            logger.info(f"[mc_body] 闲逛下发失败：{self.arbiter.last_error}")
            self._quiet_since = time.monotonic()

    async def _release(self, why: str) -> None:
        self._target = None
        await self.arbiter.release_walk(OWNER)
