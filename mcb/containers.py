"""通用容器模板 —— "加新容器 = 加一行表"，不是加一个函数。

## 为什么要有这一层

所有容器类操作本质是同一个形状：

    往某个角色的格子放东西  ->  （也许等一等）  ->  从某个角色的格子拿东西

合成、开箱子、冶炼、切石、锻造全都是这个。所以不写"冶炼函数""切石函数"，
写一张表 + 一套原语。（用户 2026-10-09 原话："箱子打开来回拿不就是大部分的操作"。）

## 硬规矩

加新容器只许加一行 CONTAINERS。 如果你发现要写新函数，说明这张表没抽象对 ——
先改表，别绕过去。

## 还没做的

needs_choice（附魔台那类要选等级/选项的）—— 用户明确说"先放着"。
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
# 注意： 这些号是原版 InventoryMenu 定死的（不是"某个容器"的格子）：
#     0 合成产物 · 1~4 合成格 · 5~8 护甲(头/胸/腿/脚) · 9~35 背包 · 36~44 快捷栏 · 45 副手
#
# ClickType.SWAP 的 button 也有约定：0~8 = 快捷栏第几格，40 = 副手。
# （40 不是"第 40 格"，是原版按 F 键时用的魔法值。）
OFFHAND_SLOT = 45
ARMOR_SLOTS = {"head": 5, "chest": 6, "legs": 7, "feet": 8}
SWAP_OFFHAND = 40
WEAR_PLACES = ("hand", "off", "head", "chest", "legs", "feet")

# ---- 容器表 ---------------------------------------------------------------
#
# match 是拿界面类型名去比的（mcb state 的 task.menu.cls）。
# 注意： 必须按类型名判，不能按格数 —— 背包和工作台都是 46 格（docs/04 坑 10n）。
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

# 容器的"输入/产物"角色名  ->  表里字段名。别在业务代码里写字符串。
ROLE_SLOTS = {
    "crafting": ("grid", "result"),
    "furnace": ("in", "result"),
    "stonecutter": ("in", "result"),
    "smithing": ("in", "result"),
}


def _from_roles(roles: str) -> dict:
    """从客户端上行来的每格角色串推布局（第 3 批 3.2）。

    角色是客户端按槽位对象的类型判的（见 bridge/client/mcbridge.js 的 mcbSlotRole）：
    R 产物 · G 合成输入 · Y 她自己的背包 · O 只能拿不能放 · C 别的容器格 · ? 认不出。

    能推出来的：
      grid / result —— R 和 G 直接就是格号，所以任何模组的"合成式"界面都自动认，
                       不用再加 CONTAINERS 里的表行
      inv_base / hot_base —— 找最长的连续 Y 段（她的背包）。注意不能拿"第一个 Y"：
                       默认界面里 5~8 是护甲格，它们也算 Y，第一个 Y 是 5 不是 9。
                       背包段是 27 格主背包 + 9 格快捷栏，所以 hot_base = inv_base + 27。
    推不出来的：熔炉那种"输入格 / 燃料格"—— 两者在类型上都是普通容器格，分不开，
    所以那一半仍靠 CONTAINERS 表。这是承认的边界，别硬凑。
    """
    out: dict = {}
    grid = [i for i, r in enumerate(roles) if r == "G"]
    marked = [i for i, r in enumerate(roles) if r == "R"]
    if grid:
        out["grid"] = grid
        out["stride"] = 3 if len(grid) == 9 else 2
        out["result"] = marked[0] if marked else None
    else:
        # 没有合成格：产物格靠"只能拿不能放"认。只有一个才敢认，多了宁可不猜。
        outputs = [i for i, r in enumerate(roles) if r == "O"]
        if marked:
            out["result"] = marked[0]
        elif len(outputs) == 1:
            out["result"] = outputs[0]
    # 最长的连续 Y 段 = 她背包那一片。长度**正好 36** 才敢用。
    #
    # 注意： 多出来的 Y 分不出来是护甲还是副手 ——
    #    默认界面实测是 "RGGGG" + 41 个 Y：5~8 是护甲格（在前），
    #    45 是副手格（在后），它们都算 Y 而且都跟背包连着。
    #    所以"取最后 36 格"和"取前 36 格"都不对（副手那次会把起点算成 10）。
    #    长度不是 36 就不推 —— 宁可让上层报"认不出"，也别照着错的格号去点格子。
    #    （已知容器的准确值在 CONTAINERS 表里，走的是另一条路，不受这条影响。）
    best_start, best_len = None, 0
    run_start, run_len = None, 0
    for i, r in enumerate(roles):
        if r == "Y":
            if run_start is None:
                run_start = i
            run_len += 1
            if run_len > best_len:
                best_start, best_len = run_start, run_len
        else:
            run_start, run_len = None, 0
    if best_len == 36:
        out["inv_base"] = best_start
        out["hot_base"] = best_start + 27
    return out


def layout_for(menu: dict | None) -> dict | None:
    """从 mcb state 的 task.menu 里认出这是哪种容器，返回它的布局。认不出返回 None。

    两条来源，合起来用：
      1 CONTAINERS 表 —— 按界面类型名查（有输入格/燃料格这类只有表才知道的信息）
      2 客户端的每格角色串 —— 按槽位类型推（合成格、产物格、背包区，任何模组都认）

    第 2 条是第 3 批 3.2 加的。加它之前，没进表的容器一律返回 None（模组机器界面
    整个用不了），进过表的也得一个模组一个模组加行。现在只要那个界面是
    服务端同步的 AbstractContainerMenu，角色串就能推出来。

    注意： 表命中不了、角色串也推不出东西时仍然返回 None —— 那就是真的认不出来，
    别返回一个空壳让上层以为能用。
    """
    if not isinstance(menu, dict):
        return None
    cls = str(menu.get("cls") or "")
    roles = str(menu.get("roles") or "")
    base = None
    for key, spec in CONTAINERS.items():
        if key in cls:
            base = dict(spec)
            break
    derived = _from_roles(roles) if roles else {}
    if base is None and not derived:
        return None
    out = base if base is not None else {"name": "未登记容器", "result": None}
    out["cls"] = cls
    out["slots"] = menu.get("slots")
    out["id"] = menu.get("id")
    # 角色串推出来的优先：它认的是槽位本身，比按类型名查表更贴近事实。
    #
    # 注意： 形状要跟着表走，不能一律盖成整数 ——
    #    合成类容器的 result 是**整数**（craft.py 用 int(layout["result"])），
    #    加工类的是**列表**（smelt.py 用 int(layout["result"][0])）。
    #    推出来的永远是单个格号，碰上表里是列表的要包成列表，
    #    否则冶炼会 TypeError（这条是自查时发现的，不是等踩了才知道）。
    for k, v in derived.items():
        if k == "result":
            old = out.get("result")
            if isinstance(old, list) and not isinstance(v, list):
                v = [v] if v is not None else None
        elif k in ("inv_base", "hot_base") and k in out:
            # 表里已经有准确值，别用角色串推的盖掉。
            # 实测过：默认界面的 Y 段有 41 格（护甲 + 背包 + 副手），
            # 从角色串推不准；表里的值是手写核过的，优先信它。
            continue
        out[k] = v
    if roles:
        out["roles"] = roles
    return out


def is_default(menu: dict | None) -> bool:
    return isinstance(menu, dict) and bool(menu.get("def"))


# ---- "这一格是谁的" 和 "这一下是不是在拿" ---------------------------------
#
# 权限层要判"她从容器拿东西"，就得先知道点的那一格属于谁。
# 这是第 3 批 3.2 的槽位角色标记解锁的能力 —— 在那之前判不出来，
# 所以 take(*) 那条规则写好了却一直没接线。


def slot_side(menu: dict | None, slot: int) -> str | None:
    """这一格属于谁那边。

    'mine'      她自己的背包
    'bench'     合成台的产物格 / 材料格 —— 那是"用手上的东西做东西"，不是谁的东西
    'container' 容器侧（箱子格、机器产出格）
    None        判不出来

    注意： 'bench' 单独分出来是必须的 —— 合成产物格（R）和材料格（G）如果算成
    "容器侧"，那 mc_craft 里点产物格取成品就会被权限门拦下，合成整个用不了。
    用她自己的材料做出来的东西，不该问主人。

    两条来源，按可靠程度排：
      1 客户端的每格角色串 —— 认的是槽位对象本身（Y = 她的背包）
      2 CONTAINERS 表的 inv_base/hot_base —— 老路子，只对进过表的容器有用
    都判不出来回 None。调用方按"不知道"处理，别猜。
    """
    if not isinstance(menu, dict):
        return None
    try:
        idx = int(slot)
    except (TypeError, ValueError):
        return None
    roles = str(menu.get("roles") or "")
    if roles and 0 <= idx < len(roles):
        r = roles[idx]
        if r == "Y":
            return "mine"
        if r in ("R", "G"):
            return "bench"
        if r in ("C", "O"):
            return "container"
        # '?' 认不出来 —— 往下走，试试表
    lay = layout_for(menu)
    if isinstance(lay, dict):
        inv_base = lay.get("inv_base")
        hot_base = lay.get("hot_base")
        if isinstance(inv_base, int) and isinstance(hot_base, int):
            # 背包段 27 格 + 快捷栏 9 格，快捷栏在最后
            if inv_base <= idx < hot_base + 9:
                return "mine"
            # 合成格和产物格在表里是 grid / result，单独认一下
            for key in ("grid", "result"):
                got = lay.get(key)
                if isinstance(got, int) and got == idx:
                    return "bench"
                if isinstance(got, list) and idx in got:
                    return "bench"
            return "container"
    return None


def _slot_has_item(menu: dict, slot: int) -> bool:
    for it in (menu.get("items") or []):
        if not isinstance(it, dict):
            continue
        try:
            if int(it.get("i")) != int(slot):
                continue
            return float(it.get("c") or 0) > 0
        except (TypeError, ValueError):
            continue
    return False


def takes_from_container(menu: dict | None, slot: int, mode: int,
                         hand_empty: bool) -> bool:
    """这一下点格子，是不是"从容器里拿东西出来"。

    判据（保守优先）：
      格子得在容器侧，而且那格上有东西 —— 空格子上拿不到任何东西。
      然后看这一下往哪边搬：
        mode=1（shift 快速移动）  -> 容器 到 她那儿，是拿
        mode=0/6（普通点 / 双击） -> 空手点算拿，手上有东西算往里放
        其余（交换 2 / 创造复制 3 / 丢 4 / 拖拽 5）-> 说不清，有东西就按拿算

    注意： 这条只管 mc_click 那条路。合成和冶炼走的是 ContainerIO 自己的点格子，
    不在这一层 —— 那是在做主人让她做的事，不是在翻主人的箱子（见 docs\\22 §3 第 5 条承认的差距）。
    """
    if not isinstance(menu, dict):
        return False
    if slot_side(menu, slot) != "container":
        return False
    if not _slot_has_item(menu, slot):
        return False
    try:
        m = int(mode)
    except (TypeError, ValueError):
        return True
    if m == 1:
        return True
    if m in (0, 6):
        return bool(hand_empty)
    return True


# ---- 原语 -----------------------------------------------------------------

class ContainerIO:
    """所有"点格子"都走这里。 上层（合成/冶炼/拿箱子）只负责组合这几个原语。"""

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
        """这种物品在当前界面的第几号格。只用注册名匹配（显示名跟语言走）。

        注意： main 是稀疏列表，不能用 enumerate 序号 —— 要用服务端给的 slot。
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
        """往 dest_slot 放一个候选材料。

        抄 mineflayer：拿起整叠  ->  右键放一个  ->  剩下的放回原处。
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
        """把整叠候选材料放进 dest_slot（冶炼/烧炼要一次放一堆，不是放一个）。

        注意： 目标格必须已经空着 —— 非空时"拿起整叠再放下"会变成交换，
        手上反而多出一叠东西，后面全乱（put_one 那三步没这问题，因为它最后把余料放回去了）。
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
        """这组格子里一共有几个东西（产物格空了没、输入格还剩多少）。

        判"冶炼好了没"就靠它 —— 别看格子非空就以为好了，数量对不上是另一回事。
        """
        m = await self.menu()
        if not isinstance(m, dict):
            return 0
        want = set(int(s) for s in slots)
        return sum(int(x.get("c") or 0) for x in (m.get("items") or [])
                   if int(x.get("i") or -1) in want)

    async def send(self, command: str) -> dict | None:
        """发一条原始桥命令（不走容器原语时用，比如 mcb hotbar 3）。"""
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
        """她身上的东西。读的是服务端真值，不是界面快照。"""
        return await self.send("mcb inventory") or {}


