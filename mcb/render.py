"""把桥回来的数据变成**给模型看的中文**。

**所有** `describe_*` 都在这 —— 别把它们再塞回 `main.py`。
理由：工具方法应该只管"授权 → 调桥 → 返回"，措辞是另一件事，
混在一起会让 `main.py` 越滚越大（它曾经 853 行、18 个工具）。

⚠️ 这里的措辞**是模型唯一看得见的东西**，要说清"这是什么、意味着什么、下一步该干嘛"。

⚠️⚠️ **本文件是纯函数，不许 import 插件里的别的东西、不许有工具方法。**
（踩过：`mc_craft` 那个 `@filter.llm_tool` 曾经被误搬到这里，而这里没有 `filter`，
插件直接加载失败 —— 见 docs/11。）
"""

from __future__ import annotations


# ---- 界面 -----------------------------------------------------------

def describe_menu(data: dict) -> str:
    """把上行里的 menu 字段渲染成人话。"""
    task = data.get("task")
    menu = task.get("menu") if isinstance(task, dict) else None
    if not isinstance(menu, dict):
        return "读不到界面信息（客户端没上报 —— 她不在线，或客户端脚本没加载）。"
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
            lines.append(f"  · 第 {it.get('i')} 号格：{it.get('n')} ×{int(it.get('c') or 0)}")
        if len(items) > 40:
            lines.append(f"  …（还有 {len(items) - 40} 格没列出来）")
    else:
        lines.append("（这个界面里是空的）")
    return "\n".join(lines)


# ---- 身体 -------------------------------------------------------------------

def describe_state(data: dict, stance: str = "defend", work: dict | None = None,
                   journal=None) -> str:
    """她现在的样子 —— **身体状态 + 正在做的事 + 最近发生的事**。

    三块缺一不可：只有身体数字，白不知道自己刚才干了什么（用户 2026-10-09
    要的「让她可以感知到，表现出来她知道她在干什么」）。
    """
    if not data.get("online"):
        return "Nanako 现在**不在线** —— 角色没连进服务器，任何动作都做不了。"

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

    blocks = [head, describe_work(work), describe_path(data.get("task"))]
    if journal is not None:
        blocks.append("最近发生的事：\n" + journal.render())
    return "\n".join(blocks)


def describe_work(work: dict | None) -> str:
    """**任务层在做什么。** 这就是「她知道自己正在干什么」那一句。"""
    if not isinstance(work, dict):
        return "正在做的事：没有任务在跑。"

    if work.get("running"):
        line = (
            f"正在做的事：**{work.get('name')}** —— "
            f"第 {work.get('step')}/{work.get('total')} 步「{work.get('doing')}」"
        )
        if work.get("paused"):
            line += f"\n  ⏸ 暂时挂着（{work['paused']}），那边完事会自动接着做"
        return line

    last = work.get("last") or {}
    if last.get("state") == "done":
        return f"正在做的事：没有 —— 上一件事（{last.get('detail')}）已经做完了。"
    if last.get("state") == "failed":
        return f"正在做的事：没有 —— 上一件事没做成：{last.get('detail')}"
    if last.get("state") == "stopped":
        return "正在做的事：没有 —— 上一件事被叫停了。"
    return "正在做的事：没有任务在跑。"


def describe_path(task: object) -> str:
    """客户端上报的 **Baritone 寻路**状态（跟上面的「任务层」不是一回事）。"""
    if not isinstance(task, dict) or not task.get("available"):
        reason = (task or {}).get("reason") if isinstance(task, dict) else None
        return f"寻路：**未知** —— {reason or '客户端没有上报状态'}"

    bits: list[str] = []
    if task.get("status"):
        bits.append(f"状态 {task['status']}")
    if task.get("goal"):
        bits.append(f"目标 {task['goal']}")
    if task.get("dist") is not None:
        bits.append(f"还剩约 {task['dist']} 格")
    if task.get("eta") is not None:
        bits.append(f"预计 {task['eta']} 秒")

    text = "寻路：" + ("，".join(bits) if bits else "无上报字段")
    if task.get("stale"):
        text += "（⚠️ 这份状态已过期，客户端可能卡住或掉线了）"
    return text


# ---- 背包 -------------------------------------------------------------------

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
    hotbar_rest = [it for i, it in enumerate(hotbar) if isinstance(it, dict) and i != held]
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


# ---- 威胁 -------------------------------------------------------------------

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


# ---- 任务清单 ---------------------------------------------------------------

def describe_catalog(catalog: list[dict]) -> str:
    """可选任务清单 —— 给工具描述和模型看。"""
    if not catalog:
        return "（现在没有可用的任务）"
    return "\n".join(f"· `{t['id']}` —— {t['name']}：{t['summary']}" for t in catalog)
