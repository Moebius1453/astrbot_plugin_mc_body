"""主动战斗 —— 去找怪打，而不是等怪贴上来。

## 为什么和"反击"分开

用户 2026-10-11：

> "主动找怪加功能不难吧  反击 和主动战斗应该是分开的嘛"

原来是混在一起的：`hunt` 姿态只是把反射状态机的**进战门槛**从"它正瞄着我"放宽到
"5 格内有怪"，5 格以外一律不理会。所以她说"我会主动去找怪物"是照着工具描述
过度承诺 —— 代码里根本没有"找"这个动作。

分开有两层：

1 层不同。
  反击（挨打 / 被瞄准 -> 还手）是保命，走 LEVEL_REFLEX，压过用户是对的。
  主动战斗（去找怪）是进攻，走 LEVEL_COMBAT —— 用户一开口就该让路。
  分层理由写在 arbiter.py 的 LEVEL_COMBAT 那一段。

2 谁触发不同。
  反击由"怪贴上来"触发（反射状态机，不过 LLM）。
  主动战斗由"她想去清场"触发（stance == hunt，白自己用 mc_stance 切）。

## 它做什么

挂在反射那一秒一次的节拍上，不另起循环、也不额外问桥 ——
反射这一 tick 本来就扫过一遍附近实体了，直接把那份 hostiles 交过来。

只有同时满足才动：

  1 姿态是 hunt（她说了要主动清怪）
  2 反射当前不在战斗态（打起来了就交给状态机，两边别抢走路通道）
  3 walk 通道没有被更高优先级的人占着（用户指令 / 保命反射）
  4 环境允许（见 _can_seek：不在水里、在地表附近、和目标的落差不大）

找到最近的敌对 -> 走到它够得着的范围 -> 收手，交给反射状态机接管
（进战、挥、走位、追那半步）。

## 边界与已知代价

- 搜索半径不是这里定的 —— 用的是反射那一份扫描（reflex_scan_range，默认 24 格）。
  不另发一次 mcb threats，省一次 RCON 往返。
- 只追 `monster`（服务端回的"真的是 Monster 类"那个字段），不追 `hostile`。
  `hostile` 是三条并集（Monster / 名字像怪 / 正瞄着我），保命用它是宁可多算；
  主动跑去杀不能那么算 —— 狼在 1.21 不是 Monster，按并集追会把主人驯的狼一起打。
  注意： 这个字段是 2026-10-11 加到 mcbridge_server.js 的，
  服务端没 /reload 之前挑不到目标（会在日志里吭一声，见 _pick 的说明）。
- 她走路靠 Baritone，而 `allowBreak` 默认开着 —— 追怪可能一路凿过去
  （用户 2026-10-10 问过的"抽风挖地板"）。所以 _can_seek 里挡了
  "明显要凿/要搭"的两种情况（地下、落差大）。这不是治本，治本要关 allowBreak。
- 高低差判据用的是目标位置，读不到就按"不知道"处理 —— 不猜。
"""

from __future__ import annotations

from astrbot.api import logger

from .arbiter import COMBAT_TTL, LEVEL_COMBAT

# 仲裁层里的 owner 名。注意： 故意和反射的 "reflex" 分开 ——
# 反射在战斗态里也在 LEVEL_COMBAT 上声明，两个 owner 分开是让它们"争不着"：
# 这里只在"没打起来"的时候动，打起来了就 release 让位（见 tick 的第 2 条）。
OWNER = "hunt"

# 这条"追过去"的声明活多久。走的是 COMBAT_TTL —— 目标没了要尽快让位。
SEEK_TTL = COMBAT_TTL

# 目标挪了这么多格就重发一次 goto。
#
# 为什么不每 tick 重发：重发 = 一条新命令 = Baritone 重新规划路径。
# 贴着怪走的时候每 tick 重算一次，她会原地来回抽。
# 为什么也不能不重发：怪在动，不重发就永远走向它当初站的地方。
REISSUE_MOVE = 1.5

# 和目标的落差超过这个就不接 —— 那意味着要凿穿或者往上搭。
# 数值取 6：一个正常矿洞/台阶的高低差够用，再大就明显是在穿地形。
MAX_Y_GAP = 6.0

# 地下不主动找怪（和 idle.py 的同一条理由：那是凿穿最凶的地方）。
# 判据是她离自己那一列地表的高度差。
UNDERGROUND_GAP = 1.5