def _rid(item: object) -> str:
    """取注册名。绝不回退到显示名 —— 那会跟语言走，匹配必然错（docs/04 坑 10o）。

    注意： 这一份是唯一的一份（2026-10-10）。曾经文件里有两份同名 _rid，
    后一份（没判 isinstance）静默覆盖了前一份 —— 于是快捷栏里的空槽位
    （null）一走过就 AttributeError。Python 不会警告重复定义，只会用最后一个。
    """
    return str((item or {}).get("id") or "") if isinstance(item, dict) else ""# ---- 「装到该去的地方」------------------------------------------------------
#
# 注意： 这是原版那几格（手/副手/护甲）的通用入口。
#    加一种新位置 = 加一行映射（同 CONTAINERS 的规矩），别写新函数。

async def wear(io: ContainerIO, item_id: str, where: str = "hand") -> str | None:
    """把 item_id 挪到 where。成功返回 None，失败返回一句人话。

    · hand  —— 快捷栏并选中（最快，不需要看界面）
    · off   —— 副手（就是那个 F 键，原版 SWAP 的 button=40）
    · head / chest / legs / feet —— 护甲格（shift 点，原版自己会路由）
    · hotbar0 ~ hotbar8 —— 放进快捷栏指定那一格（跟那格现有的东西对调）
    · backpack —— 从快捷栏收进背包（找个空格放下）

    后两个是 2026-10-10 加的：她快捷栏 9 格被杂物占满、武器躺在背包里，
    没有"来回搬"的能力就谈不上换武器（见 main/docs/13-战斗与物品.md §2）。
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
        # SWAP 的 button 就是目标快捷栏格号
        await io.click(slot, idx, MODE_SWAP)
        # 第 3 批 3.3：搬完告诉客户端"维持手上是这个"。
        # 不给这一句的话，插件以为手上是它、实际随时可能被别的东西改掉 ——
        # 那种错从日志上很难看出来（表现是"她明明该拿镐子却在用火把"）。
        await io.send(f"mcb hotbar {idx}")
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
        # 注意： 这里用"拿起 -> 放下"两步（不是 SWAP）—— SWAP 的 button 只能填快捷栏格号，
        #    对"背包里的任意空格"没法表达。放下前已确认目标格是空的，不会变成交换。
        await io.click(slot, BTN_LEFT, MODE_PICKUP)
        await io.click(dest, BTN_LEFT, MODE_PICKUP)
        return None

    if w not in WEAR_PLACES:
        return (f"不认得「{where}」这个位置（只认 {' / '.join(WEAR_PLACES)}"
                " / hotbar0~hotbar8 / backpack）")

    # 1 快捷栏 —— 这条路不需要界面布局，最省事也最可靠。
    #    （踩过：一上来就要求布局，快捷栏里的东西反而拿不到。）
    data = await io.inventory()
    hot_index = None
    for i, it in enumerate(data.get("hotbar") or []):
        if _rid(it) == item_id:
            hot_index = i
            break
    if w == "hand" and hot_index is not None:
        # 第 3 批 3.3：手上拿什么变成槽 —— 告诉客户端"维持这一格"。
        # 不维持的话，"手上是它"只在发命令那一瞬间成立，之后随时会被改掉。
        await io.send(f"mcb hotbar {hot_index}")
        return None

    # 2 其它位置（和"东西不在快捷栏"的情况）才需要知道界面布局
    layout = await io.layout()
    if layout is None:
        return "读不到界面布局（客户端没上报界面信息？）"

    slot = await io.slot_of(item_id, layout)
    if slot is None:
        # 注意： slot_of 看不见副手（它只翻 hotbar / main）——
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
        # 注意：注意： 不能只判 slot >= hot_base（踩过）：副手是 hot_base + 9，
        #    落在快捷栏区间外面。按 >= hot_base 算会得到 mcb hotbar 9
        #    （快捷栏只有 0~8） ->  越界、静默失败，而函数还以为成功了。
        # SWAP 的 button=0 = 和快捷栏第 0 格对调，对任何格子都成立，包括副手。
        await io.click(slot, 0, MODE_SWAP)
        await io.send("mcb hotbar 0")
        return None

    if w == "off":
        if slot == _offhand_slot(layout):
            return None          # 已经在副手了，别自己和自己对调
        await io.click(slot, SWAP_OFFHAND, MODE_SWAP)
        return None

    # 护甲：shift 点 —— 原版 moveItemStackTo 会自己塞进对应的护甲格
    await io.click(slot, BTN_LEFT, MODE_QUICK_MOVE)
    return None


async def find_empty_main_slot(io: ContainerIO, layout: dict) -> int | None:
    """找一个空着的背包格（返回菜单格号；找不到返回 None）。

    注意： 空槽位不上报 —— mcb inventory 只列有东西的格子，
    所以只能反推：背包区间 [inv_base, hot_base) 减去已经被占的那些。

    注意： 这里用的是服务端真值（mcb inventory），不是界面快照 ——
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
            # 服务端报的是玩家背包格号（9~35），换算成菜单格号
            occupied.add(inv_base + max(0, int(raw) - 9))
    for s in range(inv_base, hot_base):
        if s not in occupied:
            return s
    return None


