
from __future__ import annotations

import asyncio
import math
import uuid

from astrbot.api import logger

from .arbiter import LEVEL_TASK

BTN_LEFT = 0
BTN_RIGHT = 1
MODE_PICKUP = 0          
MODE_QUICK_MOVE = 1      
MODE_SWAP = 2            

# 这些是 InventoryMenu 固定格号，不是当前容器的布局。
# SWAP 的 button=40 表示副手，不是第 40 格。
OFFHAND_SLOT = 45
ARMOR_SLOTS = {"head": 5, "chest": 6, "legs": 7, "feet": 8}
SWAP_OFFHAND = 40
WEAR_PLACES = ("hand", "off", "head", "chest", "legs", "feet")

# 按类型名而非格数识别；背包和工作台格数相同。
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
    "ContainerScreen": {          
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

ROLE_SLOTS = {
    "crafting": ("grid", "result"),
    "furnace": ("in", "result"),
    "stonecutter": ("in", "result"),
    "smithing": ("in", "result"),
}


def _from_roles(roles: str) -> dict:
    r"""背包仅认完整连续 Y 段；输入与燃料仍依赖表。"""
    out: dict = {}
    grid = [i for i, r in enumerate(roles) if r == "G"]
    marked = [i for i, r in enumerate(roles) if r == "R"]
    if grid:
        out["grid"] = grid
        out["stride"] = 3 if len(grid) == 9 else 2
        out["result"] = marked[0] if marked else None
    else:
        # 只认唯一的只取不放槽，多个时不猜产物。
        outputs = [i for i, r in enumerate(roles) if r == "O"]
        if marked:
            out["result"] = marked[0]
        elif len(outputs) == 1:
            out["result"] = outputs[0]
    # 仅推断长度恰为 36 的 Y 段；护甲和副手会与背包连成更长段。
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
    r"""表与角色均无法识别时返回 None，不伪造空布局。
    两条来源，合起来用：
      1 CONTAINERS 表 —— 按界面类型名查（有输入格/燃料格这类只有表才知道的信息）
      2 客户端的每格角色串 —— 按槽位类型推（合成格、产物格、背包区，任何模组都认）"""
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
    # 覆盖 result 时保留表中整数或列表的形状，否则加工调用会出错。
    for k, v in derived.items():
        if k == "result":
            old = out.get("result")
            if isinstance(old, list) and not isinstance(v, list):
                v = [v] if v is not None else None
        elif k in ("inv_base", "hot_base") and k in out:
            # 已知布局优先，角色串无法可靠区分相连的护甲和副手。
            continue
        out[k] = v
    if roles:
        out["roles"] = roles
    return out


def is_default(menu: dict | None) -> bool:
    return isinstance(menu, dict) and bool(menu.get("def"))




def slot_side(menu: dict | None, slot: int) -> str | None:
    r"""合成输入/产物单列 bench，避免自有材料被取物权限拦截。
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
    lay = layout_for(menu)
    if isinstance(lay, dict):
        inv_base = lay.get("inv_base")
        hot_base = lay.get("hot_base")
        if isinstance(inv_base, int) and isinstance(hot_base, int):
            if inv_base <= idx < hot_base + 9:
                return "mine"
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
    r"""未知点击模式保守按取物；此门仅覆盖 mc_click。"""
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



class ContainerIO:

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
        r"""main 为稀疏列表，使用服务端 slot，不用枚举下标。"""
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
        r"""        抄 mineflayer：拿起整叠  ->  右键放一个  ->  剩下的放回原处。
        """
        src = None
        for cand in candidates:
            src = await self.slot_of(cand, layout)
            if src is not None:
                break
        if src is None:
            logger.warning(f"[mc_body] 摆料：身上没有 {candidates[0] if candidates else '?'}")
            return False
        before = await self.menu()
        if not before or before.get("id") != layout.get("id"):
            return False
        old = next((it for it in before.get("items", [])
                    if int(it.get("i", -1)) == dest_slot), {})
        old_count = int(old.get("c") or 0)
        await self.click(src, BTN_LEFT, MODE_PICKUP)
        await self.click(dest_slot, BTN_RIGHT, MODE_PICKUP)
        await self.click(src, BTN_LEFT, MODE_PICKUP)
        after = await self.menu()
        if not after or after.get("id") != before.get("id"):
            return False
        placed = next((it for it in after.get("items", [])
                       if int(it.get("i", -1)) == dest_slot), {})
        return (placed.get("id") in candidates
                and int(placed.get("c") or 0) == old_count + 1)

    async def put_stack(self, layout: dict, dest_slot: int, candidates: list[str]) -> bool:
        r"""目标必须为空，否则操作会交换而非放入。"""
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
        await self.click(slot, BTN_LEFT, MODE_QUICK_MOVE)

    async def empty_slots(self, slots: list[int], max_rounds: int = 8) -> None:
        for _ in range(max_rounds):
            before = await self.count_slots(slots)
            if before == 0:
                return
            for s in slots:
                await self.take_all(s)
            if await self.count_slots(slots) >= before:
                return          

    async def count_slots(self, slots: list[int]) -> int:
        m = await self.menu()
        if not isinstance(m, dict):
            return 0
        want = set(int(s) for s in slots)
        return sum(int(x.get("c") or 0) for x in (m.get("items") or [])
                   if int(x.get("i") or -1) in want)

    async def send(self, command: str) -> dict | None:
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
        return await self.send("mcb inventory") or {}


def _rid(item: object) -> str:
    r"""只用注册名，空槽位先判类型，不回退显示名。"""
    return str((item or {}).get("id") or "") if isinstance(item, dict) else ""

async def wear(io: ContainerIO, item_id: str, where: str = "hand") -> str | None:
    w = str(where or "hand").strip().lower()

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
            await io.send(f"mcb hotbar {idx}")     
            return None
        await io.click(slot, idx, MODE_SWAP)
        # 搬完后维持选中槽，防止其他动作改掉手持物。
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
            return None                            
        if slot >= hot_base + 9:
            return None                            
        dest = await find_empty_main_slot(io, layout)
        if dest is None:
            return "背包满了，腾不出空格放它 —— 先丢掉/用掉点什么。"
        # SWAP 只能指定快捷栏；背包空格用拿起再放下。
        await io.click(slot, BTN_LEFT, MODE_PICKUP)
        await io.click(dest, BTN_LEFT, MODE_PICKUP)
        return None

    if w not in WEAR_PLACES:
        return (f"不认得「{where}」这个位置（只认 {' / '.join(WEAR_PLACES)}"
                " / hotbar0~hotbar8 / backpack）")

    # 快捷栏不依赖界面布局，必须先处理。
    data = await io.inventory()
    hot_index = None
    for i, it in enumerate(data.get("hotbar") or []):
        if _rid(it) == item_id:
            hot_index = i
            break
    if w == "hand" and hot_index is not None:
        # 持续维持选中槽，不能只切换一次。
        await io.send(f"mcb hotbar {hot_index}")
        return None

    layout = await io.layout()
    if layout is None:
        return "读不到界面布局（客户端没上报界面信息？）"

    slot = await io.slot_of(item_id, layout)
    if slot is None:
        # slot_of 不查副手，必须单独读取。
        if _rid(data.get("offhand")) == item_id:
            slot = _offhand_slot(layout)
        else:
            return f"身上没有 {item_id}"

    if w == "hand":
        hot_base = int(layout["hot_base"])
        if hot_base <= slot < hot_base + 9:
            await io.send(f"mcb hotbar {int(slot) - hot_base}")
            return None
        # 副手 hot_base+9 不属于快捷栏，不能仅用下界判断。
        await io.click(slot, 0, MODE_SWAP)
        await io.send("mcb hotbar 0")
        return None

    if w == "off":
        if slot == _offhand_slot(layout):
            return None          
        await io.click(slot, SWAP_OFFHAND, MODE_SWAP)
        return None

    await io.click(slot, BTN_LEFT, MODE_QUICK_MOVE)
    return None


async def find_empty_main_slot(io: ContainerIO, layout: dict) -> int | None:
    r"""库存是稀疏列表；默认菜单快照不包含完整背包。"""
    data = await io.inventory()
    inv_base, hot_base = int(layout["inv_base"]), int(layout["hot_base"])
    occupied = set()
    for it in (data.get("main") or []):
        if not isinstance(it, dict) or not it.get("id"):
            continue
        raw = it.get("slot")
        if isinstance(raw, (int, float)):
            occupied.add(inv_base + max(0, int(raw) - 9))
    for s in range(inv_base, hot_base):
        if s not in occupied:
            return s
    return None


def _offhand_slot(layout: dict) -> int:
    r"""副手按 hot_base+9 换算。"""
    return int(layout["hot_base"]) + 9


ARRIVE_RADIUS = 3.0
# Baritone 中途会短暂 idle，需连续确认停止。
STOP_DEBOUNCE = 2


async def wait_baritone(bridge, *, timeout: float = 60.0, target=None,
                        checkpoint=None) -> str:
    r"""先确认起步再确认停止；目标附近允许原地到达。"""
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
            ready = bool(st.get("online")) and bool(task.get("available"))
            near = target is not None and _within(st, target, ARRIVE_RADIUS)
            if ready and moved and still >= STOP_DEBOUNCE:
                if near:
                    return "arrived"
                return "stopped" if target is not None else "unconfirmed"
            if ready and not moved and near:
                return "arrived"
        await asyncio.sleep(1.0)
        waited += 1.0
    return "timeout"


def _within(state: dict, target, radius: float) -> bool:
    try:
        return math.dist((float(state["x"]), float(state["z"])), target) <= radius
    except (KeyError, TypeError, ValueError):
        return False


async def find_block(bridge, block_id: str, radius: int = 16) -> list[int] | None:
    r"""半径受服务端预算钳制；未找到不等于范围外不存在。"""
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
                     want_cls: str, radius: int = 16, *, arbiter=None) -> tuple[bool, str]:
    r"""分别核实到达和菜单类型，失败返回具体阶段。"""
    pos = await find_block(bridge, block_id, radius)
    if pos is None:
        logger.warning(f"[mc_body] 附近 {radius} 格内没有 {block_id}")
        return False, f"附近 {radius} 格内没找到 {block_id}"
    x, y, z = pos
    logger.info(f"[mc_body] 走向 {block_id} ({x},{y},{z})")
    if arbiter is None:
        return False, "移动仲裁未接线，未移动或打开容器"
    holder = arbiter.walk_holder()
    if holder is not None and holder.level <= LEVEL_TASK:
        return False, "身体正执行其他路线，未移动或打开容器"
    owner = "container:" + uuid.uuid4().hex

    async def checkpoint():
        holder = arbiter.walk_holder()
        if holder is None or holder.owner != owner:
            raise InterruptedError("容器路线被抢占或撤销")
        if arbiter.last_error:
            raise InterruptedError("容器路线下发失败：" + arbiter.last_error)

    try:
        await arbiter.claim_walk(LEVEL_TASK, owner, f"goto {x} {z}", "打开容器")
        await checkpoint()
        await bridge.call("mcb closeGui")
        await checkpoint()
        got = await wait_baritone(bridge, timeout=30.0, target=(float(x), float(z)),
                                  checkpoint=checkpoint)
        logger.info(f"[mc_body] 走到 {block_id} 附近：{got}")
        if got != "arrived":
            return False, f"{block_id} 在 ({x},{y},{z})，但未确认到达（{got}）"
        await checkpoint()
        await arbiter.claim_walk(LEVEL_TASK, owner, None, "容器交互")
        await checkpoint()
        await asyncio.sleep(0.5)
        await checkpoint()
        await bridge.call(f"mcb useOnAt {x} {y} {z}")
        await asyncio.sleep(1.2)
        await checkpoint()
        menu = await io.menu()
        cls = str((menu or {}).get("cls") or "")
        if want_cls in cls:
            return True, ""
        logger.warning(f"[mc_body] 开了 {cls}，不是期望的 {want_cls}")
        return False, (
            f"走到 ({x},{y},{z}) 也右键了，但开出来的是 `{cls or '(空)'}`、不是 {want_cls}"
            f" —— 多半是**手上还开着别的容器顶不掉**"
        )
    except InterruptedError as exc:
        return False, str(exc)
    finally:
        await arbiter.release_walk(owner)


async def run_process(io: "ContainerIO", layout: dict, inputs: list[list[str]],
                      fuel: list[str] | None,
                      result_slots: list[int], *, timeout: float = 30.0) -> bool:
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
