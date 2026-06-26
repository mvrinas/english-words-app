import os
import bcrypt
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

CEO_EMAIL    = os.environ.get("CEO_EMAIL", "032disk@gmail.com")
CEO_PASSWORD = os.environ.get("CEO_PASSWORD", "wordsapp_ceo_2024")


def get_conn():
    return engine.connect()


def _add_col(conn, table, col, col_type):
    if IS_PG:
        try:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"))
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass
    else:
        try:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass


def hash_pw(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_pw(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def _create_default_topics(conn, user_id: int):
    defaults = [
        ("Общие слова", "#6366f1"), ("Путешествия", "#f59e0b"),
        ("Бизнес", "#10b981"),      ("Технологии",  "#3b82f6"),
    ]
    for name, color in defaults:
        try:
            conn.execute(
                text("INSERT INTO topics (user_id, name, color) VALUES (:uid, :n, :c)"),
                {"uid": user_id, "n": name, "c": color}
            )
        except Exception:
            pass
    conn.commit()


def init_db():
    with engine.connect() as conn:

        # ── Users ─────────────────────────────────────────────────────────────
        if IS_PG:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS users (
                    id            SERIAL PRIMARY KEY,
                    email         TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    name          TEXT,
                    role          TEXT NOT NULL DEFAULT 'user',
                    level         TEXT DEFAULT NULL,
                    created_at    TEXT DEFAULT (CURRENT_TIMESTAMP)
                )
            """))
        else:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    email         TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    name          TEXT,
                    role          TEXT NOT NULL DEFAULT 'user',
                    level         TEXT DEFAULT NULL,
                    created_at    TEXT DEFAULT (datetime('now'))
                )
            """))

        # ── Topics ────────────────────────────────────────────────────────────
        if IS_PG:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS topics (
                    id         SERIAL PRIMARY KEY,
                    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    name       TEXT NOT NULL,
                    color      TEXT NOT NULL DEFAULT '#6366f1',
                    created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
                )
            """))
        else:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS topics (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    name       TEXT NOT NULL,
                    color      TEXT NOT NULL DEFAULT '#6366f1',
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """))

        # ── Words ─────────────────────────────────────────────────────────────
        if IS_PG:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS words (
                    id           SERIAL PRIMARY KEY,
                    user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    word         TEXT NOT NULL,
                    translation  TEXT,
                    example      TEXT,
                    topic_id     INTEGER REFERENCES topics(id) ON DELETE SET NULL,
                    learned      INTEGER NOT NULL DEFAULT 0,
                    next_review  TEXT,
                    srs_interval INTEGER DEFAULT 1,
                    ease_factor  REAL DEFAULT 2.5,
                    srs_reps     INTEGER DEFAULT 0,
                    level        TEXT DEFAULT NULL,
                    created_at   TEXT DEFAULT (CURRENT_TIMESTAMP)
                )
            """))
        else:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS words (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    word         TEXT NOT NULL,
                    translation  TEXT,
                    example      TEXT,
                    topic_id     INTEGER REFERENCES topics(id) ON DELETE SET NULL,
                    learned      INTEGER NOT NULL DEFAULT 0,
                    next_review  TEXT,
                    srs_interval INTEGER DEFAULT 1,
                    ease_factor  REAL DEFAULT 2.5,
                    srs_reps     INTEGER DEFAULT 0,
                    level        TEXT DEFAULT NULL,
                    created_at   TEXT DEFAULT (datetime('now'))
                )
            """))

        # ── Settings ──────────────────────────────────────────────────────────
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """))

        # ── Column migrations ─────────────────────────────────────────────────
        _add_col(conn, "topics", "user_id",      "INTEGER")
        _add_col(conn, "words",  "user_id",      "INTEGER")
        _add_col(conn, "words",  "next_review",  "TEXT")
        _add_col(conn, "words",  "srs_interval", "INTEGER DEFAULT 1")
        _add_col(conn, "words",  "ease_factor",  "REAL DEFAULT 2.5")
        _add_col(conn, "words",  "srs_reps",     "INTEGER DEFAULT 0")
        _add_col(conn, "users",  "level",        "TEXT DEFAULT NULL")
        _add_col(conn, "words",  "level",         "TEXT DEFAULT NULL")
        conn.commit()

        # ── CEO account ───────────────────────────────────────────────────────
        ceo = conn.execute(
            text("SELECT id FROM users WHERE email=:e"), {"e": CEO_EMAIL}
        ).one_or_none()

        if not ceo:
            ph = hash_pw(CEO_PASSWORD)
            if IS_PG:
                ceo_row = conn.execute(text("""
                    INSERT INTO users (email, password_hash, name, role)
                    VALUES (:e, :ph, 'Marina K', 'ceo') RETURNING id
                """), {"e": CEO_EMAIL, "ph": ph}).one()
                ceo_id = ceo_row[0]
            else:
                conn.execute(text("""
                    INSERT INTO users (email, password_hash, name, role)
                    VALUES (:e, :ph, 'Marina K', 'ceo')
                """), {"e": CEO_EMAIL, "ph": ph})
                ceo_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
            conn.commit()
            _create_default_topics(conn, ceo_id)
        else:
            ceo_id = ceo[0]

        # Update CEO name if it was set to old default
        conn.execute(text("UPDATE users SET name='Marina K' WHERE email=:e AND name='Marina (CEO)'"), {"e": CEO_EMAIL})

        # Assign pre-auth orphan records to CEO
        conn.execute(text("UPDATE topics SET user_id=:u WHERE user_id IS NULL"), {"u": ceo_id})
        conn.execute(text("UPDATE words  SET user_id=:u WHERE user_id IS NULL"), {"u": ceo_id})
        conn.commit()
