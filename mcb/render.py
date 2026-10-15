
from __future__ import annotations


# 失败按固定文本结构输出，避免按 JSON 子串误判。



class Kind:
    r"""失败类型取所需子集。"""

    BAD_ARGUMENT = "bad_argument"    
    NOT_FOUND = "not_found"          
    OUT_OF_REACH = "out_of_reach"    
    NO_PATH = "no_path"              
    NO_MATERIAL = "no_material"      
    DENIED = "denied"                
    TIMEOUT = "timeout"              
    INTERRUPTED = "interrupted"      
    OFFLINE = "offline"              
    BRIDGE = "bridge"                
    REFUSED = "refused"              
    FAILED = "failed"                


def fail(kind: str, what: str, detail: str = "", usage: str = "",
         hint: str = "") -> str:
    lines = [what]
    lines.append(f"error: {kind}" + (f" —— {detail}" if detail else ""))
    if usage:
        lines.append(f"usage: {usage}")
    if hint:
        lines.append(f"hint: {hint}")
    return "\n".join(lines)


def reword(err, what: str, kind: str = Kind.FAILED, detail: str = "",
           usage: str = "", hint: str = "") -> str:
    return fail(kind, what, detail=str(err or detail or "桥没说原因"),
                usage=usage, hint=hint)



def _menu_items(menu) -> dict[int, tuple[str, int]] | None:
    r"""读不到返回 None，不用空字典冒充空菜单。"""
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
    r"""点击后比较前后槽位，直接报告可验证结果。"""
    a = _menu_items(after)
    if a is None:
        return (
            f"点了第 {slot} 号格（mode={mode}），但**读不到点完之后的界面**，没法确认生效。\n"
            "hint: 过几秒用 `mc_menu` 看一次；一直读不到就用 `mc_state` 看她在不在线"
        )

    # 菜单身份变化后不能沿用旧格号。
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



HOLD_RELEASES = {
    "dead": "body_online_and_alive",
    "external": "session_lock_acquired",
    "blocked": "next_addressed_message_rechecks_provider",
    "failed": "next_addressed_message",
}


def describe_hold(data: dict, reason: str | None = None, detail: str = "") -> str:
    if not data.get("online") or (isinstance(data.get("hp"), (int, float))
                                  and data["hp"] <= 0):
        reason, detail = "dead", "body_unavailable"
    if reason not in HOLD_RELEASES:
        return "[mc:hold] none"
    return f"[mc:hold] {reason} detail={detail or '?'} release={HOLD_RELEASES[reason]}"


def describe_state(data: dict, stance: str = "defend", work: dict | None = None,
                   journal=None, *, hold: str = "") -> str:
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

    # 显示正在使用，避免模型重复命令重置进度。
    using = data.get("using")
    if isinstance(using, dict) and using.get("isUsing"):
        parts.append(f"正在使用 {using.get('item')}（还剩 {using.get('remain')} tick）")

    head = "Nanako 在线，" + "，".join(parts) if parts else "Nanako 在线。"

    blocks = [head, describe_work(work), describe_path(data.get("task"))]
    if hold:
        blocks.append(hold)
    if journal is not None:
        blocks.append("最近发生的事：\n" + journal.render())
    return "\n".join(blocks)


def describe_work(work: dict | None) -> str:
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



_COMPASS_CN = {
    "north": "北", "south": "南", "east": "东", "west": "西",
    "northeast": "东北", "northwest": "西北",
    "southeast": "东南", "southwest": "西南",
}
_FACE_CN = {"up": "顶面", "down": "底面", "north": "北面", "south": "南面",
            "east": "东面", "west": "西面"}


def describe_look(data: dict, places=None) -> str:
    r"""服务端朝向约延迟一 tick，不等于本地渲染帧。"""
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
        # 玻璃等可透视障碍也要报告，避免误认为无遮挡。
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



