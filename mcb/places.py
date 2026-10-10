"""地点记忆 —— 「哪儿有什么、我在那儿干过什么」。

## 为什么要有它

**用户 2026-10-10 定的**：她得有个**基于坐标和描述、能滚动更新**的记忆，
"当日志写：坐标、有什么、做了什么"。

不然她每次都得重新找路，而**服务端扫描只有 16 格** —— 出了这个圈她就是瞎的。
（实测：她的田在 40 格开外，`mcb scan` 根本看不见，只能靠人报坐标。）

## 和状态日志（`journal.py`）的区别

| | journal | places |
|---|---|---|
| 记什么 | **发生过什么事**（流水） | **哪儿是什么**（地图） |
| 会不会丢 | 环形缓冲，**重启就清** | **写磁盘，重启还在** |
| 谁写 | 程序（任务/反射自动记） | 白自己（她走过路过记下来） |

**两张表都要有** —— 一个是"我刚干了什么"，一个是"我知道些什么"。

## 形态

一个地点 = `名字 → {x, y, z, dim, what, note, seen}`。
**同名覆盖 = 滚动更新**（她再去一次，坐标和描述就刷新了）。

⚠️ **不存大件**：只存"这是什么、在哪"，不存箱子内容那种会变的东西 ——
那该现场看（`mc_menu`）。存了就会骗人。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from astrbot.api import logger

# 最多记多少个地方。满了踢**最久没去过**的（不是最早建的）—— 常用的地方该留下
CAPACITY = 200

# 名字和描述的长度上限（防她自己写长文进去，每次调工具都要付 token）
MAX_NAME = 24
MAX_WHAT = 40
MAX_NOTE = 80

# 指纹（那个坐标上的方块 id）长度上限 —— 正常就 "minecraft:crafting_table" 这么长
MAX_FP = 64


class PlaceBook:
    """地点簿。**同名覆盖**，**写磁盘**。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._places: dict[str, dict] = {}
        self.load()

    # ---- 磁盘 -----------------------------------------------------------

    def load(self) -> None:
        """读盘。**读不出来就当空的** —— 记忆坏了不该让插件起不来。"""
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._places = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
                logger.info(f"[mc_body] 地点簿读入 {len(self._places)} 个地方")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 地点簿读不出来（当空的用）：{exc}")
            self._places = {}

    def save(self) -> None:
        """写盘。失败只警告 —— **绝不能因为存不下就把插件搞崩**。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._places, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 地点簿写不进去：{exc}")

    # ---- 写 -------------------------------------------------------------

    def remember(self, name: str, x: float, y: float, z: float, *,
                 dim: str = "", what: str = "", note: str = "",
                 fp: str = "") -> str:
        """记一个地方。**同名覆盖**（滚动更新）。返回一句人话。

        `fp` = **指纹**：记的这一刻，那个坐标上是什么方块（方块 id）。
        下次再查到它就能知道**那地方变了没**（`check()`）。
        ⚠️ 抄的是 mcpfabric `memory.ts` 的 `fingerprint` + `valid/changed/gone` 自检。
        """
        key = _clean(name, MAX_NAME)
        if not key:
            return "得给这个地方起个名字。"

        old = self._places.get(key)
        entry = {
            "x": round(float(x), 1),
            "y": round(float(y), 1),
            "z": round(float(z), 1),
            "dim": _clean(dim, 32) or (old or {}).get("dim", ""),
            # ⚠️ 新描述为空时**保留旧的** —— 她只想刷新坐标，不该把"这是什么"抹掉
            "what": _clean(what, MAX_WHAT) or (old or {}).get("what", ""),
            "note": _clean(note, MAX_NOTE) or (old or {}).get("note", ""),
            # ⚠️ 指纹**只在明确给了新的才覆盖** —— 读不到方块时别把旧指纹抹成空，
            #    那样会丢掉"这地方原来是什么"这唯一一条线索
            "fp": _clean(fp, MAX_FP) or (old or {}).get("fp", ""),
            "seen": time.time(),
        }
        self._places[key] = entry
        self._evict()
        self.save()

        verb = "更新了" if old else "记住了"
        return (f"{verb}「{key}」：({entry['x']:g}, {entry['y']:g}, {entry['z']:g})"
                + (f" —— {entry['what']}" if entry["what"] else ""))

    def check(self, name: str, current: str | None) -> str:
        """比对指纹 —— **那地方还是原来的样子吗**。

        返回 `ok` / `changed` / `gone` / `unknown`（没记指纹或读不到就是 unknown）。

        ⚠️ **`unknown` 不等于 `ok`** —— 「不知道」和「没变」是两件事，
        混在一起她就会以为一切正常（docs/04 坑 10f 同一个道理）。
        """
        hit = self.get(name)
        if hit is None:
            return "unknown"
        was = str(hit.get("fp") or "")
        if not was or current is None:
            return "unknown"
        cur = str(current)
        if cur == was:
            return "ok"
        if not cur or "air" in cur:
            return "gone"      # 那格空了 —— 被挖掉/被炸没了
        return "changed"

    def forget(self, name: str) -> str:
        key = _clean(name, MAX_NAME)
        if self._places.pop(key, None) is None:
            return f"地点簿里没有「{key}」。"
        self.save()
        return f"忘掉「{key}」了。"

    # ---- 读 -------------------------------------------------------------

    def get(self, name: str) -> dict | None:
        """按名字找一个地方。**大小写不敏感** —— 她可能写成 "Home" 或 "home"。"""
        key = _clean(name, MAX_NAME)
        if key in self._places:
            return self._places[key]
        low = key.lower()
        for k, v in self._places.items():
            if k.lower() == low:
                return v
        return None

    def match(self, text: str) -> tuple[str, dict] | None:
        """把一段文本当地点名找 —— **也认"家" vs "我的家"这种包含关系**。"""
        hit = self.get(text)
        if hit is not None:
            return _clean(text, MAX_NAME), hit
        low = str(text or "").strip().lower()
        if not low:
            return None
        for k, v in self._places.items():
            if low in k.lower() or k.lower() in low:
                return k, v
        return None

    def near(self, x: float, y: float, z: float, *, radius: float = 8.0,
             dim: str = "") -> tuple[str, dict, float] | None:
        """离这个坐标**最近**的记过的地点（在 radius 内）。没有就 None。

        给"我正看着什么"用 —— 让她认出"这是我记过的熔炉区"。
        ⚠️ `dim` 只在**两边都非空**时比，且只比 `:` 后面那截 ——
        我们存过 "overworld"，也见过 "minecraft:overworld"，直接比字符串会假不匹配。
        """
        want = str(dim or "").split(":")[-1].lower()
        best = None
        bestd = float(radius)
        for k, v in self._places.items():
            have = str(v.get("dim") or "").split(":")[-1].lower()
            if want and have and want != have:
                continue
            try:
                d = ((float(v["x"]) - x) ** 2 + (float(v["y"]) - y) ** 2
                     + (float(v["z"]) - z) ** 2) ** 0.5
            except (KeyError, TypeError, ValueError):
                continue
            if d <= bestd:
                best, bestd = (k, v), d
        if best is None:
            return None
        return best[0], best[1], round(bestd, 1)

    def list(self) -> list[dict]:
        """按**最近去过**排序（常用的/新的在前）。"""
        rows = [dict(v, name=k) for k, v in self._places.items()]
        rows.sort(key=lambda r: float(r.get("seen") or 0), reverse=True)
        return rows

    def render(self) -> str:
        rows = self.list()
        if not rows:
            return "（地点簿是空的 —— 她还没记过任何地方）"
        out = []
        for r in rows[:30]:
            line = f"· {r['name']} ({r['x']:g}, {r['y']:g}, {r['z']:g})"
            if r.get("what"):
                line += f" —— {r['what']}"
            if r.get("note"):
                line += f"（{r['note']}）"
            out.append(line)
        if len(rows) > 30:
            out.append(f"…（还有 {len(rows) - 30} 个）")
        return "\n".join(out)

    def __len__(self) -> int:
        return len(self._places)

    # ---- 内部 -----------------------------------------------------------

    def _evict(self) -> None:
        """满了踢**最久没去过**的。"""
        over = len(self._places) - CAPACITY
        if over <= 0:
            return
        oldest = sorted(self._places.items(), key=lambda kv: float(kv[1].get("seen") or 0))
        for k, _ in oldest[:over]:
            self._places.pop(k, None)


def _clean(text: object, limit: int) -> str:
    """压成单行 + 限长。**换行会把上下文撑爆**，所以一律拍平。"""
    flat = " ".join(str(text or "").split())
    return flat[:limit]
