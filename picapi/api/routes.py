import time
from typing import Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse, RedirectResponse

from config import ALLOWED_SUFFIXES, GALLERY_DIR, RECURSIVE
from models import RandomPicOut, RateIn, RateOut, ReindexOut, SearchOut
from services.gallery_service import (
    list_all_files,
    list_subdirs,
    list_top_categories,
    progress,
    random_pic,
    rebuild_fts,
    refresh_fts_tags,
    reindex,
    search_candidates,
    sync_subjects,
)
from services.rating_service import rate_image

router = APIRouter()


@router.get("/health")
def health():
    files = list_all_files(GALLERY_DIR)
    return {
        "ok": True,
        "gallery": str(GALLERY_DIR),
        "allowed_suffixes": ALLOWED_SUFFIXES,
        "recursive": RECURSIVE,
        "top_categories": list_top_categories(),
        "total_files": len(files),
    }


@router.get("/categories")
def categories():
    return {"categories": list_top_categories()}


@router.get("/dirs")
def dirs(path: str = ""):
    return list_subdirs(path)


@router.get("/random_pic", response_model=RandomPicOut)
def random_pic_route(
    cat: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    redirect: bool = False,
    bias: Optional[str] = None,
    alpha: Optional[float] = None,
):
    payload = random_pic(cat=cat, q=q, bias=bias, alpha=alpha)
    if redirect:
        return RedirectResponse(url=payload["url"], status_code=302)
    return JSONResponse({k: v for k, v in payload.items() if k != "latency_ms"})


@router.post("/rate", response_model=RateOut)
def rate(body: RateIn):
    return rate_image(body.id, body.score, body.note)


@router.post("/reindex", response_model=ReindexOut)
def reindex_route(purge_missing: bool = Body(default=False)):
    return reindex(purge_missing)


@router.post("/sync_subjects")
def sync_subjects_route(limit: int = 0):
    return sync_subjects(limit)


@router.get("/search", response_model=SearchOut)
def search(q: str = Query(...), limit: int = 10):
    st = time.perf_counter()
    items = search_candidates(q, limit)
    return {"q": q, "mode": "fts_or_like", "latency_ms": int((time.perf_counter() - st) * 1000), "items": items}


@router.get("/admin/sync_progress")
def sync_progress():
    return progress()


@router.post("/admin/rebuild_fts")
def admin_rebuild_fts(full: bool = True):
    return rebuild_fts(full)


@router.post("/admin/refresh_fts_tags")
def admin_refresh_fts_tags():
    return refresh_fts_tags()
