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
import math

from astrbot.api import logger

# clickSlot 的参数（对应原版 ClickType）
BTN_LEFT = 0
BTN_RIGHT = 1
MODE_PICKUP = 0          # 普通点（拿起/放下）
MODE_QUICK_MOVE = 1      # shift 快速移动
MODE_SWAP = 2            # 和快捷栏/副手对调

# ---- 玩家自己身上那几个固定格子 -------------------------------------------
#
# ⚠️ 这些号是**原版 `InventoryMenu` 定死的**（不是"某个容器"的格子）：
#     0 合成产物 · 1~4 合成格 · **5~8 护甲(头/胸/腿/脚)** · 9~35 背包 · 36~44 快捷栏 · **45 副手**
#
# `ClickType.SWAP` 的 `button` 也有约定：**0~8 = 快捷栏第几格**，**40 = 副手**。
# （40 不是"第 40 格"，是原版按 F 键时用的魔法值。）
OFFHAND_SLOT = 45
ARMOR_SLOTS = {"head": 5, "chest": 6, "legs": 7, "feet": 8}
SWAP_OFFHAND = 40
WEAR_PLACES = ("hand", "off", "head", "chest", "legs", "feet")

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

    async def put_stack(self, layout: dict, dest_slot: int, candidates: list[str]) -> bool:
        """把**整叠**候选材料放进 `dest_slot`（冶炼/烧炼要一次放一堆，不是放一个）。

        ⚠️ **目标格必须已经空着** —— 非空时"拿起整叠再放下"会变成**交换**，
        手上反而多出一叠东西，后面全乱（`put_one` 那三步没这问题，因为它最后把余料放回去了）。
        """
        src = None
        for cand in candidates:
            src = await self.slot_of(cand, layout)
            if src is not None:
                break
        if src is None:
            logger.warning(f"[mc_body] 放料：身上没有 {candidates[0] if candidates else '?'}")
            return False
        if await self.count_slots([dest_slot]) > 0:
            logger.warning(f"[mc_body] 放料：{dest_slot} 号格不是空的，放整叠会变成交换，跳过")
            return False
        await self.click(src, BTN_LEFT, MODE_PICKUP)
        await self.click(dest_slot, BTN_LEFT, MODE_PICKUP)
        return True

    async def take_all(self, slot: int) -> None:
        """shift 点一格 —— 把那一叠整个挪走（产物格 / 箱子格都这么拿）。"""
        await self.click(slot, BTN_LEFT, MODE_QUICK_MOVE)

    async def empty_slots(self, slots: list[int], max_rounds: int = 8) -> None:
        """把一组格子搬空（比如开箱子全拿走）。shift 点一遍，反复直到不再减少。"""
        for _ in range(max_rounds):
            before = await self.count_slots(slots)
            if before == 0:
                return
            for s in slots:
                await self.take_all(s)
            if await self.count_slots(slots) >= before:
                return          # 没进展了，别死循环

    async def count_slots(self, slots: list[int]) -> int:
        """这组格子里一共有**几个东西**（产物格空了没、输入格还剩多少）。

        判"冶炼好了没"就靠它 —— **别看格子非空就以为好了**，数量对不上是另一回事。
        """
        m = await self.menu()
        if not isinstance(m, dict):
            return 0
        want = set(int(s) for s in slots)
        return sum(int(x.get("c") or 0) for x in (m.get("items") or [])
                   if int(x.get("i") or -1) in want)

    async def send(self, command: str) -> dict | None:
        """发一条**原始桥命令**（不走容器原语时用，比如 `mcb hotbar 3`）。"""
        try:
            reply = await self.bridge.call(command)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 容器命令 {command!r} 失败：{exc}")
            return None
        if not reply.get("ok"):
            return None
        data = reply.get("data")
        return data if isinstance(data, dict) else {}

    async def inventory(self) -> dict:
        """她身上的东西。**读的是服务端真值**，不是界面快照。"""
        return await self.send("mcb inventory") or {}


def _rid(item: object) -> str:
    """取注册名。**绝不回退到显示名** —— 那会跟语言走，匹配必然错（docs/04 坑 10o）。

    ⚠️ **这一份是唯一的一份**（2026-10-10）。曾经文件里有两份同名 `_rid`，
    后一份（没判 `isinstance`）**静默覆盖**了前一份 —— 于是快捷栏里的空槽位
    （`null`）一走过就 `AttributeError`。**Python 不会警告重复定义，只会用最后一个。**
    """
    return str((item or {}).get("id") or "") if isinstance(item, dict) else ""# ---- 「装到该去的地方」------------------------------------------------------
#
# ⚠️ 这是**原版那几格**（手/副手/护甲）的通用入口。
#    **加一种新位置 = 加一行映射**（同 `CONTAINERS` 的规矩），别写新函数。

