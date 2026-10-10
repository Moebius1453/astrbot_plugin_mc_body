"""仲裁层 —— **谁在写哪个控制通道**。

## 为什么要有它（用户 2026-10-10 提的真问题）

> "我们这些个解耦的功能配合好像不咋地啊……我想不出来代码怎么实现人类行为
> 比如说**走过去看**之类的"

调研给的诊断（`docs/14` §8.5 原话）：

> **"你们的痛点本质是**没有 memory 层**，把朝向和移动挤在同一个控制通道里。"**

具体症状：`mc_goto` 一下发，**任务还在跑，两者抢同一个 Baritone**；
反射要保命时直接 `mcb stop`，任务**不知道**自己的寻路被撤了。

## 模型（抄原版 `Brain` 的 memory 抢占 + AltoClef 的 `TaskChain`）

⭐ **抢占不是靠"打断"，而是靠抢通道**：高优先级的声明顶掉低的，
**低的挂起不取消**；高的释放后**低的自动恢复**。

```
优先级（小的赢）：   反射（保命） > 用户指令 > 当前任务
```

这跟现有的 `reflex.suspend()/resume()` 是**同一个语义** —— 只是从"反射 vs 任务"两条线
泛化成 N 条线，而且**每个通道各有一份栈**。

## 通道

| 通道 | 谁在写 | 空闲时 |
|---|---|---|
| **`walk`** | 反射逃跑 / 用户 goto / 任务 | `mcb stop` |

⚠️ **`aim` 和 `use` 通道在客户端侧（`mcbSlots`），暂时不走这里** ——
它们和 walk 是**不同的通道、天然并存**（这正是"走过去 + 看着她"能成立的原因）。
等真出现两个模块抢 aim 的需求再加，别提前造。

## 用法

**任何模块都不许再直接调 `mcb baritone` / `mcb stop`** —— 一律走这里：

    await arbiter.claim_walk(LEVEL_TASK, task_id, "goto 100 -200")
    await arbiter.release_walk(task_id)
    await arbiter.claim_walk(LEVEL_REFLEX, "reflex", None)   # None = 要求停
"""

from __future__ import annotations

from astrbot.api import logger

# 优先级 —— **小的赢**。数字留间隔，以后好插。
LEVEL_REFLEX = 0     # 保命。挨打、濒死、卡住
LEVEL_USER = 10      # 用户/LLM 的直接指令
LEVEL_TASK = 20      # 正在跑的多步任务

_LEVEL_CN = {LEVEL_REFLEX: "反射", LEVEL_USER: "用户", LEVEL_TASK: "任务"}


class _Claim:
    __slots__ = ("level", "owner", "cmd", "note")

    def __init__(self, level: int, owner: str, cmd: str | None, note: str) -> None:
        self.level = level
        self.owner = owner
        self.cmd = cmd
        self.note = note


class Arbiter:
    """控制通道的仲裁。**同一个 owner 在同一通道只保留最新那条**。"""

    def __init__(self, call) -> None:
        # `call(cmd: str) -> (data, err)` —— 就是插件那个走 RCON 的 `_call`
        self._call = call
        self._walk: list[_Claim] = []
        # 现在**实际下发**的是哪条命令（去重用 —— 别重复下同一条）
        self._walk_applied: str | None = None
        # 上一次下发的错误。⚠️ 工具层要拿它回话给白 ——
        # 仲裁把异常吞了，要是连错误都不留，白会以为"发出去了"。
        self.last_error: str | None = None

    # ---- walk 通道 ------------------------------------------------------

    async def claim_walk(self, level: int, owner: str, cmd: str | None,
                         note: str = "") -> None:
        """声明"我要走"。`cmd=None` 表示**要求停下**（不是"我不要了"，是"现在别走"）。

        ⚠️ 这条区别很重要：反射要保命时用的是 `cmd=None` ——
        它顶掉任务的 goto **并停下**，但**不删除**任务的声明，
        所以脱战之后任务的路线会**自己恢复**。
        """
        self._walk = [c for c in self._walk if c.owner != owner]
        self._walk.append(_Claim(level, owner, cmd, note))
        # 稳定排序：小的在前，同级按插入顺序（list 顺序天然是插入序）
        self._walk.sort(key=lambda c: c.level)
        await self._sync_walk("claim:" + owner)

    async def release_walk(self, owner: str) -> None:
        """撤销自己的声明。**别人（更高优先级）的声明不受影响。**"""
        before = len(self._walk)
        self._walk = [c for c in self._walk if c.owner != owner]
        if len(self._walk) == before:
            return
        await self._sync_walk("release:" + owner)

    def walk_holder(self) -> _Claim | None:
        return self._walk[0] if self._walk else None

    def walk_stack(self) -> list[_Claim]:
        return list(self._walk)

    async def _sync_walk(self, why: str) -> None:
        """把栈顶那条施加下去。**只有真的变了才下发**。"""
        top = self.walk_holder()
        want = top.cmd if top is not None else None
        if want == self._walk_applied:
            return
        self._walk_applied = want
        cmd = f"mcb baritone {want}" if want else "mcb stop"
        self.last_error = None
        try:
            _, err = await self._call(cmd)
        except Exception as exc:  # noqa: BLE001 - 下发出错不该炸掉调用方
            self.last_error = str(exc)
            logger.warning(f"[mc_body] 仲裁下发失败（{why}）：{exc}")
            return
        if err:
            self.last_error = str(err)
            logger.warning(f"[mc_body] 仲裁下发被拒（{why}）：{err}")
            return
        held = f"{_LEVEL_CN.get(top.level, top.level)}:{top.owner}" if top else "空闲"
        logger.info(f"[mc_body] walk 通道 -> {held}"
                    + (f"（{top.cmd}）" if top and top.cmd else "（停）")
                    + f"  [{why}]")

    # ---- 给状态包 / 日志看 ----------------------------------------------

    def render(self) -> str:
        """一行摘要，塞进状态数据包。**只摆事实，不做解读。**"""
        top = self.walk_holder()
        if top is None:
            return "walk=idle"
        held = f"{_LEVEL_CN.get(top.level, top.level)}:{top.owner}"
        waiting = len(self._walk) - 1
        return f"walk={held}" + (f"(+{waiting})" if waiting > 0 else "")

    def describe(self) -> str:
        """给人看的完整栈。"""
        if not self._walk:
            return "walk 通道：空闲"
        out = []
        for i, c in enumerate(self._walk):
            mark = "← 生效" if i == 0 else ""
            lv = _LEVEL_CN.get(c.level, str(c.level))
            out.append(f"  [{lv}] {c.owner}：{c.cmd or '（停）'}"
                       + (f" —— {c.note}" if c.note else "") + f" {mark}")
        return "walk 通道：\n" + "\n".join(out)

    # ---- 批量撤销（mc_stop 用）------------------------------------------

    async def clear_user_and_task(self) -> int:
        """撤掉**用户级和任务级**的全部声明。**反射级的不动** —— 保命优先。

        返回撤掉几条。`mc_stop` 应该用这个，而不是无脑 `mcb stop`：
        无脑停会把反射的逃跑路线也撤了。
        """
        keep = [c for c in self._walk if c.level < LEVEL_USER]
        dropped = len(self._walk) - len(keep)
        self._walk = keep
        if dropped:
            await self._sync_walk("clear user+task")
        return dropped
