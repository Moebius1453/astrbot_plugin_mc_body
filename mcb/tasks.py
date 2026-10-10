"""任务层 —— 把多步行为串成一个目标。

## 为什么要有这一层

单步能力（走、挖、合、点格子）都有了，缺的是**把多步串成一个目标**：
「挖 5 个铁矿 → 烧成锭 → 做把镐」。

## 两类任务，一个外壳

用户 2026-10-09 的判断：**不该拿递归规划器去做种田**。对 —— 要分两类：

  · **规划型**：有形式化的图（配方 DAG）→ `craft.py` 的递归规划器
  · **流程型**：固定步骤，没有图（种田 / 插火把 / 睡觉）→ **本文件的步骤表**

两类**共用**这一层外壳：执行器 + 中断 + 感知。**加新任务 = 加一张表**，
跟 `containers.py` 的「加新容器 = 加一行表」一个思路。

## 抄的是车万女仆（TLM）的**分层**，不是它的代码

TLM 的任务**不能照抄代码** —— `IMaidTask.createBrainTasks(EntityMaid)` 要一个
`EntityMaid` 对象，返回的 `BehaviorControl` 喂给原版 Brain 系统、跑在**服务端实体
tick 循环**里。白是**真实客户端玩家**，喂不进去（这一点已扒 jar 核实）。

**能抄、也值得抄的是它的架构**：

    IMaidTask.getMaidActionSummary()       ← 让 AI 知道自己在干什么
    IMaidTask.enablePanic / enableEating   ← 每个任务的「允不允许被打断」
    MaidContexts$CurrentTaskContext        ← 当前任务作为**上下文**注入 LLM
    SwitchWorkTaskTool                     ← LLM 的**唯一出口**：切模式

**我们也只给 LLM 两个出口**：`mc_task <名字>` / `mc_task stop`。干活全在程序侧。
（TLM 那套结果码 `Already on task` / `missing item` / `unknown task_id` 也照抄了语义。）

## 中断是**一等公民**，不是异常

三级优先级（高 → 低）：

    反射（保命）  >  用户指令  >  当前任务

**被打断 ≠ 被丢弃。** 反射抢占时任务**挂起**，反射过去**自己回来接着做**
（女仆的 `MaidPanicTask` 也是这个语义）。用户叫停才是真停。

## ⚠️ 步骤必须**可重入**

抢占发生在步骤中途时，恢复后**从这一步的开头重来**（步骤是协程，接不到中间）。
所以每个步骤都要能重复执行而不出错：
「走到 X」重复下发无害 ✅；「拿起一个面包」就该写成「**确保手上有**面包」❌→✅。

## ⚠️ 抢占的一个已知缺口

反射的 `mcb stop` 会让「正在走路」的步骤看到「没在移动」从而**误判成走到了**。
所以步骤**别只信 `ctx.wait_path()` 的返回值**，该用坐标/数量复核就用。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from astrbot.api import logger

from .arbiter import LEVEL_TASK
from .containers import ContainerIO, find_block, wait_baritone, wear
from . import render

# 任务协程的任务名 —— 用来识别并掐掉重载留下的孤儿任务（同 reflex.py）
_TASK_NAME = "mc_body_work"


class Paused(Exception):
    """被更高优先级的事抢占了 —— 让路，等会儿从**本步开头**重来。"""


# ===== 步骤 =================================================================

StepFn = Callable[["TaskContext"], Awaitable["str | None"]]


@dataclass
class Step:
    """一步。`run` 返回 None = 成功，返回字符串 = 失败原因（会终止任务）。"""

    name: str
    run: StepFn


@dataclass
class Task:
    id: str
    name: str
    summary: str          # 给模型看的一句话 —— 描述写得含糊模型就会用错
    steps: list[Step] = field(default_factory=list)
    # ⭐ **受理前检查**（抄 Numen `Task.prepare` / `TaskDispatch.setTask`）。
    #
    #    契约和 `Step.run` 一样：返回 `None` = 可以开工；返回字符串 = 失败原因。
    #    ⚠️ **它在 `create_task` 之前跑，判不过就"不换进槽"** ——
    #    手上的旧活**一点不受影响**。
    #
    #    原来没有这一层，`TaskRunner.start` 是"**先 stop() 掐掉旧活、再看新活行不行**" ——
    #    新活一失败她就**闲着**，而旧活已经没了。
    precheck: StepFn | None = None


# ===== 上下文 ===============================================================

def _rid(item: object) -> str:
    """取注册名。**绝不回退到显示名** —— 显示名跟语言走，匹配必错（docs/04 坑 10o）。"""
    return str((item or {}).get("id") or "") if isinstance(item, dict) else ""


class TaskContext:
    """步骤能用的一切：读状态、发命令、等、被抢占。"""

    def __init__(self, runner: "TaskRunner", params: dict) -> None:
        self._runner = runner
        self.bridge = runner.bridge
        self.io: ContainerIO = runner.io
        self.params = params or {}
        self.data: dict = {}          # 步骤之间传数据（"刚才那根火把在哪一格"）

    # ---- 抢占 -----------------------------------------------------------

    async def checkpoint(self) -> None:
        """被抢占就在这里抛 `Paused`。**长循环里必须反复调它**，否则抢占不生效。"""
        await self._runner.checkpoint()

    def note(self, text: str) -> None:
        """往状态日志里记一条（白自己看得见）。"""
        self._runner.note(text)

    # ---- 桥 -------------------------------------------------------------

    async def call(self, command: str) -> dict | None:
        return await self._runner.call(command)

    async def state(self) -> dict:
        return await self._runner.state()

    # ---- 常用动作 -------------------------------------------------------

    async def inventory(self) -> dict:
        return await self.call("mcb inventory") or {}

    async def count_item(self, item_id: str) -> int:
        """身上（含快捷栏 / 背包 / 副手）有几个这种物品。"""
        data = await self.inventory()
        total = 0
        for key in ("hotbar", "main"):
            for it in (data.get(key) or []):
                if _rid(it) == item_id:
                    total += int(it.get("c") or 0) if isinstance(it, dict) else 0
        off = data.get("offhand")
        if _rid(off) == item_id and isinstance(off, dict):
            total += int(off.get("c") or 0)
        return total

    async def ensure_held(self, item_id: str) -> int | None:
        """**确保手上拿着**这种物品，返回快捷栏格号；做不到返回 None。

        ⚠️ 写成「确保」而不是「拿起一个」是因为**步骤必须可重入**：被抢占后
        这一步会从头再跑一遍，重复执行不能出岔子。

        真正的活交给 `containers.wear`（手/副手/护甲**共用一份实现**）——
        别在这儿抄第二遍。
        """
        err = await wear(self.io, item_id, "hand")
        if err is not None:
            return None
        data = await self.inventory()
        for i, it in enumerate(data.get("hotbar") or []):
            if _rid(it) == item_id:
                return i
        return None

    async def wait_path(self, timeout: float = 60.0, target=None) -> str:
        """等 Baritone 走完。返回 `"arrived"` / `"timeout"`。

        ⚠️⚠️ **别天真地写成"不在走 = 到了"**（2026-10-09 实战踩到）：
        刚 `goto` 完时 Baritone 的 `isPathing()` **还是 false** —— 那种写法会
        **立刻判定到达**。实测从 (-10,88) 去 (-45,66) 只花 9 秒就报"到了"，
        而人一步没动，后面全在**错误的位置**上干。

        真正的逻辑在 **`containers.wait_baritone`**（`open_block` 也用它）——
        那里处理了"先看起步再看停下"和去抖。别在这儿抄第二遍。
        """
        return await wait_baritone(
            self.bridge, timeout=timeout, target=target, checkpoint=self.checkpoint
        )

    async def goto(self, x: float, z: float, timeout: float = 60.0) -> str:
        # ⚠️ **走仲裁层，别直接 `mcb baritone`** —— 反射要保命时优先级比任务高，
        #    由仲裁决定谁生效。**顶掉时任务被挂起、声明不删**，
        #    所以脱战之后这条路线会**自己恢复**（这正是"挂起不是取消"的落点）。
        arb = self._runner.arbiter
        if arb is not None:
            await arb.claim_walk(LEVEL_TASK, "task",
                                 f"goto {int(x)} {int(z)}", "任务路线")
        else:
            await self.call(f"mcb baritone goto {int(x)} {int(z)}")
        return await self.wait_path(timeout, target=(float(x), float(z)))

    async def place_torch_here(self, item_id: str) -> bool:
        """在**她旁边**的地面上插一根火把。插上了返回 True。

        ⚠️ **成功判据是"身上少了一个"，不是"命令发出去了"。**
        `useOnAt` 是发出去就算成功，服务端可能因为够不着 / 那格不是实心 /
        已经有东西而拒掉 —— 唯一可靠的反馈是背包里的数量真的变了。

        `useOnAt` 固定用**上表面**（见客户端脚本），所以对着 `(x, y-1, z)` 右键，
        火把就落在 `(x, y, z)`。依次试四个相邻方向，第一个成了就收工。
        """
        st = await self.state()
        if not st.get("online"):
            return False
        try:
            bx, by, bz = math.floor(float(st["x"])), math.floor(float(st["y"])), math.floor(float(st["z"]))
        except (KeyError, TypeError, ValueError):
            return False

        before = await self.count_item(item_id)
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            await self.checkpoint()
            await self.call(f"mcb useOnAt {bx + dx} {by - 1} {bz + dz}")
            await asyncio.sleep(0.6)
            if await self.count_item(item_id) < before:
                return True
        return False


# ===== 任务表 ===============================================================

TASKS: dict[str, Task] = {}

# 认得的火把。灵魂火把也算 —— 照明效果一样。
TORCH_IDS = ("minecraft:torch", "minecraft:soul_torch")


def _register(task: Task) -> Task:
    TASKS[task.id] = task
    return task


async def _precheck_online(ctx: TaskContext) -> str | None:
    """所有任务共用的一条：**身体不在，什么任务都开不了工。**

    ⚠️ 放"受理前"而不是第 0 步 —— 她不在线时**旧任务不该被掐掉**。
    """
    st = await ctx.state()
    if not st.get("online"):
        return "她的身体不在服务器上（客户端没连）—— 等上线再派活"
    return None


async def _precheck_have_torch(ctx: TaskContext) -> str | None:
    """受理前检查 —— **缺东西要立刻说，且不打断手上的活。**"""
    for tid in TORCH_IDS:
        if await ctx.count_item(tid) > 0:
            ctx.data["torch"] = tid
            ctx.note(f"身上有 {tid}")
            return None
    return "身上没有火把（torch / soul_torch 都没有）—— 先给我火把"


async def _precheck_torch(ctx: TaskContext) -> str | None:
    return await _precheck_online(ctx) or await _precheck_have_torch(ctx)


async def _step_hold_torch(ctx: TaskContext) -> str | None:
    tid = ctx.data.get("torch")
    if not tid:
        return "不知道要用哪种火把（受理前的检查没跑成）"
    if await ctx.ensure_held(tid) is None:
        return f"火把（{tid}）不在快捷栏，也没能挪过去"
    return None


async def _step_light_along(ctx: TaskContext) -> str | None:
    """沿 **+X 方向**走，路边隔一段插一根火把。

    ⚠️ 方向写死 +X 是刻意的 v1 取舍 —— 挑方向要读朝向，而朝向那套还没接。
    想改方向就传 `dir` 参数（目前只认 x 轴正负）。
    """
    rounds = max(1, int(ctx.params.get("rounds") or 6))
    spacing = max(3, int(ctx.params.get("spacing") or 8))
    sign = -1 if str(ctx.params.get("dir") or "+x").lower() in ("-x", "west") else 1
    tid = ctx.data.get("torch")

    placed = 0
    for i in range(rounds):
        await ctx.checkpoint()
        if await ctx.place_torch_here(tid):
            placed += 1
            ctx.note(f"第 {i + 1}/{rounds} 处：插上了（共 {placed} 根）")
        else:
            ctx.note(f"第 {i + 1}/{rounds} 处：这里插不了，跳过")
        if i + 1 >= rounds:
            break
        st = await ctx.state()
        try:
            nx = float(st["x"]) + sign * spacing
            nz = float(st["z"])
        except (KeyError, TypeError, ValueError):
            break
        await ctx.goto(nx, nz, timeout=45)

    ctx.data["placed"] = placed
    if placed <= 0:
        return "一根都没插上（可能脚下不是实地，或者火把被用光了）"
    return None


_register(Task(
    id="torch",
    name="沿途照明",
    summary="沿一个方向走，路边隔一段插一根火把。需要身上有火把。",
    precheck=_precheck_torch,
    steps=[
        Step("拿到手上", _step_hold_torch),
        Step("沿路插火把", _step_light_along),
    ],
))


# ---- 种田 ------------------------------------------------------------------
#
# ⚠️ **这一条不靠我们实现什么，靠 Baritone 的 `farm`** ——
#    它会自己收割成熟的作物、把种子种回去、必要时用锄头翻地。
#    我们只负责"走到田边 + 开干 + 判断收工"。（这正是 docs/11 说的"胶水要薄"。）

# 能拿来定位"哪有田"的方块。⚠️ 顺序 = 优先级：先找熟透的作物，再找耕地。
FIELD_BLOCKS = (
    "minecraft:wheat",
    "minecraft:carrots",
    "minecraft:potatoes",
    "minecraft:beetroots",
    "minecraft:melon_stem",
    "minecraft:pumpkin_stem",
    "minecraft:farmland",
)

# ⚠️ **没有"收工判据"这个东西** —— 收作物是破坏方块，期间 `isPathing()` 常常为 false，
#    拿"没在走"当收工信号会**刚开干就收工**（踩过）。所以只能按时间跑。
FARM_POLL = 5.0
FARM_DEFAULT_MINUTES = 3.0    # 默认跑多久


async def _step_find_field(ctx: TaskContext) -> str | None:
    """走到田边。**找不到不算失败** —— 她可能本来就站在田里。

    两种入口：
      · 给了坐标（`mc_task farm at="-50 100"`）→ 先去那儿，再在周围找作物
      · 没给 → 就地找（服务端扫描上限 16 格，**田远了就看不到**）
    """
    tx, tz = ctx.params.get("x"), ctx.params.get("z")
    if isinstance(tx, (int, float)) and isinstance(tz, (int, float)):
        ctx.note(f"先走到指定位置 {int(tx)},{int(tz)}")
        await ctx.goto(tx, tz, timeout=120)
        await asyncio.sleep(1.0)

    for bid in FIELD_BLOCKS:
        pos = await find_block(ctx.bridge, bid, radius=16)
        if pos is not None:
            ctx.note(f"附近看到 {bid.split(':')[-1]} @ {pos[0]},{pos[1]},{pos[2]}")
            ctx.data["field"] = pos
            await ctx.goto(pos[0], pos[2], timeout=45)
            return None
    ctx.note("附近 16 格内没扫到田地 —— 就地开干（Baritone 管她周围）")
    return None


async def _step_check_farm_tools(ctx: TaskContext) -> str | None:
    """软检查：报告身上有没有锄头/种子。**缺了也不失败** —— 只收割也能干活。"""
    data = await ctx.inventory()
    ids = {
        str(it.get("id")) for it in (data.get("hotbar") or []) + (data.get("main") or [])
        if isinstance(it, dict)
    }
    hoes = sorted(i for i in ids if i.endswith("_hoe"))
    seeds = sorted(
        i for i in ids
        if "seed" in i or i in ("minecraft:wheat", "minecraft:carrots",
                                "minecraft:potatoes", "minecraft:beetroot")
    )
    ctx.note(f"锄头：{'、'.join(h.split(':')[-1] for h in hoes) or '没有'}；"
             f"种子/作物：{'、'.join(s.split(':')[-1] for s in seeds) or '没有'}")
    if not hoes and not seeds:
        ctx.note("既没锄头也没种子 —— 只能收，不能种")
    return None


async def _step_run_farm(ctx: TaskContext) -> str | None:
    """开干 + **跑满时间** + 报收成。

    ⚠️⚠️ **不能靠"Baritone 没在走"判收工**（2026-10-09 踩到）：
    收作物靠**破坏方块**，那期间 `isPathing()` 常常是 false ——
    第一版用"安静 15 秒"当收工信号，结果**刚开干 15 秒就收工了**。

    **只好按时间跑。** 想知道"真的干完没"，得让客户端上报
    **当前跑的是哪个 Baritone 进程**（`getCurrentProcess()`）——
    那要改客户端脚本 + 重启客户端，先记在 docs/12。
    """
    minutes = float(ctx.params.get("minutes") or FARM_DEFAULT_MINUTES)
    limit = max(30.0, minutes * 60)
    before = await _inventory_counts(ctx)

    await ctx.call("mcb baritone farm")
    ctx.note(f"开始 farm（跑满 {limit / 60:g} 分钟）")

    waited = 0.0
    while waited < limit:
        await ctx.checkpoint()
        await asyncio.sleep(FARM_POLL)
        waited += FARM_POLL
        if int(waited) % 60 == 0:
            ctx.note(f"farm 进行中 {int(waited)}s/{int(limit)}s")

    await ctx.call("mcb stop")
    await asyncio.sleep(0.5)

    gained, lost = _diff_counts(before, await _inventory_counts(ctx))
    ctx.data["gained"] = gained
    ctx.data["lost"] = lost
    if gained:
        ctx.note("收成：" + "、".join(f"{k.split(':')[-1]}×{v}" for k, v in gained.items()))
    if lost:
        ctx.note("用掉/种下：" + "、".join(f"{k.split(':')[-1]}×{v}" for k, v in lost.items()))
    if not gained and not lost:
        ctx.note("背包没变化 —— 这块地可能没熟，或者本来就没种东西")
    return None


async def _inventory_counts(ctx: TaskContext) -> dict[str, int]:
    data = await ctx.inventory()
    out: dict[str, int] = {}
    for key in ("hotbar", "main"):
        for it in (data.get(key) or []):
            if isinstance(it, dict) and it.get("id"):
                i = str(it["id"])
                out[i] = out.get(i, 0) + int(it.get("c") or 0)
    off = data.get("offhand")
    if isinstance(off, dict) and off.get("id"):
        out[str(off["id"])] = out.get(str(off["id"]), 0) + int(off.get("c") or 0)
    return out


def _diff_counts(before: dict[str, int], after: dict[str, int]) -> tuple[dict, dict]:
    """比对前后背包。返回 `(多了什么, 少了什么)` —— 这就是"收成"的实据。

    ⚠️ `lost` 那边**必须用 `v` 减去 `after`**。第一版写成了
    `before.get(k,0) - v`（也就是自己减自己），**恒等于 0** ——
    日志里就会看到「用掉 wheat_seeds×0」这种见了鬼的行。
    """
    gained = {k: v - before.get(k, 0) for k, v in after.items() if v > before.get(k, 0)}
    lost = {k: v - after.get(k, 0) for k, v in before.items() if v > after.get(k, 0)}
    return gained, lost


_register(Task(
    id="farm",
    name="种田",
    summary="找块田走过去，收割+补种（靠 Baritone 的 farm）。有锄头/种子更好，没有也能只收。",
    precheck=_precheck_online,
    steps=[
        Step("找田", _step_find_field),
        Step("看看有什么工具", _step_check_farm_tools),
        Step("开干并等收工", _step_run_farm),
    ],
))


# ===== 执行器 ===============================================================

class TaskRunner:
    """跑一个任务，管中断，往日志里记账。**一个实例只跑一个任务。**"""

    def __init__(self, bridge, journal, *, io: ContainerIO | None = None) -> None:
        self.bridge = bridge
        self.journal = journal
        self.io = io or ContainerIO(bridge)

        self._task: asyncio.Task | None = None
        self._job: Task | None = None
        self._ctx: TaskContext | None = None
        self._index = 0
        self._paused = ""            # 非空 = 被抢占的理由
        self._outcome: dict = {}     # 上一次的结局
        # 仲裁层（见 mcb/arbiter.py）。**没绑也能跑** —— 那就退回直接下发，
        # 免得单测/降级路径被这条依赖卡住。
        self.arbiter = None

    def bind_arbiter(self, arbiter) -> None:
        """把仲裁层接上。`main.py` 在 `__init__` 里调。"""
        self.arbiter = arbiter

    # ---- 抢占（反射层调）------------------------------------------------

    def suspend(self, reason: str) -> None:
        """挂起当前任务。**反射保命时调这个** —— 不是停，是让路。"""
        if self._job is None or self._task is None or self._task.done():
            return
        if self._paused == reason:
            return
        self._paused = str(reason or "被抢占")
        logger.info(f"[mc_body] ⏸ 任务挂起（{self._paused}）")
        self.note(f"挂起（{self._paused}）")

    def resume(self) -> None:
        """接着做。已经不在挂起状态就什么都不做。"""
        if not self._paused:
            return
        logger.info(f"[mc_body] ▶ 任务恢复（之前因为 {self._paused}）")
        self.note("恢复")
        self._paused = ""

    @property
    def paused(self) -> str:
        return self._paused

    @property
    def busy(self) -> bool:
        return self._job is not None and self._task is not None and not self._task.done()

    # ---- 给工具和感知用的 -----------------------------------------------

    def catalog(self) -> list[dict]:
        """可选的任务清单（给工具描述用 —— 抄 TLM 的 `getTaskIdParameterDesc`）。"""
        return [{"id": t.id, "name": t.name, "summary": t.summary} for t in TASKS.values()]

    def status(self) -> dict:
        """当前在干嘛。**这是"她知道自己正在干什么"的数据来源。**"""
        if self.busy and self._job is not None:
            total = len(self._job.steps)
            idx = max(1, min(self._index, total))
            return {
                "running": True,
                "id": self._job.id,
                "name": self._job.name,
                "step": idx,
                "total": total,
                "doing": self._job.steps[idx - 1].name,
                "paused": self._paused or None,
            }
        return {"running": False, "last": self._outcome}

    # ---- 开关 -----------------------------------------------------------

    async def start(self, task_id: str, **params) -> str:
        """切到某个任务。返回一句人话（工具直接回给模型）。"""
        task = TASKS.get(str(task_id or "").strip())
        if task is None:
            known = "、".join(f"{t.id}（{t.name}）" for t in TASKS.values()) or "（一个都没有）"
            return render.fail(
                render.Kind.NOT_FOUND, f"没有「{task_id}」这个任务",
                detail=f"认得的是：{known}",
                hint="从上表里挑一个 id 原样传；**别自己编名字**",
            )

        if self.busy and self._job is not None and self._job.id == task.id:
            # 抄 TLM 的 `Already on task %s`
            return f"已经在做「{task.name}」了 —— 没换。"

        # ⭐ **受理前先判，判不过不换进槽**（`docs\25` §三 1.3）。
        #    ⚠️ 顺序要紧：**先判、后停旧活**。反过来的话，新活一失败她就两手空空。
        ctx = TaskContext(self, params)
        if task.precheck is not None:
            try:
                why = await task.precheck(ctx)
            except Exception as exc:  # noqa: BLE001 - 检查自己炸了也要说人话
                logger.exception("[mc_body] 任务受理前检查炸了")
                why = f"受理前检查出错：{exc}"
            if why:
                still = f"（「{self._job.name}」还在做，没被打断）" if self.busy else ""
                return render.fail(
                    render.Kind.NO_MATERIAL, f"「{task.name}」现在还开不了工{still}",
                    detail=str(why),
                    hint="照上面的说法凑齐，再调一次 `mc_task`；**别换个任务名硬试**",
                )

        if self.busy and self._job is not None:
            await self.stop(quiet=True)

        self._job = task
        self._index = 0
        self._paused = ""
        self._outcome = {}
        self._ctx = ctx
        self.journal.add("task", f"开始「{task.name}」")

        self._task = asyncio.create_task(self._run(task), name=_TASK_NAME)
        return f"「{task.name}」开始了。想知道进度用 mc_state 看「正在做的事」。"

    async def stop(self, *, quiet: bool = False) -> str:
        """**真停** —— 不是挂起，是丢弃。"""
        task = self._task
        self._task = None
        self._job = None
        self._index = 0
        self._paused = ""
        if task is None or task.done():
            return "现在没有在跑的任务。"
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        # ⚠️ **真停 = 撤掉自己在 walk 通道的声明** —— 悬着的话，
        #    下次反射释放时会把这条**早就不要了的**路线又恢复出来。
        if self.arbiter is not None:
            await self.arbiter.release_walk("task")
        if not quiet:
            self.journal.add("task", "被叫停")
        return "已经停下了。"

    # ---- 内部 -----------------------------------------------------------

    async def checkpoint(self) -> None:
        if self._paused:
            raise Paused
        # ⚠️⚠️ **另一个挂起来源：walk 通道被别人（反射/用户）抢走了。**
        #    这时"没在移动"是**别人造成的**，不是"走到了" ——
        #    不判的话 `wait_path` 会把"反射保命时把她叫停"**误判成到达**。
        #    （这条坑一直记在本文件顶部第 51 行，从来没有真正的解法。）
        arb = self.arbiter
        if arb is not None:
            top = arb.walk_holder()
            if top is not None and top.owner != "task":
                raise Paused

    def note(self, text: str) -> None:
        self.journal.add("task", text)

    async def call(self, command: str) -> dict | None:
        try:
            reply = await self.bridge.call(command)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 任务命令 {command!r} 失败：{exc}")
            return None
        if not reply.get("ok"):
            logger.warning(f"[mc_body] 任务命令 {command!r} 被拒：{reply.get('error')}")
            return None
        data = reply.get("data")
        return data if isinstance(data, dict) else {}

    async def state(self) -> dict:
        return await self.call("mcb state") or {}

    def _finish(self, state: str, detail: str) -> None:
        self._outcome = {"state": state, "detail": detail, "at": time.time()}
        self._job = None
        self._paused = ""

    async def _gate(self) -> None:
        """挂起时卡在这。**每个步骤之前都过一遍。**"""
        while self._paused:
            await asyncio.sleep(0.5)

    async def _run(self, task: Task) -> None:
        ctx = self._ctx
        total = len(task.steps)
        try:
            for i, step in enumerate(task.steps, 1):
                while True:
                    await self._gate()
                    self._index = i
                    self.note(f"{task.name} · 第 {i}/{total} 步：{step.name}")
                    try:
                        err = await step.run(ctx)
                    except Paused:
                        # 被抢占了。**不是失败** —— 等恢复了从这一步重来。
                        self.note(f"被抢占，稍后从「{step.name}」重来")
                        continue
                    break
                if err:
                    self._finish("failed", err)
                    self.journal.add("task", f"「{task.name}」没做成：{err}")
                    return
            done = f"「{task.name}」干完了"
            placed = (ctx.data or {}).get("placed")
            if isinstance(placed, int):
                done += f"（插了 {placed} 根火把）"
            self._finish("done", done)
            self.journal.add("task", done)
        except asyncio.CancelledError:
            self._finish("stopped", "被叫停")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[mc_body] 任务炸了")
            self._finish("failed", f"出了意外：{exc}")
            self.journal.add("error", f"任务出错：{exc}")
