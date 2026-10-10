"""闲逛 —— 没人管她的时候，她自己动一动。

## 为什么要有它

用户 2026-10-11："玩家就应该经常动。"

一个大活人不会站在原地像尊石像。**而且这一层不花 token** —— 这是关键：
"经常动"是身体层的事，不该拿模型调用去买。Numen 的宪法里说得最清楚
（docs/architecture-mind-model.md §一）：身体每 tick 竞价，本能链是硬编码不花 token 的，
"LLM 是出价最低的竞价者"。我们照这个分层：

    身体层（这里）：动 = 寻路 +  Arbitration + 空闲摆头，零 token
    大脑层（以后）：决定做什么 = 事件 + 熟度，才花 token

## 它怎么决定动不动

没有自己的循环 —— 挂在反射那 1 秒一次的节拍上（`ReflexGuard.bind_idle`）。
理由是省一次 RCON 往返：反射每 tick 已经读了 `mcb state`，闲逛直接用它那份。

只有同时满足这些才动：
  1 仲裁层的 walk 通道空着（反射/用户/任务谁都没占）
  2 安静够久了（默认 25 秒没人管她）
  3 她在**地表**（y 不低于地表 1.5 格以上）—— 见下面那条坑
  4 不在水里、不在睡觉

走一段就停一下（到了就释放、重新计时），不是永动机。

## 注意：它现在会挖地板

Baritone 的 `allowBreak` 默认是开的 —— 为了到达目标它允许破坏方块。
所以闲逛**可能一路凿过去**。这正是用户 2026-10-10 问过的"抽风挖地板"
（detail 见 main/docs/18 与 docs/04）。

治本的做法是关掉它（客户端一行 `s.allowBreak.value = false`），
但那要改客户端脚本 + 重启（约 110 秒），而且代价是"地下会经常走不过去"。
用户当时说"有空再慢慢弄"。所以这一版先按能缓解的来：

    **只在地表闲逛**。地下是凿穿最凶的地方，也是"闲逛"最没意义的地方。
    地表短距离（默认 16 格内）一般有现成的路，需要凿的概率小得多。

这条限制不是设计洁癖，是**在没有关掉 allowBreak 之前能做的最大缓解**。
"""

from __future__ import annotations

import math
import random
import time

from astrbot.api import logger

from .arbiter import LEVEL_IDLE

OWNER = "idle"          # 仲裁层里这条声明的 owner 名

# 闲逛的 walk 声明用多长的 TTL。
#
# 注意： 它和反射的 ttl 不同 —— 反射是"保命，短命就够"，
# 闲逛是"这一段路我打算走完"。而 renew_active 会在她真的在走时续期（level >= LEVEL_USER），
# 所以这个值只需要盖住"寻路中途打了个盹"的空档。到了没人续就自己让位。
WANDER_TTL = 20.0

# 目标点最少离她多远。太近了 Baritone 一步就到，看着像在原地抖。
MIN_STEP = 3.0


class IdleGuard:
    """闲逛。反射每 tick 把已经读到的状态交给它，它不自己去问桥。"""

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
        # 主人在这么近的时候，只在她附近小范围挪动 —— 别聊着聊着人走没了。
        self.stay_near_owner = max(0.0, float(stay_near_owner))
        self.near_owner_radius = max(MIN_STEP + 1.0, float(near_owner_radius))
        self.owner_name = str(owner_name or "").strip()
        self.journal = journal

        self._quiet_since: float | None = None   # 通道变空闲的那一刻；None = 还没起算
        self._target: tuple[int, int] | None = None
        self._rand = random.Random()

    # ---- 外部 -----------------------------------------------------------

    def bind_arbiter(self, arbiter) -> None:
        self.arbiter = arbiter

    def note_activity(self) -> None:
        """有人跟她打交道了 —— 安静计时重新起算。

        现在只有聊天上行那条路会调它（用户在公屏/QQ 跟她说话）。
        走路类的指令不用调：它们会占住 walk 通道，tick 里自己就把计时重置了。
        """
        self._quiet_since = None
        self._target = None

    def render(self) -> str:
        """一行摘要，给状态包看。只摆事实。"""
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

    # ---- 每 tick --------------------------------------------------------

    async def tick(self, data: dict) -> None:
        """反射读到的状态原样交给这里 —— 不额外问桥。"""
        if not self.enabled or self.arbiter is None:
            return
        if not isinstance(data, dict) or not data.get("online"):
            self._quiet_since = None
            return

        top = self.arbiter.walk_holder()
        mine = top is not None and top.owner == OWNER
        if top is not None and not mine:
            # 别人在管 —— 让开，安静计时重新起算（她刚被叫去做事）
            self._quiet_since = None
            self._target = None
            return

        if not self._can_wander(data):
            if mine:
                await self._release("环境不适合闲逛（水里/地下/在睡）")
            self._quiet_since = None
            return

        if not mine:
            # 通道空着 —— 攒够安静时间再动
            if self._quiet_since is None:
                self._quiet_since = time.monotonic()
                return
            if time.monotonic() - self._quiet_since < self.after_seconds:
                return
            await self._start(data)
            return

        # 我正在闲逛：到了就收手，重新计时（走一段停一下，别当永动机）
        status = ""
        task = data.get("task")
        if isinstance(task, dict):
            status = str(task.get("status") or "")
        if status != "moving":
            await self._release("这一趟走完了")
            self._quiet_since = time.monotonic()
            return
        # 还在走 —— 续个命，免得 WANDER_TTL 到点把她从半路上拽下来
        await self.arbiter.touch_walk(OWNER, WANDER_TTL)

    # ---- 内部 -----------------------------------------------------------

    def _can_wander(self, data: dict) -> bool:
        if data.get("inWater") or data.get("underWater"):
            return False
        if data.get("sleeping"):
            return False
        y = data.get("y")
        surf = data.get("surfY")
        if isinstance(y, (int, float)) and isinstance(surf, (int, float)):
            # 只在地表闲逛 —— 见模块头注释里 allowBreak 那条坑
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
            # 主人就在旁边：只在她附近挪，别走开
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
        """主人离她多远。读不到回 None（不假设成 0 —— 那会让她以为主人在旁边）。"""
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
