"""本能名册 —— 加一条反射只写反射本身（抄 Numen ReflexRegistry，docs\\25 §三 1.2）。

## 为什么要有它

反射是程序替她做的决定（挨打就停、血低就跑、饿就吃、卡住就停、溺水就上浮）——
不过 LLM，也不花 token。

注意： 但按我们的设计律（能力自我认知 = 工具表 + 状态包），
凡是不由工具暴露的能力，在她的自我模型里等于不存在 ——
她会否认自己有的能力，也看不见程序替她做的事（docs\\12 §1.4 那条律，第五次复现）。

所以这份名册有两个用处，缺一不可：

1. 给她看：overview() 拼成一行塞进接线说明 —— 让她知道"我的身体有这些本能"。
2. 给我们看：这张表是唯一的事实来源。加一条反射、删一条反射，
   只改这里 + 反射本身，不许再散在 render / schema 里各写一份。

## 注意： 抄 Numen 的一条纪律（ReflexRegistry.java:17-20 原话）

> "只有开关没有入口的话，每次启动读一个文件、写一个文件，里面的值永远全是 true。
> 看着有、实际没有，比明说没做更难查。"

落到我们身上：_conf_schema.json 里有一堆开关，得自查有没有"入口"。
check.py 14 就是干这个的 —— 表里写了 config_key 的，必须真的被代码读到。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instinct:
    """一条本能。

    trigger / action 是给模型看的英文（这段会进提示词，按 docs\\25 §七 的口径）。
    中文解释写在注释里。
    """

    id: str
    trigger: str                  # 什么时候触发
    action: str                   # 会做什么
    config_key: str | None = None # 能关掉它的配置项；None = 关不掉（保命的就别给人关）
    tunable: str = ""             # 可调的阈值（配置项名，逗号分隔）；纯粹给人看


# 注意： 改这里 = 改她的自我认知。删一条之前先确认反射本身也删了。
INSTINCTS: tuple[Instinct, ...] = (
    Instinct(
        id="eat",
        trigger="hunger drops below `hunger_low`, OR hp drops to `hp_low` or below",
        action="finds food (by saturation when hurt, else by nutrition), puts it in hand, eats it",
        config_key="hunger_low",
        tunable="hunger_low,hp_low",
    ),
    Instinct(
        id="combat",
        trigger="you take damage (hp went down), or a hostile is aiming at you",
        action="stops whatever you were doing, enters combat, and fights back only if in reach",
        config_key=None,
    ),
    Instinct(
        id="flee",
        trigger="in combat and hp falls below `hp_low` x1.25",
        action="runs a short distance -- toward your owner if near, otherwise away from danger",
        config_key="flee_distance",
        tunable="hp_low,flee_distance,retreat_toward",
    ),
    Instinct(
        id="stuck",
        trigger="pathing but has not moved 1.5 blocks in 18 seconds",
        action="cancels the walk and writes a line to the journal (it does not retry)",
        config_key="stuck_seconds",
        tunable="stuck_seconds",
    ),
    Instinct(
        id="drown",
        trigger="underwater with air below 180/300",
        action="walks/swims up to the surface of your own column",
        config_key=None,
    ),
)


def overview() -> str:
    """拼成一行给模型看的"本能总览"。英文 —— 这段会进提示词。"""
    bits = [f"{i.id} ({i.trigger} -> {i.action})" for i in INSTINCTS]
    return (
        "[instincts] Your body also has instincts that fire on their own, every 2 seconds, "
        "without you deciding and without costing you anything: "
        + "; ".join(bits)
        + ". They are reflexes -- you cannot switch them off. What you DO control is the "
        "higher-level intent (stance with `mc_stance`, tasks with `mc_task`). "
        "If one of them interrupts something you were doing, that is why."
    )


def config_keys() -> set[str]:
    """表里声明"能关掉/能调"的配置项 —— 给 check.py 去核对是不是真的有入口。"""
    out: set[str] = set()
    for i in INSTINCTS:
        if i.config_key:
            out.add(i.config_key)
        out.update(k.strip() for k in i.tunable.split(",") if k.strip())
    return out
