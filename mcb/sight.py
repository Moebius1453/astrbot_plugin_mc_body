"""眼睛 —— 截图  ->  识图转述成文字  ->  给白。

## 定位（用户 2026-10-10 定）

> "方便 AI 通过图片描述查看世界；用户看来，她真的看得见。"

让白自己调，不主动轮询。 她说"看看周围"就截一张，
而不是每 5 分钟自动截 —— 那既费钱又没人看。

## 铁律（从 2026-10-08 起就没变过）

图片永远不进白的大脑。 走的是 "截图  ->  识图 API  ->  文字  ->  上下文"。
本地视觉模型已被用户否掉，别再提。

## 路怎么走

```
R820 的 mc-brain 容器（Xvfb :99）
   ↓  ssh + docker exec + import           <-  需要 sudo
宿主 /data/mc-brain/client/shots/*.png      <-  这是绑定挂载，容器里写=宿主上有
   ↓  scp                                   <-  不需要 sudo（文件是 644）
本机临时目录
   ↓  AstrBot 已配的视觉 provider（默认）
文字
```

注意： /data/mc-brain/client 是绑定挂载（docker inspect 实测）——
所以 scp 的路径是宿主的 /data/mc-brain/client/shots/，不是容器内的 /data/client/。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from astrbot.api import logger

# ssh / 截图 的最长等待
SSH_TIMEOUT = 45.0
# 识图模型的最长等待（图片 + 推理，比纯文本慢）
VISION_TIMEOUT = 90.0

# 截图落在容器的哪个文件（容器内路径）
REMOTE_PNG = "/data/client/shots/mcb_look.png"
# 同一个文件在宿主上的路径（绑定挂载的另一头）——scp 用这个
HOST_PNG = "/data/mc-brain/client/shots/mcb_look.png"

# 给识图模型的指令。要的是"事实转述"，不是创作。
#
# 注意： 这条提示词按用户偏好应由他自己定稿（见 docs/12 §4.1）——
#    现在是可跑通的草稿，重点是它明确要求"只讲看得见的、不脑补"。
VISION_PROMPT = (
    "这是一张 Minecraft 游戏的第一人称截图。用中文简要描述画面里看得见的东西，"
    "只讲你确实看到的：地形、方块、生物、你正对着什么、界面和手上的物品、天气和时间。"
    "不要推测玩家意图，不要编造看不见的东西。控制在 150 字以内，直接描述，不要开场白。"
)


class SightError(Exception):
    """看一眼失败了（截图没抓到 / 传不回来 / 识图不通）。"""


class Sight:
    """看一眼世界。用完就扔，不持有状态（除了配置）。"""

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

    # ---- 对外 -----------------------------------------------------------

    async def look(self, question: str = "") -> str:
        """看一眼 + 转述。返回给模型看的文字（永远不抛，失败也回人话）。

        注意： 两条路都要接住 SightError —— 抓图和识图各会抛一次
        （踩过：只接了抓图那条，识图的异常直接冒到工具层，她那边就是一条报错）。
        """
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

    # ---- 1 抓图 ---------------------------------------------------------

    async def _grab(self) -> Path:
        if not self.ssh_host:
            raise SightError("没配 client_ssh_host（游戏客户端在哪台机器上？）")

        # 注意： sudo 密码走 stdin 不走命令行 —— 命令行会被 ps 看见
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

    # ---- 2 转述 ---------------------------------------------------------

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
            # 注意： 别只说"图可能是黑的" —— 实测最像"模型坏了"的原因其实是 token 被掐：
            #    推理型模型（如 deepseek-v4-flash-vision-exp）把额度全花在 reasoning 上，
            #    content 就是空的。把这条说出来，省得下次又去查半天模型。
            return (
                "识图模型没给出正文。两种可能：① 图是黑的/没渲染出来；"
                "② **给这个视觉模型配的 `max_tokens` 太小**，token 全花在思考上、"
                "还没轮到正文就被截断了（推理型模型常见，把额度调大即可）。"
            )
        return "你看到的：\n" + text

    def _pick_provider(self):
        """挑一个有视觉能力的 provider。

        注意： 默认不用 get_using_provider() —— 那是她聊天用的模型（deepseek 文本版），
        多半看不了图。没配 vision_provider_id 时自己找一个看着像视觉模型的。

        注意：注意： 但更推荐在配置里钉死 vision_provider_id —— 自动挑是按名字猜，
        猜错了不报错、只是每次都回一句"没给出内容"。2026-10-10 实测四个候选：

        | provider | 结果 |
        |---|---|
        | deepseek/deepseek-v4-flash-vision-exp | 已完成： 选它（配置里已钉）。9.8s，描述准，自带 prompt 缓存 |
        | siliconflow/Qwen/Qwen3-VL-8B-Thinking | 已完成： 3~4s，快而稳，但细节少 |
        | siliconflow/Qwen/Qwen3-VL-32B-Instruct | 注意： 6.8s / 26.1s，啰嗦，会编（把玩家说成"一只羊"） |
        | moyuu/gemini-3.8-flash | 注意： 描述最细，但中转站不稳（SSL 时好时坏，实测 3 次挂 2 次） |

        注意：注意： deepseek-v4-flash-vision-exp 是推理型模型 —— 别给它设小 max_tokens！
        实测 max_tokens=400 时 400 个 token 全花在 reasoning_content 上、content 是空的
        （finish_reason: 'length'），看起来就像"模型坏了"。
        AstrBot 默认不传 max_tokens（它那个 8192 兜底只对 nvidia 的 minimax-m3 生效），
        所以现状是对的 —— 但别在面板里给这个 provider 配一个小的 max_tokens。
        """
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

        # 名字里带这些词的八成是视觉模型（用户配的 Qwen3-VL / *-vision-* / gemini）
        marks = ("vl", "vision", "gpt-4o", "gemini", "claude", "qwen-vl")
        for p in allp:
            blob = _provider_blob(p).lower()
            if any(m in blob for m in marks):
                logger.info(f"[mc_body] 👁 自动挑中识图模型：{blob[:70]}")
                return p
        # 实在没有就退回默认 —— 说不定那个恰好也能看图
        fallback = self.context.get_using_provider()
        if fallback is None:
            raise SightError("没有可用的 provider")
        logger.warning("[mc_body] 没找到明显带视觉的 provider，退回默认那个（可能看不了图）")
        return fallback


# ---- 小工具 ---------------------------------------------------------------


async def _run(argv: list[str], *, stdin_data: str | None = None,
               timeout: float = SSH_TIMEOUT) -> tuple[str, str, int]:
    """跑一条外部命令。返回 (stdout, stderr, 返回码)；超时返回码给 -1。"""
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
    """把一个 provider 拼成一段可搜索的文本，用来猜它是不是视觉模型。

    注意： 属性名是猜的 —— AstrBot 的 Provider 没稳定公开这些。
    所以每个都单独 try，拿不到就跳过；全拿不到就返回空串（那就退回默认 provider）。
    """
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
    """本机有没有 ssh（没有的话截图这条路直接不通，给个明白话）。"""
    return shutil.which("ssh") is not None
