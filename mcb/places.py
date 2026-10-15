
from __future__ import annotations

import json
import time
from pathlib import Path

from astrbot.api import logger

CAPACITY = 200

MAX_NAME = 24
MAX_WHAT = 40
MAX_NOTE = 80

MAX_FP = 64


class PlaceBook:

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._places: dict[str, dict] = {}
        self.load()


    def load(self) -> None:
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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._places, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[mc_body] 地点簿写不进去：{exc}")


    def remember(self, name: str, x: float, y: float, z: float, *,
                 dim: str = "", what: str = "", note: str = "",
                 fp: str = "") -> str:
        r"""fp 保存方块 ID，供 check 检测地点变化。"""
        key = _clean(name, MAX_NAME)
        if not key:
            return "得给这个地方起个名字。"

        old = self._places.get(key)
        entry = {
            "x": round(float(x), 1),
            "y": round(float(y), 1),
            "z": round(float(z), 1),
            "dim": _clean(dim, 32) or (old or {}).get("dim", ""),
            # 刷新坐标时不以空描述覆盖旧描述。
            "what": _clean(what, MAX_WHAT) or (old or {}).get("what", ""),
            "note": _clean(note, MAX_NOTE) or (old or {}).get("note", ""),
            # 未读取到新指纹时保留旧值，避免丢失比对基线。
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
        r"""unknown 不等于 ok。"""
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
            return "gone"      
        return "changed"

    def forget(self, name: str) -> str:
        key = _clean(name, MAX_NAME)
        if self._places.pop(key, None) is None:
            return f"地点簿里没有「{key}」。"
        self.save()
        return f"忘掉「{key}」了。"


    def get(self, name: str) -> dict | None:
        key = _clean(name, MAX_NAME)
        if key in self._places:
            return self._places[key]
        low = key.lower()
        for k, v in self._places.items():
            if k.lower() == low:
                return v
        return None

    def match(self, text: str) -> tuple[str, dict] | None:
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
        r"""维度去命名空间后比较，且仅在两边均非空时检查。"""
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


    def _evict(self) -> None:
        over = len(self._places) - CAPACITY
        if over <= 0:
            return
        oldest = sorted(self._places.items(), key=lambda kv: float(kv[1].get("seen") or 0))
        for k, _ in oldest[:over]:
            self._places.pop(k, None)


def _clean(text: object, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit]
