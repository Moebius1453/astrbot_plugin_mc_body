"""冶炼 —— 熔炉那一趟活。

## 这一层是**薄的**（按 docs/11 的硬规矩）

真正干活的是 `containers.py` 的通用原语：`open_block`（走过去+开容器）
和 `ContainerIO`（点格子）。**这里没有发明任何新机制**，只多三件事：

  1. **挑配方** —— 产物有好几个冶炼配方（铁矿/深层铁矿/粉碎矿…），
     要挑**她身上真的有料**的那一个，而不是报"缺铁矿"却让她身上揣着生铁干瞪眼
  2. **配燃料** —— 从一组候选里挑一个她有的
  3. **等烧完** —— 熔炉是**加工类容器**（`wait: True`），跟合成不一样，放进去还得等

⚠️ **没有"冶炼函数"这种东西**：熔炉/高炉/烟熏炉在 `CONTAINERS` 里**各是一行表**，
共用同一套原语。想加高炉，加一行表就行 —— 别在这儿写第二个函数。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

from .containers import ContainerIO, open_block

# 熔炉类方块 → 期望开出来的界面类型（用来确认"真的开对了"）
FURNACE_BLOCK = "minecraft:furnace"
FURNACE_CLS = "FurnaceMenu"

# 走到熔炉附近再开。⚠️ 服务端 `mcb scan` 的半径**由预算钳制**（2026-10-10 起），
# 传 16 实际只扫到约 11 —— 再大就是几百毫秒的 tick 卡顿（实测 Rhino 每格 ~10µs）。
FURNACE_RADIUS = 16

# 燃料候选。**只放"塞进熔炉燃料格一定能烧"的东西**，并按耐烧程度排序。
# ⚠️ 木制品的燃烧时间差很多（木板 15 秒 / 木棍 5 秒），排在煤炭后面只是聊胜于无。
FUELS = (
    "minecraft:coal",
    "minecraft:charcoal",
    "minecraft:coal_block",
    "minecraft:blaze_rod",
    "minecraft:oak_planks",
    "minecraft:stick",
)

# 等烧完的上限。一个铁矿 10 秒，留足余量。
SMELT_TIMEOUT = 60.0

# 轮询间隔
POLL = 2.0


class Smelter:
    """一趟冶炼。**用完就扔**，不持有状态。"""

    def __init__(self, bridge, journal=None) -> None:
        self.bridge = bridge
        self.journal = journal

    def _note(self, text: str) -> None:
        if self.journal is not None:
            self.journal.add("craft", text)

    async def _call(self, command: str) -> dict | None:
        try:
            reply = await self.bridge.call(command)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 冶炼命令 {command!r} 失败：{exc}")
            return None
        if not reply.get("ok"):
            return None
        data = reply.get("data")
        return data if isinstance(data, dict) else {}

    async def _inventory(self) -> dict:
        return await self._call("mcb inventory") or {}

    async def _count(self, item_id: str) -> int:
        data = await self._inventory()
        total = 0
        for key in ("hotbar", "main"):
            for it in (data.get(key) or []):
                if isinstance(it, dict) and str(it.get("id")) == item_id:
                    total += int(it.get("c") or 0)
        return total

    async def _have_ids(self) -> set[str]:
        data = await self._inventory()
        out = {str(it.get("id")) for it in (data.get("hotbar") or []) if isinstance(it, dict)}
        out |= {str(it.get("id")) for it in (data.get("main") or []) if isinstance(it, dict)}
        off = data.get("offhand")
        if isinstance(off, dict) and off.get("id"):
            out.add(str(off["id"]))
        return out

    async def find_recipe(self, target: str) -> tuple[str, list[str]] | None:
        """给产物名，找一个**她身上真有料**的冶炼配方。

        返回 `(配方id, 候选原料列表)`；没有可做的返回 None。

        ⚠️ 一个产物往往有好几个冶炼配方（`iron_ingot` 就有：铁矿 / 深层铁矿 /
        粉碎矿 / 生铁…）。**必须挑她有的那个** —— 否则会报"缺铁矿石"，
        而她身上明明揣着生铁。
        """
        data = await self._call(f"mcb recipe {target}")
        if not data:
            return None
        smelting = [r for r in (data.get("recipes") or [])
                    if isinstance(r, dict) and r.get("kind") == "smelting"]
        if not smelting:
            return None
        have = await self._have_ids()
        for r in smelting:
            for group in (r.get("shapeless") or []):
                cands = [str(c) for c in group]
                if any(c in have for c in cands):
                    return str(r.get("id") or "?"), cands
        return None

    async def _pick_fuel(self) -> list[str] | None:
        have = await self._have_ids()
        for f in FUELS:
            if f in have:
                return [f]
        return None

    async def smelt(self, target: str) -> str:
        """把 `target` 炼出来。返回一句人话（工具直接回给模型）。"""
        name = str(target or "").strip()
        if not name:
            return "没说要炼什么。"
        if ":" not in name:
            name = f"minecraft:{name}"

        found = await self.find_recipe(name)
        if found is None:
            return (
                f"炼不出 {name}。两种可能：**它本来就没有冶炼配方**"
                "（那就得用 mc_craft 在工作台做），或者**她身上没有能炼的原料**。"
                "先用 mc_inventory 看看她有什么。"
            )
        recipe_id, cands = found
        logger.info(f"[mc_body] 🔥 冶炼 {name} ← {cands}（配方 {recipe_id}）")

        fuel = await self._pick_fuel()
        if fuel is None:
            return (
                f"原料有了（{cands[0]}），**但身上没有燃料** —— "
                "熔炉要煤/木炭之类的。给她点燃料再来。"
            )

        before = await self._count(name)
        self._note(f"开始烧 {name}（原料 {cands[0]}，燃料 {fuel[0]}）")

        io = ContainerIO(self.bridge)
        if not await open_block(self.bridge, FURNACE_BLOCK, io, FURNACE_CLS,
                                radius=FURNACE_RADIUS):
            return (
                "附近没找到熔炉（或者开不出来）。"
                f"⚠️ 服务端扫描半径上限 16 格 —— 站远了她就看不见。"
            )

        layout = await io.layout()
        if layout is None or not layout.get("in"):
            return "开出来的界面不是熔炉（格子表对不上）。"

        in_slot = int(layout["in"][0])
        fuel_slot = int(layout["fuel"][0])
        result_slot = int(layout["result"][0])

        if not await io.put_stack(layout, in_slot, cands):
            await self._close(io)
            return "放原料失败了（她身上其实没有？）。"
        if not await io.put_stack(layout, fuel_slot, fuel):
            await self._close(io)
            return "放燃料失败了。"

        # ⚠️ 熔炉是**加工类**：放进去还得**等**。合成是"放了立刻有"，这个不是。
        waited = 0.0
        while waited < SMELT_TIMEOUT:
            await asyncio.sleep(POLL)
            waited += POLL
            if await io.count_slots([result_slot]) > 0:
                break
        else:
            await self._close(io)
            return (
                f"料和燃料都放进去了，但等了 {SMELT_TIMEOUT:.0f} 秒还没出东西。"
                "可能燃料不够烧、或者这个配方其实不是冶炼。"
            )

        await io.take_all(result_slot)
        await asyncio.sleep(0.8)
        await self._close(io)

        after = await self._count(name)
        got = after - before
        if got <= 0:
            return (
                "熔炉里出了东西，但**没进她背包** —— "
                "可能背包满了，或者拿的那一下没生效。去看一眼熔炉。"
            )
        self._note(f"烧好了：{name} ×{got}")
        return f"烧好了 **{name} ×{got}**（原料 {cands[0]}，燃料 {fuel[0].split(':')[-1]}）。"

    async def _close(self, io: ContainerIO) -> None:
        """关界面。⚠️ 必须能关掉 —— 界面开着的时候她动不了（docs/04 坑 10s）。"""
        await io.bridge.call("mcb closeGui")
