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

from .arbiter import LEVEL_REFLEX

# 战斗态持续多久（秒）。这段时间内持续评估要不要跑。
COMBAT_WINDOW_SECONDS = 12.0

# 卡住判定：坐标多少秒没动就算卡住
STUCK_SECONDS = 18.0
STUCK_MIN_MOVE = 1.5

# 溺水判定 —— ⚠️ **判据是氧气，不是血量**（见 _check_drowning 的说明）
DROWN_AIR = 180.0        # 氧气（满 300）低于它就管
DROWN_TICKS = 3          # 连续几次才算，防抖（刚扎个猛子不该触发）
DROWN_RETRY_TICKS = 10   # 上浮没成功就多久再试一次（别每 tick 重发命令）

# 怪贴到这个距离内就进入战斗（不等到挨打）—— 抄 Kindred 的修复思路
NEAR_HOSTILE_RANGE = 5.0

# 够得着就打，够不着就过去（抄女仆的 SetWalkTargetFromAttackTargetIfTargetOutOfReach）
ENGAGE_RANGE = 3.0

# 连续多少 tick 看不到怪就脱战（抄女仆的 StopAttackingIfTargetInvalid）
COMBAT_EXIT_TICKS = 3

# 脱战滞后：怪要拉开到 NEAR_HOSTILE_RANGE × 这个倍数才算"走了"。
# 防止怪在边界上晃一下导致"进战/脱战"反复横跳（踩过：刷屏到用户的 QQ 里）。
# ⚠️ 2026-10-10 从 1.6 收到 1.25（8.0 格 → 6.25 格）—— 用户："脱战这个看看能不能
#    条件宽松一点点，因为**跑路绑架移动很蛋疼**"。蠹虫贴着她转，1.6 倍永远脱不了战。
COMBAT_EXIT_RATIO = 1.25

# 另一个脱战出口：**连续这么久没挨打**、而且**没有任何东西在瞄着她** → 直接脱战。
# ⚠️ 只看"怪在不在附近"是不够的（原来的判据）：怪站在她旁边发呆，
#    她也被永久锁在战斗态里出不来。这条是"其实已经安全了"的出口。
COMBAT_QUIET_SECONDS = 10.0

# ---- 反射的 walk 声明的生存时间（秒）------------------------------------
#
# 用户 2026-10-10："**反射肯定有作用时间，加个限制不至于无限触发**"。
# 到期由 `arbiter.expire_stale()` 自动让位 —— **不再需要一个"释放"的调用点**，
# 这是"卡住 = 永久锁死"那个 bug 的兜底（见 `mcb/arbiter.py` 的模块注释）。
FLEE_TTL = 15.0      # 逃跑路线：久一点，逃跑本来要跑一会儿
STOP_TTL = 8.0       # "停一下"类（进战刹车 / 卡住 / 溺水）

