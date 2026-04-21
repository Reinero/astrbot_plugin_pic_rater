import time

from fastapi import HTTPException

from config import GALLERY_DIR, WRITE_META_MIN_COUNT
from infra.db import db
from infra.metadata import write_metadata
from services.gallery_service import file_id_for


def rate_image(ident: str, score: float, note: str | None) -> dict:
    with db() as conn:
        row = conn.execute("SELECT id, relpath, cnt, avg FROM images WHERE id=?", (ident,)).fetchone()
        if not row:
            row = conn.execute("SELECT id, relpath, cnt, avg FROM images WHERE relpath=?", (ident,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="image id not found")
        db_id = row["id"]
        rel = row["relpath"]
        old_cnt = int(row["cnt"] or 0)
        old_avg = float(row["avg"] or 0.0)
        new_cnt = old_cnt + 1
        new_avg = (old_avg * old_cnt + float(score)) / new_cnt
        conn.execute("INSERT INTO ratings(image_id, score, note, ts) VALUES (?,?,?,?)", (db_id or file_id_for(rel), float(score), note, int(time.time())))
        conn.execute("UPDATE images SET cnt=?, avg=? WHERE relpath=?", (new_cnt, new_avg, rel))
        conn.commit()
    wrote = False
    if new_cnt >= WRITE_META_MIN_COUNT:
        write_metadata((GALLERY_DIR / rel).resolve(), new_avg, new_cnt)
        wrote = True
    return {"id": db_id or ident, "avg": round(new_avg, 3), "count": new_cnt, "wrote_meta": wrote}
