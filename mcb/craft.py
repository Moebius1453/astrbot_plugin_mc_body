"""合成规划 + 执行 —— 从"我要一把木镐"反推到"先拿两根原木"。

## 分两层（刻意解耦）

· **`Crafter.plan()` —— 纯规划**，不碰世界。只查配方和背包。
  可以离线单测：给它一个假的 bridge 就行。
· **`CraftRunner.run()` —— 执行**。把规划结果变成一串 `mcb clickSlot`。

## 抄的是谁

`mineflayer-crafting-util` 的思路：**递归解析依赖**。
mineflayer 自己的 `craft.js` 只负责"把一条配方变成一串点格子"，
**多步依赖是调用方的责任** —— 所以这一层我们自己写。算法：

    要 N 个 X → 差额 = N - 手上有的
      差额 <= 0 → 不用做
      选一条配方，每次出 outN 个 → 做 ceil(差额 / outN) 次
      对配方每一格材料：递归"要 材料 × 次数"
      最后 append "做 X"

**多条配方时按顺序试，挑第一条能凑齐的**（凑不齐就回滚，试下一条）。

## 为什么配方不写死

全来自服务端 `RecipeManager`（`mcb recipe <物品>`）—— **mod 配方自动兼容**。

## 边界（诚实记账）

· **只做工作台/背包合成**。熔炉冶炼、切石机、锻造台**还没做**。
· **不会去挖/去找** —— 缺料就如实报"缺什么"。
· 背包 2×2 要**先关掉容器**；工作台 3×3 要**调用方先走过去打开**。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field

from astrbot.api import logger

from . import containers
from .containers import (BTN_LEFT, BTN_RIGHT, MODE_PICKUP, MODE_QUICK_MOVE,
                          ContainerIO, open_block)

# 递归深度上限。配方可能有环（A 要 B、B 要 A），没有上限会栈溢出。
MAX_DEPTH = 8




def _id(raw) -> str:
    s = str(raw or "").strip()
    if not s:
        return s
    return s if ":" in s else f"minecraft:{s}"


def _short(item_id: str) -> str:
    return str(item_id).split(":")[-1]


def _cells_of(recipe: dict) -> list[list[str]]:
    """配方摊平成"每格要哪些候选材料"。shaped 用 grid（行优先），shapeless 每料一格。"""
    if recipe.get("type") == "shaped":
        return [c for c in (recipe.get("grid") or []) if isinstance(c, list)]
    return [c for c in (recipe.get("shapeless") or []) if isinstance(c, list)]


def _is_placeholder(cell: list[str]) -> bool:
    return (not cell) or all(str(c).startswith("#") for c in cell)


@dataclass
class CraftStep:
    item: str
    recipe: dict
    times: int


@dataclass
class Plan:
    steps: list[CraftStep] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing

    def describe(self) -> str:
        if not self.steps and not self.missing:
            return "手上已经够了，不用做。"
        lines = []
        if self.missing:
            lines.append("**缺材料**（这些得去挖/去找，我不会自己弄）：" + "、".join(self.missing))
        if self.steps:
            lines.append("要做这几步（按顺序）：")
            for i, s in enumerate(self.steps, 1):
                r = s.recipe
                where = "工作台" if r.get("needsTable") else "背包 2×2"
                lines.append(
                    f"  {i}. 用【{where}】做 {_short(s.item)} ×{int(r.get('outN') or 1) * s.times}"
                    f"（{s.times} 次，配方 {r.get('id')}）"
                )
        return "\n".join(lines)


class _Sim:
    """规划时的**沙盘**：假装东西已经做出来了，这样多步之间能复用中间产物。"""

    def __init__(self, have: dict[str, int]) -> None:
        self.have = dict(have)
        self.steps: list[CraftStep] = []
        self.missing: list[str] = []

    def snapshot(self):
        return (dict(self.have), list(self.steps), list(self.missing))

    def restore(self, snap) -> None:
        self.have, self.steps, self.missing = snap


# ---- 规划 -----------------------------------------------------------------

class Crafter:
    def __init__(self, bridge) -> None:
        self.bridge = bridge
        self._cache: dict[str, list[dict]] = {}

    async def recipes(self, item: str) -> list[dict]:
        key = _id(item)
        if key not in self._cache:
            out: list[dict] = []
            try:
                reply = await self.bridge.call(f"mcb recipe {key}")
                out = [r for r in ((reply.get("data") or {}).get("recipes") or []) if r.get("outN")]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[mc_body] 查配方失败（{key}）：{exc}")
            self._cache[key] = out
        return self._cache[key]

    async def inventory(self) -> dict[str, int]:
        """手上有什么 → {物品注册名: 总数}。

        ⚠️ **只能用注册名（`id`），不能用显示名**（`n`）—— 显示名跟语言走，
        中文客户端叫"橡木木板"、英文叫"Oak Planks"，拿它跟配方匹配一定会错。
        """
        have: dict[str, int] = {}
        try:
            data = (await self.bridge.call("mcb inventory")).get("data") or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 读背包失败：{exc}")
            return have

        def add(item, count) -> None:
            if not item:
                return
            k = _id(item)
            have[k] = have.get(k, 0) + int(count or 0)

        for key in ("hotbar", "main"):
            for item in (data.get(key) or []):
                if isinstance(item, dict):
                    add(item.get("id") or item.get("n"), item.get("c"))
        off = data.get("offhand")
        if isinstance(off, dict):
            add(off.get("id") or off.get("n"), off.get("c"))
        return have

    async def plan(self, item: str, count: int = 1) -> Plan:
        target = _id(item)
        sim = _Sim(await self.inventory())
        await self._ensure(target, max(1, int(count)), sim, 0, set())
        plan = Plan(steps=sim.steps, missing=sim.missing)
        logger.info(
            f"[mc_body] 🧮 合成规划 {_short(target)}×{count}：{len(plan.steps)} 步"
            + (f"，缺 {'、'.join(plan.missing)}" if plan.missing else "")
        )
        return plan

    async def _ensure(self, item: str, qty: int, sim: _Sim, depth: int, chain: set[str]) -> bool:
        """让沙盘里至少有 qty 个 item（不够就规划着做）。**不消耗**。"""
        if sim.have.get(item, 0) >= qty:
            return True
        if depth > MAX_DEPTH:
            sim.missing.append(f"{_short(item)}（依赖超过 {MAX_DEPTH} 层）")
            return False
        if item in chain:
            sim.missing.append(f"{_short(item)}（配方成环）")
            return False

        recipes = await self.recipes(item)
        need_more = qty - sim.have.get(item, 0)
        if not recipes:
            sim.missing.append(f"{_short(item)} ×{need_more}")
            return False

        first_missing = len(sim.missing)
        # ⚠️⚠️ **要报「最接近能做的那条配方」的缺口，不是最后一条。**
        #
        #    2026-10-10 实际踩到（她在游戏里报"基础配方被枪炮模组截胡了"）：
        #    `minecraft:stick` 有 **3 条**配方 ——
        #      ① `stick_from_bamboo_item`（竹子×2）
        #      ② `minecraft:stick`（任意木板×2）
        #      ③ `mynethersdelight:crafting/stick_alt`（**powder_cannon** + 竹子）
        #    原来每条循环都覆盖 `last_missing`，于是**最后试的第③条的
        #    "缺 powder_cannon"被报了出来** —— 明明 ①② 才是正常路子，
        #    看上去却像"基础配方被 mod 截胡"。**根因是"报错了配方"，不是"选错了配方"。**
        #
        #    排序判据（依次比较）：
        #      ① **她手上已经有料的那条优先** —— 有木板时"用木板做木棍"显然比
        #         "用竹子做木棍"更接近能做。这条最有用，放最前面。
        #      ② 缺口条目数少的优先（离能凑齐更近）
        #      ③ **原版优先**（`minecraft:` 命名空间）—— 基础东西走原版路子，
        #         别让一个 mod 的冷门配方顶掉它
        best_missing: list[str] | None = None
        best_rank: tuple[int, int, int, int] | None = None
        for recipe in recipes:
            snap = sim.snapshot()
            cells = _cells_of(recipe)
            # ⚠️ 必须在 `_apply` **之前**算 —— 它会把材料扣掉
            partial = 1
            for cell in cells:
                if any(not str(c).startswith("#") and sim.have.get(_id(c), 0) > 0 for c in cell):
                    partial = 0
                    break
            per = max(1, int(recipe.get("outN") or 1))
            times = math.ceil(need_more / per)
            if cells and await self._apply(cells, times, sim, depth, chain | {item}):
                sim.have[item] = sim.have.get(item, 0) + per * times
                sim.steps.append(CraftStep(item=item, recipe=recipe, times=times))
                return True
            # ⚠️ 记下这一轮新加的"缺什么"，然后回滚 ——
            #    报**根因**（缺原木）比报"缺木镐"有用得多
            got = list(sim.missing[first_missing:])
            rank = (partial, len(got),
                    # ⚠️ 同样接近时**优先不需要工作台的** —— 背包 2×2 就能做，
                    #    不用跑去找台子、也不会撞上"容器顶不掉"那堆破事。
                    0 if not recipe.get("needsTable") else 1,
                    0 if str(recipe.get("id") or "").startswith("minecraft:") else 1)
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best_missing = got
            sim.restore(snap)
        sim.missing.extend(best_missing if best_missing else [f"{_short(item)} ×{need_more}"])
        return False

    async def _apply(self, cells: list[list[str]], times: int, sim: _Sim,
                     depth: int, chain: set[str]) -> bool:
        """把"这条配方做 times 次"需要的材料都凑齐（并就地扣掉）。凑不齐返回 False。"""
        need: dict[str, int] = {}
        for cell in cells:
            cands = [c for c in cell if not str(c).startswith("#")]
            if not cands:
                continue
            # 优先挑手上真有的那一种候选（"任意木板"有 14 种，不能随便选）
            pick = next((_id(c) for c in cands if sim.have.get(_id(c), 0) > 0), _id(cands[0]))
            need[pick] = need.get(pick, 0) + times

        # ⚠️⚠️ **顺序很重要：先凑"中间产物"，再凑基础材料。**
        #    踩过：木镐要 3 木板 + 2 木棍，手上有 4 块木板 —— 先检查"3 块木板"通过了，
        #    可接着做木棍又吃掉 2 块，最后实际只剩 2 块，凑不出来却报了"能行"。
        #    中间产物是要**做出来**的，做的过程会消耗基础材料，所以它必须先结算。
        ordered: list[tuple[str, int, bool]] = []
        for mat, cnt in need.items():
            ordered.append((mat, cnt, bool(await self.recipes(mat))))
        ordered.sort(key=lambda x: (not x[2], x[0]))     # 可合成的排前面

        # **凑一个扣一个**，别攒到最后一起扣 —— 攒着扣会让后面的材料看到过期余额
        for mat, cnt, _ in ordered:
            if not await self._ensure(mat, cnt, sim, depth + 1, chain):
                return False
            sim.have[mat] = sim.have.get(mat, 0) - cnt
        return True


# ---- 执行 -----------------------------------------------------------------

class CraftRunner:
    """把 Plan 变成真的点格子。

    ⚠️ **所有"点格子"都委托给 `ContainerIO`**（`mcb/containers.py`）——
    这一层只负责"按什么顺序组合"。加新容器请改那张表，别在这儿写分支。
    """

    def __init__(self, bridge, *, delay: float = 0.4) -> None:
        self.bridge = bridge
        self.io = ContainerIO(bridge, delay=delay)

    async def _open_table(self) -> tuple[bool, str]:
        return await open_block(self.bridge, "minecraft:crafting_table",
                                self.io, want_cls="CraftingMenu")

    async def run_step(self, step: CraftStep) -> tuple[int, str]:
        """做一步。返回 (成功次数, 人话说明)。"""
        layout = await self.io.layout()
        if layout is None:
            return 0, "当前开着的是别的容器（不是合成界面），没法合成"
        if "grid" not in layout:
            return 0, f"{layout.get('name')} 不是合成容器"
        cells = _cells_of(step.recipe)
        if not cells:
            return 0, f"{_short(step.item)}：这条配方读不出形状"

        targets = self._targets(step, cells, layout)
        done = 0
        for _ in range(step.times):
            for slot, cands in targets:
                if not await self.io.put_one(layout, slot, cands):
                    return done, f"{_short(step.item)}：摆料摆不进去（材料没了？）"
            await self.io.take_all(int(layout["result"]))
            done += 1
        return done, f"{_short(step.item)} ×{done * int(step.recipe.get('outN') or 1)}"

    @staticmethod
    def _targets(step: CraftStep, cells: list[list[str]], layout: dict) -> list[tuple[int, list[str]]]:
        """配方格子 → 界面格子号。

        ⚠️ **shaped 必须贴着左上角摆**（原版规矩）—— 在 3×3 里做 2×2 配方时，
        摆到右下角是**合不出来**的。
        """
        stride = int(layout["stride"])
        grid = list(layout["grid"])
        w = int(step.recipe.get("w") or 1)
        h = int(step.recipe.get("h") or 1)
        out: list[tuple[int, list[str]]] = []
        if step.recipe.get("type") == "shaped":
            for r in range(h):
                for c in range(w):
                    i = r * w + c
                    if i >= len(cells) or _is_placeholder(cells[i]):
                        continue
                    out.append((int(grid[r * stride + c]), cells[i]))
        else:
            for idx, cell in enumerate(cells):
                if _is_placeholder(cell):
                    continue
                # shapeless 也贴着左上角摆
                pos = grid[(idx // stride) * stride + (idx % stride)] if stride == 3 else grid[idx]
                out.append((int(pos), cell))
        return out

    async def run(self, plan: Plan) -> str:
        if not plan.ok:
            return plan.describe()
        if not plan.steps:
            return "不用合成，手上已经够了。"

        needs_table = any(s.recipe.get("needsTable") for s in plan.steps)

        # ⚠️ 她手上开着别的容器（箱子/熔炉）时，2×2 那条路走不通 ——
        #    那类界面压根没有合成格。**开一次工作台**最省事：
        #    它会顶掉当前容器，而且 3×3 也能做 2×2 的配方。
        if (await self.io.layout()) is None and not needs_table:
            logger.info("[mc_body] 她开着别的容器 —— 改用工作台来做（3×3 也能做 2×2）")
            needs_table = True

        if needs_table:
            ok, why = await self._open_table()
            if not ok:
                # ⚠️ **报具体哪一步断的**，不要三种原因混一句 ——
                #    她原来复述的是"要么附近没工作台/走不过去/容器顶不掉"，
                #    分不清该去搬个工作台、还是该先按 Esc 关掉手上的界面。
                return f"做不了：{why}。"

        out = []
        try:
            for step in plan.steps:
                n, msg = await self.run_step(step)
                if n < step.times:
                    out.append(f"⚠️ {msg}（想做 {step.times} 次，只成了 {n} 次）")
                    break
                out.append(f"✅ {msg}")
        finally:
            await self.bridge.call("mcb closeGui")   # 界面开着她就动不了
        return "合成结果：" + chr(10) + chr(10).join(out)
