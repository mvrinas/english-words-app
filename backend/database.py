import os
from sqlalchemy import create_engine, text

_url = os.environ.get("DATABASE_URL", "")
if _url.startswith("postgres://"):
    _url = _url.replace("postgres://", "postgresql://", 1)

if not _url:
    _db_path = os.environ.get(
        "WORDS_DB",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "words.db")
    )
    _url = f"sqlite:///{_db_path}"

IS_PG = "postgresql" in _url
engine = create_engine(_url, connect_args={"check_same_thread": False} if not IS_PG else {})


def get_conn():
    return engine.connect()


def _add_col(conn, table, col, col_type):
    """Add column safely (works for both PG and SQLite)."""
    if IS_PG:
        try:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"))
        except Exception:
            pass
    else:
        try:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
        except Exception:
            pass  # already exists


def init_db():
    with engine.connect() as conn:
        # ── Topics ────────────────────────────────────────────────────────────
        if IS_PG:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS topics (
                    id    SERIAL PRIMARY KEY,
                    name  TEXT NOT NULL UNIQUE,
                    color TEXT NOT NULL DEFAULT '#6366f1',
                    created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
                )
            """))
        else:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS topics (
                    id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    name  TEXT NOT NULL UNIQUE,
                    color TEXT NOT NULL DEFAULT '#6366f1',
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """))

        # ── Words ─────────────────────────────────────────────────────────────
        if IS_PG:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS words (
                    id          SERIAL PRIMARY KEY,
                    word        TEXT NOT NULL,
                    translation TEXT,
                    example     TEXT,
                    topic_id    INTEGER REFERENCES topics(id) ON DELETE SET NULL,
                    learned     INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT DEFAULT (CURRENT_TIMESTAMP)
                )
            """))
        else:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS words (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    word        TEXT NOT NULL,
                    translation TEXT,
                    example     TEXT,
                    topic_id    INTEGER REFERENCES topics(id) ON DELETE SET NULL,
                    learned     INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT DEFAULT (datetime('now'))
                )
            """))

        # ── SRS columns migration ─────────────────────────────────────────────
        _add_col(conn, "words", "next_review",   "TEXT")
        _add_col(conn, "words", "srs_interval",  "INTEGER DEFAULT 1")
        _add_col(conn, "words", "ease_factor",   "REAL DEFAULT 2.5")
        _add_col(conn, "words", "srs_reps",      "INTEGER DEFAULT 0")

        # ── Settings (streak etc.) ────────────────────────────────────────────
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """))

        # ── Default topics ────────────────────────────────────────────────────
        count = conn.execute(text("SELECT COUNT(*) FROM topics")).scalar()
        if count == 0:
            conn.execute(text(
                "INSERT INTO topics (name, color) VALUES (:n, :c)"
            ), [
                {"n": "Общие слова", "c": "#6366f1"},
                {"n": "Путешествия",  "c": "#f59e0b"},
                {"n": "Бизнес",       "c": "#10b981"},
                {"n": "Технологии",   "c": "#3b82f6"},
            ])
        conn.commit()
