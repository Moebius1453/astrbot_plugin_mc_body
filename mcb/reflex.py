r"""战斗、撤退、脱困和进食由反射执行，不调用模型。"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import time

from astrbot.api import logger

from .arbiter import COMBAT_TTL, LEVEL_COMBAT, LEVEL_REFLEX

COMBAT_WINDOW_SECONDS = 12.0

STUCK_SECONDS = 18.0
STUCK_MIN_MOVE = 1.5

# 溺水判氧气而非血量，抗性可能让伤害为零。
DROWN_AIR = 180.0        
DROWN_TICKS = 3          
DROWN_RETRY_TICKS = 10   

# 近距离且瞄准自身的威胁可触发战斗。
NEAR_HOSTILE_RANGE = 5.0

# 够不着时接近目标。
ENGAGE_RANGE = 3.0

# 连续丢失目标后脱战。
COMBAT_EXIT_TICKS = 3

# 脱战保留滞后，防止边界反复切态。
COMBAT_EXIT_RATIO = 1.25

# 附近有怪不等于仍在交战，需要安静超时出口。
COMBAT_QUIET_SECONDS = 10.0

KITE_BACK_UNDER = 2.0        
KITE_STEP = 2.5              
KITE_COOLDOWN_SECONDS = 1.5  # 使用墙钟，战斗与平时 tick 间隔不同。
KITE_TTL = 2.0               

COMBAT_INTERVAL = 0.25

FLEE_TTL = 15.0      
STOP_TTL = 8.0       

# 武器按真实 atk 识别，显示名可能是本地化 key。
WEAPON_MIN_GAIN = 1.0    

APPROACH_EVERY_TICKS = 2

STANCE_DEFEND = "defend"
STANCE_HUNT = "hunt"
STANCES = (STANCE_DEFEND, STANCE_HUNT)

HUNT_RANGE = 5.0

# 饥饿低于阈值时进食。
HUNGER_LOW = 16
# 进食后限频检查背包。
EAT_COOLDOWN_TICKS = 5

# 立即换手会打断正在使用的物品。
EAT_RESTORE_DELAY_TICKS = 2

_TASK_NAME = "mc_body_reflex"

RETREAT_SAFE = "safe"
RETREAT_OWNER = "owner"
RETREAT_AUTO = "auto"

RETREAT_OWNER_RANGE = 24.0


class ReflexGuard:

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
        stuck_seconds: float = STUCK_SECONDS,
        stance: str = STANCE_DEFEND,
        owner_name: str = "",
        flee_toward: str = RETREAT_SAFE,
        combat_interval: float = COMBAT_INTERVAL,
        notify=None,
        journal=None,
    ) -> None:
        self.bridge = bridge
        self.interval = max(0.5, float(interval))
        self.combat_interval = max(0.1, min(float(combat_interval), self.interval))
        # 计时按真实间隔，不能把不同节拍当相同 tick。
        self._last_dt = self.interval
        self._tick_at = 0.0
        self.hp_low = float(hp_low)
        self.hp_critical = float(hp_critical)
        self.flee_distance = max(8, int(flee_distance))
        self.scan_range = max(6, int(scan_range))
        self.hunger_low = float(hunger_low)
        self.stuck_seconds = max(3.0, float(stuck_seconds))
        self.owner_name = str(owner_name or "").strip()
        self.flee_toward = (
            flee_toward
            if flee_toward in (RETREAT_SAFE, RETREAT_OWNER, RETREAT_AUTO)
            else RETREAT_AUTO
        )
        self._notify = notify  

        self._journal = journal

        # 抢占是挂起任务，不是取消。
        self._tasks = None
        self.arbiter = None
        self._idle = None
        self._hunt = None

        self._task: asyncio.Task | None = None

        self._last_hp: float | None = None
        self._fleeing = False
        self._last_sample: tuple[float, float, float, float] | None = None
        self._stuck_reported = False
        self._fail_streak = 0
        self._drown_ticks = 0
        self._drown_cool = 0

        self._stance = stance if stance in STANCES else STANCE_DEFEND
        self._in_combat = False
        self._no_threat_ticks = 0
        self._approach_wait = 0
        # defend 也可追击正在攻击自己的目标。
        self._engaged = False
        # 身体在线状态供唤醒闸使用，不能与主人在线混同。
        self.online = False
        self._kite_at = 0.0
        self._no_hurt_seconds = 0.0
        self._equip_cool = 0

        self._eat_cool = 0
        self._no_food_warned = False
        self._restore_slot: int | None = None
        self._restore_wait = 0


    def bind_tasks(self, runner) -> None:
        self._tasks = runner

    def bind_arbiter(self, arbiter) -> None:
        self.arbiter = arbiter

    def bind_idle(self, idle) -> None:
        r"""闲逛复用当前节拍和状态，不额外轮询。"""
        self._idle = idle

    def bind_hunt(self, hunt) -> None:
        r"""主动清怪复用当前 hostiles，不额外索敌。"""
        self._hunt = hunt

    # 保命走 LEVEL_REFLEX，进攻位移走 LEVEL_COMBAT。
    async def _walk_claim(self, cmd: str, note: str, ttl: float = FLEE_TTL) -> None:
        if self.arbiter is None:
            await self._cmd(f"mcb baritone {cmd}")
            return
        await self.arbiter.claim_walk(LEVEL_REFLEX, "reflex", cmd, note, ttl=ttl)

    async def _walk_engage(self, cmd: str, note: str, ttl: float = COMBAT_TTL) -> None:
        r"""沿用 reflex owner，确保进战停止可被追击声明替换。"""
        if self.arbiter is None:
            await self._cmd(f"mcb baritone {cmd}")
            return
        await self.arbiter.claim_walk(LEVEL_COMBAT, "reflex", cmd, note, ttl=ttl)

    async def _walk_stop(self, note: str, ttl: float = STOP_TTL) -> None:
        r"""停是短期抢占，不是释放或永久禁走。"""
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
        # 新建前取消同名孤儿循环，防止重复 use 重置进度。
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
            f"卡住 {self.stuck_seconds:g} 秒自动停"
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


    @property
    def stance(self) -> str:
        return self._stance

    def set_stance(self, mode: str) -> str:
        r"""切姿态重置战斗态，避免沿用旧判据。"""
        m = str(mode or "").strip().lower()
        if m not in STANCES:
            raise ValueError(f"未知的战斗姿态：{mode!r}（只认 {' / '.join(STANCES)}）")
        if m != self._stance:
            logger.info(f"[mc_body] ⚔ 战斗姿态 {self._stance} → {m}")
        self._stance = m
        self._in_combat = False
        self._no_threat_ticks = 0
        self._fleeing = False
        self._engaged = False
        return m


    async def _loop(self) -> None:
        while True:
            now = time.monotonic()
            if self._tick_at:
                self._last_dt = min(5.0, max(0.05, now - self._tick_at))
            self._tick_at = now
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
            await asyncio.sleep(self.combat_interval if self._in_combat else self.interval)

    async def _tick(self) -> None:
        reply = await self.bridge.call("mcb state")
        if not reply.get("ok"):
            # 桥失败不等于角色离线，保留上次状态。
            return
        data = reply.get("data") or {}
        self.online = bool(data.get("online"))
        if not data.get("online"):
            self._last_hp = None
            self._last_sample = None
            return

        if self.arbiter is not None:
            # 活动声明需先续期再过期，复用当前状态。
            task_info = data.get("task")
            await self.arbiter.renew_active(
                task_info.get("status") if isinstance(task_info, dict) else None
            )
            await self.arbiter.expire_stale()

        # 仲裁更新后才判断闲逛，避免抢占未释放通道。
        if self._idle is not None:
            await self._idle.tick(data)

        hp = data.get("hp")
        hp = float(hp) if isinstance(hp, (int, float)) else None

        hurt = hp is not None and self._last_hp is not None and hp < self._last_hp
        was_hp = self._last_hp
        self._last_hp = hp

        if hurt:
            self._no_hurt_seconds = 0.0
        else:
            self._no_hurt_seconds += self._last_dt

        hostiles = await self._hostiles()
        # closest 决定距离阈值，target 决定攻击目标。
        closest = hostiles[0] if hostiles else None
        target = self._pick_target(hostiles) if hostiles else None
        targeting = next((h for h in hostiles if h.get("targeting")), None)
        engaging = bool(hurt or targeting is not None)

        # 战斗按索敌、接近、攻击、脱战推进。
        if not self._in_combat:
            why = self._combat_trigger(hurt, closest, targeting)
            if why:
                self._in_combat = True
                self._no_threat_ticks = 0
                self._no_hurt_seconds = 0.0
                self._engaged = engaging
                # 自动反射只记日志，不占聊天正文。
                logger.warning(f"[mc_body] ⚔ 进入战斗（{why}）")
                self._log(f"进入战斗（{why}）")
                self._suspend_tasks("战斗中")
                if hurt:
                    await self._walk_stop("进战：别顺着原路线撞进去")
                await self._equip_best_weapon(data)
        else:
            # 脱战使用距离滞后和无攻击超时，避免长期占用 walk。
            if engaging:
                self._engaged = True
            gone = (closest is None
                    or closest.get("dist", 1e9) > NEAR_HOSTILE_RANGE * COMBAT_EXIT_RATIO)
            quiet = (self._stance == STANCE_DEFEND
                     and self._no_hurt_seconds >= COMBAT_QUIET_SECONDS
                     and targeting is None)
            if gone or quiet:
                self._no_threat_ticks += 1
                if self._no_threat_ticks >= COMBAT_EXIT_TICKS:
                    self._in_combat = False
                    self._fleeing = False
                    self._engaged = False
                    logger.info(
                        "[mc_body] ⚔ 脱战：" + ("怪已经拉开或没了" if gone else "安静够久了")
                    )
                    self._log("脱离战斗")
                    self._resume_tasks()
                    # 脱战释放 walk，否则被挂起路线无法恢复。
                    await self._walk_release()
            else:
                self._no_threat_ticks = 0
                # defend 仅追击正在攻击自己的目标。
                chase = (self._stance == STANCE_HUNT) or self._engaged
                await self._fight(data, target, hp, chase=chase)

        # 主动清怪读取本 tick 更新后的战斗态，避免抢 walk。
        if self._hunt is not None:
            await self._hunt.tick(
                data, hostiles, stance=self._stance, in_combat=self._in_combat,
                arrive_range=HUNT_RANGE,
            )

        if hurt:
            logger.warning(f"[mc_body] ⚠ 白挨打了：血量 {was_hp:g} → {hp:g}")
            self._log(f"挨打了，血量 {was_hp:g} → {hp:g}")

        await self._check_stuck(data)

        await self._check_drowning(data)

        # 进食不调用模型。
        await self._maybe_eat(data)

    async def _check_drowning(self, data: dict) -> None:
        r"""按氧气而非血量判断；缺 surfY 时只停并记日志，上浮未实测。"""
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
        r"""低血量也需进食；使用中禁止重发 use，吃完限频检查并恢复手持。受伤优先饱食度，否则优先营养值。"""
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
            return  

        # 使用中禁止重发 use，否则重置倒计时，永远吃不完。
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
                # 通知只给事实，不代写角色台词。
                await self._notify_safe(f"{why} food_items=0")
            return

        where, idx, item_id, name, score = best
        slot = idx
        if where == "main":
            moved = await self._move_food_to_hotbar(item_id)
            if moved is None:
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
        self._log(f"想吃东西（{why}）→ 吃 {name}")

        # 等使用结束再恢复手持，否则会打断进度。
        before = data.get("held") if isinstance(data.get("held"), int) else None
        await self._cmd(f"mcb hotbar {slot}")
        await self._cmd("mcb use")
        if before is not None and before != slot:
            self._restore_slot = before
            self._restore_wait = EAT_RESTORE_DELAY_TICKS
        else:
            self._restore_slot = None

    async def _restore_hand(self) -> None:
        r"""进食结束后恢复手持，避免中断使用。"""
        if self._restore_slot is None:
            return
        if self._restore_wait > 0:
            self._restore_wait -= 1
            return
        slot = self._restore_slot
        self._restore_slot = None
        await self._cmd(f"mcb hotbar {slot}")

    async def _find_food(self, *, prefer_saturation: bool = False):
        r"""同时查快捷栏和背包；背包食物需先移至快捷栏。"""
        try:
            reply = await self.bridge.call("mcb inventory")
        except Exception:
            return None
        if not reply.get("ok"):
            return None
        data = reply.get("data") or {}
        want = "saturation" if prefer_saturation else "nutrition"

        best = None
        for where in ("hotbar", "main"):     
            for idx, item in enumerate(data.get(where) or []):
                if not isinstance(item, dict):
                    continue
                fp = item.get("food")
                if not isinstance(fp, dict):
                    continue          
                iid = item.get("id")
                if not iid:
                    continue
                score = fp.get(want)
                if not isinstance(score, (int, float)):
                    score = fp.get("nutrition")      
                if not isinstance(score, (int, float)):
                    continue
                if where == "main" and best is not None:
                    continue          
                if best is None or score > best[4]:
                    best = (where, idx, str(iid), str(item.get("n") or "?"), float(score))
        return best

    async def _move_food_to_hotbar(self, item_id: str) -> int | None:
        r"""仅用空快捷栏槽，不交换已有物品。"""
        try:
            reply = await self.bridge.call("mcb inventory")
        except Exception:
            return None
        hotbar = ((reply.get("data") or {}).get("hotbar")) or []
        from .containers import ContainerIO, wear
        for i in range(9):
            # 快捷栏空槽位为 null，必须判空。
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
        if hurt:
            return "挨打了"

        if self._stance == STANCE_HUNT:
            if nearest is not None and nearest.get("dist", 1e9) <= HUNT_RANGE:
                return f"{nearest.get('name')} 在 {nearest.get('dist'):g} 格内（hunt 主动出击）"
            return None

        # 不能仅按实体类别判断威胁，还需检查攻击目标。
        if targeting is not None and targeting.get("dist", 1e9) <= NEAR_HOSTILE_RANGE:
            return f"{targeting.get('name')} 正瞄着我，贴到 {targeting.get('dist'):g} 格"
        return None

    @staticmethod
    def _threat_score(h: dict) -> float:
        score = 0.0
        if h.get("targeting"):
            score += 100.0
        if h.get("hostile"):
            score += 30.0
        dist = h.get("dist")
        if isinstance(dist, (int, float)):
            score -= float(dist)
        return score

    @staticmethod
    def _pick_target(hostiles: list[dict]) -> dict | None:
        if not hostiles:
            return None
        return max(hostiles, key=ReflexGuard._threat_score)

    async def _nearest_targeting(self) -> dict | None:
        hostiles = await self._hostiles()
        return next((h for h in hostiles if h.get("targeting")), None)

    async def _fight(
        self, data: dict, nearest: dict, hp: float | None, *, chase: bool = True
    ) -> None:
        r"""defend 不追击够不着的目标，hunt 则接近目标。"""
        if hp is not None and hp <= self.hp_critical:
            # 逃跑声明可能到期或被停止声明替换，丢失时需重算。
            if self.arbiter is not None and not self.arbiter.holds_route("reflex"):
                self._fleeing = False
            if not self._fleeing:
                self._log(f"血量危急（{hp:g}），撤")
                await self._retreat(data, nearest, self.flee_toward)
            elif self.arbiter is not None:
                # 有效逃跑路线只续期，避免每 tick 随机改方向。
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
                return
            if self._approach_wait <= 0:
                self._approach_wait = APPROACH_EVERY_TICKS
                logger.info(
                    f"[mc_body] ⚔ {nearest['name']} 在 {dist:g} 格外，走过去 ({tx},{ty},{tz})"
                )
                await self._walk_engage(f"goto {int(tx)} {int(tz)}", "打怪：靠近")
            else:
                self._approach_wait -= 1
        else:
            await self._cmd(f"mcb attackAt {tx} {ty} {tz}")
            await self._kite(data, nearest)

    async def _kite(self, data: dict, target: dict) -> None:
        r"""短距离后撤；冷却按墙钟，不能依赖可变 tick 节拍。"""
        now = time.monotonic()
        if (now - self._kite_at) < KITE_COOLDOWN_SECONDS:
            return
        dist = target.get("dist")
        pos = target.get("pos")
        if not isinstance(dist, (int, float)) or not isinstance(pos, list) or len(pos) < 3:
            return
        if float(dist) > KITE_BACK_UNDER:
            return          
        try:
            x = float(data.get("x"))
            z = float(data.get("z"))
            tx = float(pos[0])
            tz = float(pos[2])
        except (TypeError, ValueError):
            return
        dx, dz = x - tx, z - tz
        norm = math.hypot(dx, dz)
        if norm < 0.05:
            dx, dz, norm = 1.0, 0.0, 1.0
        bx = int(round(x + dx / norm * KITE_STEP))
        bz = int(round(z + dz / norm * KITE_STEP))
        self._kite_at = now
        await self._walk_engage(f"goto {bx} {bz}", "砍完退半步", ttl=KITE_TTL)

    async def _equip_best_weapon(self, data: dict) -> None:
        r"""按真实 atk 选武器；使用中不能换手；明显更强才切换。"""
        if self._equip_cool > 0:
            self._equip_cool -= 1
            return
        using = data.get("using")
        if isinstance(using, dict) and using.get("isUsing"):
            return                      

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
            return                      

        # held 是快捷栏下标，物品取 hotbar[held]。
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


    async def retreat(self, toward: str = RETREAT_AUTO) -> str:
        t = str(toward or "").strip().lower() or RETREAT_AUTO
        if t not in (RETREAT_SAFE, RETREAT_OWNER, RETREAT_AUTO):
            raise ValueError(
                f"未知的撤离方向：{toward!r}"
                f"（只认 {RETREAT_AUTO} / {RETREAT_OWNER} / {RETREAT_SAFE}）"
            )

        self._in_combat = False
        self._no_threat_ticks = 0
        self._fleeing = False
        self._engaged = False
        self._approach_wait = 0

        data = await self._state()
        if not data.get("online"):
            return "她不在线，撤不了。"
        hostiles = await self._hostiles()
        return await self._retreat(data, hostiles[0] if hostiles else None, t)

    async def _retreat(self, data: dict, nearest: dict | None, toward: str) -> str:
        r"""随机安全点可能落入地下实心块，allowBreak 开启时仍可能凿穿。"""
        t = str(toward or "").strip().lower() or RETREAT_AUTO
        if t not in (RETREAT_SAFE, RETREAT_OWNER, RETREAT_AUTO):
            t = RETREAT_AUTO

        if t in (RETREAT_OWNER, RETREAT_AUTO):
            owner = await self._where(self.owner_name)
            same_dim = owner is not None and owner.get("dim") == data.get("dim")
            if t == RETREAT_AUTO:
                dist = self._owner_distance(data, owner) if same_dim else None
                if dist is not None and dist <= RETREAT_OWNER_RANGE:
                    t = RETREAT_OWNER
                    logger.info(f"[mc_body] 🏃 脱战 —— 主人在 {dist:g} 格内，往他那儿跑")
                else:
                    t = RETREAT_SAFE
                    where = "?" if dist is None else f"{dist:g}"
                    logger.info(f"[mc_body] 🏃 脱战 —— 主人离 {where} 格，慌不择路")
            if t == RETREAT_OWNER:
                if same_dim:
                    try:
                        tx, tz = int(owner["x"]), int(owner["z"])
                    except (KeyError, TypeError, ValueError):
                        tx = tz = None
                    if tx is not None:
                        # 主人会移动，使用 follow 而非坐标快照 goto。
                        logger.warning(
                            f"[mc_body] 🏃 脱战 —— 撤向主人 {self.owner_name}"
                            f"（follow，他当前在 {tx},{tz}）"
                        )
                        await self._walk_claim(f"follow player {self.owner_name}", "脱战：撤向主人")
                        # 置位后才不会每 tick 重算撤退并重置 TTL。
                        self._fleeing = True
                        return "正往你那边跑（会一直跟到你身边）"
                logger.warning(
                    f"[mc_body] 🏃 脱战时找不到 {self.owner_name}（不在线/不在同维度），"
                    "改往安全方向跑"
                )

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

        if dx is None and self._last_sample is not None:
            mx, mz = x - self._last_sample[0], z - self._last_sample[2]
            if abs(mx) + abs(mz) > 0.3:
                dx, dz = -mx, -mz

        if dx is None:
            dx, dz = 1.0, 0.0

        ang = math.atan2(dz, dx) + random.uniform(-0.5, 0.5)
        tx = int(round(x + math.cos(ang) * self.flee_distance))
        tz = int(round(z + math.sin(ang) * self.flee_distance))

        who = f"躲开 {nearest['name']}" if nearest else "背离来时方向"
        logger.warning(
            f"[mc_body] 🏃 脱战 —— {who}，跑 {self.flee_distance} 格到 ({tx},{tz})"
        )
        await self._walk_claim(f"goto {tx} {tz}", f"脱战：{who}")
        # 自动反射只记日志，不发送聊天。
        return f"往安全方向跑（x={tx} z={tz}）"

    async def _state(self) -> dict:
        try:
            reply = await self.bridge.call("mcb state")
        except Exception:
            return {}
        if not reply.get("ok"):
            return {}
        return reply.get("data") or {}

    async def _where(self, name: str) -> dict | None:
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

    @staticmethod
    def _owner_distance(data: dict, owner: dict | None) -> float | None:
        r"""未知距离不能当 0，否则会错误选择向主人撤退。"""
        if not isinstance(owner, dict):
            return None
        try:
            dx = float(owner["x"]) - float(data.get("x"))
            dy = float(owner["y"]) - float(data.get("y"))
            dz = float(owner["z"]) - float(data.get("z"))
        except (KeyError, TypeError, ValueError):
            return None
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    async def _check_stuck(self, data: dict) -> None:
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
            self._last_sample = (x, y, z, now)
            self._stuck_reported = False
            return

        if elapsed >= self.stuck_seconds and not self._stuck_reported:
            self._stuck_reported = True
            logger.warning(
                f"[mc_body] ⚠ 白卡住了：{elapsed:.0f} 秒只挪了 {moved:.1f} 格，已让她停下"
            )
            self._log(f"卡住了：{elapsed:.0f} 秒只挪了 {moved:.1f} 格，停下来了")
            await self._walk_stop("卡住了")


    def _log(self, text: str) -> None:
        r"""自动反射只写日志，不占聊天正文。"""
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
