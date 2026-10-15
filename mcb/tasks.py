r"""## 抄的是车万女仆（TLM）的分层，不是它的代码

TLM 的任务不能照抄代码 —— IMaidTask.createBrainTasks(EntityMaid) 要一个
EntityMaid 对象，返回的 BehaviorControl 喂给原版 Brain 系统、跑在服务端实体
tick 循环里。白是真实客户端玩家，喂不进去（这一点已扒 jar 核实）。

我们也只给 LLM 两个出口：mc_task <名字> / mc_task stop。干活全在程序侧。
（TLM 那套结果码 Already on task / missing item / unknown task_id 也照抄了语义。）

被打断 ≠ 被丢弃。 反射抢占时任务挂起，反射过去自己回来接着做
（女仆的 MaidPanicTask 也是这个语义）。用户叫停才是真停。"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from astrbot.api import logger

from .arbiter import LEVEL_TASK, TASK_TTL
from .containers import ContainerIO, find_block, wait_baritone, wear
from . import render

_TASK_NAME = "mc_body_work"


class Paused(Exception):
    """被更高优先级的事抢占了 —— 让路，等会儿从本步开头重来。"""



StepFn = Callable[["TaskContext"], Awaitable["str | None"]]


@dataclass
class Step:
    """一步。run 返回 None = 成功，返回字符串 = 失败原因（会终止任务）。"""

    name: str
    run: StepFn


@dataclass
class Task:
    id: str
    name: str
    summary: str          
    steps: list[Step] = field(default_factory=list)
    # 重点： 受理前检查（抄 Numen Task.prepare / TaskDispatch.setTask）。
    precheck: StepFn | None = None



def _rid(item: object) -> str:
    return str((item or {}).get("id") or "") if isinstance(item, dict) else ""


class TaskContext:

    def __init__(self, runner: "TaskRunner", params: dict) -> None:
        self._runner = runner
        self.bridge = runner.bridge
        self.io: ContainerIO = runner.io
        self.params = params or {}
        self.data: dict = {}          


    async def checkpoint(self) -> None:
        r"""长循环必须调用，否则无法响应抢占。"""
        await self._runner.checkpoint()

    def note(self, text: str) -> None:
        self._runner.note(text)


    async def call(self, command: str) -> dict | None:
        if self._runner.arbiter is not None and command.startswith("mcb baritone "):
            await self.checkpoint()
            await self._runner.arbiter.claim_walk(
                LEVEL_TASK, "task", command.removeprefix("mcb baritone "), "任务动作"
            )
            return {} if self._runner.arbiter.last_error is None else None
        if self._runner.arbiter is not None and command == "mcb stop":
            await self._runner.arbiter.release_walk("task")
            return {}
        return await self._runner.call(command)

    async def state(self) -> dict:
        return await self._runner.state()


    async def inventory(self) -> dict:
        return await self.call("mcb inventory") or {}

    async def count_item(self, item_id: str) -> int:
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
        r"""步骤抢占后从头重入，持物操作必须幂等。"""
        err = await wear(self.io, item_id, "hand")
        if err is not None:
            return None
        data = await self.inventory()
        for i, it in enumerate(data.get("hotbar") or []):
            if _rid(it) == item_id:
                return i
        return None

    async def wait_path(self, timeout: float = 60.0, target=None) -> str:
        r"""等待起步和停止由 containers.wait_baritone 统一确认。"""
        return await wait_baritone(
            self.bridge, timeout=timeout, target=target, checkpoint=self.checkpoint
        )

    async def goto(self, x: float, z: float, timeout: float = 60.0) -> str:
        arb = self._runner.arbiter
        if arb is not None:
            await arb.claim_walk(LEVEL_TASK, "task",
                                 f"goto {int(x)} {int(z)}", "任务路线",
                                 no_break=bool(self.params.get("no_break")))
        else:
            if self.params.get("no_break"):
                return "unconfirmed"
            await self.call(f"mcb baritone goto {int(x)} {int(z)}")
        return await self.wait_path(timeout, target=(float(x), float(z)))

    async def place_torch_here(self, item_id: str) -> bool:
        r"""以库存实际减少确认成功，不以 useOnAt 下发为准。"""
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



TASKS: dict[str, Task] = {}

TORCH_IDS = ("minecraft:torch", "minecraft:soul_torch")


def _register(task: Task) -> Task:
    TASKS[task.id] = task
    return task


async def _precheck_online(ctx: TaskContext) -> str | None:
    r"""受理前检查，失败不能取消旧任务。"""
    st = await ctx.state()
    if not st.get("online"):
        return "她的身体不在服务器上（客户端没连）—— 等上线再派活"
    return None


async def _precheck_have_torch(ctx: TaskContext) -> str | None:
    r"""缺料拒绝新任务，不中断旧任务。"""
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
    rounds = max(1, int(ctx.params.get("rounds") or 6))
    spacing = max(3, int(ctx.params.get("spacing") or 8))
    heading = str(ctx.params.get("dir") or "east").lower()
    dx, dz = {"east": (1, 0), "west": (-1, 0), "south": (0, 1),
              "north": (0, -1), "+x": (1, 0), "-x": (-1, 0)}[heading]
    tid = ctx.data.get("torch")

    progress = ctx.data.setdefault("lighting", {"attempted": 0, "target": None})
    ctx.data.setdefault("placed", 0)
    while progress["attempted"] < rounds or progress["target"] is not None:
        await ctx.checkpoint()
        if progress["target"] is not None:
            nx, nz = progress["target"]
            arrival = await ctx.goto(nx, nz, timeout=45)
            await ctx.checkpoint()
            if ctx.params.get("no_break") and arrival != "arrived":
                return "不挖地路线未确认到达，已停止照明"
            progress["target"] = None
        if progress["attempted"] >= rounds:
            break
        st = await ctx.state()
        try:
            nx = float(st["x"]) + dx * spacing
            nz = float(st["z"]) + dz * spacing
        except (KeyError, TypeError, ValueError):
            return "读不到位置，已停止照明"
        await ctx.checkpoint()
        # 放置可能已产生副作用，先占额度，抢占后不重试同一处。
        progress["attempted"] += 1
        if progress["attempted"] < rounds:
            progress["target"] = (nx, nz)
        if await ctx.place_torch_here(tid):
            ctx.data["placed"] += 1
            ctx.note(f"第 {progress['attempted']}/{rounds} 处：插上了（共 {ctx.data['placed']} 根）")
        else:
            ctx.note(f"第 {progress['attempted']}/{rounds} 处：这里插不了，跳过")

    if ctx.data["placed"] <= 0:
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



FIELD_BLOCKS = (
    "minecraft:wheat",
    "minecraft:carrots",
    "minecraft:potatoes",
    "minecraft:beetroots",
    "minecraft:melon_stem",
    "minecraft:pumpkin_stem",
    "minecraft:farmland",
)

# farm 破坏方块时 isPathing 可为 false，不能据此判收工。
FARM_POLL = 5.0
FARM_DEFAULT_MINUTES = 3.0    


async def _step_find_field(ctx: TaskContext) -> str | None:
    r"""找不到田不直接失败，角色可能已在田里。"""
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
    r"""farm 中 isPathing 可为 false，只能按时间运行。"""
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



class TaskRunner:

    def __init__(self, bridge, journal, *, io: ContainerIO | None = None) -> None:
        self.bridge = bridge
        self.journal = journal
        self.io = io or ContainerIO(bridge)

        self._task: asyncio.Task | None = None
        self._job: Task | None = None
        self._ctx: TaskContext | None = None
        self._index = 0
        self._paused = ""            
        self._outcome: dict = {}     
        # 未绑定仲裁时保留直接下发的降级路径。
        self.arbiter = None

    def bind_arbiter(self, arbiter) -> None:
        self.arbiter = arbiter


    def suspend(self, reason: str) -> None:
        if self._job is None or self._task is None or self._task.done():
            return
        if self._paused == reason:
            return
        self._paused = str(reason or "被抢占")
        logger.info(f"[mc_body] ⏸ 任务挂起（{self._paused}）")
        self.note(f"挂起（{self._paused}）")

    def resume(self) -> None:
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


    def catalog(self) -> list[dict]:
        r"""可选的任务清单（给工具描述用 —— 抄 TLM 的 getTaskIdParameterDesc）。"""
        return [{"id": t.id, "name": t.name, "summary": t.summary} for t in TASKS.values()]

    def status(self) -> dict:
        r"""当前在干嘛。这是"她知道自己正在干什么"的数据来源。"""
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


    async def start(self, task_id: str, **params) -> str:
        task = TASKS.get(str(task_id or "").strip())
        if task is None:
            known = "、".join(f"{t.id}（{t.name}）" for t in TASKS.values()) or "（一个都没有）"
            return render.fail(
                render.Kind.NOT_FOUND, f"没有「{task_id}」这个任务",
                detail=f"认得的是：{known}",
                hint="从上表里挑一个 id 原样传；**别自己编名字**",
            )

        if self.busy and self._job is not None and self._job.id == task.id:
            # 抄 TLM 的 Already on task %s
            return f"已经在做「{task.name}」了 —— 没换。"

        # 先检查新任务再停旧任务，失败不能丢掉旧活。
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
        # 取消任务必须撤销 walk 声明，防止后续误恢复。
        if self.arbiter is not None:
            await self.arbiter.release_walk("task")
        if not quiet:
            self.journal.add("task", "被叫停")
        return "已经停下了。"

    async def fail_permission(self, detail: str) -> None:
        name = self._job.name if self._job is not None else "任务"
        await self.stop(quiet=True)
        self._finish("failed", detail)
        self._outcome["kind"] = render.Kind.DENIED
        self.journal.add("task", f"「{name}」因权限拒绝终止：{detail}")


    async def checkpoint(self) -> None:
        if self._paused:
            raise Paused
        # 注意： 另一个挂起来源：walk 通道被别人（反射/用户）抢走了。
        arb = self.arbiter
        if arb is not None:
            top = arb.walk_holder()
            if top is not None and top.owner != "task":
                raise Paused
            # checkpoint 续期任务声明，任务消失后由 TTL 释放。
            # 注意： 这不是"释放"的来源，是"我还想要"的来源：
            await arb.touch_walk("task", TASK_TTL)

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
        while self._paused or (self.arbiter is not None
                               and self.arbiter.walk_holder() is not None
                               and self.arbiter.walk_holder().owner != "task"):
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
        finally:
            if self.arbiter is not None:
                await self.arbiter.release_walk("task")
