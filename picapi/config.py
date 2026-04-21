import os
from pathlib import Path

PICK_BIAS = os.environ.get("PICK_BIAS", "min").lower()
PICK_BIAS_ALPHA = float(os.environ.get("PICK_BIAS_ALPHA", "1.0"))
GALLERY_DIR = Path(os.environ.get("GALLERY_DIR", "/data/gallery")).resolve()
STATIC_PREFIX = os.environ.get("STATIC_PREFIX", "/static")
ALLOWED_SUFFIXES = tuple(
    s.strip().lower() for s in os.environ.get("ALLOWED_SUFFIXES", ".jpg,.jpeg,.png,.gif,.webp").split(",")
)
RECURSIVE = os.environ.get("RECURSIVE", "true").lower() in {"1", "true", "yes"}
DB_PATH = Path("/data/db/picapi.sqlite")
WRITE_META_MIN_COUNT = int(os.environ.get("WRITE_META_MIN_COUNT", "1"))
