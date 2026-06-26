from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional
import os, jwt
from datetime import date, timedelta, datetime, timezone
from sqlalchemy import text
from deep_translator import GoogleTranslator, MyMemoryTranslator
import requests as http_requests
from database import init_db, get_conn, IS_PG, hash_pw, check_pw, _create_default_topics

FRONTEND   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "index.html")
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-on-railway")
JWT_EXP_DAYS = 30

app = FastAPI(title="English Words API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
security = HTTPBearer(auto_error=False)


# ── Auth helpers ───────────────────────────────────────────────────────────────

def make_token(user_id: int, email: str, role: str) -> str:
    payload = {
        "sub": user_id, "email": email, "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXP_DAYS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def get_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        raise HTTPException(401, "Необходима авторизация")
    try:
        return jwt.decode(creds.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Токен истёк")
    except Exception:
        raise HTTPException(401, "Неверный токен")


# ── Models ─────────────────────────────────────────────────────────────────────

class RegisterIn(BaseModel):
    email: str
    password: str
    name: Optional[str] = None

class LoginIn(BaseModel):
    email: str
    password: str

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
    rating: int


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()


@app.get("/")
def frontend():
    if os.path.exists(FRONTEND):
        return FileResponse(FRONTEND)
    return {"status": "API running"}


# ── Auth endpoints ─────────────────────────────────────────────────────────────

@app.post("/auth/register", status_code=201)
def register(body: RegisterIn):
    if len(body.password) < 6:
        raise HTTPException(400, "Пароль минимум 6 символов")
    with get_conn() as conn:
        exists = conn.execute(
            text("SELECT id FROM users WHERE email=:e"), {"e": body.email.lower()}
        ).one_or_none()
        if exists:
            raise HTTPException(400, "Email уже зарегистрирован")
        ph = hash_pw(body.password)
        if IS_PG:
            row = conn.execute(text("""
                INSERT INTO users (email, password_hash, name, role)
                VALUES (:e, :ph, :n, 'user') RETURNING id, email, name, role
            """), {"e": body.email.lower(), "ph": ph, "n": body.name or body.email.split("@")[0]}).mappings().one()
            user = dict(row)
        else:
            conn.execute(text("""
                INSERT INTO users (email, password_hash, name, role)
                VALUES (:e, :ph, :n, 'user')
            """), {"e": body.email.lower(), "ph": ph, "n": body.name or body.email.split("@")[0]})
            uid = conn.execute(text("SELECT last_insert_rowid()")).scalar()
            user = {"id": uid, "email": body.email.lower(), "name": body.name, "role": "user"}
        conn.commit()
        _create_default_topics(conn, user["id"])
    token = make_token(user["id"], user["email"], user["role"])
    return {"token": token, "user": {k: user[k] for k in ("id","email","name","role")}}


@app.post("/auth/login")
def login(body: LoginIn):
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT id, email, name, role, password_hash FROM users WHERE email=:e"),
            {"e": body.email.lower()}
        ).mappings().one_or_none()
    if not row or not check_pw(body.password, row["password_hash"]):
        raise HTTPException(401, "Неверный email или пароль")
    token = make_token(row["id"], row["email"], row["role"])
    return {"token": token, "user": {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"]}}


@app.get("/auth/me")
def me(u=Depends(get_user)):
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT id, email, name, role, created_at FROM users WHERE id=:id"), {"id": u["sub"]}
        ).mappings().one_or_none()
    if not row:
        raise HTTPException(404, "Not found")
    return dict(row)


# ── Topics ─────────────────────────────────────────────────────────────────────

@app.get("/topics")
def list_topics(u=Depends(get_user)):
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT t.id, t.name, t.color, t.created_at, COUNT(w.id) as word_count
            FROM topics t LEFT JOIN words w ON w.topic_id = t.id
            WHERE t.user_id=:uid
            GROUP BY t.id, t.name, t.color, t.created_at
            ORDER BY t.name
        """), {"uid": u["sub"]}).mappings().all()
    return [dict(r) for r in rows]


@app.post("/topics", status_code=201)
def create_topic(body: TopicIn, u=Depends(get_user)):
    with get_conn() as conn:
        try:
            row = conn.execute(
                text("INSERT INTO topics (user_id, name, color) VALUES (:uid,:name,:color) RETURNING *"),
                {"uid": u["sub"], "name": body.name, "color": body.color}
            ).mappings().one()
            conn.commit()
            return dict(row)
        except Exception as e:
            raise HTTPException(400, str(e))


@app.delete("/topics/{topic_id}", status_code=204)
def delete_topic(topic_id: int, u=Depends(get_user)):
    with get_conn() as conn:
        conn.execute(text("UPDATE words SET topic_id=NULL WHERE topic_id=:id AND user_id=:uid"), {"id": topic_id, "uid": u["sub"]})
        conn.execute(text("DELETE FROM topics WHERE id=:id AND user_id=:uid"), {"id": topic_id, "uid": u["sub"]})
        conn.commit()


# ── Words ──────────────────────────────────────────────────────────────────────

@app.get("/words")
def list_words(topic_id: Optional[int]=None, learned: Optional[bool]=None, q: Optional[str]=None, u=Depends(get_user)):
    conditions = ["w.user_id=:uid"]
    params: dict = {"uid": u["sub"]}
    if topic_id is not None:
        conditions.append("w.topic_id=:topic_id"); params["topic_id"] = topic_id
    if learned is not None:
        conditions.append("w.learned=:learned"); params["learned"] = 1 if learned else 0
    if q:
        conditions.append("(LOWER(w.word) LIKE :q OR LOWER(w.translation) LIKE :q)")
        params["q"] = f"%{q.lower()}%"
    with get_conn() as conn:
        rows = conn.execute(text(f"""
            SELECT w.*, t.name as topic_name, t.color as topic_color
            FROM words w LEFT JOIN topics t ON t.id=w.topic_id
            WHERE {' AND '.join(conditions)} ORDER BY w.id DESC
        """), params).mappings().all()
    return [dict(r) for r in rows]


@app.post("/words", status_code=201)
def create_word(body: WordIn, u=Depends(get_user)):
    with get_conn() as conn:
        row = conn.execute(text("""
            INSERT INTO words (user_id, word, translation, example, topic_id, learned)
            VALUES (:uid,:word,:translation,:example,:topic_id,:learned) RETURNING *
        """), {"uid": u["sub"], "word": body.word, "translation": body.translation,
               "example": body.example, "topic_id": body.topic_id,
               "learned": 1 if body.learned else 0}).mappings().one()
        conn.commit()
    return dict(row)


@app.patch("/words/{word_id}")
def update_word(word_id: int, body: WordUpdate, u=Depends(get_user)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "learned" in updates:
        updates["learned"] = 1 if updates["learned"] else 0
    if not updates:
        with get_conn() as conn:
            row = conn.execute(text("SELECT * FROM words WHERE id=:id AND user_id=:uid"),
                               {"id": word_id, "uid": u["sub"]}).mappings().one_or_none()
        return dict(row) if row else HTTPException(404)
    set_clause = ", ".join(f"{k}=:{k}" for k in updates)
    updates.update({"word_id": word_id, "uid": u["sub"]})
    with get_conn() as conn:
        conn.execute(text(f"UPDATE words SET {set_clause} WHERE id=:word_id AND user_id=:uid"), updates)
        conn.commit()
        row = conn.execute(text("""
            SELECT w.*, t.name as topic_name, t.color as topic_color
            FROM words w LEFT JOIN topics t ON t.id=w.topic_id WHERE w.id=:id
        """), {"id": word_id}).mappings().one()
    return dict(row)


@app.delete("/words/{word_id}", status_code=204)
def delete_word(word_id: int, u=Depends(get_user)):
    with get_conn() as conn:
        conn.execute(text("DELETE FROM words WHERE id=:id AND user_id=:uid"), {"id": word_id, "uid": u["sub"]})
        conn.commit()


# ── SRS ────────────────────────────────────────────────────────────────────────

def _sm2(rating, reps, ease, interval):
    quality = [0, 2, 3, 5][max(0, min(3, rating))]
    if quality < 2:
        reps = 0; interval = 1
    else:
        if reps == 0: interval = 1
        elif reps == 1: interval = 6
        else: interval = max(1, round(interval * ease))
        reps += 1
    ease = max(1.3, ease + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    next_review = (date.today() + timedelta(days=interval)).isoformat()
    return reps, round(ease, 3), interval, next_review


def _sk(uid, key): return f"u{uid}_{key}"

def _get_s(conn, uid, key, default=""):
    row = conn.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": _sk(uid,key)}).one_or_none()
    return row[0] if row else default

def _set_s(conn, uid, key, value):
    k = _sk(uid, key)
    if IS_PG:
        conn.execute(text("INSERT INTO settings(key,value) VALUES(:k,:v) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"), {"k":k,"v":str(value)})
    else:
        conn.execute(text("INSERT OR REPLACE INTO settings(key,value) VALUES(:k,:v)"), {"k":k,"v":str(value)})

def _update_streak(conn, uid):
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last_date   = _get_s(conn, uid, "last_study_date")
    streak      = int(_get_s(conn, uid, "streak", "0"))
    today_count = int(_get_s(conn, uid, "today_reviewed", "0"))
    daily_goal  = int(_get_s(conn, uid, "daily_goal", "10"))
    if last_date == today:
        today_count += 1
    else:
        if last_date == yesterday and today_count >= daily_goal:
            streak += 1
        elif last_date != yesterday:
            streak = 0
        _set_s(conn, uid, "streak", streak)
        today_count = 1
        _set_s(conn, uid, "last_study_date", today)
    _set_s(conn, uid, "today_reviewed", today_count)


@app.get("/words/due")
def words_due(u=Depends(get_user)):
    today = date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT w.*, t.name as topic_name, t.color as topic_color
            FROM words w LEFT JOIN topics t ON t.id=w.topic_id
            WHERE w.user_id=:uid AND (w.next_review IS NULL OR w.next_review<=:today)
            ORDER BY COALESCE(w.next_review,'0000-00-00') ASC LIMIT 50
        """), {"uid": u["sub"], "today": today}).mappings().all()
    return [dict(r) for r in rows]


@app.post("/words/{word_id}/review")
def review_word(word_id: int, body: ReviewIn, u=Depends(get_user)):
    uid = u["sub"]
    with get_conn() as conn:
        w = conn.execute(text("SELECT * FROM words WHERE id=:id AND user_id=:uid"), {"id":word_id,"uid":uid}).mappings().one_or_none()
        if not w: raise HTTPException(404)
        reps, ease, interval, nr = _sm2(body.rating, w.get("srs_reps") or 0, w.get("ease_factor") or 2.5, w.get("srs_interval") or 1)
        conn.execute(text("UPDATE words SET srs_reps=:r,ease_factor=:e,srs_interval=:i,next_review=:nr WHERE id=:id"),
                     {"r":reps,"e":ease,"i":interval,"nr":nr,"id":word_id})
        _update_streak(conn, uid)
        conn.commit()
        return {"next_review": nr, "interval": interval,
                "today_reviewed": int(_get_s(conn,uid,"today_reviewed","0")),
                "streak": int(_get_s(conn,uid,"streak","0")),
                "daily_goal": int(_get_s(conn,uid,"daily_goal","10"))}


@app.get("/streak")
def get_streak(u=Depends(get_user)):
    uid = u["sub"]; today = date.today().isoformat()
    with get_conn() as conn:
        streak      = int(_get_s(conn,uid,"streak","0"))
        today_count = int(_get_s(conn,uid,"today_reviewed","0"))
        daily_goal  = int(_get_s(conn,uid,"daily_goal","10"))
        last_date   = _get_s(conn,uid,"last_study_date")
        if last_date and last_date != today: today_count = 0
        due = conn.execute(text("SELECT COUNT(*) FROM words WHERE user_id=:uid AND (next_review IS NULL OR next_review<=:t)"),
                           {"uid":uid,"t":today}).scalar()
    return {"streak":streak,"today_reviewed":today_count,"daily_goal":daily_goal,"due_count":due}


# ── Translate / Example ────────────────────────────────────────────────────────

@app.get("/translate")
def translate(text_: str="", text: str=""):
    val = (text_ or text).strip()
    if not val: return {"translation": ""}
    try:
        r = GoogleTranslator(source="en", target="ru").translate(val)
        if r: return {"translation": r}
    except Exception: pass
    try:
        r = MyMemoryTranslator(source="en-US", target="ru-RU").translate(val)
        return {"translation": r or ""}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/example")
def get_example(word: str=""):
    if not word.strip(): return {"example": ""}
    try:
        r = http_requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word.strip()}", timeout=5)
        if r.ok:
            for entry in r.json():
                for m in entry.get("meanings", []):
                    for d in m.get("definitions", []):
                        if d.get("example"): return {"example": d["example"]}
    except Exception: pass
    return {"example": ""}


# ── Stats ──────────────────────────────────────────────────────────────────────

@app.get("/stats")
def stats(u=Depends(get_user)):
    uid = u["sub"]
    with get_conn() as conn:
        total   = conn.execute(text("SELECT COUNT(*) FROM words WHERE user_id=:uid"), {"uid":uid}).scalar()
        learned = conn.execute(text("SELECT COUNT(*) FROM words WHERE user_id=:uid AND learned=1"), {"uid":uid}).scalar()
        topics  = conn.execute(text("SELECT COUNT(*) FROM topics WHERE user_id=:uid"), {"uid":uid}).scalar()
    return {"total":total,"learned":learned,"not_learned":total-learned,"topics":topics}


# ── Admin: list users (CEO only) ───────────────────────────────────────────────

@app.get("/admin/users")
def list_users(u=Depends(get_user)):
    if u.get("role") not in ("ceo", "admin"):
        raise HTTPException(403, "Нет доступа")
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT u.id, u.email, u.name, u.role, u.created_at,
                   COUNT(w.id) as word_count
            FROM users u LEFT JOIN words w ON w.user_id=u.id
            GROUP BY u.id ORDER BY u.created_at
        """)).mappings().all()
    return [dict(r) for r in rows]
