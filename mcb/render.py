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


# ---- 失败的三段式（抄 Numen，见 docs\25 §三 1.1）----------------------
#
# ⚠️ **为什么要统一**：模型收到一句"没能让她出发：桥断了"之后**不知道该干什么** ——
#    是换个参数？等她上线？还是这条路根本走不通？**它只能瞎猜或者放弃。**
#
#    Numen 的做法（`docs\19` §一.3）：失败**不是 JSON，是固定形状的几行文本**。
#    它试过给结构化 JSON，最后**明确把 `data` 字段删了** —— 原话是"模型会把它
#    连同文字一起读进去"，反而更乱。所以我们返回**文本**，形状固定：
#
#         <第一行：一句话说清发生了什么>
#         error: <kind> —— 错在哪，点名参数与它的值>
#         usage: <正确写法>            ← 可省
#         hint:  <能直接照抄的下一行>   ← 可省
#
# ⭐ **`hint` 是最值钱的一行**：它必须是**下一次调用**（`mc_goto x=… z=…`），
#    不是"你可以试试走过去"这种话。模型照抄就能重试。
#
# ⚠️ **`kind` 由调用方显式给，绝不从错误文本里猜** ——
#    Numen 的 `ToolOutcome` 类注释专门骂过旧实现 `contains("\"error\"")` 的误判：
#    **拿关键词猜只会误判**。

class Kind:
    """失败的种类。**抄 Numen `ErrorKind` 的子集**（它 14 个，挑我们真有的）。

    ⚠️ 加新种类前先问：**模型拿到这个种类，能做出不一样的事吗？**
    不能的话就别加 —— 种类太多等于没有种类。
    """

    BAD_ARGUMENT = "bad_argument"    # 参数写错了（名字、类型、越界、缺）
    NOT_FOUND = "not_found"          # 世界里没这个东西
    OUT_OF_REACH = "out_of_reach"    # 有，但够不着 / 走不过去
    NO_PATH = "no_path"              # 寻路失败：没有连通的路
    NO_MATERIAL = "no_material"      # 缺材料 / 缺工具
    DENIED = "denied"                # 没有权限（我们：调用者不在白名单）
    TIMEOUT = "timeout"              # 等超时了
    INTERRUPTED = "interrupted"      # 被更高优先级抢走了（反射保命）
    OFFLINE = "offline"              # 她的身体不在（客户端没连）—— 我们特有
    BRIDGE = "bridge"                # 桥断了（RCON/隧道/脚本）—— 我们特有
    REFUSED = "refused"              # 服务端退回了这一下 —— 我们特有
    FAILED = "failed"                # 兜底：确实失败了，但归不到上面任何一类


def fail(kind: str, what: str, detail: str = "", usage: str = "",
         hint: str = "") -> str:
    """拼一条失败回执。**形状固定**，见本段开头的说明。"""
    lines = [what]
    lines.append(f"error: {kind}" + (f" —— {detail}" if detail else ""))
    if usage:
        lines.append(f"usage: {usage}")
    if hint:
        lines.append(f"hint: {hint}")
    return "\n".join(lines)


def reword(err, what: str, kind: str = Kind.FAILED, detail: str = "",
           usage: str = "", hint: str = "") -> str:
    """把 `_call` / 仲裁层回来的一句错误，包成三段式。

    `err` 是桥或仲裁层给的**原始错误文本**（可为 None）——
    没拿到就把 `detail` 换成"桥没说原因"。
    """
    return fail(kind, what, detail=str(err or detail or "桥没说原因"),
                usage=usage, hint=hint)


# ---- 界面 -----------------------------------------------------------

def _menu_items(menu) -> dict[int, tuple[str, int]] | None:
    """菜单 → `{格号: (物品名, 数量)}`。读不到返回 None（**不是空 dict**）。"""
    if not isinstance(menu, dict):
        return None
    out: dict[int, tuple[str, int]] = {}
    for it in (menu.get("items") or []):
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        try:
            cnt = int(float(it.get("c") or 0))
        except (TypeError, ValueError):
            cnt = 0
        out[idx] = (str(it.get("n") or "?"), cnt)
    return out


