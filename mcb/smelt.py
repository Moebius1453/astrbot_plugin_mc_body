
from __future__ import annotations

import asyncio

from astrbot.api import logger

from .containers import ContainerIO, open_block

FURNACE_BLOCK = "minecraft:furnace"
FURNACE_CLS = "FurnaceMenu"

# 扫描半径受服务端预算钳制。
FURNACE_RADIUS = 16

# 燃料只选可用候选，并按耐烧程度排序。
FUELS = (
    "minecraft:coal",
    "minecraft:charcoal",
    "minecraft:coal_block",
    "minecraft:blaze_rod",
    "minecraft:oak_planks",
    "minecraft:stick",
)

SMELT_TIMEOUT = 60.0

POLL = 2.0


class Smelter:

    def __init__(self, bridge, journal=None, *, arbiter=None) -> None:
        self.bridge = bridge
        self.journal = journal
        self.arbiter = arbiter

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
        ok, why = await open_block(self.bridge, FURNACE_BLOCK, io, FURNACE_CLS,
                                   radius=FURNACE_RADIUS, arbiter=self.arbiter)
        if not ok:
            return (
                f"开不了熔炉：{why}。"
                f"（服务端扫描半径上限 {FURNACE_RADIUS} 格 —— 站远了她就看不见）"
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
        await io.bridge.call("mcb closeGui")