# ---- 进战自动换武器 --------------------------------------------------------
#
# 用户 2026-10-10："**战斗依然无法切换武器，经常性不会主动换武器**"。
# 实测诊断过：她手上攥着一根 `minecraft:string`，钻石斧/钻石剑全在背包里，
# `mc_attack` 用手上的东西挥 → **伤害 0**（见 `main/docs/13-战斗与物品.md` §2）。
#
# ⚠️ 分数**只看真实攻击力**（服务端 `mcbStackInfo` 的 `atk`）—— **绝不按名字认武器**：
#    显示名不可信（tacz 直接返回本地化 key `item.tacz.modern_kinetic_gun`）。
WEAPON_MIN_GAIN = 1.0    # 新武器至少比手上这把强这么多才换，免得快捷栏里来回倒腾

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
        journal=None,
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

        # 状态日志（见 mcb/journal.py）—— 反射事件要**让白自己看得见**，
        # 不能只躺在服务器日志里（否则用户问"刚才怎么了"她答不上来）。
        self._journal = journal

        # 任务层（`bind_tasks` 注入）—— 保命要能**抢占**它。
        # ⚠️ 是**挂起**不是停：危险过去她会自己接着做（用户 2026-10-09 拍板的分层）。
        self._tasks = None
        # 仲裁层（见 mcb/arbiter.py）。没绑就退回直接下发，行为跟以前一样。
        self.arbiter = None

        self._task: asyncio.Task | None = None

        self._last_hp: float | None = None
        self._fleeing = False
        self._last_sample: tuple[float, float, float, float] | None = None
        self._stuck_reported = False
        self._fail_streak = 0
        self._drown_ticks = 0
        self._drown_cool = 0

        # 战斗状态机
        self._stance = stance if stance in STANCES else STANCE_DEFEND
        self._in_combat = False
        self._no_threat_ticks = 0
        self._approach_wait = 0
        # 距离上次挨打过了多久（秒）—— 脱战的第二个出口（COMBAT_QUIET_SECONDS）
        self._no_hurt_seconds = 0.0
        # 换武器的冷却：换完先安静几个 tick，别在快捷栏里来回倒腾
        self._equip_cool = 0

        # 吃饭
        self._eat_cool = 0
        self._no_food_warned = False
        self._restore_slot: int | None = None
        self._restore_wait = 0

    # ---- 生命周期 -------------------------------------------------------

    def bind_tasks(self, runner) -> None:
        """接上任务层 —— 之后挨打/濒死会**挂起**她的任务，而不是让它继续跑。"""
        self._tasks = runner

    def bind_arbiter(self, arbiter) -> None:
        """接上仲裁层（见 `mcb/arbiter.py`）。`main.py` 在 `__init__` 里调。"""
        self.arbiter = arbiter

    # ⚠️ **反射的 walk 声明是最高优先级**（保命 > 用户 > 任务）。
    #    而且它**只在声明、不删别人的** —— 所以脱战一 release，
    #    任务/用户的路线会**自己恢复**（"挂起不是取消"的落点）。
    #
    # ⭐ 2026-10-10：**每一条都带 `ttl`** —— 到期由 `expire_stale()` 自动让位。
    #    这是"卡住把 walk 通道永久锁死"那个真 bug 的兜底，见 `arbiter.py`。
    async def _walk_claim(self, cmd: str, note: str, ttl: float = FLEE_TTL) -> None:
        if self.arbiter is None:
            await self._cmd(f"mcb baritone {cmd}")
            return
        await self.arbiter.claim_walk(LEVEL_REFLEX, "reflex", cmd, note, ttl=ttl)

    async def _walk_stop(self, note: str, ttl: float = STOP_TTL) -> None:
        """保命：顶掉别人的路线并停下。**不是"我不要走了"** —— 见 `claim_walk` 的注释。

        ⚠️ 默认 **8 秒就自动让位** —— 停一下是为了打断错误寻路，不是永久禁走。
        """
        if self.arbiter is None:
            await self._cmd("mcb stop")
            return
        await self.arbiter.claim_walk(LEVEL_REFLEX, "reflex", None, note, ttl=ttl)

    async def _walk_release(self) -> None:
        if self.arbiter is not None:
            await self.arbiter.release_walk("reflex")

    def _suspend_tasks(self, reason: str) -> None:
        if self._tasks is not None:
            self._tasks.suspend(reason)

    def _resume_tasks(self) -> None:
        if self._tasks is not None:
            self._tasks.resume()

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

        # ⭐ **先扫一遍过期的 walk 声明**（每 tick 都做）。
        #    反射声明的 TTL 到点就自动让位 —— 这是"永久锁死"的兜底，
        #    见 `arbiter.expire_stale` 和 `mcb/arbiter.py` 的模块注释。
        if self.arbiter is not None:
            await self.arbiter.expire_stale()

        hp = data.get("hp")
        hp = float(hp) if isinstance(hp, (int, float)) else None

        # 挨打判定：血量下降 = 刚刚受伤
        hurt = hp is not None and self._last_hp is not None and hp < self._last_hp
        was_hp = self._last_hp
        self._last_hp = hp

        # 「多久没挨打了」——脱战的一个出口（见 COMBAT_QUIET_SECONDS）
        if hurt:
            self._no_hurt_seconds = 0.0
        else:
            self._no_hurt_seconds += self.interval

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
                self._no_hurt_seconds = 0.0
                # ⚠️ **只写日志，不发 QQ** —— 战斗是自动跑的，用户不需要在聊天里看到它。
                #    只有"白主动要告诉用户的事"才走 _notify（比如"我饿了但没吃的"）。
                logger.warning(f"[mc_body] ⚔ 进入战斗（{why}）")
                self._log(f"进入战斗（{why}）")
                # 保命优先 —— 把她的任务**挂起**（不是取消），危险过去会自己接着做
                self._suspend_tasks("战斗中")
                if hurt:
                    await self._walk_stop("进战：别顺着原路线撞进去")
                # ⭐ **进战先看手上拿的是什么** —— 武器在背包里攥着面包打怪是白给。
                #    （2026-10-10 实测诊断：她手上是 `minecraft:string`，
                #     钻石斧/钻石剑全在背包里，`mc_attack` 伤害 0。见 docs/13 §2）
                await self._equip_best_weapon(data)
        else:
            # ⚠️ 脱战要**滞后**（hysteresis）：进战是 ≤5 格，脱战得等拉开到
            #    NEAR_HOSTILE_RANGE × COMBAT_EXIT_RATIO（现在是 6.25 格）。
            #    否则怪在边界上晃一下就是"进战/脱战"来回刷（用户看到的现象）。
            #
            # ⭐ 2026-10-10 加**第二个出口**：连续 `COMBAT_QUIET_SECONDS` 秒没挨打、
            #    而且没有任何东西在瞄着她 → 直接算脱战。
            #    只看"怪在不在附近"的话，一只站在旁边发呆的蠹虫就能把她永久锁进战斗态
            #    （实测：21:10–21:19 连续 9 分钟，walk 通道一直被反射占着，
            #     用户说"跟着我"完全下不去）。
            gone = (nearest is None
                    or nearest.get("dist", 1e9) > NEAR_HOSTILE_RANGE * COMBAT_EXIT_RATIO)
            quiet = (self._stance == STANCE_DEFEND
                     and self._no_hurt_seconds >= COMBAT_QUIET_SECONDS
                     and targeting is None)
            if gone or quiet:
                self._no_threat_ticks += 1
                if self._no_threat_ticks >= COMBAT_EXIT_TICKS:
                    self._in_combat = False
                    self._fleeing = False
                    logger.info(
                        "[mc_body] ⚔ 脱战：" + ("怪已经拉开或没了" if gone else "安静够久了")
                    )
                    self._log("脱离战斗")
                    self._resume_tasks()
                    # ⚠️ **还要把 walk 通道还给别人** —— 不 release 的话，
                    #    她的任务/用户路线永远恢复不了（反射声明优先级最高，一直压着）。
                    await self._walk_release()
            else:
                self._no_threat_ticks = 0
                # 只有 hunt 姿态才**追出去**；defend 姿态够不着就站着等它过来。
                await self._fight(data, nearest, hp, chase=(self._stance == STANCE_HUNT))

        # 挨打通知（只在掉血时，不刷屏）
        if hurt:
            logger.warning(f"[mc_body] ⚠ 白挨打了：血量 {was_hp:g} → {hp:g}")
            self._log(f"挨打了，血量 {was_hp:g} → {hp:g}")

        # 卡住判定
        await self._check_stuck(data)

        # 溺水 —— **判据是氧气，不是血量**（见方法说明）
        await self._check_drowning(data)

        # 吃饭 —— 抄女仆 MaidHealSelfTask：饿了就吃，不经过 LLM
        await self._maybe_eat(data)

    async def _check_drowning(self, data: dict) -> None:
        """溺水反射 —— ⚠️⚠️ **判据是氧气，不是血量。**

        2026-10-10 实测：她卡在水里 **253 秒**，事件流里 `drown` 每秒一条，
        但 `dmg: 0`（身上挂着抗性 V）→ **只看 hp 的反射永远不触发**，她就一直泡着。

        ⭐ **教训：凡是要"保命"的判据，都得从「状态」判，不能只从「伤害」判。**
        （抗性、水肺药水、吸收伤害……任何一个都能让"受伤"这条线失效。）

        动作：往**她自己那一列的地表**发一条 3D goto（服务端顺带报 `surfY`），
        让她想办法浮上去。

        ⚠️ **这条动作没在真实溺水场景里验过** —— 拿不到 `surfY` 时退回"停 + 记一笔"，
        至少别让她继续往下沉。
        """
        air = data.get("air")
        in_water = data.get("inWater") is True or data.get("underWater") is True

        if not in_water or not isinstance(air, (int, float)):
            self._drown_ticks = 0
            return
        if air >= DROWN_AIR:
            self._drown_ticks = 0
            return

        self._drown_ticks += 1
        if self._drown_ticks < DROWN_TICKS:
            return
        if self._drown_cool > 0:
            self._drown_cool -= 1
            return
        self._drown_cool = DROWN_RETRY_TICKS

        surf = data.get("surfY")
        x, z = data.get("x"), data.get("z")
        if isinstance(surf, (int, float)) and isinstance(x, (int, float)) and isinstance(z, (int, float)):
            self._log(f"快淹死了（氧气 {air:g}/300），往上浮到地表 y={surf:g}")
            await self._walk_claim(f"goto {float(x):.1f} {float(surf) + 1:.1f} {float(z):.1f}", "溺水上浮")
        else:
            self._log(f"快淹死了（氧气 {air:g}/300），但拿不到地表高度")
            await self._walk_stop("溺水但不知道往哪浮")

    async def _maybe_eat(self, data: dict) -> None:
        """自己吃东西。**两个触发条件**（用户 2026-10-10 拍板）：

          · **饿了**（`food < hunger_low`）—— 防饿死
          · **快死了**（`hp <= hp_low`）—— **受伤回血靠的是饱食度，不是"饿不饿"**

        ⚠️⚠️ **2026-10-10 踩到的大坑**：原来只有"饿了才吃"这一条，
        而她饥饿度**恰好卡在 16**（`food < 16` 不成立），于是
        **血 4.67 一直不回、也一直不吃**，卡死在那。
        原版自然回血要**饱食度 ≥ 18**，跟"饿不饿"根本是两回事。

        抄了女仆这几个细节：
          · 限频（`setMaxCheckRate`）—— 吃完隔几 tick 再看，别每 tick 翻背包
          · **记住原来手上拿的是什么，吃完换回来**（`memoryHandItemStack`）
        **受伤时优先挑饱食度高的**（saturation，回血看它）；只是饿了就挑顶饱的（nutrition）。

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
        hp = data.get("hp")
        hungry = isinstance(level, (int, float)) and level < self.hunger_low
        hurt = isinstance(hp, (int, float)) and hp <= self.hp_low
        if not hungry and not hurt:
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

        best = await self._find_food(prefer_saturation=hurt)
        if best is None:
            if not self._no_food_warned:
                self._no_food_warned = True
                why = (f"food={level:g}/20" if isinstance(level, (int, float)) else "food=?")
                if isinstance(hp, (int, float)):
                    why += f" hp={hp:g}"
                logger.warning(f"[mc_body] 🍖 想吃（{why}）但身上没吃的")
                # ⚠️ **只报事实，不写句子** —— 用户 2026-10-09：
                #    "感知我是没法接受程序文本的"。怎么讲是她的事。
                await self._notify_safe(f"{why} food_items=0")
            return

        where, idx, item_id, name, score = best
        slot = idx
        if where == "main":
            # 吃的**在背包里** —— 先挪到快捷栏的空格，再去吃。
            moved = await self._move_food_to_hotbar(item_id)
            if moved is None:
                # 快捷栏 9 格全占着 → 老实说，别硬顶掉她手上的东西
                if not self._no_food_warned:
                    self._no_food_warned = True
                    logger.warning(f"[mc_body] 🍖 背包里有 {name}，但快捷栏满、腾不出格")
                    await self._notify_safe(
                        f"food_items=1 hotbar_full=1 food={level if isinstance(level, (int, float)) else '?'}"
                    )
                return
            slot = moved
            self._log(f"把背包里的 {name} 挪到快捷栏第 {slot} 格")
        self._no_food_warned = False
        self._eat_cool = EAT_COOLDOWN_TICKS
        why = "受伤" if (hurt and not hungry) else "饿了"
        logger.info(
            f"[mc_body] 🍖 {why} → 吃 {name}（第 {slot} 格，"
            f"饱食度 {score:g}）hp={hp if isinstance(hp, (int, float)) else '?'} "
            f"food={level if isinstance(level, (int, float)) else '?'}"
        )
        # ⚠️ **只报事实**（原始读数），不写"我饿了"这种台词
        self._log(f"想吃东西（{why}）→ 吃 {name}")

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

    async def _find_food(self, *, prefer_saturation: bool = False):
        """找最该吃的那个食物。返回 `(在哪, 位置, 注册名, 显示名, 分数)`；没有回 `None`。

        ⚠️⚠️ **快捷栏和背包都要翻。**
        踩过（2026-10-10，用户报的"**时不时还是弹出 `[mc:alert] food=15/20 hp=18 food_items=0`**"）：
        原来只翻快捷栏，可她背包里揣着 **64 个面包** ——
        于是反射一次次判定"身上没吃的"，**一遍遍弹通知，还永远吃不上**。

        那条"⚠️ 只能找快捷栏（0-8）—— 背包里的东西还挪不出来"的注释
        **当时是对的，现在已经过时了**：v0.20.0 就做了"物品能在快捷栏/背包之间搬"
        （`containers.wear` 的 `hotbar0`~`hotbar8` / `backpack`）。
        **代码跟着注释一起烂掉了** —— 能力有了，用它的地方没跟上。

        吃的在背包里时 `在哪 = "main"`，**调用方负责先把它挪到快捷栏**再吃。
        """
        try:
            reply = await self.bridge.call("mcb inventory")
        except Exception:
            return None
        if not reply.get("ok"):
            return None
        data = reply.get("data") or {}
        want = "saturation" if prefer_saturation else "nutrition"

        best = None
        for where in ("hotbar", "main"):     # 快捷栏优先 —— 在那儿就不用搬了
            for idx, item in enumerate(data.get(where) or []):
                if not isinstance(item, dict):
                    continue
                fp = item.get("food")
                if not isinstance(fp, dict):
                    continue          # 没有 food 字段 = 不能吃
                iid = item.get("id")
                if not iid:
                    continue
                # 受伤时按**饱食度**排（回血看它），平时按 nutrition 排（顶饱）
                score = fp.get(want)
                if not isinstance(score, (int, float)):
                    score = fp.get("nutrition")      # 饱食度缺失时退回营养值
                if not isinstance(score, (int, float)):
                    continue
                if where == "main" and best is not None:
                    continue          # 快捷栏已经有得吃，就别动背包的了
                if best is None or score > best[4]:
                    best = (where, idx, str(iid), str(item.get("n") or "?"), float(score))
        return best

    async def _move_food_to_hotbar(self, item_id: str) -> int | None:
        """把背包里某样吃的挪进**空的快捷栏格**，返回格号；腾不出格回 `None`。

        ⚠️ **只找空格，不顶掉快捷栏里现有的东西。**
        她快捷栏那几格是剑 / 信标 / 附魔台 / 铁砧 一堆要紧玩意儿，
        别为了啃口面包把它们换进背包（`hotbar0`~`hotbar8` 是**对调**，不是放下）。
        """
        try:
            reply = await self.bridge.call("mcb inventory")
        except Exception:
            return None
        hotbar = ((reply.get("data") or {}).get("hotbar")) or []
        from .containers import ContainerIO, wear
        for i in range(9):
            # 空槽位在 `mcb inventory` 里是 `null`（不是缺字段）—— 不判就炸
            if i < len(hotbar) and isinstance(hotbar[i], dict):
                continue
            err = await wear(ContainerIO(self.bridge), item_id, f"hotbar{i}")
            if err is None:
                return i
            logger.warning(f"[mc_body] 🍖 把 {item_id} 挪到快捷栏第 {i} 格失败：{err}")
            return None
        return None

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
            # ⚠️ 先看**我的路线还在不在**：可能已经到期（FLEE_TTL），
            #    或者被"卡住"判定顶成了"停"（`touch_walk` 刻意不续"停"）。
            #    丢了就重算一次方向 —— 不然她会站在怪堆里不动。
            if self.arbiter is not None and not self.arbiter.holds_route("reflex"):
                self._fleeing = False
            if not self._fleeing:
                self._log(f"血量危急（{hp:g}），撤")
                await self._retreat(data, nearest, self.flee_toward)
            elif self.arbiter is not None:
                # 还在逃 —— **只续命，不重算方向**。
                # 重算一次 `_retreat` 要查主人坐标 + 掷随机抖动，每 tick 来一遍既浪费又抖。
                # ⚠️ 不续命的话 FLEE_TTL 一到，用户/任务的路线会立刻把她从逃跑里拽走。
                await self.arbiter.touch_walk("reflex", FLEE_TTL)
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
                await self._walk_claim(f"goto {int(tx)} {int(tz)}", "打怪：靠近", ttl=STOP_TTL)
            else:
                self._approach_wait -= 1
        else:
            # 够得着 —— 挥。冷却由客户端判（抄原版 startAttack 的逻辑）
            await self._cmd(f"mcb attackAt {tx} {ty} {tz}")

    async def _equip_best_weapon(self, data: dict) -> None:
        """进战时确保手上是**最能打的那件**。判据只看 `atk`（真实攻击力）。

        评分规则：
          · 分数 = 物品的 `atk`（服务端读 `getAttributeModifiers().modifiers()`；
            **原版存的是加成，服务端已经 +1 了**，别再加）
          · 没有 `atk` 字段 = 不是武器，跳过
          · 只有**明显更好**（差 ≥ `WEAPON_MIN_GAIN`）才换

        ⚠️ 三个不这么干就踩的坑：
          ① **不按名字认武器** —— 显示名不可信（tacz 返回本地化 key）
          ② **吃东西时别换手** —— 换手会打断"正在使用"，等于把啃了一半的苹果扔了
          ③ 换完**冷却几个 tick** —— 快捷栏里来回倒腾比不换还难看
        """
        if self._equip_cool > 0:
            self._equip_cool -= 1
            return
        using = data.get("using")
        if isinstance(using, dict) and using.get("isUsing"):
            return                      # 正在吃东西/喝药 —— 别打断

        try:
            reply = await self.bridge.call("mcb inventory")
        except Exception:
            return
        if not reply.get("ok"):
            return
        inv = reply.get("data") or {}
        hotbar = inv.get("hotbar") or []

        best_atk, best_id = 0.0, None
        for where in ("hotbar", "main"):
            for it in (inv.get(where) or []):
                if not isinstance(it, dict):
                    continue
                iid = it.get("id")
                atk = it.get("atk")
                if not iid or isinstance(atk, bool) or not isinstance(atk, (int, float)):
                    continue
                if float(atk) > best_atk:
                    best_atk, best_id = float(atk), str(iid)
        if best_id is None or best_atk <= 0:
            return                      # 身上一件能打的都没有

        # 手上已经是这把（或更好的）就别动
        # ⚠️ `held` 是**选中格的序号**（0~8），不是物品 —— 物品是 `hotbar[held]`。
        held = inv.get("held")
        if isinstance(held, (int, float)) and not isinstance(held, bool):
            idx = int(held)
            if 0 <= idx < len(hotbar):
                cur = hotbar[idx]
                cur_atk = cur.get("atk") if isinstance(cur, dict) else None
                if isinstance(cur_atk, (int, float)) and not isinstance(cur_atk, bool):
                    if best_atk < float(cur_atk) + WEAPON_MIN_GAIN:
                        return
                elif best_atk < WEAPON_MIN_GAIN:
                    return

        from .containers import ContainerIO, wear

        err = await wear(ContainerIO(self.bridge), best_id, "hand")
        if err:
            logger.warning(f"[mc_body] ⚔ 换武器失败（{best_id}）：{err}")
            return
        self._equip_cool = 3
        logger.info(f"[mc_body] ⚔ 换上 {best_id}（atk={best_atk:g}）")
        self._log(f"换上 {best_id}（伤害 {best_atk:g}）")

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
                    await self._walk_claim(f"follow player {self.owner_name}", "脱战：撤向主人")
                    # ⚠️ 这一句别漏：不置位的话 `_fight` 下一 tick 又会**重算一遍**
                    #    （`_where` 是一次 RCON 往返），而且每 tick 重声明 = TTL 形同虚设。
                    self._fleeing = True
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
        await self._walk_claim(f"goto {tx} {tz}", f"脱战：{who}")
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
            self._log(f"卡住了：{elapsed:.0f} 秒只挪了 {moved:.1f} 格，停下来了")
            await self._walk_stop("卡住了")
            # ⚠️ 只写日志，**不发 QQ**（同上：自动反射不占聊天正文）

    # ---- 小工具 ---------------------------------------------------------

    def _log(self, text: str) -> None:
        """反射事件写进**状态日志**（白自己看得见）。

        ⚠️ 只写日志、**不发 QQ** —— 用户 2026-10-09 明确要求：
        自动反射的状态翻转"这种东西日志里面出现就好了"，不许占聊天正文。
        """
        if self._journal is not None:
            self._journal.add("reflex", text)

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