def describe_click_result(slot, mode, before, after) -> str:
    """点完一格**当场对账** —— 拿点之前的界面快照和点之后比。

    抄 Numen `ContainerOps.route/place`（点完当场 diff 前后格子并回一句人话），
    理由（`docs\\25` §三 1.8）：原来只回"过一会儿用 mc_inventory 自己看" ——
    **那是把验证甩给一个不会去验证的人**（`docs\\17` D3 那条教训）。
    """
    a = _menu_items(after)
    if a is None:
        return (
            f"点了第 {slot} 号格（mode={mode}），但**读不到点完之后的界面**，没法确认生效。\n"
            "hint: 过几秒用 `mc_menu` 看一次；一直读不到就用 `mc_state` 看她在不在线"
        )

    # 🔴 界面被顶掉了 —— 这一下多半点在**另一个容器**上（`docs\\04` 坑 13）
    b_id = before.get("id") if isinstance(before, dict) else None
    a_id = after.get("id")
    if b_id is not None and a_id is not None and b_id != a_id:
        return (
            f"⚠️ **没点成**：点之前是界面 id={b_id}，点之后变成了 id={a_id} —— "
            "中途被别的容器顶掉了，这一下点在别的界面上了。\n"
            "error: refused —— 界面 id 变了\n"
            f"hint: 用 `mc_menu` 重新看一次现在的界面，再决定点哪一格"
        )

    b = _menu_items(before)
    if b is None:
        tail = describe_menu(after)
        return f"点了第 {slot} 号格（mode={mode}）。**点之前没取到快照**，只能给你现在的界面：\n{tail}"

    changed = []
    for idx in sorted(set(b) | set(a)):
        was = b.get(idx)
        now = a.get(idx)
        if was == now:
            continue
        if was and now:
            changed.append(f"第 {idx} 号格：{was[0]}×{was[1]} → {now[0]}×{now[1]}")
        elif now:
            changed.append(f"第 {idx} 号格：{now[0]}×{now[1]}（原本是空的）")
        else:
            changed.append(f"第 {idx} 号格：{was[0]}×{was[1]} → 空了")

    if not changed:
        return (
            f"⚠️ **没生效**：点了第 {slot} 号格（mode={mode}），但界面**一个格子都没变**。\n"
            "error: refused —— 服务端可能把这一下退回了（界面 id 对不上 / 那一格放不下）\n"
            "hint: 先用 `mc_menu` 看一次当前界面 id，确认还是同一个容器再重点一次"
        )
    head = f"✅ 点了第 {slot} 号格（mode={mode}），**界面确实变了**："
    return head + "".join(f"\n  · {c}" for c in changed[:8])


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

    # 朝向 —— 和状态包里同一个理由（2026-10-10）：她自己读不到"头朝哪"，
    # 用户问她"你怎么一直低头"时她只能编。这里用 `describe_look` 那套措辞，保持一致。
    facing = data.get("facing")
    if facing:
        pitch = data.get("pitch")
        extra = ""
        if isinstance(pitch, (int, float)) and not isinstance(pitch, bool):
            if pitch <= -50:
                extra = "，基本在往上看"
            elif pitch >= 50:
                extra = "，基本在往下看"
        parts.append(f"面朝{_COMPASS_CN.get(str(facing), str(facing))}{extra}")

    # 时间 / 天气 —— "现在几点、下没下雨"。**不做解读**（不写"该收工了"）
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    hhmm = env.get("hhmm")
    if isinstance(hhmm, str) and hhmm:
        dayno = env.get("day")
        parts.append(f"游戏时间 {hhmm}" + (f"（第 {dayno} 天）" if isinstance(dayno, int) else ""))
    rain, storm = env.get("raining"), env.get("thundering")
    if storm is True:
        parts.append("雷暴")
    elif rain is True:
        parts.append("下雨")
    elif rain is False:
        parts.append("没下雨")
    moon = env.get("moon")
    if isinstance(moon, int) and moon == 0:
        parts.append("满月（今晚刷怪多）")
    elif isinstance(moon, int):
        parts.append(f"月相 {moon}/8")

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


