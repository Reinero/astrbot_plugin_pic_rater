from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from typing import Optional, List, Tuple
import os, random, sqlite3, time, hashlib, urllib.parse, subprocess, math
import json
from fastapi import Body  # 新增：用于接收 JSON body
from pydantic import BaseModel, Field
import shlex
from fastapi import Query

# 过滤：把我们写回的内部评分标签排除掉
_TAG_BLACKLIST_PREFIXES = ("score:", "count:")
_TAG_BLACKLIST_FIXED = {"rated"}

PICK_BIAS = os.environ.get("PICK_BIAS", "min").lower()      # off|min|weighted
PICK_BIAS_ALPHA = float(os.environ.get("PICK_BIAS_ALPHA", "1.0"))  # weighted 的指数

FTS_TABLE = os.environ.get("FTS_TABLE", "images_fts")

GALLERY_DIR = Path(os.environ.get("GALLERY_DIR", "/data/gallery")).resolve()
STATIC_PREFIX = os.environ.get("STATIC_PREFIX", "/static")
ALLOWED_SUFFIXES = tuple(
    s.strip().lower() for s in os.environ.get("ALLOWED_SUFFIXES", ".jpg,.jpeg,.png,.gif,.webp").split(",")
)
RECURSIVE = os.environ.get("RECURSIVE", "true").lower() in {"1", "true", "yes"}
DB_PATH = Path("/data/db/picapi.sqlite")
WRITE_META_MIN_COUNT = int(os.environ.get("WRITE_META_MIN_COUNT", "1"))
SCORE_PRECISION = int(os.environ.get("SCORE_PRECISION", "2"))

app = FastAPI(title="Picture API with Ratings", version="2.0.0")
app.mount(STATIC_PREFIX, StaticFiles(directory=str(GALLERY_DIR), html=False), name="static")

def get_counts_for_rels(rels: List[str]) -> List[int]:
    """
    批量查询这些相对路径的评分次数 cnt。
    不在数据库(images表)里的，默认 cnt=0。
    为避免 SQLite 占位符上限，做分片查询。
    """
    if not rels:
        return []
    out_map = {}
    with db() as conn:
        for i in range(0, len(rels), 900):
            chunk = rels[i:i+900]
            qmarks = ",".join("?" * len(chunk))
            cur = conn.execute(f"SELECT relpath, cnt FROM images WHERE relpath IN ({qmarks})", tuple(chunk))
            for row in cur.fetchall():
                out_map[row["relpath"]] = int(row["cnt"])
    return [out_map.get(r, 0) for r in rels]

def _assert_fts5_available():
    try:
        with db() as conn:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS __fts5_probe USING fts5(x)")
            conn.execute("DROP TABLE __fts5_probe")
    except Exception as e:
        raise RuntimeError("SQLite 未启用 FTS5，无法使用全文索引") from e

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")  # 新增：最多等 5s
    conn.row_factory = sqlite3.Row
    return conn



