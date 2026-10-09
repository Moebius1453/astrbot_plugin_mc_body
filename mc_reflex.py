"""防御反射 —— 不经过 LLM 的保命循环。

设计来自前人验证过的做法（见 `universal-modder` 的 Kindred 笔记）：
> **"Reflexes over inference"** —— 攻击/撤退、捡东西、吃东西全部不调 LLM 直接执行，
> 只有高层规划和对话才交给模型。

**它踩过的坑就是我们要治的病**：同伴被怪打死，因为只把"敌对实体"当威胁、
而且没有登记"谁在打我"。修复方式是**把挨打算成威胁、记 5 秒、每几 tick 跑一次反射、不调 LLM**。

白 2026-10-09 死于 Drowned（HP 每 10 秒掉 2 点，一路没反应）—— 同一个病。

这套反射只做三件事，都很保守（**宁可停下，不要乱动**）：
  1. **挨打** → 立刻 `mcb stop`（别再顺着原路线一头撞进去），并进入战斗态
  2. **战斗态里继续掉血** → 朝反方向跑一段（`mcb baritone goto`）
  3. **卡住**（在寻路但坐标长时间不动）→ `mcb stop`，别死等

⚠️ 还没做**反击** —— 我们没有可靠的目标选取（准星射线实测经常 MISS）。
Kindred 有 `Senses` 做实体扫描；我们要做得另写一层。**先保命，再谈还手。**
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random

from astrbot.api import logger

# 战斗态持续多久（秒）。这段时间内持续评估要不要跑。
COMBAT_WINDOW_SECONDS = 12.0

# 卡住判定：坐标多少秒没动就算卡住
STUCK_SECONDS = 18.0
STUCK_MIN_MOVE = 1.5

# 怪贴到这个距离内就进入战斗（不等到挨打）—— 抄 Kindred 的修复思路
NEAR_HOSTILE_RANGE = 5.0

# 够得着就打，够不着就过去（抄女仆的 SetWalkTargetFromAttackTargetIfTargetOutOfReach）
ENGAGE_RANGE = 3.0

# 连续多少 tick 看不到怪就脱战（抄女仆的 StopAttackingIfTargetInvalid）
COMBAT_EXIT_TICKS = 5

# 脱战滞后：怪要拉开到 NEAR_HOSTILE_RANGE × 这个倍数才算"走了"。
# 防止怪在边界上晃一下导致"进战/脱战"反复横跳（踩过：刷屏到用户的 QQ 里）。
COMBAT_EXIT_RATIO = 1.6

# 走向目标的节流：别每 tick 都下发一遍 goto
APPROACH_EVERY_TICKS = 2

# 吃饭（抄女仆 MaidHealSelfTask）：饥饿低于这个值就吃
HUNGER_LOW = 16
# 限频 —— 抄女仆的 setMaxCheckRate：吃完隔一会儿再看，别每 tick 查背包
EAT_COOLDOWN_TICKS = 5


class ReflexGuard:
    """挨打就停 / 继续挨打就跑 / 卡住就停。"""

    def __init__(
        self,
        bridge,
        *,
        interval: float = 2.0,
        hp_low: float = 12.0,
        hp_critical: float = 6.0,
        flee_distance: int = 32,
        scan_range: int = 24,
        hunger_low: float = HUNGER_LOW,
        notify=None,
    ) -> None:
        self.bridge = bridge
        self.interval = max(0.5, float(interval))
        self.hp_low = float(hp_low)
        self.hp_critical = float(hp_critical)
        self.flee_distance = max(8, int(flee_distance))
        self.scan_range = max(6, int(scan_range))
        self.hunger_low = float(hunger_low)
        self._notify = notify  # 可选：async callable(str)

        self._task: asyncio.Task | None = None

        self._last_hp: float | None = None
        self._fleeing = False
        self._last_sample: tuple[float, float, float, float] | None = None
        self._stuck_reported = False
        self._fail_streak = 0

        # 战斗状态机
        self._in_combat = False
        self._no_threat_ticks = 0
        self._approach_wait = 0

        # 吃饭
        self._eat_cool = 0
        self._no_food_warned = False

    # ---- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="mc_body_reflex")
        logger.info(
            f"[mc_body] 防御反射已启动：每 {self.interval:g} 秒看一眼，"
            f"血量 <{self.hp_low:g} 警戒、<{self.hp_critical:g} 逃跑；"
            f"卡住 {STUCK_SECONDS:g} 秒自动停"
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        logger.info("[mc_body] 防御反射已停止")

    # ---- 主循环 ---------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
                self._fail_streak = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._fail_streak += 1
                if self._fail_streak in (1, 5, 20):
                    logger.warning(
                        f"[mc_body] 防御反射第 {self._fail_streak} 次失败"
                        f"（{exc}），隧道断了？照常退避"
                    )
                await asyncio.sleep(min(30.0, self.interval * 5))
                continue
            await asyncio.sleep(self.interval)

    async def _tick(self) -> None:
        reply = await self.bridge.call("mcb state")
        if not reply.get("ok"):
            return
        data = reply.get("data") or {}
        if not data.get("online"):
            self._last_hp = None
            self._last_sample = None
            return

        hp = data.get("hp")
        hp = float(hp) if isinstance(hp, (int, float)) else None

        # 挨打判定：血量下降 = 刚刚受伤
        hurt = hp is not None and self._last_hp is not None and hp < self._last_hp
        was_hp = self._last_hp
        self._last_hp = hp

        nearest = await self._nearest_hostile()

        # ---- 战斗状态机（抄女仆：StartAttacking / 走过去 / 挥 / 脱战）----
        if not self._in_combat:
            close = nearest is not None and nearest.get("dist", 1e9) <= NEAR_HOSTILE_RANGE
            if hurt or close:
                self._in_combat = True
                self._no_threat_ticks = 0
                why = "挨打了" if hurt else f"{nearest['name']} 贴到 {nearest['dist']:g} 格"
                # ⚠️ **只写日志，不发 QQ** —— 战斗是自动跑的，用户不需要在聊天里看到它。
                #    只有"白主动要告诉用户的事"才走 _notify（比如"我饿了但没吃的"）。
                logger.warning(f"[mc_body] ⚔ 进入战斗（{why}）")
                if hurt:
                    await self._cmd("mcb stop")   # 别再顺着原路线撞进去
        else:
            # ⚠️ 脱战要**滞后**（hysteresis）：进战是 ≤5 格，脱战得等拉开到 ~8 格。
            #    否则怪在边界上晃一下就是"进战/脱战"来回刷（用户看到的现象）。
            gone = nearest is None or nearest.get("dist", 1e9) > NEAR_HOSTILE_RANGE * COMBAT_EXIT_RATIO
            if gone:
                self._no_threat_ticks += 1
                if self._no_threat_ticks >= COMBAT_EXIT_TICKS:
                    self._in_combat = False
                    self._fleeing = False
                    logger.info("[mc_body] ⚔ 脱战：怪已经拉开或没了")
            else:
                self._no_threat_ticks = 0
                await self._fight(data, nearest, hp)

        # 挨打通知（只在掉血时，不刷屏）
        if hurt:
            logger.warning(f"[mc_body] ⚠ 白挨打了：血量 {was_hp:g} → {hp:g}")

        # 卡住判定
        await self._check_stuck(data)

        # 吃饭 —— 抄女仆 MaidHealSelfTask：饿了就吃，不经过 LLM
        await self._maybe_eat(data)

    async def _maybe_eat(self, data: dict) -> None:
        """饿了就自己吃。判据抄女仆：**看 FoodProperties，不看白名单**（mod 食物自动兼容）。

        抄了女仆这几个细节：
          · 限频（`setMaxCheckRate`）—— 吃完隔几 tick 再看，别每 tick 翻背包
          · 优先用手上/快捷栏里最顶饱的
          · **记住原来手上拿的是什么，吃完换回来**（`memoryHandItemStack`）
        """
        if self._eat_cool > 0:
            self._eat_cool -= 1
            return

        food = data.get("food") or {}
        level = food.get("level")
        if not isinstance(level, (int, float)) or level >= self.hunger_low:
            return
        if self._in_combat:
            return  # 战斗的时候先保命，别停下来啃面包

        best = await self._find_food()
        if best is None:
            if not self._no_food_warned:
                self._no_food_warned = True
                logger.warning(f"[mc_body] 🍖 饿了（{level:g}）但身上没吃的")
                await self._notify_safe(f"我饿了（饱食度 {level:g}），但身上没有食物。")
            return

        slot, name, nutrition = best
        self._no_food_warned = False
        self._eat_cool = EAT_COOLDOWN_TICKS
        logger.info(f"[mc_body] 🍖 饿了（{level:g}）→ 吃 {name}（第 {slot} 格，营养 {nutrition}）")

        # 换到手上 → 吃 → 换回原来那格（抄女仆的 memoryHandItemStack）
        before = data.get("held") if isinstance(data.get("held"), int) else None
        await self._cmd(f"mcb hotbar {slot}")
        await self._cmd("mcb use")
        if before is not None and before != slot:
            await self._cmd(f"mcb hotbar {before}")

    async def _find_food(self) -> tuple[int, str, float] | None:
        """在**快捷栏**里找最顶饱的食物。

        ⚠️ 只能找快捷栏（0-8）—— 背包里的东西要先用 `swap` 挪出来，那一步还没接。
        """
        try:
            reply = await self.bridge.call("mcb inventory")
        except Exception:
            return None
        if not reply.get("ok"):
            return None
        hotbar = (reply.get("data") or {}).get("hotbar") or []
        best = None
        for slot, item in enumerate(hotbar):
            if not isinstance(item, dict):
                continue
            fp = item.get("food")
            if not isinstance(fp, dict):
                continue          # 没有 food 字段 = 不能吃
            nutrition = fp.get("nutrition")
            if not isinstance(nutrition, (int, float)):
                continue
            if best is None or nutrition > best[2]:
                best = (slot, str(item.get("n") or "?"), float(nutrition))
        return best

    async def _fight(self, data: dict, nearest: dict, hp: float | None) -> None:
        """战斗态的一次决策：逃 / 走过去 / 挥。"""
        # 濒死优先逃
        if hp is not None and hp <= self.hp_critical:
            if not self._fleeing:
                await self._flee(data, nearest)
            return
        self._fleeing = False

        dist = nearest.get("dist")
        pos = nearest.get("pos")
        if not isinstance(dist, (int, float)) or not isinstance(pos, list) or len(pos) < 3:
            return
        tx, ty, tz = pos[0], pos[1], pos[2]

        if dist > ENGAGE_RANGE:
            # 够不着 —— 走过去（抄 SetWalkTargetFromAttackTargetIfTargetOutOfReach）
            if self._approach_wait <= 0:
                self._approach_wait = APPROACH_EVERY_TICKS
                logger.info(
                    f"[mc_body] ⚔ {nearest['name']} 在 {dist:g} 格外，走过去 ({tx},{ty},{tz})"
                )
                await self._cmd(f"mcb baritone goto {int(tx)} {int(tz)}")
            else:
                self._approach_wait -= 1
        else:
            # 够得着 —— 挥。冷却由客户端判（抄原版 startAttack 的逻辑）
            await self._cmd(f"mcb attackAt {tx} {ty} {tz}")

    async def _notify_safe(self, text: str) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 通知发不出去：{exc}")

    async def _nearest_hostile(self, close_range: float | None = None) -> dict | None:
        """服务端索敌。返回最近的敌对实体，没有就 None。

        服务端直接查世界（`mcb threats`）—— **不靠客户端准星射线**（实测经常 MISS）。
        """
        try:
            reply = await self.bridge.call(f"mcb threats {int(self.scan_range)}")
        except Exception:
            return None
        if not reply.get("ok"):
            return None
        hostiles = (reply.get("data") or {}).get("hostiles") or []
        best = None
        for h in hostiles:
            dist = h.get("dist")
            if not isinstance(dist, (int, float)):
                continue
            if close_range is not None and dist > close_range:
                continue
            if best is None or dist < best["dist"]:
                best = h
        return best

    # ---- 反射动作 -------------------------------------------------------

    async def _flee(self, data: dict, nearest: dict | None = None) -> None:
        """朝**最近的怪的反方向**跑。没有再退回"刚才来的反方向"。"""
        self._fleeing = True
        try:
            x = float(data.get("x"))
            z = float(data.get("z"))
        except (TypeError, ValueError):
            return

        dx, dz = None, None
        # 首选：直接背离最近的怪
        if nearest is not None and isinstance(nearest.get("pos"), list) and len(nearest["pos"]) >= 3:
            try:
                hx, hz = float(nearest["pos"][0]), float(nearest["pos"][2])
                vx, vz = x - hx, z - hz
                if abs(vx) + abs(vz) > 0.3:
                    dx, dz = vx, vz
            except (TypeError, ValueError):
                dx, dz = None, None

        # 退回：她刚才走过方向的反向（那是"她正在走进去的地方"）
        if dx is None and self._last_sample is not None:
            mx, mz = x - self._last_sample[0], z - self._last_sample[2]
            if abs(mx) + abs(mz) > 0.3:
                dx, dz = -mx, -mz

        if dx is None:
            dx, dz = 1.0, 0.0

        # 加点抖动，免得每次都撞同一面墙
        ang = math.atan2(dz, dx) + random.uniform(-0.5, 0.5)
        tx = int(round(x + math.cos(ang) * self.flee_distance))
        tz = int(round(z + math.sin(ang) * self.flee_distance))

        who = f"躲开 {nearest['name']}" if nearest else "背离来时方向"
        logger.warning(
            f"[mc_body] ⚠ 血量危急 —— {who}，跑 {self.flee_distance} 格到 ({tx},{tz})"
        )
        await self._cmd(f"mcb baritone goto {tx} {tz}")
        # ⚠️ 只写日志，**不发 QQ** —— 自动反射的状态变化不该占用户的聊天正文

    async def _check_stuck(self, data: dict) -> None:
        """在寻路但坐标长时间不动 = 卡住了。"""
        task = data.get("task") or {}
        moving = task.get("available") and task.get("status") == "moving"
        try:
            x, y, z = float(data.get("x")), float(data.get("y")), float(data.get("z"))
        except (TypeError, ValueError):
            return

        loop = asyncio.get_running_loop()
        now = loop.time()

        if not moving:
            self._last_sample = (x, y, z, now)
            self._stuck_reported = False
            return

        if self._last_sample is None:
            self._last_sample = (x, y, z, now)
            return

        lx, ly, lz, lt = self._last_sample
        moved = math.dist((x, y, z), (lx, ly, lz))
        elapsed = now - lt

        if moved >= STUCK_MIN_MOVE:
            # 在动，刷新基准
            self._last_sample = (x, y, z, now)
            self._stuck_reported = False
            return

        if elapsed >= STUCK_SECONDS and not self._stuck_reported:
            self._stuck_reported = True
            logger.warning(
                f"[mc_body] ⚠ 白卡住了：{elapsed:.0f} 秒只挪了 {moved:.1f} 格，已让她停下"
            )
            await self._cmd("mcb stop")
            # ⚠️ 只写日志，**不发 QQ**（同上：自动反射不占聊天正文）

    # ---- 小工具 ---------------------------------------------------------

    async def _cmd(self, command: str) -> None:
        try:
            r = await self.bridge.call(command)
            if not r.get("ok"):
                logger.warning(f"[mc_body] 反射命令 {command!r} 被拒：{r.get('error')}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[mc_body] 反射命令 {command!r} 发送失败：{exc}")

    async def _safe_notify(self, text: str) -> None:
        try:
            await self._notify(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 反射通知发不出去：{exc}")