# ---- 准星注视（mc_lookat）---------------------------------------------------

_COMPASS_CN = {
    "north": "北", "south": "南", "east": "东", "west": "西",
    "northeast": "东北", "northwest": "西北",
    "southeast": "东南", "southwest": "西南",
}
_FACE_CN = {"up": "顶面", "down": "底面", "north": "北面", "south": "南面",
            "east": "东面", "west": "西面"}


def describe_look(data: dict, places=None) -> str:
    """把 `mcb lookat` 的结果说清楚 —— "我正看着什么"。

    ⚠️ 服务端给的是**近似**（用的是服务端收到的朝向，约一 tick 延迟），
    不是客户端真正渲染的那一帧。数据里 `err` 非空时要如实报出来。
    """
    if not isinstance(data, dict):
        return "看不了：桥返回的不是数据。"

    ent = data.get("entity")
    blk = data.get("block")
    lines: list[str] = []

    if isinstance(ent, dict):
        pos = ent.get("pos") if isinstance(ent.get("pos"), list) else []
        where = f" @ ({_num(pos[0])},{_num(pos[1])},{_num(pos[2])})" if len(pos) == 3 else ""
        hp = ent.get("hp")
        lines.append(
            f"你正看着 **{ent.get('name')}**（{ent.get('type')}）{where}，"
            f"距 {_num(ent.get('dist'))} 格" + (f"，它血量 {_num(hp)}" if hp is not None else "")
        )
    elif isinstance(blk, dict):
        pos = blk.get("pos") if isinstance(blk.get("pos"), list) else []
        where = f" @ ({_num(pos[0])},{_num(pos[1])},{_num(pos[2])})" if len(pos) == 3 else ""
        face = _FACE_CN.get(str(blk.get("face")), str(blk.get("face") or "?"))
        lines.append(
            f"你正看着 **{blk.get('id')}**{where}，距 {_num(blk.get('dist'))} 格（对着它的{face}）"
        )
        # 中间隔着玻璃/树叶这类"看得穿但摸得着"的东西 —— 报出来，
        # 否则她会以为眼前空着（服务端射线**刻意**看得穿它们，见 mcbRayBlock）
        thru = data.get("through")
        if isinstance(thru, dict):
            tpos = thru.get("pos") if isinstance(thru.get("pos"), list) else []
            tw = f" @ ({_num(tpos[0])},{_num(tpos[1])},{_num(tpos[2])})" if len(tpos) == 3 else ""
            lines.append(f"  （中间隔着一层 **{thru.get('id')}**{tw} —— 看得见，但那层挡手）")
    else:
        lines.append(f"视线 {_num(data.get('reach'))} 格内**没有东西** —— 你在看空气或看天。")

    facing = data.get("facing")
    if facing:
        pitch = data.get("pitch")
        extra = ""
        if isinstance(pitch, (int, float)) and not isinstance(pitch, bool):
            if pitch <= -50:
                extra = "（基本在往上看）"
            elif pitch >= 50:
                extra = "（基本在往下看）"
        lines.append(f"你面朝{_COMPASS_CN.get(str(facing), str(facing))}{extra}。")

    # 这是不是她记过的地方 —— 地点簿终于在"看"这条路上有用了
    if places is not None:
        pos = None
        for src in (ent, blk):
            if isinstance(src, dict) and isinstance(src.get("pos"), list) and len(src["pos"]) == 3:
                pos = src["pos"]
                break
        if pos is not None:
            try:
                hit = places.near(float(pos[0]), float(pos[1]), float(pos[2]), radius=8.0)
            except (TypeError, ValueError):
                hit = None
            if hit is not None:
                name, entry, dist = hit
                lines.append(
                    f"📌 这附近有你记过的「{name}」—— 只差 {dist:g} 格"
                    + (f"，你记的是：{entry['what']}" if entry.get("what") else "")
                )

    if data.get("entityOccluded"):
        lines.append("（视线上有只生物被前面的方块挡住了 —— 看得见名字，但打不着。）")

    err = data.get("err") or []
    if err:
        lines.append("（查询异常：" + "；".join(str(e) for e in err) + "）")
    return "\n".join(lines)