def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS images (
            id TEXT PRIMARY KEY,
            relpath TEXT NOT NULL UNIQUE,
            category TEXT,
            cnt INTEGER NOT NULL DEFAULT 0,
            sum REAL NOT NULL DEFAULT 0.0,
            avg REAL NOT NULL DEFAULT 0.0,
            last_ts INTEGER
        );""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            rid INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id TEXT NOT NULL,
            score REAL NOT NULL,
            note TEXT,
            ts INTEGER NOT NULL,
            FOREIGN KEY(image_id) REFERENCES images(id)
        );""")
init_db()




def list_all_files(root: Path) -> List[Path]:
    if RECURSIVE:
        return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES]
    else:
        return [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES]

def list_top_categories() -> List[str]:
    return sorted([p.name for p in GALLERY_DIR.iterdir() if p.is_dir()])

def to_url(rel: str) -> str:
    return f"{STATIC_PREFIX}/" + "/".join(urllib.parse.quote(seg) for seg in rel.split("/"))

def file_id_for(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8", errors="replace")).hexdigest()[:16]

def collect_in_category(cat_path: str) -> List[Path]:
    base = (GALLERY_DIR / cat_path).resolve()
    try:
        base.relative_to(GALLERY_DIR)
    except Exception:
        return []
    if not base.exists() or not base.is_dir():
        return []
    return list_all_files(base)

def parse_weighted_cats(cat_param: Optional[str]) -> List[Tuple[str, int]]:
    if not cat_param:
        return []
    parts = [s.strip() for s in cat_param.split(",") if s.strip()]
    out: List[Tuple[str,int]] = []
    for item in parts:
        if ":" in item:
            name, w = item.rsplit(":", 1)
            try:
                wv = max(1, int(w))
            except:
                wv = 1
            out.append((name.strip(), wv))
        else:
            out.append((item, 1))
    return out

def choice_by_weight(groups: List[Tuple[str,int]]) -> str:
    names = [n for n,_ in groups]; weights = [w for _,w in groups]
    return random.choices(names, weights=weights, k=1)[0]

def ensure_image_record(rel: str, category: Optional[str]):
    iid = file_id_for(rel)
    ts = int(time.time())
    with db() as conn:
        conn.execute("""
        INSERT INTO images (id, relpath, category, last_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(relpath) DO NOTHING;
        """, (iid, rel, category, ts))
    return iid

# ===== reindex 辅助函数（复制整段）=====
def _is_image_file(p: Path) -> bool:
    """判断是否为支持的图片扩展名"""
    allow = {".jpg",".jpeg",".png",".gif",".webp",".bmp",".tiff",".jfif",".avif"}
    return p.suffix.lower() in allow

def _top_category_of(relpath: str) -> Optional[str]:
    """把相对路径的顶级文件夹作为 category（如 a/b/c.jpg -> a）"""
    parts = relpath.split("/", 1)
    return parts[0] if len(parts) >= 2 else None
# ===== reindex 辅助函数 END =====


#-------
def _get_current_subjects(abs_path: Path) -> list:
    """
    读取现有 XMP:Subject，返回列表。
    兼容没有 Subject、或 Subject 是字符串/列表两种情况。
    """
    try:
        res = subprocess.run(
            ["exiftool", "-j", "-XMP:Subject", str(abs_path)],
            capture_output=True,
            check=True,
        )
        data = json.loads(res.stdout.decode("utf-8", errors="ignore") or "[]")
        if isinstance(data, list) and data:
            subj = data[0].get("Subject")
            if isinstance(subj, list):
                return [str(x) for x in subj]
            if isinstance(subj, str):
                # 个别情况下可能是以逗号拼的单字符串
                return [s.strip() for s in subj.split(",") if s.strip()]
    except Exception:
        pass
    return []


def write_metadata(abs_path: Path, avg: float, cnt: int):
    """
    - XMP:Rating 写入 0~5 的整数（四舍五入）
    - XMP:Subject 采用“覆写”策略：保留非评分条目，覆盖 rated/score/count 为最新一份
    """
    if not abs_path.exists():
        return

    # 1) 评分整数化（0~5）
    rounded = int(round(avg))
    if rounded < 0:
        rounded = 0
    if rounded > 5:
        rounded = 5

    # 2) 读出现有 Subject 并过滤我们维护的三类
    subjects = _get_current_subjects(abs_path)
    filtered = []
    seen = set()
    for s in subjects:
        if not s:
            continue
        low = s.lower()
        if low == "rated" or low.startswith("score:") or low.startswith("count:"):
            continue
        if s not in seen:
            seen.add(s)
            filtered.append(s)

    # 3) 重新构建要写入的 Subject 列表（先保留原有其它标签，再追加最新评分信息）
    new_subjects = filtered + ["rated", f"score:{rounded}", f"count:{cnt}"]

    # 4) exiftool 命令：
    #    - 覆盖 XMP:Rating
    #    - 先清空 Subject（-XMP:Subject=），再用 += 按顺序填入 new_subjects
    args = [
        "exiftool",
        "-overwrite_original",
        f"-XMP:Rating={rounded}",
        "-XMP:Subject=",
    ]
    for item in new_subjects:
        args.append(f"-XMP:Subject+={item}")
    args.append(str(abs_path))

    try:
        subprocess.run(args, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        # 如需排查可打印 e.stderr
        pass

#------------

def _extract_subjects_from_file(abs_path: Path) -> list[str]:
    """
    用 exiftool 读取 XMP:Subject，返回纯净的标签列表（过滤我们内部的评分标签）。
    """
    try:
        # -j json输出；只取 XMP:Subject；-s 简洁键名；文件路径用 str(abs_path)
        cmd = ["exiftool", "-j", "-s", "-XMP:Subject", str(abs_path)]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        arr = json.loads(out.decode("utf-8", "ignore"))
        if not arr:
            return []
        subs = arr[0].get("Subject")
        if not subs:
            return []
        clean = []
        for t in subs:
            tl = str(t).strip()
            tlc = tl.lower()
            if tlc in _TAG_BLACKLIST_FIXED:
                continue
            if any(tlc.startswith(p) for p in _TAG_BLACKLIST_PREFIXES):
                continue
            clean.append(tl)
        return clean
    except Exception:
        return []

def _upsert_tags(conn, relpath: str, tags: list[str]):
    # 幂等：先清，再插
    conn.execute("DELETE FROM image_tags WHERE relpath=?", (relpath,))
    rows = [(relpath, t, t.lower()) for t in tags]
    if rows:
        conn.executemany("INSERT OR IGNORE INTO image_tags(relpath, tag, tag_lc) VALUES (?,?,?)", rows)

@app.post("/sync_subjects")
def sync_subjects(limit: int = 0):
    """
    扫描数据库中的图片，读取 XMP:Subject 写入 image_tags。
    limit>0 时只处理最近的 N 条（按 rowid DESC，便于分批）。
    """
    with db() as conn:
        if limit and limit > 0:
            cur = conn.execute("SELECT relpath FROM images ORDER BY rowid DESC LIMIT ?", (limit,))
        else:
            cur = conn.execute("SELECT relpath FROM images")
        rels = [r[0] for r in cur.fetchall()]

        n = 0
        for rel in rels:
            abs_path = (GALLERY_DIR / rel).resolve()
            tags = _extract_subjects_from_file(abs_path)
            _upsert_tags(conn, rel, tags)
            n += 1
            if n % 200 == 0:
                conn.commit()
        conn.commit()
    return {"processed": len(rels)}

#------------
def _drop_legacy_objs(conn: sqlite3.Connection):
    """
    ⚠️ 强力清理：无条件删除数据库里所有触发器和视图。
    目的：彻底清掉任何可能引用历史 SQL（如 T.tags / image_tags_norm）的对象。
    """
    rows = conn.execute("""
        SELECT name, type
          FROM sqlite_master
         WHERE type IN ('view','trigger')
    """).fetchall()
    for name, typ in rows:
        if typ == 'view':
            conn.execute(f"DROP VIEW IF EXISTS {name}")
        else:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")

    # 不返回任何东西，只负责清空



#------------ 彻底稳妥的 FTS 初始化（无 t.tags/T.tags）------------
def _init_fts_schema():
    _assert_fts5_available()
    with db() as conn:
        _drop_legacy_objs(conn)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS image_tags(
          relpath TEXT NOT NULL,
          tag     TEXT NOT NULL,
          tag_lc  TEXT NOT NULL,
          PRIMARY KEY(relpath, tag)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_image_tags_tag_lc ON image_tags(tag_lc)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_image_tags_relpath ON image_tags(relpath)")

        try: conn.execute("ALTER TABLE images ADD COLUMN filename TEXT")
        except Exception: pass
        for (rel,) in conn.execute("SELECT relpath FROM images WHERE filename IS NULL OR filename=''"):
            conn.execute("UPDATE images SET filename=? WHERE relpath=?", (Path(rel).name, rel))

        conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
          relpath, filename, tags,
          content='images', content_rowid='rowid',
          tokenize='unicode61'
        )""")

        conn.execute(f"""
        INSERT INTO {FTS_TABLE}(rowid, relpath, filename, tags)
        SELECT i.rowid, i.relpath, COALESCE(i.filename,''), ''
          FROM images AS i
         WHERE i.rowid NOT IN (SELECT rowid FROM {FTS_TABLE})
        """)
        conn.commit()





