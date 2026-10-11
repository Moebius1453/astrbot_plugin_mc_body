"""空闲扫视 —— 她闲着的时候，脑袋会像人一样偶尔看来看去。

## 为什么要有它

用户 2026-10-11："玩家就应该经常动"、"脑袋会时不时看来看去"。
一尊石像和一个在场的人，差别往往就在这一点上。而且这一层不花 token ——
和闲逛一样，是身体层的事，不该拿模型调用去买。

## 它是从哪来的

这是把退役的客户端脚本里的 mcbIdleClaim（bridge/client/mcbridge.js:253）
搬到插件里重写，当年那套设计原样保留：

  · 做事的时候不抢（当年看 Baritone 忙不忙，这里看她在不在移动）
  · 每 4~7 秒抽一次签
  · 多半看向附近的人（"陪伴"最直白的一条），否则相对当前朝向随机偏一点
  · 慢慢转过去，不是硬切 —— 硬切看着像鬼畜

## 为什么走外挂

Numen 不把"转头"这个能力交给模型：全仓 113 个暴露给模型的函数里，
没有任何一个是设朝向的。它的身体只在自己做动作时瞄（挖、打、用）。
所以这条路只能从外面发原版命令。

可用的是 tp 而不是 rotate —— 这台服务器的 1.21.1 上没有 /rotate
（实测报 "Unknown or incomplete command"）。

注意： `~` 必须相对她自己。直接写 `tp Nanako ~ ~ ~ ...` 的话，RCON 的命令源
    没有位置，`~` 会解析成 0,0,0 —— 实测把她从 6.5,-6.5 挪到了 0,69,0。
    所以 `at @s` 那一段不能省。

## 它不碰 MCP

状态一律读 `mcb state`（服务端直读，零干扰），不调 Numen 的 lua ——
外脑那边有一条"一具身体一件活"的闸门，插件去调 lua 有抢走她正在跑的
任务的风险。读状态这种事没必要冒那个险。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import time

from astrbot.api import logger

# 判定"她动了"的阈值（格）。小于这个数当作没动 —— 免得站着时的微小抖动被当成在走路。
MOVED_EPSILON = 0.3


class IdleGlance:
    """空闲扫视。自己一个循环，不挂在反射那根节拍上（反射现在是关的）。"""

    def __init__(
        self,
        bridge,
        *,
        enabled: bool = True,
        companion_name: str = "Nanako",
        owner_name: str = "",
        after_seconds: float = 20.0,
        hold_min: float = 4.0,
        hold_max: float = 7.0,
        look_at_player: float = 0.6,
        near_player: float = 8.0,
        yaw_span: float = 35.0,
        pitch_span: float = 15.0,
        step_yaw: float = 8.0,
        step_pitch: float = 4.0,
        step_interval: float = 0.15,
        refresh_every: float = 1.0,
    ) -> None:
        self.bridge = bridge
        self.enabled = bool(enabled)
        self.companion = str(companion_name or "").strip()
        self.owner_name = str(owner_name or "").strip()
        self.after_seconds = max(1.0, float(after_seconds))
        self.hold_min = max(1.0, float(hold_min))
        self.hold_max = max(self.hold_min, float(hold_max))
        self.look_at_player = min(1.0, max(0.0, float(look_at_player)))
        self.near_player = max(1.0, float(near_player))
        self.yaw_span = max(1.0, float(yaw_span))
        self.pitch_span = max(0.0, float(pitch_span))
        self.step_yaw = max(0.5, float(step_yaw))
        self.step_pitch = max(0.5, float(step_pitch))
        self.step_interval = max(0.05, float(step_interval))
        self.refresh_every = max(0.2, float(refresh_every))

        self._task: asyncio.Task | None = None
        self._rand = random.Random()

        # 当前这一刻的状态（由 _read_state 每 refresh_every 秒刷新一次）
        self._online = False
        self._x = self._z = 0.0
        self._cur_yaw = self._cur_pitch = 0.0
        self._quiet_since: float | None = None

        # 正在转过去的那个目标
        self._t_yaw: float | None = None
        self._t_pitch = 0.0
        self._hold_until = 0.0

        self._last_read = 0.0
        self._last_error = ""

    # ---- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        if self._task is not None or not self.enabled:
            return
        if not self.companion:
            logger.warning("[mc_body] 空闲扫视没拿到她的名字，不启动")
            return
        self._task = asyncio.create_task(self._loop(), name="mc_body_glance")
        logger.info(
            f"[mc_body] 空闲扫视已启动：安静 {self.after_seconds:g} 秒后每 "
            f"{self.hold_min:g}~{self.hold_max:g} 秒看一次，"
            f"每 {self.step_interval:g} 秒转 {self.step_yaw:g} 度"
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        logger.info("[mc_body] 空闲扫视已停止")

    def note_activity(self) -> None:
        """有人跟她打交道了 —— 安静计时重新起算（有人说话时别乱看）。"""
        self._quiet_since = None
        self._t_yaw = None

    def render(self) -> str:
        if not self.enabled:
            return "glance=off"
        if not self._online:
            return "glance=offline"
        if self._t_yaw is not None:
            return "glance=turning"
        if self._quiet_since is None:
            return "glance=busy"
        left = max(0.0, self.after_seconds - (time.monotonic() - self._quiet_since))
        return f"glance=waiting({left:.0f}s)"

    # ---- 主循环 ---------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.step_interval)
                now = time.monotonic()
                if now - self._last_read >= self.refresh_every:
                    self._last_read = now
                    await self._read_state()
                if self.enabled:
                    await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 隧道断了不能把她这条循环打死
                self._last_error = str(exc)
                await asyncio.sleep(1.0)

    async def _read_state(self) -> None:
        """读一次她的状态。读的是服务端，不碰 MCP。"""
        try:
            reply = await self.bridge.call("mcb state")
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            self._online = False
            return
        data = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(data, dict) or not data.get("online"):
            self._online = False
            return

        self._online = True
        try:
            x = float(data.get("x"))
            z = float(data.get("z"))
        except (TypeError, ValueError):
            self._online = False
            return

        # 她在动吗 —— 用位置差判。这是替当年的"Baritone 忙不忙"。
        moved = math.hypot(x - self._x, z - self._z) > MOVED_EPSILON
        self._x, self._z = x, z

        yaw = data.get("yaw")
        pitch = data.get("pitch")
        if isinstance(yaw, (int, float)):
            self._cur_yaw = float(yaw)
        if isinstance(pitch, (int, float)):
            self._cur_pitch = float(pitch)

        if moved or not self._can_glance(data):
            # 在做事 / 环境不适合 —— 让开，安静计时重新起算
            self._quiet_since = None
            self._t_yaw = None
            return
        if self._quiet_since is None:
            self._quiet_since = time.monotonic()

    @staticmethod
    def _can_glance(data: dict) -> bool:
        if data.get("inWater") or data.get("underWater") or data.get("sleeping"):
            return False
        y = data.get("y")
        surf = data.get("surfY")
        if isinstance(y, (int, float)) and isinstance(surf, (int, float)):
            # 只在地表看 —— 悬空或地底下转脑袋没意义，而且 tp 会抹掉速度
            if float(y) < float(surf) - 1.5:
                return False
        return True

    async def _step(self) -> None:
        """这一拍该不该转、转到哪。"""
        if not self._online or self._quiet_since is None:
            return
        now = time.monotonic()

        if self._t_yaw is None:
            if now - self._quiet_since < self.after_seconds:
                return
            await self._pick_target()

        # 朝目标走一步
        nyaw, npitch = self._advance()
        await self._send_rotation(nyaw, npitch)

        if abs(_wrap(self._t_yaw - nyaw)) < 1.0 and abs(self._t_pitch - npitch) < 1.0:
            # 到了 —— 保持一会儿再抽下一次
            self._cur_yaw, self._cur_pitch = nyaw, npitch
            self._t_yaw = None
            hold = self._rand.uniform(self.hold_min, self.hold_max)
            self._quiet_since = now - self.after_seconds + hold

    def _advance(self) -> tuple[float, float]:
        """从当前朝向朝目标挪一步，返回这一步之后的朝向。"""
        yaw = self._cur_yaw + _clamp(_wrap(self._t_yaw - self._cur_yaw), self.step_yaw)
        pitch = self._cur_pitch + _clamp(self._t_pitch - self._cur_pitch, self.step_pitch)
        self._cur_yaw, self._cur_pitch = yaw, pitch
        return yaw, pitch

    async def _pick_target(self) -> None:
        """抽一次签：多半看向附近的人，否则随机偏一点。"""
        target: tuple[float, float] | None = None

        who = await self._nearby_player()
        if who is not None and self._rand.random() < self.look_at_player:
            wx, _wy, wz = who
            dx = wx - self._x
            dz = wz - self._z
            # MC 的 yaw：0 = +Z，顺时针；atan2(-dx, dz) 是标准换算
            yaw = math.degrees(math.atan2(-dx, dz)) if (dx or dz) else self._cur_yaw
            target = (yaw, 0.0)

        if target is None:
            yaw = self._cur_yaw + self._rand.uniform(-self.yaw_span, self.yaw_span)
            pitch = self._rand.uniform(-self.pitch_span, self.pitch_span)
            target = (yaw, pitch)

        self._t_yaw = _wrap(target[0])
        self._t_pitch = _clamp(target[1], self.pitch_span)

    async def _nearby_player(self) -> tuple[float, float, float] | None:
        """主人离她够近吗。够近就返回他的坐标，否则 None。

        注意： 这里只问主人一个人 —— 问"附近所有人"要另一条命令，
           而"陪伴"这件事里，她在意的本来就只有主人。
        """
        if not self.owner_name:
            return None
        try:
            reply = await self.bridge.call(f"mcb where {self.owner_name}")
        except Exception:  # noqa: BLE001 - 查不到就当不知道
            return None
        if not isinstance(reply, dict) or not reply.get("ok"):
            return None
        who = reply.get("data")
        if not isinstance(who, dict) or not who.get("online"):
            return None
        try:
            wx, wy, wz = float(who.get("x")), float(who.get("y")), float(who.get("z"))
        except (TypeError, ValueError):
            return None
        if math.hypot(wx - self._x, wz - self._z) > self.near_player:
            return None
        return (wx, wy, wz)

    async def _send_rotation(self, yaw: float, pitch: float) -> None:
        """发一条原版命令设她的朝向。

        注意： `at @s` 不能省 —— 少了它，RCON 的命令源没有位置，
            `~` 会解析成 0,0,0，她会被真的传送走（实测踩过）。
        """
        cmd = (
            f"execute as {self.companion} at @s run tp @s "
            f"~ ~ ~ {yaw:.1f} {pitch:.1f}"
        )
        try:
            await self.bridge.run(cmd)
        except Exception as exc:  # noqa: BLE001 - 发不出去就下一拍再说
            self._last_error = str(exc)


def _wrap(deg: float) -> float:
    """把角度收进 -180~180 —— yaw 在 ±180 处会绕回来，不收的话会绕地球一圈。"""
    while deg > 180.0:
        deg -= 360.0
    while deg < -180.0:
        deg += 360.0
    return deg


def _clamp(value: float, limit: float) -> float:
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value
