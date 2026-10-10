"""权限层：唯一裁决口 —— 「这个动作能不能改世界」。

抄 Numen 的三样（依据 docs\\22）：事实（这一格是谁放的）、唯一裁决口（Gate.decide）、
规则是数据（Rule / RuleSet）。身体层一行不抄。

分工定死（docs\\22 必答一）：
    服务端只回答事实 —— 这一格是谁放的（桥命令 mcb placed）。
    插件只回答裁决 —— 这个动作能不能做（本模块）。
裁决不写在服务端 tick 里，因为"去问主人"要挂起等待，那不是 tick 里能做的事。

注意： 别和 main.py 的 _guard/_authorize 混为一谈（docs\\22 §1 第 9 行）：
    _guard 管的是"谁有资格下命令"（发送者白名单）；
    本模块管的是"这个动作能不能改世界"。
    并列两道门，分开命名、分开日志。

注意：注意： 我们做到的粒度比 Numen 粗一档，这是承认的差距（docs\\22 §3 第 5 条）：
    它插桩在每一格挖掘的落点上；我们挖靠 Baritone，插不进去。
    逐格破坏的裁决仍在服务端 BlockEvents.broken 里（已部署、已实测），
    本模块管的是"派发工具之前"这一层。两层不重复，是纵深。

这个模块是纯的：不碰网络、不碰磁盘（load_owner_rules 除外）、不 import 插件其它部分 ——
所以能离线单测（plugin-check\\check.py 第 17 节）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# ---- 模式 -----------------------------------------------------------


class Mode:
    """每个同伴一个，主人设。抄 Numen Mode。"""

    ASK = "ask"          # 走规则表（默认）
    BYPASS = "bypass"    # 全放行：单机不想被打扰时用
    OBSERVE = "observe"  # 只看不动：拒绝一切改世界的动作，等于 plan mode

    ALL = (ASK, BYPASS, OBSERVE)

    @staticmethod
    def by_name(name: str) -> str:
        """认不出就回 ASK。注意： 别改成"认不出回 BYPASS" —— 配错一个字母就等于全放行。"""
        low = str(name or "").strip().lower()
        return low if low in Mode.ALL else Mode.ASK


# ---- 动作 -----------------------------------------------------------


class Kind:
    """动作的动词。只留我们真有的四个（docs\\22 §1 第 2 行）。

    use_block 并进 place 了；drop / command 我们没有对应的工具，砍掉。
    """

    BREAK = "break"
    PLACE = "place"
    ATTACK = "attack"
    TAKE = "take"

    ALL = (BREAK, PLACE, ATTACK, TAKE)


@dataclass(frozen=True)
class Action:
    """一个要被裁决的动作。不带工具名、不带 JSON（Numen Action 的简化版）。"""

    kind: str
    pos: tuple[int, int, int] | None = None
    # 目标格上的方块注册名（break / take 看它）
    block: str | None = None
    # 手上的东西（place 看它）—— 服务端给的是注册名，不是显示名
    item: str | None = None
    # 目标实体的 uuid（attack 看它）
    entity: str | None = None

    def subject_id(self) -> str | None:
        """规则里写种类 id（minecraft:chest）时认的那个 id。

        读不到的返回 None —— 别拿"读不到"当"不是它"，那会让 allow 行误命中。
        """
        if self.kind in (Kind.BREAK, Kind.TAKE):
            return self.block
        if self.kind == Kind.PLACE:
            return self.item
        # ATTACK 要认的是实体种类，我们这一版没有读取通道（docs\\22 §5 未核实项）
        return None


# ---- 事实 -----------------------------------------------------------


@dataclass
class Facts:
    """裁决一个动作要用到的事实。全部由调用方问服务端拿到，这里只做搬运。

    读不到的一律留 None —— 缺失不等于假（README 硬约束速查里那条）。
    信号函数再按"哪种答案更保守"决定拿 None 怎么办。
    """

    actor: str = ""                    # 她自己的 uuid（服务端给的 entity.uuid）
    pos: tuple[int, int, int] | None = None
    dim: str = ""
    block: str | None = None           # 目标格上的方块注册名
    placed_by: str | None = None       # 这一格的放置记录记的是谁（uuid）
    placed_name: str = ""              # 记的那个人的名字（给她看）
    block_entity: bool = False         # 这一格是不是带方块实体（容器/设施）
    contents: bool | None = None       # 容器里有没有东西（None = 没通道读，按"有"算）
    near_placed: bool | None = None    # 附近有没有别人放的方块（None = 没通道读，按"有"算）
    entity_owned: bool | None = None   # 目标实体有没有主人
    entity_named: bool | None = None   # 目标实体有没有名字
    entity_villager: bool | None = None
    entity_hostile: bool | None = None

    @classmethod
    def from_placed_reply(cls, reply: dict, actor: str = "") -> "Facts":
        """把桥命令 mcb placed 的回执翻成事实。

        回执形状（服务端 mcbPlacedFact）：pos / dim / block / placed / blockEntity / actor。
        actor 是"她自己"的 uuid —— self_placed 那个信号靠它把"我放的"和"别人放的"分开。
        旧版服务端没这个字段时退回外面传进来的 actor（拿不到就是空串：self_placed 恒不成立，
        裁决会偏保守，不会偏放行）。

        placed 为 None = 这一格没有放置记录。它有两种意思，别混：
            真没人放过（天然方块），或者放过但那格后来被换掉了（服务端读到 block 对不上会清记录）。
        """
        if not isinstance(reply, dict):
            return cls(actor=actor)
        raw_pos = reply.get("pos")
        pos = None
        if isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 3:
            try:
                pos = (int(raw_pos[0]), int(raw_pos[1]), int(raw_pos[2]))
            except (TypeError, ValueError):
                pos = None
        placed = reply.get("placed")
        placed_by = None
        placed_name = ""
        if isinstance(placed, dict):
            placed_by = str(placed.get("uuid") or "") or None
            placed_name = str(placed.get("name") or "")
        return cls(
            actor=str(reply.get("actor") or actor or ""),
            pos=pos,
            dim=str(reply.get("dim") or ""),
            block=str(reply.get("block") or "") or None,
            placed_by=placed_by,
            placed_name=placed_name,
            block_entity=bool(reply.get("blockEntity")),
        )


# ---- 信号 -----------------------------------------------------------
#
# 每个信号只回答一个通用问题、各自独立、无状态（抄 Numen Signals 的立意）。
# 不按方块或生物种类枚举 —— "玩家放的 / 带方块实体 / 有主人 / 有名字"这几个信号
# 覆盖原版和任何模组，不用一个模组一个模组适配。
#
# "读不到"取哪个值，判据只有一条：取会让裁决更保守的那个。
# 也就是宁可多问一次主人，也别悄悄放行。


@dataclass(frozen=True)
class Signal:
    name: str
    label: str
    irreversible: bool
    test: object  # (Action, Facts) -> bool


def _placed(a: Action, f: Facts) -> bool:
    return f.placed_by is not None and f.placed_by != f.actor


def _self_placed(a: Action, f: Facts) -> bool:
    return f.placed_by is not None and f.actor != "" and f.placed_by == f.actor


def _block_entity(a: Action, f: Facts) -> bool:
    return bool(f.block_entity)


def _contents(a: Action, f: Facts) -> bool:
    if not f.block_entity:
        return False
    # 读不到（没有通道）按"有"算 —— 拆了东西洒一地会消失，撤不回（Numen 同一条）
    return True if f.contents is None else bool(f.contents)


def _near_placed(a: Action, f: Facts) -> bool:
    # 读不到（我们还没有邻域查询）按"有"算 —— 只影响危险物品那一行
    return True if f.near_placed is None else bool(f.near_placed)


def _owned(a: Action, f: Facts) -> bool:
    # 读不到按"有主人"算 —— 宁可问一句也别打死谁的宠物
    return True if f.entity_owned is None else bool(f.entity_owned)


def _named(a: Action, f: Facts) -> bool:
    return True if f.entity_named is None else bool(f.entity_named)


def _villager(a: Action, f: Facts) -> bool:
    return bool(f.entity_villager)


def _hostile(a: Action, f: Facts) -> bool:
    return bool(f.entity_hostile)


HAZARD_ITEMS = frozenset({
    "minecraft:lava_bucket",
    "minecraft:flint_and_steel",
    "minecraft:fire_charge",
    "minecraft:tnt",
    "minecraft:water_bucket",
})


def _hazard_item(a: Action, f: Facts) -> bool:
    return a.item is not None and a.item in HAZARD_ITEMS


SIGNALS: dict[str, Signal] = {
    s.name: s for s in (
        Signal("placed", "是别人放置的方块", False, _placed),
        Signal("self_placed", "是她自己放的方块", False, _self_placed),
        Signal("block_entity", "带方块实体（容器或设施）", False, _block_entity),
        Signal("contents", "容器里有东西", True, _contents),
        Signal("near_placed", "附近有别人放的方块", False, _near_placed),
        Signal("owned", "目标实体有主人", True, _owned),
        Signal("named", "目标实体有名字", False, _named),
        Signal("villager", "目标是村民", False, _villager),
        Signal("hostile", "目标是敌对生物", False, _hostile),
        Signal("hazard_item", "是会烧会炸会淹的危险物品", False, _hazard_item),
    )
}


# ---- 裁决 -----------------------------------------------------------


class VerdictKind:
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


UNCOVERED = "没有任何一行规则覆盖这个动作"


@dataclass(frozen=True)
class Verdict:
    """一次裁决的答复。抄 Numen Verdict 的三分：放行 / 拒绝 / 去问。

    cause 是给回执的整句理由（放行为空串）。
    """

    kind: str
    cause: str = ""
    rule: str | None = None

    @property
    def allowed(self) -> bool:
        return self.kind == VerdictKind.ALLOW

    @property
    def asks(self) -> bool:
        return self.kind == VerdictKind.ASK

    def reason(self) -> str:
        if self.kind == VerdictKind.ALLOW:
            return ""
        if self.kind == VerdictKind.DENY:
            return self.cause
        return f"{self.cause}：需要主人同意"


ALLOWED = Verdict(VerdictKind.ALLOW)


# ---- 规则 -----------------------------------------------------------
#
# 一行字符串：动作(项 & 项 & !项)，与 Claude Code 的 Tool(specifier) 同形（抄 Numen Rule）。
# 全仓只在这一个类里解析 —— 别在调用方自己 split。
#
# 注意： 与 Numen 的两处差异，都是"我们没有那个通道"，不是偷懒：
#     1. 标签项（#minecraft:beds）不支持 —— 我们读不到方块的标签，解析时直接报教学式错误。
#        写一个永远不会命中的规则，比报错更糟（那正是"看着有、实际没有"）。
#     2. 指令项（command(...)）不支持 —— 我们没有向 LLM 暴露任意游戏指令的工具。


class RuleError(ValueError):
    """规则写错了。消息要说清哪儿错、认得的有哪些。"""


@dataclass(frozen=True)
class _Term:
    type: str          # any / signal / id / entity
    negated: bool
    signal: Signal | None = None
    id: str | None = None
    uuid: str | None = None
    text: str = ""

    def matches(self, a: Action, f: Facts) -> bool:
        if self.type == "any":
            hit = True
        elif self.type == "signal":
            hit = bool(self.signal and self.signal.test(a, f))  # type: ignore[operator]
        elif self.type == "id":
            hit = self.id is not None and self.id == a.subject_id()
        else:
            hit = a.entity is not None and self.uuid is not None and self.uuid == a.entity
        return hit != self.negated

    def describe(self) -> str:
        if self.type == "any":
            return ""
        if self.type == "signal":
            return self.signal.label if self.signal else ""
        if self.type == "id":
            return f"是 {self.id}"
        return f"是实体 {self.uuid}"


@dataclass(frozen=True)
class Rule:
    text: str
    kind: str | None            # None = 任何动作
    terms: tuple[_Term, ...]

    @staticmethod
    def parse(raw: str) -> "Rule":
        text = (raw or "").strip()
        open_at = text.find("(")
        if open_at <= 0 or not text.endswith(")"):
            raise RuleError(
                f"规则要写成 动作(项 & 项 & !项) 的形状，例如 break(placed & !block_entity)：{raw!r}"
            )
        verb = text[:open_at].strip()
        kind: str | None = None
        if verb != "*":
            if verb not in Kind.ALL:
                raise RuleError(
                    f"规则 {raw!r} 里不认识动词 {verb!r}；"
                    f"认得的动词是 {'、'.join(Kind.ALL)}，或者用 * 表示任何动作"
                )
            kind = verb
        inner = text[open_at + 1:-1].strip()
        if not inner:
            raise RuleError(f"规则至少要有一个项（写 * 表示任何）：{raw!r}")
        terms = tuple(_parse_term(piece.strip(), raw) for piece in inner.split("&"))
        return Rule(text=text, kind=kind, terms=terms)

    def matches(self, a: Action, f: Facts) -> bool:
        if self.kind is not None and self.kind != a.kind:
            return False
        return all(t.matches(a, f) for t in self.terms)

    def irreversible(self) -> bool:
        """命中这条规则的动作撤不回（它的正项里有撤不回的信号）。"""
        return any(
            not t.negated and t.type == "signal" and t.signal is not None
            and t.signal.irreversible
            for t in self.terms
        )

    def describe(self) -> str:
        """命中时给回执的短语：各正项的自述。"""
        parts = [t.describe() for t in self.terms if not t.negated and t.describe()]
        return "，".join(parts) if parts else self.text

    def __str__(self) -> str:
        return self.text


def _parse_term(raw: str, rule: str) -> _Term:
    negated = raw.startswith("!")
    body = raw[1:].strip() if negated else raw
    if not body:
        raise RuleError(f"规则 {rule!r} 里有一个空项")
    if body == "*":
        return _Term("any", negated, text=body)
    if body.startswith("#"):
        raise RuleError(
            f"规则 {rule!r} 用了标签项 {body!r}，这一版还不支持 —— "
            "我们还没有读方块标签的通道。请改写成具体的方块 id（例如 minecraft:chest），"
            "或者用 block_entity 这个信号代替"
        )
    if body.startswith("entity:"):
        uuid = body[7:].strip()
        if not uuid:
            raise RuleError(f"规则 {rule!r} 里的 entity: 后面没写 uuid")
        return _Term("entity", negated, uuid=uuid, text=body)
    if ":" in body:
        return _Term("id", negated, id=body, text=body)
    signal = SIGNALS.get(body)
    if signal is None:
        raise RuleError(
            f"规则 {rule!r} 里不认识信号 {body!r}；"
            f"认得的信号是 {'、'.join(SIGNALS)}，"
            "或者写一个带命名空间的 id（minecraft:chest）、entity:<uuid>、*"
        )
    return _Term("signal", negated, signal=signal, text=body)


# ---- 规则表 ---------------------------------------------------------
#
# 一层三张表：deny / allow / ask，层内顺序 deny -> allow -> ask（抄 Numen RuleSet）。
# allow 排在 ask 前面，是为了以后"允许并记住"抠出来的细 allow 行能轮得到。
#
# 注意： 出厂 allow 行必须写得比 ask 行窄 —— 自然方块那一行要把有人放过的、
#     带方块实体的排除在外，它们才轮得到 ask 表。改这几行之前先想清楚这件事。


FACTORY_ALLOW = (
    # 挖自然方块：谁都没放过、她自己也没放过、没有方块实体
    "break(!placed & !self_placed & !block_entity)",
    # 拆她自己搭的：垫的柱子、搭的桥。里面装着东西的容器除外，那个走 ask
    "break(self_placed & !contents)",
    # 放不危险的东西
    "place(!hazard_item)",
    # 危险物品（岩浆/打火石/TNT/水桶）：附近没有别人放的东西才放
    "place(hazard_item & !near_placed)",
    # 打没主人、没名字、不是村民的（敌对生物与野生动物）
    "attack(!owned & !named & !villager)",
)

FACTORY_ASK = (
    # 装着东西的容器先于通用的"别人放的" —— 更具体的在前
    "break(self_placed & contents)",
    "break(placed)",
    "break(block_entity)",
    # 攻击有主的、有名字的、村民
    "attack(owned)",
    "attack(named)",
    "attack(villager)",
    "place(hazard_item & near_placed)",
    # 注意：注意： 从容器里拿东西一律问 —— 这一条是我们和 Numen 立场不同的地方。
    #     Numen 把 take(*) 放进出厂 allow（它的理由是"相当于 Claude Code 读项目文件"）。
    #     那是它的立场，不是我们的：对我们来说"她翻箱子把东西拿走"恰恰是要防的。
    #     抄机制，不抄这条立场（docs\\22 §3 第 2 条）。
    "take(*)",
)


@dataclass
class RuleSet:
    """一层规则。规则是数据 —— 这里只存和查，不判世界。"""

    deny: tuple[Rule, ...] = ()
    allow: tuple[Rule, ...] = ()
    ask: tuple[Rule, ...] = ()

    @staticmethod
    def factory() -> "RuleSet":
        return _FACTORY

    @staticmethod
    def parse_rows(deny=(), allow=(), ask=()) -> "RuleSet":
        return RuleSet(
            deny=tuple(Rule.parse(r) for r in deny),
            allow=tuple(Rule.parse(r) for r in allow),
            ask=tuple(Rule.parse(r) for r in ask),
        )

    def table(self, name: str) -> tuple[Rule, ...]:
        return {"deny": self.deny, "allow": self.allow, "ask": self.ask}[name]


_FACTORY = RuleSet(
    deny=(),
    allow=tuple(Rule.parse(r) for r in FACTORY_ALLOW),
    ask=tuple(Rule.parse(r) for r in FACTORY_ASK),
)

EMPTY = RuleSet()


def _first_match(table: tuple[Rule, ...], a: Action, f: Facts) -> Rule | None:
    for rule in table:
        if rule.matches(a, f):
            return rule
    return None


def load_owner_rules(path: str | Path) -> RuleSet:
    """读主人自己写的那一层。文件不存在或读不动就当没写过（EMPTY），不抛。

    形状：{"deny": [...], "allow": [...], "ask": [...]} —— 三张表都可以缺。
    写错的规则会抛 RuleError，由调用方决定怎么办（别静默吞掉：一条永远不会命中的规则
    比一个报错更糟）。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return EMPTY
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise RuleError("主人规则文件要是一个对象：{\"deny\": [...], \"allow\": [...], \"ask\": [...]}")
    return RuleSet.parse_rows(
        deny=raw.get("deny") or (),
        allow=raw.get("allow") or (),
        ask=raw.get("ask") or (),
    )