#---------------------------
def _fts_query_from_kw(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    import shlex
    try:
        parts = shlex.split(s)
    except Exception:
        parts = s.split()

    def is_ascii(tok: str) -> bool:
        try:
            tok.encode("ascii")
            return True
        except Exception:
            return False

    out = []
    for tok in parts:
        tok = tok.strip()
        if not tok:
            continue
        if " " in tok:            # 短语原样
            out.append(f'"{tok}"')
        else:                     # 仅 ASCII 加 *
            out.append(tok + "*" if is_ascii(tok) else tok)
    return " ".join(out)



def _init_indices():
    with db() as conn:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_relpath ON images(relpath)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_cnt ON images(cnt)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_cat ON images(category)")
        conn.commit()


@app.post("/admin/nuke_legacy")
def admin_nuke_legacy():
    """
    仅清理旧视图/触发器；不会动任何表。
    目的：把所有可能引用 T.tags / image_tags_norm 的东西清干净。
    """
    with db() as conn:
        _drop_legacy_objs(conn)
        conn.commit()
    return {"ok": True, "done": "dropped legacy views/triggers"}


@app.on_event("startup")
def _on_startup():
    _init_indices()
    if os.environ.get("SKIP_FTS_INIT"):
        return
    _init_fts_schema()



@app.post("/admin/rebuild_fts")
def admin_rebuild_fts(full: bool = True):
    try:
        with db() as conn:
            _drop_legacy_objs(conn)
            conn.commit()

            if full:
                to_drop = [FTS_TABLE,
                           f"{FTS_TABLE}_data",
                           f"{FTS_TABLE}_idx",
                           f"{FTS_TABLE}_docsize",
                           f"{FTS_TABLE}_config"]
                for t in to_drop:
                    conn.execute(f"DROP TABLE IF EXISTS {t}")
                conn.commit()

        _init_fts_schema()

        # 回填 tags（只从 image_tags.tag 聚合）
        with db() as conn:
            conn.execute(f"""
                UPDATE {FTS_TABLE}
                   SET tags = COALESCE((
                       SELECT GROUP_CONCAT(image_tags.tag, ' ')
                         FROM image_tags
                        WHERE image_tags.relpath = {FTS_TABLE}.relpath
                   ), '')
            """)
            conn.commit()

        return {"ok": True, "fts": FTS_TABLE}

    except Exception as e:
        # 把真实错误抛给客户端，便于你直接看到原因
        raise HTTPException(status_code=500, detail=f"rebuild_fts failed: {e}")



@app.post("/admin/refresh_fts_tags")
def admin_refresh_fts_tags():
    with db() as conn:
        conn.execute(f"""
            UPDATE {FTS_TABLE}
               SET tags = COALESCE((
                   SELECT GROUP_CONCAT(image_tags.tag, ' ')
                     FROM image_tags
                    WHERE image_tags.relpath = {FTS_TABLE}.relpath
               ), '')
        """)
        conn.commit()
    return {"ok": True, "fts": FTS_TABLE}






@app.post("/reindex")
def reindex(purge_missing: bool = Body(default=False, description="是否删除库里已不存在的图片记录")):
    """
    扫描 GALLERY_DIR，把所有图片登记到 images 表（仅补齐，不覆盖评分）；
    可选：purge_missing=True 会删除数据库里存在、但磁盘已删除的记录。
    """
    # 1) 扫盘收集所有图片的相对路径
    all_relpaths: List[str] = []
    for root, _, files in os.walk(GALLERY_DIR):
        for fn in files:
            p = Path(root) / fn
            if _is_image_file(p):
                rel = p.relative_to(GALLERY_DIR).as_posix()
                all_relpaths.append(rel)

    inserted = 0
    purged = 0
    with db() as conn:
        # 2) 批量补充插入（已存在则忽略，不覆盖评分/次数）-------------------------------
        rows = [(file_id_for(r), r, _top_category_of(r), Path(r).name) for r in all_relpaths]
        for i in range(0, len(rows), 800):
            chunk = rows[i:i + 800]
            conn.executemany(
                "INSERT OR IGNORE INTO images(id, relpath, category, filename) VALUES (?, ?, ?, ?)",
                chunk
            )

        conn.commit()
        # 统计本次“确实新增”的数量
        cur = conn.execute("SELECT COUNT(*) FROM images")
        total_rows = cur.fetchone()[0]
        # 粗略估算：插入后总行数减去原有行数 ≈ 新增（如需精确可先查旧总数再插入）
        # 这里简化处理，不强求 inserted 的精确值
        inserted = None  # 可按需删去这行并实现精确统计

        # 3) 可选：删除磁盘已不存在的记录
        if purge_missing:
            cur = conn.execute("SELECT relpath FROM images")
            db_paths = [row[0] for row in cur.fetchall()]
            disk_set = set(all_relpaths)
            missing = [r for r in db_paths if r not in disk_set]
            for i in range(0, len(missing), 800):
                chunk = missing[i:i+800]
                q = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM images WHERE relpath IN ({q})", tuple(chunk))
            conn.commit()
            purged = len(missing)

    return {"indexed": len(all_relpaths), "purged": purged}



@app.get("/health")
def health():
    files = list_all_files(GALLERY_DIR)
    return {
        "ok": True,
        "gallery": str(GALLERY_DIR),
        "allowed_suffixes": ALLOWED_SUFFIXES,
        "recursive": RECURSIVE,
        "top_categories": list_top_categories(),
        "total_files": len(files),
        "db": str(DB_PATH),
    }

@app.get("/categories")
def categories():
    return {"categories": list_top_categories()}

@app.get("/random_pic")
def random_pic(
    cat: Optional[str] = Query(default=None, description="分类过滤，支持权重：风景 或 风景,人像 或 风景:3,人像:1；支持多级：壁纸/风景"),
    q:   Optional[str] = Query(default=None, description="全文搜索关键字（路径/文件名/标签）"),
    redirect: bool = False,
    bias: Optional[str] = None,
    alpha: Optional[float] = None,
):
    import random

    # ① 带 q：用 LIKE 做检索 → 从前200里随机挑一张（避免 ORDER BY RANDOM() 全表扫）
    if q and q.strip():
        terms = _split_terms(q)
        where_sql, args = _build_like_where_and_args(terms)
        with db() as conn:
            cur = conn.execute(
                f"""
                SELECT i.relpath, i.id, i.category, i.filename, i.cnt, i.avg
                FROM images i
                WHERE {where_sql}
                ORDER BY i.cnt ASC, i.avg DESC
                LIMIT 200
                """,
                args
            )
            items = cur.fetchall()
        if not items:
            raise HTTPException(status_code=404, detail="No images matched the query.")
        row = random.choice(items)
        relpath, iid, category, filename, cnt, avg = row
        url = to_url(relpath)
        payload = {
            "id": iid,
            "relpath": relpath,
            "filename": filename or relpath.split("/")[-1],
            "category": category,
            "url": url,
        }
        if redirect:
            return RedirectResponse(url=url, status_code=302)
        return JSONResponse(payload)

    # ② 没有 q：保持你原来的“分类/权重 + 少评优先/加权/纯随机”的本地文件逻辑
    if cat:
        weighted = parse_weighted_cats(cat)
        chosen = choice_by_weight(weighted)
        files = collect_in_category(chosen)
        if not files:
            for name, _ in weighted:
                cand = collect_in_category(name)
                if cand:
                    files, chosen = cand, name
                    break
            else:
                raise HTTPException(404, "No images under given categories.")
        category = chosen
    else:
        files = list_all_files(GALLERY_DIR)
        category = None

    if not files:
        raise HTTPException(404, "No images in gallery.")

    rels_all = [p.relative_to(GALLERY_DIR).as_posix() for p in files]
    cnts = get_counts_for_rels(rels_all)

    eff_bias = (bias or PICK_BIAS or "off").lower()
    eff_alpha = float(alpha if (alpha is not None) else PICK_BIAS_ALPHA)
    eff_alpha = max(eff_alpha, 0.0001)

    if eff_bias == "min":
        min_cnt = min(cnts)
        candidates = [i for i, c in enumerate(cnts) if c == min_cnt]
        idx = random.choice(candidates)
    elif eff_bias == "weighted":
        weights = [1.0 / ((c + 1.0) ** eff_alpha) for c in cnts]
        idx = random.choices(range(len(files)), weights=weights, k=1)[0]
    else:
        idx = random.randrange(len(files))

    pic = files[idx]
    rel = rels_all[idx]
    iid = ensure_image_record(rel, category)
    url = to_url(rel)
    payload = {
        "id": iid,
        "relpath": rel,
        "filename": pic.name,
        "category": category,
        "url": url,
    }
    if redirect:
        return RedirectResponse(url=url, status_code=302)
    return JSONResponse(payload)



class RateIn(BaseModel):
    # 这里的 id 既可以是真正的 images.id，也可以直接传 relpath
    id: str
    score: float = Field(ge=0.0, le=5.0)
    note: Optional[str] = None

@app.post("/rate")
def rate_image(body: RateIn):
    ident = body.id

    with db() as conn:
        # ① 先按 id 精确查（适配 TEXT/CHAR/VARCHAR 等）
        row = conn.execute(
            "SELECT id, relpath, cnt, avg FROM images WHERE id = ?",
            (ident,)
        ).fetchone()

        # ② 找不到就把 ident 当成 relpath 再查一遍
        if not row:
            row = conn.execute(
                "SELECT id, relpath, cnt, avg FROM images WHERE relpath = ?",
                (ident,)
            ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="image id not found")

        db_id  = row["id"]
        rel    = row["relpath"]
        old_cnt = int(row["cnt"] or 0)
        old_avg = float(row["avg"] or 0.0)

        # ③ 计算新均分/次数
        new_cnt = old_cnt + 1
        new_avg = (old_avg * old_cnt + float(body.score)) / new_cnt
#-----------
        # ③.5 记录评分历史
        conn.execute(
            "INSERT INTO ratings(image_id, score, note, ts) VALUES (?,?,?,?)",
            (db_id or file_id_for(rel), float(body.score), body.note, int(time.time()))
        )
        #--------
        # ④ 用 relpath 做 WHERE（不依赖 id 的类型/是否稳定）
        conn.execute(
            "UPDATE images SET cnt = ?, avg = ? WHERE relpath = ?",
            (new_cnt, new_avg, rel)
        )
        conn.commit()

    # ⑤ 达阈值写回 XMP（你之前已实现“覆写整数分”的 write_metadata）
    wrote = False
    try:
        if new_cnt >= WRITE_META_MIN_COUNT:
            abs_path = (GALLERY_DIR / rel).resolve()
            write_metadata(abs_path, new_avg, new_cnt)
            wrote = True
    except Exception:
        pass

    return {"id": db_id or ident, "avg": round(new_avg, 3), "count": new_cnt, "wrote_meta": wrote}




@app.get("/stats")
def stats(id: Optional[str] = None, top: int = 50):
    with db() as conn:
        if id:
            cur = conn.execute("SELECT * FROM images WHERE id=?", (id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "image id not found")
            cur2 = conn.execute("SELECT score, ts, note FROM ratings WHERE image_id=? ORDER BY rid DESC LIMIT 100", (id,))
            ratings = [dict(r) for r in cur2.fetchall()]
            return {"image": dict(row), "ratings": ratings}
        else:
            cur = conn.execute("SELECT * FROM images ORDER BY avg DESC, cnt DESC LIMIT ?", (int(top),))
            return {"top": [dict(r) for r in cur.fetchall()]}

from fastapi import Query
from typing import List, Tuple

def _like_escape(s: str) -> str:
    # 逃逸 %, _ 和 \
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def _split_terms(q: str) -> List[str]:
    q = (q or "").strip()
    if not q:
        return []
    # 用空白切词；引号里视作短语
    import shlex
    try:
        parts = [p for p in shlex.split(q) if p]
    except Exception:
        parts = [p for p in q.split() if p]
    return parts

def _build_like_where_and_args(terms: List[str]) -> Tuple[str, List[str]]:
    """
    WHERE ( i.relpath LIKE ? ESCAPE '\' OR i.filename LIKE ? ESCAPE '\' OR
            EXISTS (SELECT 1 FROM image_tags t WHERE t.relpath=i.relpath AND t.tag LIKE ? ESCAPE '\') )
    对每个 term 生成一组三个条件，然后 AND 串联（多词＝都要匹配）。
    """
    where_clauses = []
    args: List[str] = []
    for raw in terms:
        t = _like_escape(raw)
        # 前后都加 %，即包含匹配；英文/数字同样走包含
        pat = f"%{t}%"
        where_clauses.append(
            "("
            " i.relpath LIKE ? ESCAPE '\\' "
            " OR i.filename LIKE ? ESCAPE '\\' "
            " OR EXISTS (SELECT 1 FROM image_tags t WHERE t.relpath = i.relpath AND t.tag LIKE ? ESCAPE '\\') "
            ")"
        )
        args.extend([pat, pat, pat])
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    return where_sql, args

@app.get("/search")
def search(q: str = Query(..., description="模糊查询（标签/文件名/路径）"), limit: int = 10):
    terms = _split_terms(q)
    where_sql, args = _build_like_where_and_args(terms)

    with db() as conn:
        # 提前保证 filename 列存在
        try:
            conn.execute("ALTER TABLE images ADD COLUMN filename TEXT")
        except Exception:
            pass
        # 回填 filename
        conn.execute(
            "UPDATE images SET filename = COALESCE(filename, substr(relpath, instr(reverse('/'||relpath), '/')+1)) "
            "WHERE filename IS NULL OR filename=''"
        )

        sql = f"""
            SELECT i.relpath, i.cnt, i.avg
            FROM images i
            WHERE {where_sql}
            ORDER BY i.cnt ASC, i.avg DESC
            LIMIT ?
        """
        cur = conn.execute(sql, (*args, limit))
        rows = [{"relpath": r[0], "cnt": r[1], "avg": r[2]} for r in cur.fetchall()]
    return {"q": q, "items": rows}