# ---- 周边概览（mc_around）---------------------------------------------------

def describe_around(data: dict, t2: dict | None = None) -> str:
    """把 `mcb around` 的结果说清楚 —— "我周围有什么"。

    `t2` 是**第二层**（`mcb t2`，跨 tick 全分辨率扫描）的状态或结果，可能没有。
    ⚠️ 有 T2 结果时，那句"没有方块实体的设施我看不见"的**缺口说明要撤掉** ——
    否则她会在明明拿到了工作台坐标的情况下还说"附近没有工作台"。
    """
    if not isinstance(data, dict):
        return "看不了：桥返回的不是数据。"

    rc = data.get("radiusChunks")
    span = f"{int(rc) * 16} 格（{_num(rc)} 区块）" if isinstance(rc, (int, float)) else "?"
    lines = [f"周围 {span} 概览，用时 {_num(data.get('ms'), 'g')}ms"]

    # ---- 地表 ----
    surf = data.get("surface") if isinstance(data.get("surface"), dict) else {}
    if surf:
        rel_min, rel_max = surf.get("relMin"), surf.get("relMax")
        if isinstance(rel_min, (int, float)) and isinstance(rel_max, (int, float)):
            lines.append(
                f"· 地势：地表比你**低 {-rel_min:g} 格**到**高 {rel_max:g} 格**"
                + ("（基本是平的）" if abs(rel_max - rel_min) <= 3 else "")
            )
        counts = surf.get("counts") or []
        if counts:
            top = "、".join(
                f"{str(c.get('id')).removeprefix('minecraft:')} {_num(c.get('n'))}"
                for c in counts[:6] if isinstance(c, dict)
            )
            more = surf.get("distinct")
            lines.append(f"· 地表成分：{top}"
                         + (f"（共 {_num(more)} 种）" if isinstance(more, int) and more > len(counts) else ""))
        water = surf.get("waterCols")
        if isinstance(water, (int, float)) and water > 0:
            lines.append(f"· 采样里 {_num(water)} 列是水面")
        if surf.get("truncated"):
            lines.append("· ⚠️ 地表抽样**没跑完**（撞到时间预算），下面是残缺的")

    # ---- 设施（方块实体）----
    poi = data.get("poi") or []
    total = data.get("poiTotal")
    if poi:
        lines.append(f"· 附近有 **{_num(total)}** 个设施/容器（按距离，最近的在前）：")
        for p in poi[:10]:
            if not isinstance(p, dict):
                continue
            pos = p.get("pos") if isinstance(p.get("pos"), list) else []
            where = f"({_num(pos[0])},{_num(pos[1])},{_num(pos[2])})" if len(pos) == 3 else "?"
            lines.append(f"  · {str(p.get('id')).removeprefix('minecraft:')} @ {where} 距 {_num(p.get('dist'))} 格")
        if isinstance(total, int) and total > 10:
            lines.append(f"  …（还有 {total - 10} 个）")
        kinds = data.get("poiKind") if isinstance(data.get("poiKind"), dict) else {}
        if kinds:
            by_n = sorted(kinds.items(), key=lambda kv: -kv[1])
            lines.append("· 按种类：" + "、".join(
                f"{str(k).removeprefix('minecraft:')}×{_num(v)}" for k, v in by_n[:6]
            ))
    else:
        lines.append("· 这一片**没有**任何方块实体（箱子/熔炉/刷怪笼…一个都没有）")

    if data.get("truncatedChunks"):
        lines.append(f"· ⚠️ 有 {_num(data.get('truncatedChunks'))} 个区块**没走到**（撞到时间预算）")

    # ---- 第二层：T2 跨 tick 全分辨率扫描 ----
    done = isinstance(t2, dict) and t2.get("job") == "done"
    lines.extend(_around_deep(t2, done))

    # ⚠️ 缺口只在**第二层还没给出结果**时才说 —— 拿到了还说"看不见"是自相矛盾
    if not done:
        lines.append(
            "⚠️ **上面这个列表只含「有方块实体」的东西**（箱子/熔炉/木桶/床/告示牌/刷怪笼/传送门…）。"
            "**工作台、铁砧、石切机、织布机、制箭台、堆肥桶、传送门框这些没有方块实体，不在里面** —— "
            "要找它们得用 `mc_goto near`（那是定点扫描，范围小但准），或者等第二层扫完。"
        )
    err = data.get("err") or []
    if err:
        lines.append("（查询异常：" + "；".join(str(e) for e in err) + "）")
    return "\n".join(lines)