# ---- 唯一裁决口 -----------------------------------------------------


@dataclass
class Judge:
    """唯一裁决口。任何"这个动作能不能做"的答案都从这里出，别在工具里另写一套。

    查的顺序（抄 Numen Gate.decide，全篇最该抄的一段）：
        模式 -> 主人层(deny -> allow -> ask) -> 出厂层(deny -> allow -> ask) -> 都不中也问。
    第一处命中即定。主人层整体先于出厂层：主人手写的行压得过出厂的。

    代码里不写死"能"也不写死"不能"：
        放行只来自 allow 行与主人选的 bypass；
        拒绝只来自 deny 行与主人选的 observe；
        其余一律问。
    """

    mode: str = Mode.ASK
    owner: RuleSet = field(default_factory=lambda: EMPTY)

    def decide(self, action: Action, facts: Facts) -> Verdict:
        if self.mode == Mode.BYPASS:
            return ALLOWED
        if self.mode == Mode.OBSERVE:
            return Verdict(VerdictKind.DENY, f"observe 模式：{action.kind} 会改动世界")
        hit: Rule | None = None
        for layer in (self.owner, RuleSet.factory()):
            denied = _first_match(layer.deny, action, facts)
            if denied is not None:
                return Verdict(VerdictKind.DENY, f"被主人写的规则挡住：{denied}")
            if _first_match(layer.allow, action, facts) is not None:
                return ALLOWED
            hit = _first_match(layer.ask, action, facts)
            if hit is not None:
                break
        if hit is None:
            return Verdict(VerdictKind.ASK, UNCOVERED)
        return Verdict(VerdictKind.ASK, hit.describe(), rule=hit.text)
