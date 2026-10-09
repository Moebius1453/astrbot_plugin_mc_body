"""把桥回来的数据变成**给模型看的中文**。

**所有** `_describe_*` 都在这 —— 别把它们再塞回 `main.py`。
理由：工具方法应该只管"授权 → 调桥 → 返回"，措辞是另一件事，
混在一起会让 `main.py` 越滚越大（它曾经 853 行、18 个工具）。

⚠️ 这里的措辞**是模型唯一看得见的东西**，要说清"这是什么、意味着什么、下一步该干嘛"。
"""

from __future__ import annotations


@staticmethod
def describe_menu(data: dict) -> str:
    """把上行里的 menu 字段渲染成人话。"""
    task = data.get("task")
    menu = task.get("menu") if isinstance(task, dict) else None
    if not isinstance(menu, dict):
        return (
            "读不到界面信息（客户端没上报 —— 她不在线，或客户端脚本没加载）。"
        )
    items = menu.get("items") or []
    mid = menu.get("id")
    slots = menu.get("slots")
    if menu.get("def"):
        head = f"她手上没有开着的容器，**只有默认的背包界面**（共 {slots} 格）。"
        head += "\n⚠️ 默认界面里我只上报 0~4 号格（合成用的）。**背包里别的东西在哪一格，用 `mc_inventory` 反推**："
        head += "\n  · 快捷栏第 n 格（0~8）→ 界面第 **36+n** 号格"
        head += "\n  · 背包第 n 格（0~26）→ 界面第 **9+n** 号格"
        head += "\n  · 合成产物在 **0** 号格，2×2 材料格是 **1~4** 号"
    else:
        head = f"她正开着**一个容器界面**（id={mid}，共 {slots} 格）。"

    lines = [head]
    if items:
        lines.append("里面有什么：")
        for it in items[:40]:
            lines.append(
                f"  · 第 {it.get('i')} 号格：{it.get('n')} ×{int(it.get('c') or 0)}"
            )
        if len(items) > 40:
            lines.append(f"  …（还有 {len(items) - 40} 格没列出来）")
    else:
        lines.append("（这个界面里是空的）")
    return "\n".join(lines)

@filter.llm_tool(name="mc_craft")
async def mc_craft(self, event: AstrMessageEvent, item: str, count: int = 1):
    """**做东西** —— 给一个物品名，她自己张罗着把它做出来。

    这是**高级工具**：它会自己算清楚整条链子 ——
    比如"做一把木镐"，它会自己推出来"得先拿原木做木板、再用木板做木棍、最后上工作台"，
    然后一步步做掉。中间产物、材料够不够、要不要工作台，**全不用你操心**。
    需要工作台时她会**自己去找、走过去、打开**。

    配方来自服务端，所以 **mod 的物品也认识**。

    ⚠️ 两件事你得知道：
    1. **材料得她自己有**。缺料她**不会去挖**，会如实告诉你缺什么 ——
       那就先派她去挖（`mc_baritone_raw` 发 `mine`）或用别的办法弄到。
    2. 现在只支持**工作台/背包合成**。熔炉冶炼、切石机、锻造台**还不能**。

    Args:
        item(string): 要做的物品，例如 "wooden_pickaxe"、"crafting_table"、
            "oak_planks"。带不带 `minecraft:` 前缀都行；mod 物品要带前缀。
        count(number): 要做几个，默认 1。
    """
    denied = self._authorize(event)
    if denied:
        return denied
    name = str(item or "").strip()
    if not name:
        return "没说要做什么。"
    try:
        n = max(1, int(count))
    except (TypeError, ValueError):
        n = 1
    if not self._cfg("enable_craft", True):
        return "合成功能被管理员关掉了。"

    crafter = Crafter(self.bridge)
    runner = CraftRunner(self.bridge)
    try:
        plan = await crafter.plan(name, n)
    except BridgeError as exc:
        return f"算配方的时候没连上桥：{exc}"
    if not plan.ok:
        return "做不了：\n" + plan.describe()
    if not plan.steps:
        return f"不用做，她手上已经有 {name} 了。"
    try:
        return await runner.run(plan)
    except BridgeError as exc:
        return f"做的过程中桥断了：{exc}"

# ---- 输出整形 -------------------------------------------------------