async def wear(io: ContainerIO, item_id: str, where: str = "hand") -> str | None:
    """把 `item_id` 挪到 `where`。成功返回 None，失败返回一句人话。

    · `hand`  —— 快捷栏并选中（最快，不需要看界面）
    · `off`   —— 副手（就是那个 F 键，原版 `SWAP` 的 button=40）
    · `head` / `chest` / `legs` / `feet` —— 护甲格（shift 点，**原版自己会路由**）
    · **`hotbar0` ~ `hotbar8`** —— 放进快捷栏**指定那一格**（跟那格现有的东西对调）
    · **`backpack`** —— 从快捷栏**收进背包**（找个空格放下）

    后两个是 2026-10-10 加的：她快捷栏 9 格被杂物占满、武器躺在背包里，
    没有"来回搬"的能力就谈不上换武器（见 `main/docs/13-战斗与物品.md` §2）。
    """
    w = str(where or "hand").strip().lower()

    # ---- 快捷栏指定格 / 收进背包：先在这儿分出去，它们不走 hot_base 那套 ----
    if w.startswith("hotbar"):
        try:
            idx = int(w[6:])
        except ValueError:
            return f"「{where}」看不懂 —— 快捷栏要写成 hotbar0 ~ hotbar8。"
        if not 0 <= idx <= 8:
            return f"快捷栏只有 0~8（给的是 {idx}）。"
        layout = await io.layout()
        if layout is None:
            return "读不到界面布局（客户端没上报界面信息？）"
        slot = await io.slot_of(item_id, layout)
        if slot is None:
            return f"身上没有 {item_id}"
        if slot == int(layout["hot_base"]) + idx:
            await io.send(f"mcb hotbar {idx}")     # 已经在那儿了，顺手选中
            return None
        # SWAP 的 button 就是**目标快捷栏格号**
        await io.click(slot, idx, MODE_SWAP)
        return None

    if w in ("backpack", "bag", "main", "stow", "inventory"):
        layout = await io.layout()
        if layout is None:
            return "读不到界面布局（客户端没上报界面信息？）"
        slot = await io.slot_of(item_id, layout)
        if slot is None:
            return f"身上没有 {item_id}"
        inv_base, hot_base = int(layout["inv_base"]), int(layout["hot_base"])
        if inv_base <= slot < hot_base:
            return None                            # 已经在背包里了
        if slot >= hot_base + 9:
            return None                            # 那是副手，别乱动
        dest = await find_empty_main_slot(io, layout)
        if dest is None:
            return "背包满了，腾不出空格放它 —— 先丢掉/用掉点什么。"
        # ⚠️ 这里用"拿起→放下"两步（不是 SWAP）—— SWAP 的 button 只能填快捷栏格号，
        #    对"背包里的任意空格"没法表达。放下前已确认目标格是空的，不会变成交换。
        await io.click(slot, BTN_LEFT, MODE_PICKUP)
        await io.click(dest, BTN_LEFT, MODE_PICKUP)
        return None

    if w not in WEAR_PLACES:
        return (f"不认得「{where}」这个位置（只认 {' / '.join(WEAR_PLACES)}"
                " / hotbar0~hotbar8 / backpack）")

    # ① 快捷栏 —— **这条路不需要界面布局**，最省事也最可靠。
    #    （踩过：一上来就要求布局，快捷栏里的东西反而拿不到。）
    data = await io.inventory()
    hot_index = None
    for i, it in enumerate(data.get("hotbar") or []):
        if _rid(it) == item_id:
            hot_index = i
            break
    if w == "hand" and hot_index is not None:
        await io.send(f"mcb hotbar {hot_index}")
        return None

    # ② 其它位置（和"东西不在快捷栏"的情况）才需要知道界面布局
    layout = await io.layout()
    if layout is None:
        return "读不到界面布局（客户端没上报界面信息？）"

    slot = await io.slot_of(item_id, layout)
    if slot is None:
        # ⚠️ **`slot_of` 看不见副手**（它只翻 hotbar / main）——
        #    不单独认一下的话，"把盾从副手换到手上"会谎报"身上没有盾"（实战踩过）。
        if _rid(data.get("offhand")) == item_id:
            slot = _offhand_slot(layout)
        else:
            return f"身上没有 {item_id}"

    if w == "hand":
        hot_base = int(layout["hot_base"])
        if hot_base <= slot < hot_base + 9:
            # 已经在快捷栏里 —— 选中就行
            await io.send(f"mcb hotbar {int(slot) - hot_base}")
            return None
        # ⚠️⚠️ **不能只判 `slot >= hot_base`**（踩过）：副手是 `hot_base + 9`，
        #    落在快捷栏区间**外面**。按 `>= hot_base` 算会得到 `mcb hotbar 9`
        #    （快捷栏只有 0~8）→ 越界、**静默失败**，而函数还以为成功了。
        # SWAP 的 button=0 = 和快捷栏第 0 格对调，对**任何**格子都成立，包括副手。
        await io.click(slot, 0, MODE_SWAP)
        await io.send("mcb hotbar 0")
        return None

    if w == "off":
        if slot == _offhand_slot(layout):
            return None          # 已经在副手了，别自己和自己对调
        await io.click(slot, SWAP_OFFHAND, MODE_SWAP)
        return None

    # 护甲：shift 点 —— 原版 `moveItemStackTo` 会自己塞进对应的护甲格
    await io.click(slot, BTN_LEFT, MODE_QUICK_MOVE)
    return None


