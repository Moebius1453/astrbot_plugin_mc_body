r"""mineflayer-crafting-util 的思路：递归解析依赖。
mineflayer 自己的 craft.js 只负责"把一条配方变成一串点格子"，
多步依赖是调用方的责任 —— 所以这一层我们自己写。算法："""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field

from astrbot.api import logger

from . import containers
from .containers import (BTN_LEFT, BTN_RIGHT, MODE_PICKUP, MODE_QUICK_MOVE,
                          ContainerIO, open_block)

# 配方依赖可能有环，限制递归深度。
MAX_DEPTH = 8




def _id(raw) -> str:
    s = str(raw or "").strip()
    if not s:
        return s
    return s if ":" in s else f"minecraft:{s}"


def _short(item_id: str) -> str:
    return str(item_id).split(":")[-1]


def _cells_of(recipe: dict) -> list[list[str]]:
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

    def __init__(self, have: dict[str, int]) -> None:
        self.have = dict(have)
        self.steps: list[CraftStep] = []
        self.missing: list[str] = []

    def snapshot(self):
        return (dict(self.have), list(self.steps), list(self.missing))

    def restore(self, snap) -> None:
        self.have, self.steps, self.missing = snap



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
        r"""物品只用注册名匹配，显示名随语言变化。"""
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
        # 缺料报告选择最接近可做的配方，不取最后一条。
        best_missing: list[str] | None = None
        best_rank: tuple[int, int, int, int] | None = None
        for recipe in recipes:
            snap = sim.snapshot()
            cells = _cells_of(recipe)
            # _apply 会扣材料，评分必须提前计算。
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
            # 回滚前保存基础材料缺口，避免只报中间产物。
            got = list(sim.missing[first_missing:])
            rank = (partial, len(got),
                    # 同等可行时优先背包合成，减少工作台依赖。
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
        need: dict[str, int] = {}
        for cell in cells:
            cands = [c for c in cell if not str(c).startswith("#")]
            if not cands:
                continue
            # 替代材料优先选库存已有的种类。
            pick = next((_id(c) for c in cands if sim.have.get(_id(c), 0) > 0), _id(cands[0]))
            need[pick] = need.get(pick, 0) + times

        # 先结算中间产物的消耗，再检查基础材料余额。
        ordered: list[tuple[str, int, bool]] = []
        for mat, cnt in need.items():
            ordered.append((mat, cnt, bool(await self.recipes(mat))))
        ordered.sort(key=lambda x: (not x[2], x[0]))     

        # 逐项扣料，避免后续材料读到过期余额。
        for mat, cnt, _ in ordered:
            if not await self._ensure(mat, cnt, sim, depth + 1, chain):
                return False
            sim.have[mat] = sim.have.get(mat, 0) - cnt
        return True



class CraftRunner:

    def __init__(self, bridge, *, delay: float = 0.4, arbiter=None) -> None:
        self.bridge = bridge
        self.arbiter = arbiter
        self.io = ContainerIO(bridge, delay=delay)
        self.completed = False

    async def _open_table(self) -> tuple[bool, str]:
        return await open_block(self.bridge, "minecraft:crafting_table",
                                self.io, want_cls="CraftingMenu", arbiter=self.arbiter)

    async def run_step(self, step: CraftStep) -> tuple[int, str]:
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
            before = (await Crafter(self.bridge).inventory()).get(step.item, 0)
            recipe_id = str(step.recipe.get("id") or "")
            if recipe_id:
                initial = await self.io.menu()
                if not initial or initial.get("id") != layout.get("id"):
                    return done, "合成界面已改变，未请求配方摆料"
                occupied = set(layout["grid"]) | {int(layout["result"])}
                if any(int(it.get("i", -1)) in occupied and int(it.get("c") or 0) > 0
                       for it in initial.get("items", [])):
                    return done, "合成格已有物品，未请求配方摆料，请先人工核对"
                reply = await self.bridge.call(f"mcb placeRecipe {recipe_id}")
                if not reply.get("ok"):
                    return done, "配方摆料请求失败，未确认合成，不自动重试"
                for _ in range(8):
                    await asyncio.sleep(0.25)
                    snapshot = await self.io.menu()
                    if not snapshot or snapshot.get("id") != layout.get("id"):
                        return done, "配方摆料期间界面改变，未取产物"
                    if any(int(it.get("i", -1)) == int(layout["result"])
                           and int(it.get("c") or 0) > 0 for it in snapshot.get("items", [])):
                        break
            else:
                for slot, cands in targets:
                    if not await self.io.put_one(layout, slot, cands):
                        return done, f"{_short(step.item)}：摆料摆不进去（材料没了？）"
            menu = await self.io.menu()
            result_slot = int(layout["result"])
            per = int(step.recipe.get("outN") or 1)
            if not menu or menu.get("id") != layout.get("id"):
                return done, f"{_short(step.item)}：合成界面发生变化，未取产物"
            product = next((it for it in menu.get("items", [])
                            if int(it.get("i", -1)) == result_slot), None)
            if not product or product.get("id") != step.item or int(product.get("c") or 0) != per:
                return done, f"{_short(step.item)}：产物格不是预期物品或数量，未取产物"
            await self.io.take_all(result_slot)
            after = (await Crafter(self.bridge).inventory()).get(step.item, 0)
            if after - before != per:
                return done, f"{_short(step.item)}：背包增加 {after - before}，预期 {per}，未确认合成成功"
            done += 1
        return done, f"{_short(step.item)} ×{done * int(step.recipe.get('outN') or 1)}"

    @staticmethod
    def _targets(step: CraftStep, cells: list[list[str]], layout: dict) -> list[tuple[int, list[str]]]:
        r"""shaped 配方必须贴左上角摆放。"""
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
                pos = grid[(idx // stride) * stride + (idx % stride)] if stride == 3 else grid[idx]
                out.append((int(pos), cell))
        return out

    async def run(self, plan: Plan) -> str:
        self.completed = False
        if not plan.ok:
            return plan.describe()
        if not plan.steps:
            self.completed = True
            return "不用合成，手上已经够了。"

        needs_table = any(s.recipe.get("needsTable") for s in plan.steps)

        # 其他容器没有背包合成格，改开工作台。
        if (await self.io.layout()) is None and not needs_table:
            logger.info("[mc_body] 她开着别的容器 —— 改用工作台来做（3×3 也能做 2×2）")
            needs_table = True

        if needs_table:
            ok, why = await self._open_table()
            if not ok:
                return f"做不了：{why}。"

        out = []
        try:
            for step in plan.steps:
                n, msg = await self.run_step(step)
                if n < step.times:
                    out.append(f"⚠️ {msg}（想做 {step.times} 次，只成了 {n} 次）")
                    break
                out.append(f"✅ {msg}")
            else:
                self.completed = True
        finally:
            await self.bridge.call("mcb closeGui")   # 界面开着时无法移动。
        return "合成结果：" + chr(10) + chr(10).join(out)
