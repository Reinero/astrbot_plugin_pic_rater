import re
from typing import Tuple

_CAT_HINT_RE = re.compile(r"[,:/]")


def build_random_params(arg_text: str) -> dict:
    t = (arg_text or "").strip()
    if not t:
        return {}

    low = t.lower()
    if low.startswith("?"):
        return {"q": t[1:].strip()}
    if low.startswith("q:"):
        return {"q": t[2:].strip()}
    if _CAT_HINT_RE.search(t):
        return {"cat": t}
    return {"q": t}


def parse_purge_flag(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"清理", "purge", "cleanup", "clean", "true", "1", "是", "yes"}


def parse_rating_text(text: str) -> Tuple[float, str | None]:
    txt = (text or "").strip()
    if not txt:
        raise ValueError("empty")
    parts = txt.split(maxsplit=1)
    score = float(parts[0])
    if not (0.0 <= score <= 5.0):
        raise ValueError("out_of_range")
    note = parts[1] if len(parts) >= 2 else None
    return score, note
