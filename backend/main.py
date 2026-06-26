from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import os
from datetime import date, timedelta
from sqlalchemy import text
from deep_translator import GoogleTranslator, MyMemoryTranslator
import requests as http_requests
from database import init_db, get_conn, engine, IS_PG

FRONTEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "index.html")

app = FastAPI(title="English Words API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    init_db()


@app.get("/")
def frontend():
    if os.path.exists(FRONTEND):
        return FileResponse(FRONTEND)
    return {"status": "API running"}


# ── Models ─────────────────────────────────────────────────────────────────────

class TopicIn(BaseModel):
    name: str
    color: Optional[str] = "#6366f1"

class WordIn(BaseModel):
    word: str
    translation: Optional[str] = None
    example: Optional[str] = None
    topic_id: Optional[int] = None
    learned: Optional[bool] = False

class WordUpdate(BaseModel):
    word: Optional[str] = None
    translation: Optional[str] = None
    example: Optional[str] = None
    topic_id: Optional[int] = None
    learned: Optional[bool] = None

class ReviewIn(BaseModel):
    rating: int  # 0=Again, 1=Hard, 2=Good, 3=Easy


# ── Topics ─────────────────────────────────────────────────────────────────────

@app.get("/topics")
def list_topics():
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT t.id, t.name, t.color, t.created_at, COUNT(w.id) as word_count
            FROM topics t LEFT JOIN words w ON w.topic_id = t.id
            GROUP BY t.id, t.name, t.color, t.created_at
            ORDER BY t.name
        """)).mappings().all()
    return [dict(r) for r in rows]


@app.post("/topics", status_code=201)
def create_topic(body: TopicIn):
    with get_conn() as conn:
        try:
            row = conn.execute(
                text("INSERT INTO topics (name, color) VALUES (:name, :color) RETURNING *"),
                {"name": body.name, "color": body.color}
            ).mappings().one()
            conn.commit()
            return dict(row)
        except Exception as e:
            raise HTTPException(400, str(e))


@app.delete("/topics/{topic_id}", status_code=204)
def delete_topic(topic_id: int):
    with get_conn() as conn:
        conn.execute(text("UPDATE words SET topic_id = NULL WHERE topic_id = :id"), {"id": topic_id})
        conn.execute(text("DELETE FROM topics WHERE id = :id"), {"id": topic_id})
        conn.commit()


# ── Words ──────────────────────────────────────────────────────────────────────

@app.get("/words")
def list_words(topic_id: Optional[int] = None, learned: Optional[bool] = None, q: Optional[str] = None):
    conditions = ["1=1"]
    params = {}
    if topic_id is not None:
        conditions.append("w.topic_id = :topic_id")
        params["topic_id"] = topic_id
    if learned is not None:
        conditions.append("w.learned = :learned")
        params["learned"] = 1 if learned else 0
    if q:
        conditions.append("(LOWER(w.word) LIKE :q OR LOWER(w.translation) LIKE :q)")
        params["q"] = f"%{q.lower()}%"

    query = f"""
        SELECT w.*, t.name as topic_name, t.color as topic_color
        FROM words w LEFT JOIN topics t ON t.id = w.topic_id
        WHERE {" AND ".join(conditions)}
        ORDER BY w.id DESC
    """
    with get_conn() as conn:
        rows = conn.execute(text(query), params).mappings().all()
    return [dict(r) for r in rows]


@app.post("/words", status_code=201)
def create_word(body: WordIn):
    with get_conn() as conn:
        row = conn.execute(
            text("""INSERT INTO words (word, translation, example, topic_id, learned)
                    VALUES (:word, :translation, :example, :topic_id, :learned) RETURNING *"""),
            {
                "word": body.word,
                "translation": body.translation,
                "example": body.example,
                "topic_id": body.topic_id,
                "learned": 1 if body.learned else 0,
            }
        ).mappings().one()
        conn.commit()
    return dict(row)


@app.patch("/words/{word_id}")
def update_word(word_id: int, body: WordUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "learned" in updates:
        updates["learned"] = 1 if updates["learned"] else 0
    if not updates:
        with get_conn() as conn:
            row = conn.execute(text("SELECT * FROM words WHERE id = :id"), {"id": word_id}).mappings().one_or_none()
        if not row:
            raise HTTPException(404, "Not found")
        return dict(row)

    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["word_id"] = word_id
    with get_conn() as conn:
        conn.execute(text(f"UPDATE words SET {set_clause} WHERE id = :word_id"), updates)
        conn.commit()
        row = conn.execute(text("""
            SELECT w.*, t.name as topic_name, t.color as topic_color
            FROM words w LEFT JOIN topics t ON t.id = w.topic_id WHERE w.id = :id
        """), {"id": word_id}).mappings().one()
    return dict(row)


@app.delete("/words/{word_id}", status_code=204)
def delete_word(word_id: int):
    with get_conn() as conn:
        conn.execute(text("DELETE FROM words WHERE id = :id"), {"id": word_id})
        conn.commit()


# ── SRS ────────────────────────────────────────────────────────────────────────

def _sm2(rating: int, reps: int, ease: float, interval: int):
    """SM-2 algorithm. rating: 0=Again,1=Hard,2=Good,3=Easy"""
    quality = [0, 2, 3, 5][max(0, min(3, rating))]
    if quality < 2:          # Again — reset
        reps = 0
        interval = 1
    else:
        if reps == 0:        interval = 1
        elif reps == 1:      interval = 6
        else:                interval = max(1, round(interval * ease))
        reps += 1
    ease = max(1.3, ease + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    next_review = (date.today() + timedelta(days=interval)).isoformat()
    return reps, round(ease, 3), interval, next_review


def _get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": key}).one_or_none()
    return row[0] if row else default


def _set_setting(conn, key: str, value):
    if IS_PG:
        conn.execute(text("""
            INSERT INTO settings (key, value) VALUES (:k, :v)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """), {"k": key, "v": str(value)})
    else:
        conn.execute(text("""
            INSERT OR REPLACE INTO settings (key, value) VALUES (:k, :v)
        """), {"k": key, "v": str(value)})


def _update_streak(conn):
    """Called after each review. Updates streak + today_reviewed counter."""
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    last_date    = _get_setting(conn, "last_study_date", "")
    streak       = int(_get_setting(conn, "streak", "0"))
    today_count  = int(_get_setting(conn, "today_reviewed", "0"))
    daily_goal   = int(_get_setting(conn, "daily_goal", "10"))

    if last_date == today:
        today_count += 1
    else:
        # New day — check if yesterday's goal was met to continue streak
        if last_date == yesterday and today_count >= daily_goal:
            streak += 1
        elif last_date != yesterday:
            streak = 0   # missed a day
        _set_setting(conn, "streak", streak)
        today_count = 1
        _set_setting(conn, "last_study_date", today)

    _set_setting(conn, "today_reviewed", today_count)


@app.get("/words/due")
def words_due():
    today = date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT w.*, t.name as topic_name, t.color as topic_color
            FROM words w LEFT JOIN topics t ON t.id = w.topic_id
            WHERE w.next_review IS NULL OR w.next_review <= :today
            ORDER BY COALESCE(w.next_review, '0000-00-00') ASC
            LIMIT 50
        """), {"today": today}).mappings().all()
    return [dict(r) for r in rows]