def _around_deep(t2: dict | None, done: bool) -> list[str]:
    """`mc_around` 的第二层（T2）那几行。**没有 T2 就返回空**，别硬凑。"""
    if not isinstance(t2, dict):
        return []
    job = t2.get("job")

    if job == "error":
        return [f"（第二层没跑起来：{t2.get('err')} —— 下面是第一层的结果）"]

    st = t2.get("status") if isinstance(t2.get("status"), dict) else {}

    if job == "running":
        pct = st.get("pct")
        left = ""
        if isinstance(st.get("ms"), (int, float)) and isinstance(pct, (int, float)) and pct > 1:
            left = f"，大概还要 {max(1, int(st['ms'] / pct * (100 - pct) / 1000))} 秒"
        head = "· 第二层（全分辨率）**刚刚开扫**" if t2.get("justStarted") else "· 第二层（全分辨率）**正在后台扫**"
        return [
            f"{head}：{_num(pct, 'g')}%{left}。",
            "  它会找到**没有方块实体的**设施（工作台/铁砧/石切机/织布机/制箭台/堆肥桶/传送门框），"
            "并数一遍完整的地表方块。**不用等也不用轮询 —— 过一会儿再叫我一次 `mc_around` 就有结果。**",
        ]

    if not done:
        return []

    # ---- 扫完了 ----
    out = ["· 第二层（全分辨率）**扫完了**："]
    deep_poi = t2.get("poi") or []
    if deep_poi:
        out.append(f"  · 找到 **{len(deep_poi)}** 个没有方块实体的设施（按距离）：")
        for p in deep_poi[:12]:
            if not isinstance(p, dict):
                continue
            out.append(
                f"    · {p.get('what') or p.get('id')} @ "
                f"({_num(p.get('x'))},{_num(p.get('y'))},{_num(p.get('z'))}) 距 {_num(p.get('d'))} 格"
            )
        if len(deep_poi) > 12:
            out.append(f"    …（还有 {len(deep_poi) - 12} 个）")
    else:
        out.append("  · **一个都没有** —— 这一片确实没有工作台/铁砧/石切机/堆肥桶这类东西")

    # 完整方块计数：**只报"有用的"** —— 石头泥土水这些噪音不占篇幅
    types = t2.get("types") or []
    useful = []
    for t in types:
        if not isinstance(t, dict):
            continue
        sid = str(t.get("id") or "").removeprefix("minecraft:")
        # 露头的矿 + 木头 —— 这两类是"要不要过去"的依据；具体矿物名一律保留
        if sid.endswith("_ore") or sid.endswith("_log") or sid.endswith("_wood") or sid.endswith("_stem"):
            useful.append(t)
    if useful:
        out.append("  · 露头的矿和木头（**完整计数，不是抽样**）：")
        for t in useful[:12]:
            n = t.get("nearest") or []
            where = f"({_num(n[0])},{_num(n[1])},{_num(n[2])})" if len(n) == 3 else "?"
            out.append(f"    · {str(t.get('id')).removeprefix('minecraft:')} ×{_num(t.get('n'))} 最近 {where}")
    return out


# ---- 查玩家位置（mc_where）--------------------------------------------------