def describe_state(self, data: dict) -> str:
    if not data.get("online"):
        return (
            f"Nanako 现在**不在线** —— 角色没连进服务器，任何动作都做不了。"
        )

    parts: list[str] = []
    if data.get("x") is not None:
        parts.append(f"坐标 x={data['x']} y={data['y']} z={data['z']}")
    if data.get("hp") is not None:
        parts.append(f"血量 {data['hp']}")
    food = data.get("food")
    if isinstance(food, dict) and food.get("level") is not None:
        level = food["level"]
        warn = "（饿了）" if isinstance(level, (int, float)) and level <= 6 else ""
        parts.append(f"饥饿 {level}/20{warn}")
    dim = data.get("dim")
    if dim:
        parts.append(f"维度 {str(dim).removeprefix('minecraft:')}")

    stance_text = "主动清怪（hunt）" if stance == "hunt" else "被动还手（defend）"
    parts.append(f"战斗姿态 {stance_text}")

    # 正在吃东西的时候说一声 —— 否则白会以为动作没生效又下一遍命令
    using = data.get("using")
    if isinstance(using, dict) and using.get("isUsing"):
        parts.append(f"正在使用 {using.get('item')}（还剩 {using.get('remain')} tick）")

    head = "Nanako 在线，" + "，".join(parts) if parts else "Nanako 在线。"
    return f"{head}\n{self.describe_task(data.get('task'))}"

@staticmethod
def describe_task(task: object) -> str:
    if not isinstance(task, dict) or not task.get("available"):
        reason = (task or {}).get("reason") if isinstance(task, dict) else None
        return f"任务状态：**未知** —— {reason or '客户端没有上报状态'}"

    bits: list[str] = []
    if task.get("status"):
        bits.append(f"状态 {task['status']}")
    if task.get("goal"):
        bits.append(f"目标 {task['goal']}")
    if task.get("dist") is not None:
        bits.append(f"还剩约 {task['dist']} 格")
    if task.get("eta") is not None:
        bits.append(f"预计 {task['eta']} 秒")

    text = "任务：" + ("，".join(bits) if bits else "无上报字段")
    if task.get("stale"):
        text += "（⚠️ 这份状态已过期，客户端可能卡住或掉线了）"
    return text

@staticmethod
def describe_inventory(data: dict) -> str:
    lines: list[str] = []

    food = data.get("food") or {}
    if isinstance(food, dict) and food.get("level") is not None:
        level = food["level"]
        note = "（饿了，该吃东西了）" if isinstance(level, (int, float)) and level <= 6 else ""
        lines.append(f"饥饿度 {level}/20{note}")

    held = data.get("held")
    hotbar = data.get("hotbar") or []
    if isinstance(held, int) and 0 <= held < len(hotbar):
        item = hotbar[held]
        name = item.get("n") if isinstance(item, dict) else None
        lines.append(f"手上（第 {held} 格）：{name or '空手'}")

    def fmt_items(items, label: str) -> None:
        if not items:
            return
        rendered = [
            f"{it.get('n')}×{int(it.get('c') or 0)}"
            for it in items
            if isinstance(it, dict) and it.get("n")
        ]
        if rendered:
            lines.append(f"{label}：" + "，".join(rendered))

    # 快捷栏排除手上那格，免得重复
    hotbar_rest = [
        it for i, it in enumerate(hotbar) if isinstance(it, dict) and i != held
    ]
    fmt_items(hotbar_rest, "快捷栏其它格")
    fmt_items(data.get("main") or [], "背包")

    off = data.get("offhand")
    if isinstance(off, dict) and off.get("n"):
        lines.append(f"副手：{off['n']}×{int(off.get('c') or 0)}")

    errors = data.get("err") or []
    if errors:
        lines.append("（读取时的异常：" + "；".join(str(e) for e in errors) + "）")

    if not lines:
        return "背包是空的，什么都读不到。"
    return "\n".join(lines)

@staticmethod
def describe_threats(data: dict) -> str:
    threats = data.get("threats") or []
    if not threats:
        return "附近没看到任何实体。"

    hostiles = [t for t in threats if isinstance(t, dict) and t.get("hostile")]
    lines: list[str] = []
    if hostiles:
        lines.append(f"⚠️ 附近有 {len(hostiles)} 只敌对：")
        for t in hostiles[:5]:
            hp = t.get("hp")
            aim = "**正瞄着你** " if t.get("targeting") else ""
            lines.append(
                f"  · {aim}{t.get('name')}（{t.get('type')}）"
                f"距 {t.get('dist')} 格" + (f"，血量 {hp}" if hp is not None else "")
            )
    else:
        lines.append("附近没有敌对怪。")

    others = [t for t in threats if isinstance(t, dict) and not t.get("hostile")]
    if others:
        names = "，".join(f"{t.get('name')}({t.get('dist')}格)" for t in others[:6])
        lines.append(f"其它实体：{names}")

    err = data.get("err") or []
    if err:
        lines.append("（查询异常：" + "；".join(str(e) for e in err) + "）")
    return "\n".join(lines)
