r"""工具派发前统一裁决；逐格破坏另由服务端 BlockEvents.broken 检查。"""
# 部分解析、裁决、默认表及 RuleSet 文字根据 Numen Rule/Gate/RuleSet 改写；上游 LGPL-3.0-only。
# 本地 Git 首次记录及规则调整：2026-10-11；来源通知更新：2026-10-16。
# 具体来源、修改和许可边界见 ../THIRD_PARTY_NOTICES.md；许可全文见 ../licenses/Numen-LGPL-3.0.txt 和 ../licenses/GPL-3.0.txt。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path



class Mode:
    r"""每个同伴独立设置权限模式。"""

    ASK = "ask"          
    BYPASS = "bypass"    
    OBSERVE = "observe"  

    ALL = (ASK, BYPASS, OBSERVE)

    @staticmethod
    def by_name(name: str) -> str:
        low = str(name or "").strip().lower()
        return low if low in Mode.ALL else Mode.ASK




class Kind:

    BREAK = "break"
    PLACE = "place"
    ATTACK = "attack"
    TAKE = "take"

    ALL = (BREAK, PLACE, ATTACK, TAKE)


@dataclass(frozen=True)
class Action:
    r"""待裁决动作不依赖工具名或 JSON。来源：Numen Action。"""

    kind: str
    pos: tuple[int, int, int] | None = None
    block: str | None = None
    item: str | None = None
    entity: str | None = None

    def subject_id(self) -> str | None:
        r"""未知返回 None，避免 allow 误命中。"""
        if self.kind in (Kind.BREAK, Kind.TAKE):
            return self.block
        if self.kind == Kind.PLACE:
            return self.item
        # ATTACK 尚无实体种类读取通道。
        return None




@dataclass
class Facts:
    """裁决一个动作要用到的事实。全部由调用方问服务端拿到，这里只做搬运。

    读不到的一律留 None —— 缺失不等于假（README 硬约束速查里那条）。
    信号函数再按"哪种答案更保守"决定拿 None 怎么办。
    """

    actor: str = ""                    
    pos: tuple[int, int, int] | None = None
    dim: str = ""
    block: str | None = None           
    placed_by: str | None = None       
    placed_name: str = ""              
    block_entity: bool = False         
    contents: bool | None = None       # 内容未知按有内容处理。
    near_placed: bool | None = None    # 邻域未知按有放置处理。
    entity_owned: bool | None = None   
    entity_named: bool | None = None   
    entity_villager: bool | None = None
    entity_hostile: bool | None = None

    @classmethod
    def from_placed_reply(cls, reply: dict, actor: str = "") -> "Facts":
        r"""placed=None 既可能天然，也可能旧记录因方块变化被清理。"""
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

    @classmethod
    def from_attack_reply(cls, reply: dict, actor: str = "") -> "Facts":
        r"""ownerVia=absent 表示无主人概念；未读到 ownerVia 则未知。"""
        if not isinstance(reply, dict):
            return cls(actor=actor)
        owner_via = str(reply.get("ownerVia") or "")
        if owner_via == "absent":
            owned: bool | None = False
        elif owner_via:
            owned = bool(str(reply.get("owner") or ""))
        else:
            owned = None
        custom = reply.get("custom")
        villager = reply.get("villager")
        return cls(
            actor=str(reply.get("actor") or actor or ""),
            entity_owned=owned,
            entity_named=None if custom is None else bool(custom),
            entity_villager=None if villager is None else bool(villager),
        )


# 信号独立、无状态，各回答一个通用问题。


@dataclass(frozen=True)
class Signal:
    name: str
    label: str
    irreversible: bool
    test: object  


def _placed(a: Action, f: Facts) -> bool:
    return f.placed_by is not None and f.placed_by != f.actor


def _self_placed(a: Action, f: Facts) -> bool:
    return f.placed_by is not None and f.actor != "" and f.placed_by == f.actor


def _block_entity(a: Action, f: Facts) -> bool:
    return bool(f.block_entity)


def _contents(a: Action, f: Facts) -> bool:
    if not f.block_entity:
        return False
    # 内容未知按有内容处理，避免不可逆损失。
    return True if f.contents is None else bool(f.contents)


def _near_placed(a: Action, f: Facts) -> bool:
    # 邻域无读取通道，按有处理。
    return True if f.near_placed is None else bool(f.near_placed)


def _owned(a: Action, f: Facts) -> bool:
    # 主人未知按有主人处理。
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




class VerdictKind:
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


UNCOVERED = "没有任何一行规则覆盖这个动作"


@dataclass(frozen=True)
class Verdict:
    r"""裁决分为放行、拒绝、询问；cause 为回执理由，放行时为空。来源：Numen Verdict。"""

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


# 规则格式为动作(项 & 项 & !项)。



class RuleError(ValueError):
    """规则写错了。消息要说清哪儿错、认得的有哪些。"""


@dataclass(frozen=True)
class _Term:
    type: str          
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
    kind: str | None            
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
        return any(
            not t.negated and t.type == "signal" and t.signal is not None
            and t.signal.irreversible
            for t in self.terms
        )

    def describe(self) -> str:
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


# 层内按 deny、allow、ask 顺序查询。
# allow 先于 ask，供窄范围授权命中。
# 出厂 allow 必须排除受保护对象，否则会抢先于 ask 放行。


FACTORY_ALLOW = (
    "break(!placed & !self_placed & !block_entity)",
    "break(self_placed & !contents)",
    "place(!hazard_item)",
    "attack(!owned & !named & !villager)",
)

FACTORY_ASK = (
    "break(self_placed & contents)",
    "break(placed)",
    "break(block_entity)",
    "attack(owned)",
    "attack(named)",
    "attack(villager)",
    "place(hazard_item)",
    # 容器取物默认询问。
    
    "take(*)",
)

# 邻域查询不可用，危险放置统一询问。



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
    r"""规则写错必须抛 RuleError，不静默当空规则。"""
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




@dataclass
class Judge:
    r"""唯一裁决入口，按模式、主人层、出厂层依次检查；层内 deny、allow、ask 首次命中即定，未命中则询问。来源：Numen Gate.decide。"""

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
