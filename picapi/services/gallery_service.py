import hashlib
import os
import random
import time
import urllib.parse
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import HTTPException

from config import ALLOWED_SUFFIXES, GALLERY_DIR, PICK_BIAS, PICK_BIAS_ALPHA, RECURSIVE, STATIC_PREFIX
from infra.db import db
from infra.metadata import extract_subjects

FTS_TABLE = "images_fts"
_progress = {"phase": "idle", "total": 0, "done": 0, "started": 0, "updated": 0}
_prog_lock = Lock()


def progress():
    with _prog_lock:
        return dict(_progress)


def _set_prog(phase: str, total: int = 0, done: int = 0):
    with _prog_lock:
        _progress.update({"phase": phase, "total": int(total), "done": int(done), "started": int(time.time()), "updated": int(time.time())})


def _tick_prog(n: int = 1):
    with _prog_lock:
        _progress["done"] += int(n)
        _progress["updated"] = int(time.time())


def list_all_files(root: Path) -> list[Path]:
    if RECURSIVE:
        return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES]
    return [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES]


def file_id_for(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8", errors="replace")).hexdigest()[:16]


def to_url(rel: str) -> str:
    return f"{STATIC_PREFIX}/" + "/".join(urllib.parse.quote(seg) for seg in rel.split("/"))


def parse_weighted_cats(cat_param: Optional[str]) -> list[tuple[str, int]]:
    if not cat_param:
        return []
    out = []
    for item in [s.strip() for s in cat_param.split(",") if s.strip()]:
        if ":" in item:
            n, w = item.rsplit(":", 1)
            try:
                out.append((n.strip(), max(1, int(w))))
            except Exception:
                out.append((n.strip(), 1))
        else:
            out.append((item, 1))
    return out


def collect_in_category(cat_path: str) -> list[Path]:
    base = (GALLERY_DIR / cat_path).resolve()
    try:
        base.relative_to(GALLERY_DIR)
    except Exception:
        return []
    if not base.exists() or not base.is_dir():
        return []
    return list_all_files(base)


def ensure_image_record(rel: str, category: Optional[str]) -> str:
    iid = file_id_for(rel)
    ts = int(time.time())
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO images(id, relpath, category, filename, last_ts) VALUES (?, ?, ?, ?, ?)",
            (iid, rel, category, Path(rel).name, ts),
        )
        conn.commit()
    return iid


def random_pic(cat: Optional[str], q: Optional[str], bias: Optional[str], alpha: Optional[float]) -> dict:
    start = time.perf_counter()
    if q and q.strip():
        items = search_candidates(q, 200)
        if not items:
            raise HTTPException(status_code=404, detail="No images matched the query.")
        row = random.choice(items)
        return {
            "id": row["id"],
            "relpath": row["relpath"],
            "filename": row["filename"] or row["relpath"].split("/")[-1],
            "category": row["category"],
            "url": to_url(row["relpath"]),
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }

    if cat:
        weighted = parse_weighted_cats(cat)
        chosen = random.choices([n for n, _ in weighted], weights=[w for _, w in weighted], k=1)[0]
        files = collect_in_category(chosen)
        category = chosen
    else:
        files = list_all_files(GALLERY_DIR)
        category = None
    if not files:
        raise HTTPException(status_code=404, detail="No images in gallery.")
    rels = [p.relative_to(GALLERY_DIR).as_posix() for p in files]
    with db() as conn:
        cnt_map = {r["relpath"]: int(r["cnt"]) for r in conn.execute(f"SELECT relpath,cnt FROM images WHERE relpath IN ({','.join('?'*len(rels))})", tuple(rels)).fetchall()} if rels else {}
    cnts = [cnt_map.get(r, 0) for r in rels]
    eff_bias = (bias or PICK_BIAS or "off").lower()
    eff_alpha = max(float(alpha if alpha is not None else PICK_BIAS_ALPHA), 0.0001)
    if eff_bias == "min":
        min_cnt = min(cnts)
        idx = random.choice([i for i, c in enumerate(cnts) if c == min_cnt])
    elif eff_bias == "weighted":
        idx = random.choices(range(len(files)), weights=[1.0 / ((c + 1.0) ** eff_alpha) for c in cnts], k=1)[0]
    else:
        idx = random.randrange(len(files))
    rel = rels[idx]
    iid = ensure_image_record(rel, category)
    return {"id": iid, "relpath": rel, "filename": files[idx].name, "category": category, "url": to_url(rel), "latency_ms": int((time.perf_counter() - start) * 1000)}


def _safe_join_under_gallery(sub: str) -> Path:
    p = (GALLERY_DIR / (sub.strip("/"))) if sub else GALLERY_DIR
    p = p.resolve()
    p.relative_to(GALLERY_DIR.resolve())
    return p


def list_subdirs(path: str = "") -> dict:
    base = _safe_join_under_gallery(path)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=404, detail="path not found")
    subdirs = []
    for child in sorted(base.iterdir()):
        if child.is_dir():
            cnt = sum(1 for root, _, files in os.walk(child) for fn in files if (Path(root) / fn).suffix.lower() in ALLOWED_SUFFIXES)
            rel = child.relative_to(GALLERY_DIR).as_posix()
            subdirs.append({"path": rel, "name": child.name, "count": cnt})
    files_here = sum(1 for f in base.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_SUFFIXES)
    return {"base": (base.relative_to(GALLERY_DIR).as_posix() if base != GALLERY_DIR else ""), "dirs": subdirs, "files_here": files_here}


def list_top_categories() -> list[str]:
    return sorted([p.name for p in GALLERY_DIR.iterdir() if p.is_dir()])


