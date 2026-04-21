import json
import subprocess
from pathlib import Path

_TAG_BLACKLIST_PREFIXES = ("score:", "count:")
_TAG_BLACKLIST_FIXED = {"rated"}


def extract_subjects(abs_path: Path) -> list[str]:
    try:
        out = subprocess.check_output(["exiftool", "-j", "-s", "-XMP:Subject", str(abs_path)], stderr=subprocess.DEVNULL)
        arr = json.loads(out.decode("utf-8", "ignore"))
        if not arr:
            return []
        subs = arr[0].get("Subject")
        if isinstance(subs, str):
            subs = [subs]
        clean = []
        for t in subs or []:
            s = str(t).strip()
            if not s:
                continue
            sl = s.lower()
            if sl in _TAG_BLACKLIST_FIXED or any(sl.startswith(p) for p in _TAG_BLACKLIST_PREFIXES):
                continue
            clean.append(s)
        return clean
    except Exception:
        return []


def write_metadata(abs_path: Path, avg: float, cnt: int):
    if not abs_path.exists():
        return
    rounded = max(0, min(5, int(round(avg))))
    subjects = extract_subjects(abs_path)
    new_subjects = subjects + ["rated", f"score:{rounded}", f"count:{cnt}"]
    args = ["exiftool", "-overwrite_original", f"-XMP:Rating={rounded}", "-XMP:Subject="]
    for item in new_subjects:
        args.append(f"-XMP:Subject+={item}")
    args.append(str(abs_path))
    try:
        subprocess.run(args, capture_output=True, check=True)
    except subprocess.CalledProcessError:
        return