class HuntGuard:
    """主动清怪。反射每 tick 把已经读到的状态和实体列表交给它。"""

    def __init__(self, bridge, arbiter=None, *, enabled: bool = True,
                 journal=None) -> None:
        self.bridge = bridge
        self.arbiter = arbiter
        self.enabled = bool(enabled)
        self.journal = journal

        # 上一次下发的目标点。用来判"怪挪了没有"，决定要不要重发。
        self._aim: tuple[int, int] | None = None
        self._aim_name = ""
        self._target = None
        # 服务端缺 monster 字段时只提醒一次（见 _saw_hostiles_without_monster_field）
        self._warned_no_monster_field = False

    # ---- 外部 -----------------------------------------------------------

    def bind_arbiter(self, arbiter) -> None:
        self.arbiter = arbiter

    def render(self) -> str:
        """一行摘要，给状态包用。没在主动找怪就什么都不写（不占字数）。

        注意： 这一行是律4（能力自我认知 = 工具表 + 状态包）的要求 ——
        "主动去找怪"是程序替她做的决定，状态包里没有的话她自己看不见。
        """
        if not self.enabled:
            return "hunt=off"
        if self._aim is None:
            return ""
        return f"hunt=追{self._aim_name or '怪'}中"

    # ---- 每 tick --------------------------------------------------------

    async def tick(self, data: dict, hostiles: list[dict], *, stance: str,
                   in_combat: bool, arrive_range: float) -> None:
        """反射读到的状态 + 它这一 tick 已经扫到的实体列表，原样交过来。

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

        # 打起来了 -> 交给反射的状态机，别两边抢 walk 通道
        if in_combat:
            if self._aim is not None:
                await self._release("已经打起来了")
            return

        top = self.arbiter.walk_holder()
        mine = top is not None and top.owner == OWNER
        # 更高优先级的人在管（用户指令 / 保命反射）—— 让开，等他们完事再重新扫
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

        # 目标没怎么动 -> 路线还有效，续个命就行，别重发（重发会让她原地抽）
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

    # ---- 内部 -----------------------------------------------------------

    @staticmethod
    def _pick(hostiles: list[dict], arrive_range: float) -> dict | None:
        """挑要追的那只。两个条件：

        1 只认 `monster`（真的是 Monster 类）。服务端的 `hostile` 是三条并集
          （Monster 类 / 名字像怪 / 正瞄着我），那是给保命用的，宁可多算 ——
          狼、羊驼都在里头。主动跑去杀不能按那个并集来，否则连主人驯的狼一起打。
        2 够得着的（<= arrive_range）不算 —— 那是反射状态机的事。
        """
        if not isinstance(hostiles, list):
            return None
        for h in hostiles:                      # 服务端已经按距离排过序
            if not isinstance(h, dict) or h.get("monster") is not True:
                continue
            dist = h.get("dist")
            if not isinstance(dist, (int, float)) or isinstance(dist, bool):
                continue
            if float(dist) > float(arrive_range):
                return h
        return None

    def _saw_hostiles_without_monster_field(self, hostiles: list[dict]) -> bool:
        """服务端脚本还没更新（没有 monster 字段）时会一直挑不到目标。

        这种情况要吭一声，不然表现是"开了 hunt 却什么都不做"，
        和"附近真的没怪"分不开 —— 又一条"没触发要追问是坏了还是没条件"。
        """
        if not isinstance(hostiles, list) or not hostiles:
            return False
        return all(isinstance(h, dict) and "monster" not in h for h in hostiles)

    def _can_seek(self, data: dict, target_y: float) -> bool:
        """环境允许不允许追过去。读不到就当"不知道"，按不允许处理（不猜）。"""
        if data.get("inWater") is True or data.get("underWater") is True:
            return False
        y = data.get("y")
        if not isinstance(y, (int, float)) or isinstance(y, bool):
            return False
        surf = data.get("surfY")
        if isinstance(surf, (int, float)) and not isinstance(surf, bool):
            if float(y) < float(surf) - UNDERGROUND_GAP:
                return False                # 地下：追过去多半要凿
        return abs(target_y - float(y)) <= MAX_Y_GAP

    async def _release(self, why: str) -> None:
        self._aim = None
        self._aim_name = ""
        self._target = None
        await self.arbiter.release_walk(OWNER)