@app.post("/words/{word_id}/review")
def review_word(word_id: int, body: ReviewIn):
    with get_conn() as conn:
        w = conn.execute(text("SELECT * FROM words WHERE id=:id"), {"id": word_id}).mappings().one_or_none()
        if not w:
            raise HTTPException(404, "Not found")
        reps, ease, interval, next_review = _sm2(
            body.rating,
            w.get("srs_reps") or 0,
            w.get("ease_factor") or 2.5,
            w.get("srs_interval") or 1,
        )
        conn.execute(text("""
            UPDATE words SET srs_reps=:reps, ease_factor=:ease,
                             srs_interval=:interval, next_review=:nr
            WHERE id=:id
        """), {"reps": reps, "ease": ease, "interval": interval, "nr": next_review, "id": word_id})
        _update_streak(conn)
        conn.commit()
        today_reviewed = int(_get_setting(conn, "today_reviewed", "0"))
        streak         = int(_get_setting(conn, "streak", "0"))
        daily_goal     = int(_get_setting(conn, "daily_goal", "10"))
    return {
        "next_review": next_review,
        "interval": interval,
        "today_reviewed": today_reviewed,
        "streak": streak,
        "daily_goal": daily_goal,
    }


@app.get("/streak")
def get_streak():
    today = date.today().isoformat()
    with get_conn() as conn:
        streak        = int(_get_setting(conn, "streak", "0"))
        today_count   = int(_get_setting(conn, "today_reviewed", "0"))
        daily_goal    = int(_get_setting(conn, "daily_goal", "10"))
        last_date     = _get_setting(conn, "last_study_date", "")
        # Reset today counter if it's a new day
        if last_date and last_date != today:
            today_count = 0
        due_count = conn.execute(text(
            "SELECT COUNT(*) FROM words WHERE next_review IS NULL OR next_review <= :today"
        ), {"today": today}).scalar()
    return {
        "streak": streak,
        "today_reviewed": today_count,
        "daily_goal": daily_goal,
        "due_count": due_count,
    }


# ── Translate ──────────────────────────────────────────────────────────────────

@app.get("/translate")
def translate(text_: str = "", text: str = ""):
    val = (text_ or text).strip()
    if not val:
        return {"translation": ""}
    try:
        result = GoogleTranslator(source="en", target="ru").translate(val)
        if result:
            return {"translation": result}
    except Exception:
        pass
    try:
        result = MyMemoryTranslator(source="en-US", target="ru-RU").translate(val)
        return {"translation": result or ""}
    except Exception as e:
        raise HTTPException(500, f"Ошибка перевода: {e}")


@app.get("/example")
def get_example(word: str = ""):
    if not word.strip():
        return {"example": ""}
    try:
        r = http_requests.get(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{word.strip()}",
            timeout=5
        )
        if r.ok:
            data = r.json()
            for entry in data:
                for meaning in entry.get("meanings", []):
                    for defn in meaning.get("definitions", []):
                        ex = defn.get("example", "")
                        if ex:
                            return {"example": ex}
    except Exception:
        pass
    return {"example": ""}


# ── Stats ──────────────────────────────────────────────────────────────────────

@app.get("/stats")
def stats():
    with get_conn() as conn:
        total   = conn.execute(text("SELECT COUNT(*) FROM words")).scalar()
        learned = conn.execute(text("SELECT COUNT(*) FROM words WHERE learned = 1")).scalar()
        topics  = conn.execute(text("SELECT COUNT(*) FROM topics")).scalar()
    return {"total": total, "learned": learned, "not_learned": total - learned, "topics": topics}
