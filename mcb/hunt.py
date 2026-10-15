
from __future__ import annotations

from astrbot.api import logger

from .arbiter import COMBAT_TTL, LEVEL_COMBAT

# 独立 owner，交战后释放，避免与反射抢 walk。
OWNER = "hunt"

SEEK_TTL = COMBAT_TTL

# 目标未明显移动时只续期；重复 goto 会重规划并抖动。
REISSUE_MOVE = 1.5

# 落差过大可能导致 Baritone 凿穿或搭路。
MAX_Y_GAP = 6.0

# 地下追怪可能凿穿地形。
UNDERGROUND_GAP = 1.5


class HuntGuard:

    def __init__(self, bridge, arbiter=None, *, enabled: bool = True,
                 journal=None) -> None:
        self.bridge = bridge
        self.arbiter = arbiter
        self.enabled = bool(enabled)
        self.journal = journal

        self._aim: tuple[int, int] | None = None
        self._aim_name = ""
        self._target = None
        self._warned_no_monster_field = False


    def bind_arbiter(self, arbiter) -> None:
        self.arbiter = arbiter

    def render(self) -> str:
        if not self.enabled:
            return "hunt=off"
        if self._aim is None:
            return ""
        return f"hunt=追{self._aim_name or '怪'}中"


    async def tick(self, data: dict, hostiles: list[dict], *, stance: str,
                   in_combat: bool, arrive_range: float) -> None:
        r"""arrive_range 由反射传入，避免两套门槛漂移。
        arrive_range 由反射传（它才是进战门槛的唯一事实来源）——
        别在这里再写一个常量，那样两边会漂。
        """
        if not self.enabled or self.arbiter is None:
            return
        if not isinstance(data, dict) or not data.get("online"):
            self._aim = None
            return

        if stance != "hunt":
            if self._aim is not None:
                await self._release("姿态不是 hunt 了")
            return

        if in_combat:
            if self._aim is not None:
                await self._release("已经打起来了")
            return

        top = self.arbiter.walk_holder()
        mine = top is not None and top.owner == OWNER
        if top is not None and not mine and top.level < LEVEL_COMBAT:
            if self._aim is not None:
                await self._release("别人在管她")
            return

        tgt = self._pick(hostiles, arrive_range)
        if tgt is None:
            if not self._warned_no_monster_field and self._saw_hostiles_without_monster_field(hostiles):
                self._warned_no_monster_field = True
                logger.warning(
                    "[mc_body] 主动清怪挑不到目标：服务端没回 monster 字段 —— "
                    "多半是 mcbridge_server.js 还没更新（scp 后要 /reload）"
                )
            if mine:
                await self._release("附近没有要打的怪了")
            return

        pos = tgt.get("pos")
        if not isinstance(pos, list) or len(pos) < 3:
            return
        try:
            tx, tz = int(pos[0]), int(pos[2])
            ty = float(pos[1])
        except (TypeError, ValueError):
            return

        if not self._can_seek(data, ty):
            if mine:
                await self._release("环境不适合追过去（水里/地下/落差太大）")
            return

        # 目标未变只续期，重复下发会抖动。
        if mine and self._aim is not None:
            dx = tx - self._aim[0]
            dz = tz - self._aim[1]
            if (dx * dx + dz * dz) ** 0.5 < REISSUE_MOVE:
                await self.arbiter.touch_walk(OWNER, SEEK_TTL)
                return

        name = str(tgt.get("name") or "怪")
        await self.arbiter.claim_walk(
            LEVEL_COMBAT, OWNER, f"goto {tx} {tz}", f"主动清怪：追 {name}", ttl=SEEK_TTL
        )
        if self.arbiter.last_error is not None:
            logger.info(f"[mc_body] 主动清怪下发失败：{self.arbiter.last_error}")
            self._aim = None
            return
        if self._aim is None:
            logger.info(
                f"[mc_body] 主动清怪：盯上 {name}（{tgt.get('dist')} 格），走过去"
            )
            if self.journal is not None:
                self.journal.add("hunt", f"主动清怪：去追 {name}")
        self._aim = (tx, tz)
        self._aim_name = name
        self._target = tgt


    @staticmethod
    def _pick(hostiles: list[dict], arrive_range: float) -> dict | None:
        r"""主动追击只认 monster；hostile 并集可能包含宠物。"""
        if not isinstance(hostiles, list):
            return None
        for h in hostiles:                      
            if not isinstance(h, dict) or h.get("monster") is not True:
                continue
            dist = h.get("dist")
            if not isinstance(dist, (int, float)) or isinstance(dist, bool):
                continue
            if float(dist) > float(arrive_range):
                return h
        return None

    def _saw_hostiles_without_monster_field(self, hostiles: list[dict]) -> bool:
        r"""缺 monster 字段时单独告警，避免误认为附近无怪。"""
        if not isinstance(hostiles, list) or not hostiles:
            return False
        return all(isinstance(h, dict) and "monster" not in h for h in hostiles)

    def _can_seek(self, data: dict, target_y: float) -> bool:
        r"""环境未知时不追击。"""
        if data.get("inWater") is True or data.get("underWater") is True:
            return False
        y = data.get("y")
        if not isinstance(y, (int, float)) or isinstance(y, bool):
            return False
        surf = data.get("surfY")
        if isinstance(surf, (int, float)) and not isinstance(surf, bool):
            if float(y) < float(surf) - UNDERGROUND_GAP:
                return False                
        return abs(target_y - float(y)) <= MAX_Y_GAP

    async def _release(self, why: str) -> None:
        self._aim = None
        self._aim_name = ""
        self._target = None
        await self.arbiter.release_walk(OWNER)