def _offhand_slot(layout: dict) -> int:
    """副手在当前界面里的格子号。

    注意： 原版所有容器都满足 副手 = hot_base + 9（快捷栏 9 格之后紧跟副手）。
    已逐个核对过 CONTAINERS 里那十种：背包 36+9=45、工作台 37+9=46、
    熔炉 30+9=39、箱子 54+9=63、切石 29+9=38、锻造 31+9=40、漏斗 32+9=41 …
    所以不用给每种容器各加一个字段 —— 加反而是找麻烦。
    """
    return int(layout["hot_base"]) + 9

# ---- 「前置条件」原语 ------------------------------------------------------
#
# 注意： 这一层的意义：上层不该关心"工作台在哪"。
#    白说"做把木镐"，它不该需要先知道"要先找张桌子"。
#    需要什么容器，这一层自己去找、走过去、打开。

# 走到目标几格以内算"到了"
ARRIVE_RADIUS = 3.0
# 连续几次轮询都没动才算真停 —— Baritone 中途会短暂 idle（挖一下、跳一下）
STOP_DEBOUNCE = 2


async def wait_baritone(bridge, *, timeout: float = 60.0, target=None,
                        checkpoint=None) -> str:
    """等 Baritone 走完。返回 "arrived" / "timeout"。

    注意：注意： 必须熬过"刚下发还没起步"那几秒（2026-10-09 实战踩到）：
    刚 goto 完时 Baritone 的 isPathing() 还是 false ——
    天真的写法（"不在走 = 到了"）会立刻判定到达。
    实测从 (-10,88) 去 (-45,66) 只花了 9 秒就报"到了"，而人一步没动，
    后面所有步骤全在一个错误的位置上干。

    正解：先看它起步，再看它停下。 传了 target 时还能确认
    "她本来就在目标附近"（那种情况没起步是对的）。

    checkpoint 是可选的无参协程 —— 任务层传进来，用来响应抢占。
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
    """她是不是已经在 (x, z) 的 radius 格以内。读不到坐标时说不知道（False）。"""
    try:
        return math.dist((float(state["x"]), float(state["z"])), target) <= radius
    except (KeyError, TypeError, ValueError):
        return False


async def find_block(bridge, block_id: str, radius: int = 16) -> list[int] | None:
    """附近找某个方块，返回 [x, y, z]；没有返回 None。

    注意： 2026-10-10 起半径由服务端按预算钳制 —— 传 16 实际只会扫到 11
    （服务端 MCB_SCAN_BUDGET，实测 Rhino 下每格 ~10µs，再大就是几百毫秒的卡顿）。
    返回值里的 clampedFrom/clampedTo 能看到有没有被钳。

    注意： 扫描不会截断类型列表了（曾经 slice(0,25) 把唯一的工作台挤掉，
    见 docs/04 坑 10r）—— 但全图只有一个的那种方块仍然容易落在半径外，
    所以找不到时该考虑换个办法（mc_around 走方块实体那条路），而不是断言"没有"。
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
                     want_cls: str, radius: int = 16) -> tuple[bool, str]:
    """走过去 + 右键打开指定方块，并确认开出来的界面类型对得上。

    返回 (成功?, 原因) —— 注意： 原因必须具体到是哪一步断的。

    踩过（2026-10-10）：调用方原来只拿到一个 bool，于是只能说
    "要么附近没工作台/走不过去/容器顶不掉" —— 三种原因混成一句，她分不清该干什么。
    （她复述的原话："熔炉和石剑报同一个错：附近没工作台走不过去，或者手上开着别的容器顶不掉"。）

    注意：注意： 而且原来 wait_baritone 的返回值压根没被检查 —— 走不过去也照样右键，
    然后报"界面不对"，于是"走不过去"和"容器顶不掉"在外部看起来一模一样。已修。
    """
    pos = await find_block(bridge, block_id, radius)
    if pos is None:
        logger.warning(f"[mc_body] 附近 {radius} 格内没有 {block_id}")
        return False, f"附近 {radius} 格内没找到 {block_id}"
    x, y, z = pos
    logger.info(f"[mc_body] 走向 {block_id} ({x},{y},{z})")
    await bridge.call("mcb closeGui")
    await bridge.call(f"mcb baritone goto {x} {z}")
    # 注意： 用共用的等待原语 —— 别在这儿再写一遍循环。
    #    这里原来写的是"睡 2 秒看一次，idle 就 break"，同样会误判：
    #    刚下发时 Baritone 还没起步，第一次轮询就是 idle，2 秒后就当到了。
    got = await wait_baritone(bridge, timeout=30.0, target=(float(x), float(z)))
    logger.info(f"[mc_body] 走到 {block_id} 附近：{got}")
    await bridge.call("mcb stop")
    if got != "arrived":
        return False, f"{block_id} 在 ({x},{y},{z})，但**走不过去**（寻路超时）"
    await asyncio.sleep(0.5)
    await bridge.call(f"mcb useOnAt {x} {y} {z}")
    await asyncio.sleep(1.2)
    menu = await io.menu()
    cls = str((menu or {}).get("cls") or "")
    if want_cls in cls:
        return True, ""
    logger.warning(f"[mc_body] 开了 {cls}，不是期望的 {want_cls}")
    return False, (
        f"走到 ({x},{y},{z}) 也右键了，但开出来的是 `{cls or '(空)'}`、不是 {want_cls}"
        f" —— 多半是**手上还开着别的容器顶不掉**"
    )


async def run_process(io: "ContainerIO", layout: dict, inputs: list[list[str]],
                      fuel: list[str] | None,
                      result_slots: list[int], *, timeout: float = 30.0) -> bool:
    """加工类容器的一趟活：放输入（+燃料） ->  等  ->  取产物。

    冶炼/高炉/烟熏炉共用这一个 —— 它们的差别只有 CONTAINERS 里那几行表。
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