def describe_where(data: dict, who: str) -> str:
    """把 `mcb where` 的结果说清楚。"""
    if not isinstance(data, dict):
        return f"查不到 {who}。"

    if not data.get("online"):
        return (f"**{data.get('name') or who} 不在线。**"
                "（不在线 ≠ 出错 —— 服务端只是没这个人。）")

    name = data.get("name") or who
    x, y, z = data.get("x"), data.get("y"), data.get("z")
    dim = str(data.get("dim") or "").removeprefix("minecraft:")
    if x is None or y is None or z is None:
        return f"{name} 在线，但坐标读不到。"

    lines = [f"**{name}** 在 ({_num(x, '.1f')}, {_num(y, '.1f')}, {_num(z, '.1f')})"
             + (f" · 维度 {dim}" if dim else "")]
    hp = data.get("hp")
    if hp is not None:
        lines.append(f"· 血量 {_num(hp, '.1f')}/20")

    if data.get("sameDim") is False:
        lines.append("· ⚠️ **和你不在同一个维度** —— `mc_goto` 走不过去，得先找传送门。")
    elif data.get("distToMe") is not None:
        lines.append(f"· 离你 **{_num(data.get('distToMe'), 'g')} 格**")
        lines.append("· 想过去就 `mc_goto` 到这个坐标（远的话会用掉不少时间，路上可能遇怪）")
    return "\n".join(lines)


# ---- 任务清单 ---------------------------------------------------------------

def describe_catalog(catalog: list[dict]) -> str:
    """可选任务清单 —— 给工具描述和模型看。"""
    if not catalog:
        return "（现在没有可用的任务）"
    return "\n".join(f"· `{t['id']}` —— {t['name']}：{t['summary']}" for t in catalog)


# ---- 状态数据包 -------------------------------------------------------------
#
# ⚠️⚠️ **这是"感知"那一块，规矩跟上面所有 describe_* 都不一样。**
#
# 用户 2026-10-09 拍板（原话）：
#   > "感知我是没法接受程序文本的。必须是如同上文附加在末尾一样，
#   >   规定一个包含信息的状态数据包让她知道处境。"
#
# 区别**不是措辞问题**：
#   ❌ `[身体] 我饿了（饱食度 6），但身上没有食物。` —— 程序替她写的**台词**
#   ✅ `food=6/20 food_items=0`                      —— 关于她身体的**数据**
#
# **程序只负责把状态摆出来，不负责说出来。**
# 凡是"她会用第一人称讲的话"，一律不许出现在这里。
#
# 另外三条：
#   · **不做任何解读** —— 不许写"（危险）""（该吃了）"。那是她的判断
#   · **缺失写 `?` 不写 0** —— 把"读不到"和"没有"分开（docs/04 坑 10f）
#   · **字段名要短** —— 每次 LLM 请求都要重发，是按字收费的


