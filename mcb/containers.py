"""通用容器模板 —— **"加新容器 = 加一行表"**，不是加一个函数。

## 为什么要有这一层

所有容器类操作本质是同一个形状：

    往某个角色的格子放东西 → （也许等一等） → 从某个角色的格子拿东西

合成、开箱子、冶炼、切石、锻造**全都是这个**。所以不写"冶炼函数""切石函数"，
写**一张表 + 一套原语**。（用户 2026-10-09 原话："箱子打开来回拿不就是大部分的操作"。）

## 硬规矩

**加新容器只许加一行 `CONTAINERS`。** 如果你发现要写新函数，说明这张表没抽象对 ——
先改表，别绕过去。

## 还没做的

`needs_choice`（附魔台那类要选等级/选项的）—— **用户明确说"先放着"**。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

# clickSlot 的参数（对应原版 ClickType）
BTN_LEFT = 0
BTN_RIGHT = 1
MODE_PICKUP = 0          # 普通点（拿起/放下）
MODE_QUICK_MOVE = 1      # shift 快速移动

# ---- 容器表 ---------------------------------------------------------------
#
# `match` 是拿**界面类型名**去比的（`mcb state` 的 `task.menu.cls`）。
# ⚠️ **必须按类型名判，不能按格数** —— 背包和工作台都是 46 格（docs/04 坑 10n）。
#
# 字段：
#   grid/result/stride —— 合成类：材料格、产物格、每行几格
#   in/fuel/result     —— 加工类：输入格、燃料格、产物格
#   wait               —— 要不要等（冶炼要等烧完，合成不用）
#   inv_base/hot_base  —— 玩家背包区起点（换算 "物品在第几号格" 用）
CONTAINERS: dict[str, dict] = {
    "InventoryMenu": {
        "name": "背包",
        "grid": [1, 2, 3, 4], "result": 0, "stride": 2,
        "inv_base": 9, "hot_base": 36,
    },
    "CraftingMenu": {
        "name": "工作台",
        "grid": [1, 2, 3, 4, 5, 6, 7, 8, 9], "result": 0, "stride": 3,
        "inv_base": 10, "hot_base": 37,
    },
    "FurnaceMenu": {
        "name": "熔炉", "in": [0], "fuel": [1], "result": [2], "wait": True,
        "inv_base": 3, "hot_base": 30,
    },
    "BlastFurnaceMenu": {
        "name": "高炉", "in": [0], "fuel": [1], "result": [2], "wait": True,
        "inv_base": 3, "hot_base": 30,
    },
    "SmokerMenu": {
        "name": "烟熏炉", "in": [0], "fuel": [1], "result": [2], "wait": True,
        "inv_base": 3, "hot_base": 30,
    },
    "StonecutterMenu": {
        "name": "切石机", "in": [0], "result": [1], "needs_choice": True,
        "inv_base": 2, "hot_base": 29,
    },
    "SmithingMenu": {
        "name": "锻造台",
        "in": [0, 1, 2], "result": [3], "roles": ["模板", "基底", "添加物"],
        "inv_base": 4, "hot_base": 31,
    },
    "ContainerScreen": {          # 箱子/木桶（大箱子 90 格，小箱子/木桶 63 格）
        "name": "箱子", "result": None,
        "inv_base": 27, "hot_base": 54,
    },
    "ShulkerBoxMenu": {
        "name": "潜影盒", "result": None,
        "inv_base": 27, "hot_base": 54,
    },
    "HopperMenu": {
        "name": "漏斗", "result": None,
        "inv_base": 5, "hot_base": 32,
    },
}

# 容器的"输入/产物"角色名 → 表里字段名。别在业务代码里写字符串。
ROLE_SLOTS = {
    "crafting": ("grid", "result"),
    "furnace": ("in", "result"),
    "stonecutter": ("in", "result"),
    "smithing": ("in", "result"),
}


def layout_for(menu: dict | None) -> dict | None:
    """从 `mcb state` 的 `task.menu` 里认出这是哪种容器，返回它的布局。认不出返回 None。

    ⚠️ **只看类型名**。曾经按"是不是默认界面"来判，结果**把开着的箱子当成了工作台**，
    点了一堆箱子格子（docs/04 坑 10n）。
    """
    if not isinstance(menu, dict):
        return None
    cls = str(menu.get("cls") or "")
    for key, spec in CONTAINERS.items():
        if key in cls:
            out = dict(spec)
            out["cls"] = cls
            out["slots"] = menu.get("slots")
            out["id"] = menu.get("id")
            return out
    return None


def is_default(menu: dict | None) -> bool:
    return isinstance(menu, dict) and bool(menu.get("def"))


# ---- 原语 -----------------------------------------------------------------

class ContainerIO:
    """**所有"点格子"都走这里。** 上层（合成/冶炼/拿箱子）只负责组合这几个原语。"""

    def __init__(self, bridge, *, delay: float = 0.4) -> None:
        self.bridge = bridge
        self.delay = delay

    async def menu(self) -> dict | None:
        task = ((await self.bridge.call("mcb state")).get("data") or {}).get("task") or {}
        m = task.get("menu")
        return m if isinstance(m, dict) else None

    async def layout(self) -> dict | None:
        return layout_for(await self.menu())

    async def click(self, slot: int, button: int = BTN_LEFT, mode: int = MODE_PICKUP) -> None:
        await self.bridge.call(f"mcb clickSlot {slot} {button} {mode}")
        await asyncio.sleep(self.delay)

    async def slot_of(self, item_id: str, layout: dict) -> int | None:
        """这种物品在**当前界面**的第几号格。只用注册名匹配（显示名跟语言走）。

        ⚠️ `main` 是**稀疏**列表，不能用 enumerate 序号 —— 要用服务端给的 `slot`。
        """
        data = (await self.bridge.call("mcb inventory")).get("data") or {}
        for i, it in enumerate(data.get("hotbar") or []):
            if isinstance(it, dict) and _rid(it) == item_id:
                return int(layout["hot_base"]) + i
        for it in (data.get("main") or []):
            if isinstance(it, dict) and _rid(it) == item_id:
                raw = it.get("slot")
                idx = (int(raw) - 9) if isinstance(raw, (int, float)) else 0
                return int(layout["inv_base"]) + max(0, idx)
        return None

    async def put_one(self, layout: dict, dest_slot: int, candidates: list[str]) -> bool:
        """往 `dest_slot` 放**一个**候选材料。

        抄 mineflayer：**拿起整叠 → 右键放一个 → 剩下的放回原处**。
        """
        src = None
        for cand in candidates:
            src = await self.slot_of(cand, layout)
            if src is not None:
                break
        if src is None:
            logger.warning(f"[mc_body] 摆料：身上没有 {candidates[0] if candidates else '?'}")
            return False
        await self.click(src, BTN_LEFT, MODE_PICKUP)
        await self.click(dest_slot, BTN_RIGHT, MODE_PICKUP)
        await self.click(src, BTN_LEFT, MODE_PICKUP)
        return True

    async def take_all(self, slot: int) -> None:
        """shift 点一格 —— 把那一叠整个挪走（产物格 / 箱子格都这么拿）。"""
        await self.click(slot, BTN_LEFT, MODE_QUICK_MOVE)

    async def empty_slots(self, slots: list[int], max_rounds: int = 8) -> None:
        """把一组格子搬空（比如开箱子全拿走）。shift 点一遍，反复直到不再减少。"""
        for _ in range(max_rounds):
            before = await self._count_items(slots)
            if before == 0:
                return
            for s in slots:
                await self.take_all(s)
            if await self._count_items(slots) >= before:
                return          # 没进展了，别死循环

    async def _count_items(self, slots: list[int]) -> int:
        m = await self.menu()
        if not isinstance(m, dict):
            return 0
        want = set(int(s) for s in slots)
        return sum(int(x.get("c") or 0) for x in (m.get("items") or [])
                   if int(x.get("i") or -1) in want)


def _rid(item: dict) -> str:
    """取注册名。**绝不回退到显示名** —— 那会跟语言走，匹配必然错（docs/04 坑 10o）。"""
    return str(item.get("id") or "")


# ---- 「前置条件」原语 ------------------------------------------------------
#
# ⚠️ 这一层的意义：**上层不该关心"工作台在哪"**。
#    白说"做把木镐"，它不该需要先知道"要先找张桌子"。
#    需要什么容器，这一层自己去找、走过去、打开。

async def find_block(bridge, block_id: str, radius: int = 16) -> list[int] | None:
    """附近找某个方块，返回 [x, y, z]；没有返回 None。

    ⚠️ 扫描**不会截断类型列表**了（曾经 `slice(0,25)` 把唯一的工作台挤掉，
    见 docs/04 坑 10r）—— 但**全图只有一个**的那种方块仍然容易落在半径外，
    所以找不到时该考虑加大半径，而不是断言"没有"。
    """
    try:
        sc = (await bridge.call(f"mcb scan {int(radius)}")).get("data") or {}
    except Exception:  # noqa: BLE001
        return None
    for t in (sc.get("types") or []):
        if isinstance(t, dict) and str(t.get("id")) == block_id:
            pos = t.get("nearest")
            if isinstance(pos, list) and len(pos) >= 3:
                return [int(pos[0]), int(pos[1]), int(pos[2])]
    return None


async def open_block(bridge, block_id: str, io: "ContainerIO",
                     want_cls: str, radius: int = 16) -> bool:
    """**走过去 + 右键打开**指定方块，并确认开出来的界面类型对得上。

    `want_cls` 是期望的界面类型关键字（比如 `"CraftingMenu"`）——
    用来确认"真的开对了"，而不是"发了右键就当成功"。
    """
    pos = await find_block(bridge, block_id, radius)
    if pos is None:
        logger.warning(f"[mc_body] 附近 {radius} 格内没有 {block_id}")
        return False
    x, y, z = pos
    logger.info(f"[mc_body] 走向 {block_id} ({x},{y},{z})")
    await bridge.call("mcb closeGui")
    await bridge.call(f"mcb baritone goto {x} {z}")
    for _ in range(12):
        await asyncio.sleep(2)
        task = ((await bridge.call("mcb state")).get("data") or {}).get("task") or {}
        if task.get("status") == "idle":
            break
    await bridge.call("mcb stop")
    await asyncio.sleep(0.5)
    await bridge.call(f"mcb useOnAt {x} {y} {z}")
    await asyncio.sleep(1.2)
    menu = await io.menu()
    cls = str((menu or {}).get("cls") or "")
    if want_cls in cls:
        return True
    logger.warning(f"[mc_body] 开了 {cls}，不是期望的 {want_cls}")
    return False


async def run_process(io: "ContainerIO", layout: dict, inputs: list[list[str]],
                      fuel: list[str] | None,
                      result_slots: list[int], *, timeout: float = 30.0) -> bool:
    """**加工类容器的一趟活**：放输入（+燃料）→ 等 → 取产物。

    冶炼/高炉/烟熏炉共用这一个 —— 它们的差别只有 `CONTAINERS` 里那几行表。
    """
    for slot, cands in zip(layout.get("in") or [], inputs):
        if not await io.put_one(layout, slot, cands):
            return False
    if fuel and layout.get("fuel"):
        if not await io.put_one(layout, int(layout["fuel"][0]), fuel):
            logger.warning("[mc_body] 没找到燃料")

    if layout.get("wait"):
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(1.5)
            waited += 1.5
            m = await io.menu()
            if any(int(x.get("i") or -1) in set(result_slots)
                   for x in ((m or {}).get("items") or [])):
                break
    for s in result_slots:
        await io.take_all(s)
    return True