def reindex(purge_missing: bool) -> dict:
    all_relpaths = []
    for root, _, files in os.walk(GALLERY_DIR):
        for fn in files:
            p = Path(root) / fn
            if p.suffix.lower() in ALLOWED_SUFFIXES:
                all_relpaths.append(p.relative_to(GALLERY_DIR).as_posix())
    with db() as conn:
        rows = [(file_id_for(r), r, r.split("/", 1)[0] if "/" in r else None, Path(r).name) for r in all_relpaths]
        for i in range(0, len(rows), 800):
            conn.executemany("INSERT OR IGNORE INTO images(id, relpath, category, filename) VALUES (?, ?, ?, ?)", rows[i : i + 800])
        purged = 0
        if purge_missing:
            db_paths = [r[0] for r in conn.execute("SELECT relpath FROM images").fetchall()]
            missing = [r for r in db_paths if r not in set(all_relpaths)]
            if missing:
                for i in range(0, len(missing), 800):
                    chunk = missing[i : i + 800]
                    conn.execute(f"DELETE FROM images WHERE relpath IN ({','.join('?'*len(chunk))})", tuple(chunk))
                purged = len(missing)
        conn.commit()
    return {"indexed": len(all_relpaths), "purged": purged}


def sync_subjects(limit: int = 0) -> dict:
    with db() as conn:
        sql = "SELECT relpath, last_ts FROM images ORDER BY rowid DESC"
        rows = conn.execute(sql + (" LIMIT ?" if limit > 0 else ""), (limit,) if limit > 0 else ()).fetchall()
    todo = []
    for r in rows:
        rel = r["relpath"]
        full = (GALLERY_DIR / rel).resolve()
        try:
            mtime = int(full.stat().st_mtime)
        except FileNotFoundError:
            continue
        if limit > 0 or int(r["last_ts"] or 0) == 0 or mtime > int(r["last_ts"] or 0):
            todo.append((rel, mtime))
    _set_prog("sync_subjects", len(todo), 0)
    with db() as conn:
        processed = 0
        for relpath, mtime in todo:
            tags = extract_subjects((GALLERY_DIR / relpath).resolve())
            conn.execute("DELETE FROM image_tags WHERE relpath=?", (relpath,))
            if tags:
                conn.executemany("INSERT OR IGNORE INTO image_tags(relpath, tag, tag_lc) VALUES (?,?,?)", [(relpath, t, t.lower()) for t in tags])
            conn.execute("UPDATE images SET last_ts=? WHERE relpath=?", (mtime, relpath))
            processed += 1
            _tick_prog(1)
        conn.commit()
    _set_prog("idle", 0, 0)
    return {"processed": len(todo)}


def ensure_fts():
    with db() as conn:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(relpath, filename, tags, content='images', content_rowid='rowid', tokenize='unicode61')"
        )
        conn.execute(
            f"INSERT INTO {FTS_TABLE}(rowid, relpath, filename, tags) SELECT i.rowid, i.relpath, COALESCE(i.filename,''), '' FROM images i WHERE i.rowid NOT IN (SELECT rowid FROM {FTS_TABLE})"
        )
        conn.commit()


def rebuild_fts(full: bool = True):
    with db() as conn:
        if full:
            for t in [FTS_TABLE, f"{FTS_TABLE}_data", f"{FTS_TABLE}_idx", f"{FTS_TABLE}_docsize", f"{FTS_TABLE}_config"]:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
    ensure_fts()
    refresh_fts_tags()
    return {"ok": True, "fts": FTS_TABLE}


def refresh_fts_tags():
    ensure_fts()
    with db() as conn:
        conn.execute(
            f"UPDATE {FTS_TABLE} SET tags = COALESCE((SELECT GROUP_CONCAT(image_tags.tag, ' ') FROM image_tags WHERE image_tags.relpath = {FTS_TABLE}.relpath), '')"
        )
        conn.commit()
    return {"ok": True, "fts": FTS_TABLE}


def _split_terms(q: str) -> list[str]:
    try:
        import shlex

        return [p for p in shlex.split((q or "").strip()) if p]
    except Exception:
        return [p for p in (q or "").split() if p]


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_like_where_and_args(terms: list[str]):
    clauses = []
    args = []
    for raw in terms:
        pat = f"%{_like_escape(raw)}%"
        clauses.append(
            "("
            " i.relpath LIKE ? ESCAPE '\\' OR i.filename LIKE ? ESCAPE '\\' OR "
            " EXISTS (SELECT 1 FROM image_tags t WHERE t.relpath = i.relpath AND t.tag LIKE ? ESCAPE '\\') "
            ")"
        )
        args.extend([pat, pat, pat])
    return " AND ".join(clauses) if clauses else "1=1", args


def search_candidates(q: str, limit: int = 10) -> list[dict]:
    terms = _split_terms(q)
    start = time.perf_counter()
    ensure_fts()
    fts_q = " ".join([f"{t}*" if t.isascii() and " " not in t else f'"{t}"' for t in terms])
    with db() as conn:
        try:
            rows = conn.execute(
                f"SELECT i.relpath, i.id, i.category, i.filename, i.cnt, i.avg FROM {FTS_TABLE} f JOIN images i ON i.rowid=f.rowid WHERE {FTS_TABLE} MATCH ? ORDER BY i.cnt ASC, i.avg DESC LIMIT ?",
                (fts_q, limit),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except Exception:
            pass
        where_sql, args = _build_like_where_and_args(terms)
        rows = conn.execute(
            f"SELECT i.relpath, i.id, i.category, i.filename, i.cnt, i.avg FROM images i WHERE {where_sql} ORDER BY i.cnt ASC, i.avg DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    _ = int((time.perf_counter() - start) * 1000)
    return [dict(r) for r in rows]
