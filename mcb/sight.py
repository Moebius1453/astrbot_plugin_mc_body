r"""图片仅交视觉 provider，主会话只收文字；scp 使用宿主挂载路径。"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from astrbot.api import logger

SSH_TIMEOUT = 45.0
VISION_TIMEOUT = 90.0

REMOTE_PNG = "/data/client/shots/mcb_look.png"
# scp 使用宿主挂载路径，不是容器内路径。
HOST_PNG = "/data/mc-brain/client/shots/mcb_look.png"

VISION_PROMPT = (
    "这是一张 Minecraft 游戏的第一人称截图。用中文简要描述画面里看得见的东西，"
    "只讲你确实看到的：地形、方块、生物、你正对着什么、界面和手上的物品、天气和时间。"
    "不要推测玩家意图，不要编造看不见的东西。控制在 150 字以内，直接描述，不要开场白。"
)


class SightError(Exception):
    """看一眼失败了（截图没抓到 / 传不回来 / 识图不通）。"""


class Sight:

    def __init__(
        self,
        *,
        ssh_host: str,
        container: str = "mc-brain",
        sudo_password: str = "",
        context=None,
        provider_id: str = "",
        vision_prompt: str = "",
    ) -> None:
        self.ssh_host = str(ssh_host or "").strip()
        self.container = str(container or "mc-brain").strip() or "mc-brain"
        self.sudo_password = str(sudo_password or "")
        self.context = context
        self.provider_id = str(provider_id or "").strip()
        self.vision_prompt = str(vision_prompt or "").strip() or VISION_PROMPT


    async def look(self, question: str = "") -> str:
        r"""抓图和识图两阶段均需捕获 SightError。"""
        try:
            local = await self._grab()
        except SightError as exc:
            return f"看不了：{exc}"
        try:
            return await self._describe(local, question)
        except SightError as exc:
            return f"看不了：{exc}"
        finally:
            _rm(local)


    async def _grab(self) -> Path:
        if not self.ssh_host:
            raise SightError("没配 client_ssh_host（游戏客户端在哪台机器上？）")

        # sudo 密码走 stdin，避免出现在进程命令行。
        remote = (
            f"docker exec {self.container} bash -c "
            f"'DISPLAY=:99 import -window root {REMOTE_PNG}'"
        )
        if self.sudo_password:
            remote = f"sudo -S -p '' {remote}"

        out, err, code = await _run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             self.ssh_host, remote],
            stdin_data=(self.sudo_password + "\n") if self.sudo_password else None,
        )
        if code != 0:
            raise SightError(f"截不到图（ssh/docker 返回 {code}）：{(err or out or '').strip()[:200]}")

        local = Path(tempfile.gettempdir()) / "mcb_look.png"
        out, err, code = await _run(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             f"{self.ssh_host}:{HOST_PNG}", str(local)],
        )
        if code != 0 or not local.exists():
            raise SightError(f"图传不回来（scp 返回 {code}）：{(err or out or '').strip()[:200]}")
        size = local.stat().st_size
        if size < 1000:
            raise SightError(f"传回来的图太小（{size} 字节）—— 她那边可能没在渲染")
        logger.info(f"[mc_body] 👁 截到一张图：{size} 字节")
        return local


    async def _describe(self, image: Path, question: str) -> str:
        provider = self._pick_provider()
        prompt = self.vision_prompt
        ask = " ".join(str(question or "").split())
        if ask:
            prompt += f"\n\n另外，提问的人特别想知道：{ask}"

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, image_urls=[str(image)]),
                timeout=VISION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return f"识图模型 {VISION_TIMEOUT:.0f} 秒没回 —— 图太大或者模型卡了。"
        except Exception as exc:  # noqa: BLE001
            return f"识图失败：{exc}"

        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            # 推理额度耗尽可能导致 content 为空，不一定是截图失败。
            return (
                "识图模型没给出正文。两种可能：① 图是黑的/没渲染出来；"
                "② **给这个视觉模型配的 `max_tokens` 太小**，token 全花在思考上、"
                "还没轮到正文就被截断了（推理型模型常见，把额度调大即可）。"
            )
        return "你看到的：\n" + text

    def _pick_provider(self):
        r"""按名字自动选择仅是启发式；推理型 provider 不宜设过小输出上限。"""
        if self.context is None:
            raise SightError("拿不到 AstrBot context，没法调识图模型")

        if self.provider_id:
            p = self.context.get_provider_by_id(self.provider_id)
            if p is None:
                raise SightError(f"配置里的视觉 provider「{self.provider_id}」不存在")
            return p

        try:
            allp = list(self.context.get_all_providers() or [])
        except Exception as exc:  # noqa: BLE001
            raise SightError(f"列不出 provider：{exc}") from exc

        marks = ("vl", "vision", "gpt-4o", "gemini", "claude", "qwen-vl")
        for p in allp:
            blob = _provider_blob(p).lower()
            if any(m in blob for m in marks):
                logger.info(f"[mc_body] 👁 自动挑中识图模型：{blob[:70]}")
                return p
        fallback = self.context.get_using_provider()
        if fallback is None:
            raise SightError("没有可用的 provider")
        logger.warning("[mc_body] 没找到明显带视觉的 provider，退回默认那个（可能看不了图）")
        return fallback




async def _run(argv: list[str], *, stdin_data: str | None = None,
               timeout: float = SSH_TIMEOUT) -> tuple[str, str, int]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return "", f"没装 {argv[0]}：{exc}", -2

    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_data.encode() if stdin_data else None), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        return "", f"{argv[0]} 超时（{timeout:.0f} 秒）", -1
    return (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace"), proc.returncode


def _provider_blob(p: object) -> str:
    r"""Provider 属性无稳定公开契约，逐个失败独立跳过。"""
    parts = []
    for attr in ("provider_config", "meta", "model", "name", "id", "provider_type"):
        try:
            val = getattr(p, attr, None)
        except Exception:  # noqa: BLE001
            continue
        if val:
            parts.append(str(val)[:200])
    return " ".join(parts)


def _rm(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def ssh_available() -> bool:
    return shutil.which("ssh") is not None
