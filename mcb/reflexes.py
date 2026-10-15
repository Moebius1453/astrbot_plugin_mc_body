r"""本能名册 —— 加一条反射只写反射本身（抄 Numen ReflexRegistry，docs\25 §三 1.2）。

1. 给她看：overview() 拼成一行塞进接线说明 —— 让她知道"我的身体有这些本能"。
2. 给我们看：这张表是唯一的事实来源。加一条反射、删一条反射，
   只改这里 + 反射本身，不许再散在 render / schema 里各写一份。

## 注意： 抄 Numen 的一条纪律（ReflexRegistry.java:17-20 原话）"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instinct:
    """一条本能。

    trigger / action 是给模型看的英文（这段会进提示词，按 docs\\25 §七 的口径）。
    中文解释写在注释里。
    """

    id: str
    trigger: str                  
    action: str                   
    config_key: str | None = None 
    tunable: str = ""             


# 名册用于能力自述，必须与实际反射保持一致。
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
        action="stops whatever you were doing, enters combat; it attacks what is in reach and "
               "closes the distance to whatever is actually attacking you",
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
    bits = [f"{i.id} ({i.trigger} -> {i.action})" for i in INSTINCTS]
    return (
        "[instincts] Your body also has instincts that fire on their own, once a second "
        "(four times a second while you are fighting), without you deciding and without "
        "costing you anything: "
        + "; ".join(bits)
        + ". They are reflexes -- you cannot switch them off yourself (the server owner can "
        "disable some of them in the plugin config). What you DO control is the higher-level "
        "intent (stance with `mc_stance`, tasks with `mc_task`). "
        "If one of them interrupts something you were doing, that is why."
    )


def config_keys() -> set[str]:
    out: set[str] = set()
    for i in INSTINCTS:
        if i.config_key:
            out.add(i.config_key)
        out.update(k.strip() for k in i.tunable.split(",") if k.strip())
    return out
