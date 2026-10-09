"""防御反射 —— 不经过 LLM 的保命循环。

设计来自前人验证过的做法（见 `universal-modder` 的 Kindred 笔记）：
> **"Reflexes over inference"** —— 攻击/撤退、捡东西、吃东西全部不调 LLM 直接执行，
> 只有高层规划和对话才交给模型。

**它踩过的坑就是我们要治的病**：同伴被怪打死，因为只把"敌对实体"当威胁、
而且没有登记"谁在打我"。修复方式是**把挨打算成威胁、记 5 秒、每几 tick 跑一次反射、不调 LLM**。

白 2026-10-09 死于 Drowned（HP 每 10 秒掉 2 点，一路没反应）—— 同一个病。

这套反射只做四件事（**宁可停下，不要乱动**）：

  1. **挨打** → 立刻 `mcb stop`（别再顺着原路线一头撞进去），并进入战斗态；然后还手
  2. **战斗** → 逃（濒死） / 走过去 / 挥。**姿态由白选**（见下）
  3. **卡住**（在寻路但坐标长时间不动）→ `mcb stop`，别死等
  4. **饿了** → 自己找东西吃（抄女仆 `MaidHealSelfTask`）

**战斗姿态**（`mc_stance` 工具，**白自己决定**）：
  · `defend`（默认）—— 只在**挨打**或**有怪正瞄着我**时才还手，够不着不追
  · `hunt`         —— 主动清怪，**5 格内**有敌对就上去打，够不着会追

**两种姿态共用一条铁律：挨打就还手，不经过白。**
（用户 2026-10-09："被攻击直接反击不需要 AI 决策" —— 保命是反射，不该等模型想明白）
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

# ---- 战斗姿态（**由白自己决定**，见插件里的 `mc_stance` 工具）----------------
#
# 两种姿态，外加一条**两种都适用**的铁律：
#
#   铁律：**挨打就还手，不经过白。**（用户 2026-10-09 定："被攻击直接反击不需要 AI 决策"）
#         —— 保命是反射，不该等模型想明白。
#
#   defend（默认）：只在**挨打**、或**有怪正瞄着我**时才还手；
#                   够得着就挥，够不着**不追**。它不主动挑事。
#   hunt          ：**5 格内**有敌对就上去打，够不着会**追过去**。主动清怪。
STANCE_DEFEND = "defend"
STANCE_HUNT = "hunt"
STANCES = (STANCE_DEFEND, STANCE_HUNT)

# hunt 姿态的索敌半径
HUNT_RANGE = 5.0

# 吃饭（抄女仆 MaidHealSelfTask）：饥饿低于这个值就吃
HUNGER_LOW = 16
# 限频 —— 抄女仆的 setMaxCheckRate：吃完隔一会儿再看，别每 tick 查背包
EAT_COOLDOWN_TICKS = 5

# 吃完之后要等几个反射 tick 才把手上那格换回去。
# ⚠️ **不能立刻换** —— 换手会打断"正在使用"，等于把吃了一半的东西扔了。
EAT_RESTORE_DELAY_TICKS = 2

# 反射循环的任务名 —— 用来识别并掐掉重载留下的孤儿循环（见 `start()`）
_TASK_NAME = "mc_body_reflex"

# ---- 脱战（主动撤）---------------------------------------------------------
#
# 用户 2026-10-09："战斗还应该有一个**主动脱战**，或者说干脆就是脱战往安全地方
# 或者我方向跑。"
#
# 两个方向：
#   safe  —— 背离最近的怪跑一段（原来的行为）
#   owner —— **朝主人跑**（"跟着我"本来就是要的效果，人多的地方也通常更安全）
RETREAT_SAFE = "safe"
RETREAT_OWNER = "owner"


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
        stance: str = STANCE_DEFEND,
        owner_name: str = "",
        flee_toward: str = RETREAT_SAFE,
        notify=None,
    ) -> None:
        self.bridge = bridge
        self.interval = max(0.5, float(interval))
        self.hp_low = float(hp_low)
        self.hp_critical = float(hp_critical)
        self.flee_distance = max(8, int(flee_distance))
        self.scan_range = max(6, int(scan_range))
        self.hunger_low = float(hunger_low)
        self.owner_name = str(owner_name or "").strip()
        self.flee_toward = flee_toward if flee_toward in (RETREAT_SAFE, RETREAT_OWNER) else RETREAT_SAFE
        self._notify = notify  # 可选：async callable(str)

        self._task: asyncio.Task | None = None

        self._last_hp: float | None = None
        self._fleeing = False
        self._last_sample: tuple[float, float, float, float] | None = None
        self._stuck_reported = False
        self._fail_streak = 0

        # 战斗状态机
        self._stance = stance if stance in STANCES else STANCE_DEFEND
        self._in_combat = False
        self._no_threat_ticks = 0
        self._approach_wait = 0

        # 吃饭
        self._eat_cool = 0
        self._no_food_warned = False
        self._restore_slot: int | None = None
        self._restore_wait = 0

    # ---- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        # ⚠️ **孤儿循环防护** —— 踩过：反复重载插件会留下**多个还在跑的反射循环**，
        #    它们各自发 `mcb use`，互相把对方的吃东西进度重置掉
        #    （2026-10-09 实测：客户端在 1 秒内收到 3 次 use）。
        #    起新的之前，先把所有同名旧任务掐掉。
        try:
            for old in asyncio.all_tasks():
                if old is asyncio.current_task():
                    continue
                if old.get_name() == _TASK_NAME and not old.done():
                    logger.warning("[mc_body] 发现上一个反射循环还活着，先掐掉它")
                    old.cancel()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 清理旧反射循环时出错（忽略）：{exc}")

        self._task = asyncio.create_task(self._loop(), name=_TASK_NAME)
        logger.info(
            f"[mc_body] 防御反射已启动：每 {self.interval:g} 秒看一眼，"
            f"姿态 {self._stance}（挨打必还手）；"
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

    # ---- 战斗姿态（由白自己决定）-----------------------------------------

    @property
    def stance(self) -> str:
        return self._stance

    def set_stance(self, mode: str) -> str:
        """切换战斗姿态。返回实际生效的姿态；不认得就抛 ValueError。

        **这是白自己的决定**（用户 2026-10-09："是否攻击怪物这个状态应该由白自己决定"）。
        改姿态时**重置战斗态** —— 免得带着上一个姿态的判断继续跑。
        """
        m = str(mode or "").strip().lower()
        if m not in STANCES:
            raise ValueError(f"未知的战斗姿态：{mode!r}（只认 {' / '.join(STANCES)}）")
        if m != self._stance:
            logger.info(f"[mc_body] ⚔ 战斗姿态 {self._stance} → {m}")
        self._stance = m
        self._in_combat = False
        self._no_threat_ticks = 0
        self._fleeing = False
        return m

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

        # 一次索敌，两个用途（别再查第二遍 —— 每 tick 一次 RCON 往返已经够了）
        hostiles = await self._hostiles()
        nearest = hostiles[0] if hostiles else None
        targeting = next((h for h in hostiles if h.get("targeting")), None)

        # ---- 战斗状态机（抄女仆：StartAttacking / 走过去 / 挥 / 脱战）----
        if not self._in_combat:
            why = self._combat_trigger(hurt, nearest, targeting)
            if why:
                self._in_combat = True
                self._no_threat_ticks = 0
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
                # 只有 hunt 姿态才**追出去**；defend 姿态够不着就站着等它过来。
                await self._fight(data, nearest, hp, chase=(self._stance == STANCE_HUNT))

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

        ⚠️⚠️ **2026-10-09 血泪教训：绝不能重发 `use`。**
        每发一次，服务端的 `useItemRemaining` 就被**重置回满值**（面包 32），
        永远数不到 0 —— 也就永远吃不完。实测服务端 remain 在 32↔31 之间反复跳。
        所以这里：**服务端说她已经在使用，就什么都别做。**
        """
        # 先把"用过之后把手换回去"这件事收尾（见 _restore_hand）
        await self._restore_hand()

        if self._eat_cool > 0:
            self._eat_cool -= 1
            return

        food = data.get("food") or {}
        level = food.get("level")
        if not isinstance(level, (int, float)) or level >= self.hunger_low:
            return
        if self._in_combat:
            return  # 战斗的时候先保命，别停下来啃面包

        # ⚠️ 关键守卫：**她正在吃东西就别再下令**。
        #    重发 = 重置进度 = 永远吃不完（这就是之前"吃不动"的真正原因）。
        using = data.get("using") or {}
        if using.get("isUsing"):
            logger.debug(
                f"[mc_body] 🍖 她正在使用 {using.get('item')}（remain={using.get('remain')}），不打断"
            )
            return

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

        # 换到手上 → 吃。
        # ⚠️ **换回原来那格必须等吃完** —— 立刻换回去等于把吃了一半的东西扔掉。
        before = data.get("held") if isinstance(data.get("held"), int) else None
        await self._cmd(f"mcb hotbar {slot}")
        await self._cmd("mcb use")
        if before is not None and before != slot:
            self._restore_slot = before
            self._restore_wait = EAT_RESTORE_DELAY_TICKS
        else:
            self._restore_slot = None

    async def _restore_hand(self) -> None:
        """吃完了把手换回原来那格（抄女仆 `memoryHandItemStack`）。"""
        if self._restore_slot is None:
            return
        if self._restore_wait > 0:
            self._restore_wait -= 1
            return
        slot = self._restore_slot
        self._restore_slot = None
        await self._cmd(f"mcb hotbar {slot}")

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

    def _combat_trigger(
        self, hurt: bool, nearest: dict | None, targeting: dict | None
    ) -> str | None:
        """现在该不该进入战斗态？返回理由（给日志用），None = 不用。

        **两种姿态共用同一条铁律：挨打就还手，不经过白。**
        （用户 2026-10-09："被攻击直接反击不需要 AI 决策"）
        """
        if hurt:
            return "挨打了"

        if self._stance == STANCE_HUNT:
            # 主动清怪：5 格内有敌对就上去打（不管它瞄不瞄我）
            if nearest is not None and nearest.get("dist", 1e9) <= HUNT_RANGE:
                return f"{nearest.get('name')} 在 {nearest.get('dist'):g} 格内（hunt 主动出击）"
            return None

        # defend：不主动挑事，但**谁正瞄着我**算威胁 —— 先下手为强。
        # 抄前人验证过的做法（Kindred 的坑 #1）：只算"敌对实体"不够，
        # 得知道**谁在盯着我**，否则怪站在旁边不动手时会被当成无害。
        if targeting is not None and targeting.get("dist", 1e9) <= NEAR_HOSTILE_RANGE:
            return f"{targeting.get('name')} 正瞄着我，贴到 {targeting.get('dist'):g} 格"
        return None

    async def _nearest_targeting(self) -> dict | None:
        """最近的那个**正瞄着白**的敌对实体。defend 姿态用它当开战判据。"""
        hostiles = await self._hostiles()
        return next((h for h in hostiles if h.get("targeting")), None)

    async def _fight(
        self, data: dict, nearest: dict, hp: float | None, *, chase: bool = True
    ) -> None:
        """战斗态的一次决策：逃 / 走过去 / 挥。

        `chase=False`（defend 姿态）：够不着就**站着等**，不追出去。
        `chase=True`（hunt 姿态）：够不着就走过去（抄女仆的
        `SetWalkTargetFromAttackTargetIfTargetOutOfReach`）。
        """
        # 濒死优先撤（**自动**脱战，选的方向由配置定）
        if hp is not None and hp <= self.hp_critical:
            if not self._fleeing:
                await self._retreat(data, nearest, self.flee_toward)
            return
        self._fleeing = False

        dist = nearest.get("dist")
        pos = nearest.get("pos")
        if not isinstance(dist, (int, float)) or not isinstance(pos, list) or len(pos) < 3:
            return
        tx, ty, tz = pos[0], pos[1], pos[2]

        if dist > ENGAGE_RANGE:
            if not chase:
                # defend：不追。只在她**已经开始走过去**时才知道停 —— 这里什么都不做。
                return
            # 够不着 —— 走过去
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

    async def _hostiles(self) -> list[dict]:
        """服务端索敌，返回**按距离排序**的敌对实体列表。

        服务端直接查世界（`mcb threats`）—— **不靠客户端准星射线**（实测经常 MISS）。
        每条带 `dist` / `pos` / `name` / `hp`，以及 `targeting`（**它是不是正瞄着我**）。
        """
        try:
            reply = await self.bridge.call(f"mcb threats {int(self.scan_range)}")
        except Exception:
            return []
        if not reply.get("ok"):
            return []
        hostiles = (reply.get("data") or {}).get("hostiles") or []
        out = [h for h in hostiles if isinstance(h, dict) and isinstance(h.get("dist"), (int, float))]
        out.sort(key=lambda h: h["dist"])
        return out

    # ---- 反射动作 -------------------------------------------------------

    async def retreat(self, toward: str = RETREAT_SAFE) -> str:
        """**主动脱战** —— 立刻停止战斗，往安全方向 / 主人方向撤。

        和"濒死才跑"不同：这个是**主动**的，血量好好的也能用 ——
        打不过、不想打、或者白自己判断该走了，都可以调它。

        返回一句人话（给工具层直接回给白）。
        """
        t = str(toward or "").strip().lower() or RETREAT_SAFE
        if t not in (RETREAT_SAFE, RETREAT_OWNER):
            raise ValueError(
                f"未知的撤离方向：{toward!r}（只认 {RETREAT_SAFE} / {RETREAT_OWNER}）"
            )

        # 退出战斗态 —— 否则下一次 tick 又会把她拉回去打
        self._in_combat = False
        self._no_threat_ticks = 0
        self._fleeing = False
        self._approach_wait = 0

        data = await self._state()
        if not data.get("online"):
            return "她不在线，撤不了。"
        hostiles = await self._hostiles()
        return await self._retreat(data, hostiles[0] if hostiles else None, t)

    async def _retreat(self, data: dict, nearest: dict | None, toward: str) -> str:
        """真正下撤离指令。返回一句人话。"""
        # 优先：往主人那边跑（人多的地方通常更安全，而且"跟着我"本来就是用户要的）
        if toward == RETREAT_OWNER:
            owner = await self._where(self.owner_name)
            if owner is not None and owner.get("dim") == data.get("dim"):
                try:
                    tx, tz = int(owner["x"]), int(owner["z"])
                except (KeyError, TypeError, ValueError):
                    tx = tz = None
                if tx is not None:
                    # ⚠️ 用 **follow** 而不是 goto —— 主人在动，goto 是快照，追不上。
                    #    follow 跟到几格以内就停，正好是"会合"的语义。
                    logger.warning(
                        f"[mc_body] 🏃 脱战 —— 撤向主人 {self.owner_name}"
                        f"（follow，他当前在 {tx},{tz}）"
                    )
                    await self._cmd(f"mcb baritone follow player {self.owner_name}")
                    return "正往你那边跑（会一直跟到你身边）"
            logger.warning(
                f"[mc_body] 🏃 脱战时找不到 {self.owner_name}（不在线/不在同维度），"
                "改往安全方向跑"
            )

        # 安全方向：背离最近的怪
        self._fleeing = True
        try:
            x = float(data.get("x"))
            z = float(data.get("z"))
        except (TypeError, ValueError):
            return "读不到坐标，撤不了。"

        dx, dz = None, None
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
            f"[mc_body] 🏃 脱战 —— {who}，跑 {self.flee_distance} 格到 ({tx},{tz})"
        )
        await self._cmd(f"mcb baritone goto {tx} {tz}")
        # ⚠️ 只写日志，**不发 QQ** —— 自动反射的状态变化不该占用户的聊天正文
        return f"往安全方向跑（x={tx} z={tz}）"

    async def _state(self) -> dict:
        """读一次服务端状态。失败回空 dict（调用方按 online 判）。"""
        try:
            reply = await self.bridge.call("mcb state")
        except Exception:
            return {}
        if not reply.get("ok"):
            return {}
        return reply.get("data") or {}

    async def _where(self, name: str) -> dict | None:
        """查**任意**玩家的位置（服务端真值）。不在线返回 None。"""
        if not name:
            return None
        try:
            reply = await self.bridge.call(f"mcb where {name}")
        except Exception:
            return None
        if not reply.get("ok"):
            return None
        data = reply.get("data") or {}
        return data if data.get("online") else None

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