async def find_empty_main_slot(io: ContainerIO, layout: dict) -> int | None:
    """找一个**空着的背包格**（返回菜单格号；找不到返回 None）。

    ⚠️ **空槽位不上报** —— `mcb inventory` 只列有东西的格子，
    所以只能**反推**：背包区间 `[inv_base, hot_base)` 减去已经被占的那些。

    ⚠️ 这里用的是**服务端真值**（`mcb inventory`），不是界面快照 ——
    界面快照对默认背包界面只报 0~4 格，看不到背包里哪些是空的。
    """
    data = await io.inventory()
    inv_base, hot_base = int(layout["inv_base"]), int(layout["hot_base"])
    occupied = set()
    for it in (data.get("main") or []):
        if not isinstance(it, dict) or not it.get("id"):
            continue
        raw = it.get("slot")
        if isinstance(raw, (int, float)):
            # 服务端报的是**玩家背包格号**（9~35），换算成菜单格号
            occupied.add(inv_base + max(0, int(raw) - 9))
    for s in range(inv_base, hot_base):
        if s not in occupied:
            return s
    return None


def _offhand_slot(layout: dict) -> int:
    """副手在当前界面里的格子号。

    ⚠️ **原版所有容器都满足 `副手 = hot_base + 9`**（快捷栏 9 格之后紧跟副手）。
    已逐个核对过 `CONTAINERS` 里那十种：背包 36+9=45、工作台 37+9=46、
    熔炉 30+9=39、箱子 54+9=63、切石 29+9=38、锻造 31+9=40、漏斗 32+9=41 …
    **所以不用给每种容器各加一个字段** —— 加反而是找麻烦。
    """
    return int(layout["hot_base"]) + 9

# ---- 「前置条件」原语 ------------------------------------------------------
#
# ⚠️ 这一层的意义：**上层不该关心"工作台在哪"**。
#    白说"做把木镐"，它不该需要先知道"要先找张桌子"。
#    需要什么容器，这一层自己去找、走过去、打开。

# 走到目标几格以内算"到了"
ARRIVE_RADIUS = 3.0
# 连续几次轮询都没动才算真停 —— Baritone 中途会短暂 idle（挖一下、跳一下）
STOP_DEBOUNCE = 2


async def wait_baritone(bridge, *, timeout: float = 60.0, target=None,
                        checkpoint=None) -> str:
    """等 Baritone 走完。返回 `"arrived"` / `"timeout"`。

    ⚠️⚠️ **必须熬过"刚下发还没起步"那几秒**（2026-10-09 实战踩到）：
    刚 `goto` 完时 Baritone 的 `isPathing()` **还是 false** ——
    天真的写法（"不在走 = 到了"）会**立刻判定到达**。
    实测从 (-10,88) 去 (-45,66) 只花了 9 秒就报"到了"，而人一步没动，
    后面所有步骤全在一个**错误的位置**上干。

    **正解：先看它起步，再看它停下。** 传了 `target` 时还能确认
    "她本来就在目标附近"（那种情况没起步是对的）。

    `checkpoint` 是可选的无参协程 —— 任务层传进来，用来响应抢占。
    """
    moved = False
    still = 0
    waited = 0.0
    while waited < timeout:
        if checkpoint is not None:
            await checkpoint()
        try:
            reply = await bridge.call("mcb state")
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1.0)
            waited += 1.0
            continue
        st = reply.get("data") or {}
        task = st.get("task") or {}
        walking = bool(task.get("available")) and task.get("status") == "moving"
        if walking:
            moved = True
            still = 0
        else:
            still += 1
            if moved and still >= STOP_DEBOUNCE:
                return "arrived"
            if not moved and target is not None and _within(st, target, ARRIVE_RADIUS):
                return "arrived"
        await asyncio.sleep(1.0)
        waited += 1.0
    return "timeout"


def _within(state: dict, target, radius: float) -> bool:
    """她是不是已经在 (x, z) 的 `radius` 格以内。读不到坐标时**说不知道**（False）。"""
    try:
        return math.dist((float(state["x"]), float(state["z"])), target) <= radius
    except (KeyError, TypeError, ValueError):
        return False


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
    # ⚠️ 用**共用**的等待原语 —— 别在这儿再写一遍循环。
    #    这里原来写的是"睡 2 秒看一次，idle 就 break"，**同样会误判**：
    #    刚下发时 Baritone 还没起步，第一次轮询就是 idle，2 秒后就当到了。
    got = await wait_baritone(bridge, timeout=30.0, target=(float(x), float(z)))
    logger.info(f"[mc_body] 走到 {block_id} 附近：{got}")
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