def _num(value, fmt: str = "g") -> str:
    """数值渲染。**读不到回 `?`，不回 0** —— 把"不知道"和"零"分开。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    return format(value, fmt)


def state_packet(data: dict, stance: str = "defend", work: dict | None = None,
                 journal=None, places=None, chat=None, *, log_lines: int = 4,
                 wiring: bool = True, instincts: str = "") -> str:
    """**紧凑的状态数据包** —— 纯数据，给白当处境感知用。

    形如：

        [body] pos=-10.5,88.0,-8.0 dim=overworld hp=20.0/20 food=17/20
        [act]  task=沿途照明 2/3 拿到手上 | stance=defend | path=idle
        [log]  23:11 插火把×2 · 23:15 烧成 iron_ingot×1
        [know] 家(-10,88,-9) 麦田(-51,65,-7)
    """
    if not isinstance(data, dict) or not data.get("online"):
        return "[mc:body] offline"

    lines: list[str] = []

    # ---- 身体 ----
    body = [f"pos={_num(data.get('x'), '.1f')},{_num(data.get('y'), '.1f')},{_num(data.get('z'), '.1f')}"]
    dim = str(data.get("dim") or "").removeprefix("minecraft:")
    body.append(f"dim={dim or '?'}")
    # 🧭 朝向 —— **她自己看不见自己的头朝哪**（2026-10-10 用户实报："她声称自己没低头，
    #    但一直是低头状态"，现场服务端读到 `pitch:81.3`）。
    #    以前只有 `mc_lookat` 吐过这个信息，而她不会去问那个问题 ——
    #    **状态包里有，才算她"知道"**（律④：能力自我认知 = 工具表 + 状态包）。
    facing = str(data.get("facing") or "")
    if facing:
        body.append(f"face={facing}")
    pitch = data.get("pitch")
    if isinstance(pitch, (int, float)) and not isinstance(pitch, bool):
        body.append(f"pitch={_num(pitch, 'g')}")
    body.append(f"hp={_num(data.get('hp'), '.1f')}/20")
    food = data.get("food") if isinstance(data.get("food"), dict) else {}
    body.append(f"food={_num(food.get('level'), 'g')}/20")
    # 时间 / 天气 —— 短token，但决定她"要不要现在去干活"
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    hhmm = env.get("hhmm")
    body.append(f"t={hhmm if isinstance(hhmm, str) and hhmm else '?'}")
    rain = env.get("raining")
    storm = env.get("thundering")
    if isinstance(storm, bool) and storm:
        weather = "storm"
    elif isinstance(rain, bool) and rain:
        weather = "rain"
    elif isinstance(rain, bool):
        weather = "clear"
    else:
        weather = "?"          # 读不到 ≠ 晴天（docs/04 坑 10f）
    body.append(f"w={weather}")
    # 氧气 / 在水里 —— **只在"值得说"的时候才占字数**（没在水里就不写 `water=no`）。
    # ⚠️ 为什么要这一维：溺水是"会慢慢死、但血量不一定掉"的情形。
    #    实测她卡在水里 253 秒、`drown` 每秒一条，但 `dmg=0`（身上挂着抗性 V），
    #    **反射只看 hp 就永远不触发**。→ 凡是要保命的判据，都得从"状态"判。
    air = data.get("air")
    in_water = data.get("inWater")
    under = data.get("underWater")
    if under is True or in_water is True:
        body.append(f"water={_num(air, 'g')}/300")
    using = data.get("using")
    if isinstance(using, dict) and using.get("isUsing"):
        body.append(f"using={using.get('item')}({_num(using.get('remain'), 'g')}t)")
    lines.append("[mc:body] " + " ".join(body))

    # ---- 在干什么 ----
    act = [f"stance={stance}"]
    if isinstance(work, dict) and work.get("running"):
        act.append(f"task={work.get('name')} {work.get('step')}/{work.get('total')} {work.get('doing')}")
        if work.get("paused"):
            act.append(f"paused={work['paused']}")
    else:
        act.append("task=-")
    path = data.get("task") if isinstance(data.get("task"), dict) else {}
    if path.get("available"):
        act.append(f"path={path.get('status') or '?'}")
    lines.append("[mc:act] " + " | ".join(act))

    # ---- 最近发生了什么 ----
    if journal is not None and len(journal) > 0:
        rows = journal.tail(log_lines)
        lines.append("[mc:log] " + " · ".join(f"{r['t'][:5]} {r['text']}" for r in rows))

    # ---- 知道的地方 ----
    if places is not None and len(places) > 0:
        top = places.list()[:5]
        lines.append("[mc:know] " + " ".join(
            f"{r['name']}({r['x']:g},{r['y']:g},{r['z']:g})" for r in top
        ))

    # ---- 游戏公屏（她"听见"的）----
    chat_text = chat_section(chat)
    if chat_text:
        lines.append(chat_text)

    # ---- 接线说明 ----
    # ⚠️ **和上面那些数据一起给**，不放到系统提示词里 ——
    #    它解释的就是上面那几个字段，分开放她会拼不起来（用户 2026-10-10 定：
    #    "提示词交代应该配合上下文注入"）。
    if wiring:
        # ⭐ **本能名册也在这一档**：反射是"程序替她做的决定"，
        #    不交代的话她既无法否决、也无法解释（`docs\25` §三 1.2）。
        #    文本由 `mcb\reflexes.py` 出 —— **本文件保持纯函数，不 import 插件里的别的东西**。
        if instincts:
            lines.append(instincts)
        lines.append(WIRING_NOTE)

    return "\n".join(lines)


# ---- 接线说明（英文，见 token_check.py 的实测）-------------------------------
#
# ⚠️ **这段的存在理由**（2026-10-10 实测）：
#    白在 QQ 被问"你能不能感知游戏内聊天"，她答"**我听不见**，我手头的传感器只有
#    身体状态、血量和方块交互" —— 她说得**字面上完全正确**，因为我们**真的没有**
#    一个"读聊天"的工具。她的能力自我认知 = **工具表 + 状态包**，
#    凡是 harness 替她做的事（uplink 注入 / 反射 / 事件流），在她那儿等于不存在。
#
#    所以这段必须**明确交代接线**，否则她会一直否认自己有的能力。
#
# ⚠️ 用**英文**：实测同等内容英文省 1.5~2.2 倍 token，而这段每次注入都要重发。
#    详见 `_AI工作区\mc-brain\plugin-check\token_check.py`。
WIRING_NOTE = (
    "[wiring] Everything tagged [mc:*] is the state of the MINECRAFT WORLD — a game. "
    "It is NOT the real world and not your user's real surroundings: if [mc:body] says "
    "rain or night, that is the GAME's weather and the GAME's clock, and it tells you "
    "nothing about the real weather or real time where your user is. Never mix the two up. "
    "These are readouts, not someone speaking to you — say what you want, or nothing; "
    "\"?\" means unreadable, not zero. "
    "In-game public chat reaches you under [mc:chat]; when someone calls your name you are "
    "woken with that line directly, and your reply is then posted to the public chat "
    "automatically (do not call mc_say). You can also pull chat yourself with mc_chat_log. "
    "You have a `future_task` tool: use it (action=create + note) to schedule your OWN next "
    "wake-up when you want to keep doing something later without waiting to be called. "
    "Your `## Skills` section tells you to open a SKILL.md with a shell command — "
    "**you have no shell, that will fail**; call `mc_skill` with the skill's NAME instead."
)

# [chat] 段最多带几条 —— 每次注入都要重发，别贪
CHAT_LINES = 8


def chat_section(lines, *, limit: int = CHAT_LINES) -> str:
    """`[chat]` 段 —— 游戏公屏里她**听到的**话。

    ⚠️ **带说话人和他在哪**（用户 2026-10-10 定的）：她视距只有 32 格，
    出了这个圈她看不见，"山那边"只能靠人告诉。真人也没有世界地图。
    """
    if not lines:
        return ""
    rows = [x for x in lines if isinstance(x, dict)][-max(1, limit):]
    if not rows:
        return ""
    parts = []
    for r in rows:
        who = str(r.get("who") or "?")
        text = " ".join(str(r.get("text") or "").split())
        if not text:
            continue
        bit = f"<{who}> {text}"
        pos = r.get("pos")
        if isinstance(pos, list) and len(pos) == 3:
            bit += f"@({_num(pos[0])},{_num(pos[1])},{_num(pos[2])}"
            d = r.get("dist")
            if isinstance(d, (int, float)) and not isinstance(d, bool):
                bit += f",d={d:g}"
            bit += ")"
        parts.append(bit)
    return ("[mc:chat] " + " · ".join(parts)) if parts else ""


def describe_chat(lines, count: int = 20) -> str:
    """翻游戏公屏 —— 给 `mc_chat_log` 工具用的版本。

    比 `[chat]` 段宽松（那是**每轮重发**的，要抠字；这是**她要才给**的，可以说清楚）。
    """
    rows = [x for x in (lines or []) if isinstance(x, dict)]
    try:
        n = max(1, int(count))
    except (TypeError, ValueError):
        n = 20
    out = []
    for r in rows[-n:]:
        who = str(r.get("who") or "?")
        text = " ".join(str(r.get("text") or "").split())
        if not text:
            continue
        bit = f"· <{who}> {text}"
        pos = r.get("pos")
        if isinstance(pos, list) and len(pos) == 3:
            bit += f"（他在 ({_num(pos[0])},{_num(pos[1])},{_num(pos[2])})"
            d = r.get("dist")
            if isinstance(d, (int, float)) and not isinstance(d, bool):
                bit += f"，离你 {d:g} 格"
            bit += "）"
        out.append(bit)
    return "\n".join(out) if out else "（空）"
