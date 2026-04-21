from config import DB_PATH
from infra.db import db

LATEST_SCHEMA_VERSION = 2


def migrate():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_ts INTEGER NOT NULL)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id TEXT PRIMARY KEY,
                relpath TEXT NOT NULL UNIQUE,
                category TEXT,
                filename TEXT,
                cnt INTEGER NOT NULL DEFAULT 0,
                sum REAL NOT NULL DEFAULT 0.0,
                avg REAL NOT NULL DEFAULT 0.0,
                last_ts INTEGER
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                rid INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id TEXT NOT NULL,
                score REAL NOT NULL,
                note TEXT,
                ts INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS image_tags(
                relpath TEXT NOT NULL,
                tag TEXT NOT NULL,
                tag_lc TEXT NOT NULL,
                PRIMARY KEY(relpath, tag)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_relpath ON images(relpath)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_cnt ON images(cnt)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_images_cat ON images(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_image_tags_tag_lc ON image_tags(tag_lc)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_image_tags_relpath ON image_tags(relpath)")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_ts) VALUES (?, strftime('%s','now'))",
            (LATEST_SCHEMA_VERSION,),
        )
        conn.commit()