def describe_around(data: dict, t2: dict | None = None) -> str:
    r"""有 T2 结果时撤去对应能力缺口说明。"""
    if not isinstance(data, dict):
        return "看不了：桥返回的不是数据。"

    rc = data.get("radiusChunks")
    span = f"{int(rc) * 16} 格（{_num(rc)} 区块）" if isinstance(rc, (int, float)) else "?"
    lines = [f"周围 {span} 概览，用时 {_num(data.get('ms'), 'g')}ms"]

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

    done = isinstance(t2, dict) and t2.get("job") == "done"
    lines.extend(_around_deep(t2, done))

    # 已有 T2 结果时不再宣称看不见无方块实体的设施。
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

    types = t2.get("types") or []
    useful = []
    for t in types:
        if not isinstance(t, dict):
            continue
        sid = str(t.get("id") or "").removeprefix("minecraft:")
        if sid.endswith("_ore") or sid.endswith("_log") or sid.endswith("_wood") or sid.endswith("_stem"):
            useful.append(t)
    if useful:
        out.append("  · 露头的矿和木头（**完整计数，不是抽样**）：")
        for t in useful[:12]:
            n = t.get("nearest") or []
            where = f"({_num(n[0])},{_num(n[1])},{_num(n[2])})" if len(n) == 3 else "?"
            out.append(f"    · {str(t.get('id')).removeprefix('minecraft:')} ×{_num(t.get('n'))} 最近 {where}")
    return out



def describe_where(data: dict, who: str) -> str:
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



def describe_catalog(catalog: list[dict]) -> str:
    if not catalog:
        return "（现在没有可用的任务）"
    return "\n".join(f"· `{t['id']}` —— {t['name']}：{t['summary']}" for t in catalog)


# 状态包只给事实，不代写台词或判断。
# 未知写 ?，不以 0 冒充。


def _num(value, fmt: str = "g") -> str:
    r"""未知显示 ?，不显示 0。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    return format(value, fmt)


def state_packet(data: dict, stance: str = "defend", work: dict | None = None,
                 journal=None, places=None, chat=None, *, log_lines: int = 4,
                 wiring: bool = True, instincts: str = "", hold: str = "",
                 hunt: str = "", wander: str = "") -> str:
    if not isinstance(data, dict) or not data.get("online"):
        return "[mc:body] offline"

    lines: list[str] = []

    body = [f"pos={_num(data.get('x'), '.1f')},{_num(data.get('y'), '.1f')},{_num(data.get('z'), '.1f')}"]
    dim = str(data.get("dim") or "").removeprefix("minecraft:")
    body.append(f"dim={dim or '?'}")
    facing = str(data.get("facing") or "")
    if facing:
        body.append(f"face={facing}")
    pitch = data.get("pitch")
    if isinstance(pitch, (int, float)) and not isinstance(pitch, bool):
        body.append(f"pitch={_num(pitch, 'g')}")
    body.append(f"hp={_num(data.get('hp'), '.1f')}/20")
    food = data.get("food") if isinstance(data.get("food"), dict) else {}
    body.append(f"food={_num(food.get('level'), 'g')}/20")
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
        weather = "?"          
    body.append(f"w={weather}")
    # 溺水可能不掉血，仍需报告氧气状态。
    air = data.get("air")
    in_water = data.get("inWater")
    under = data.get("underWater")
    if under is True or in_water is True:
        body.append(f"water={_num(air, 'g')}/300")
    using = data.get("using")
    if isinstance(using, dict) and using.get("isUsing"):
        body.append(f"using={using.get('item')}({_num(using.get('remain'), 'g')}t)")
    lines.append("[mc:body] " + " ".join(body))

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
    if hunt:
        act.append(hunt)
    if wander:
        act.append(wander)
    lines.append("[mc:act] " + " | ".join(act))
    if hold:
        lines.append(hold)

    if journal is not None and len(journal) > 0:
        rows = journal.tail(log_lines)
        lines.append("[mc:log] " + " · ".join(f"{r['t'][:5]} {r['text']}" for r in rows))

    if places is not None and len(places) > 0:
        top = places.list()[:5]
        lines.append("[mc:know] " + " ".join(
            f"{r['name']}({r['x']:g},{r['y']:g},{r['z']:g})" for r in top
        ))

    chat_text = chat_section(chat)
    if chat_text:
        lines.append(chat_text)

    # 接线说明与数据同侧注入，不放 system 破坏缓存。
    if wiring:
        if instincts:
            lines.append(instincts)
        lines.append(WIRING_NOTE)

    return "\n".join(lines)


# 此段每轮重发，英文可减少 token。
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

CHAT_LINES = 8


def chat_section(lines, *, limit: int = CHAT_LINES) -> str:
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
