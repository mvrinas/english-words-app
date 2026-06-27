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
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None

class LoginIn(BaseModel):
    email: str
    password: str

class ProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    bio: Optional[str] = None
    is_public: Optional[bool] = None

class MessageIn(BaseModel):
    receiver_id: int
    content: str

class TopicIn(BaseModel):
    name: str
    color: Optional[str] = "#6366f1"

class WordIn(BaseModel):
    word: str
    translation: Optional[str] = None
    example: Optional[str] = None
    topic_id: Optional[int] = None
    learned: Optional[bool] = False
    level: Optional[str] = None

class WordUpdate(BaseModel):
    word: Optional[str] = None
    translation: Optional[str] = None
    example: Optional[str] = None
    topic_id: Optional[int] = None
    learned: Optional[bool] = None
    level: Optional[str] = None

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
        # Validate / generate username
        import re
        uname = (body.username or body.email.split("@")[0]).lower()
        uname = re.sub(r"[^a-z0-9_]", "", uname)[:30] or "user"
        # Ensure unique
        base = uname; n = 1
        while conn.execute(text("SELECT id FROM users WHERE username=:u"), {"u": uname}).one_or_none():
            uname = f"{base}{n}"; n += 1
        if IS_PG:
            row = conn.execute(text("""
                INSERT INTO users (email, password_hash, name, role, username, first_name, last_name)
                VALUES (:e, :ph, :n, 'user', :u, :fn, :ln) RETURNING id, email, name, role, username
            """), {"e": body.email.lower(), "ph": ph,
                  "n": body.name or body.email.split("@")[0],
                  "u": uname, "fn": body.first_name, "ln": body.last_name}).mappings().one()
            user = dict(row)
        else:
            conn.execute(text("""
                INSERT INTO users (email, password_hash, name, role, username, first_name, last_name)
                VALUES (:e, :ph, :n, 'user', :u, :fn, :ln)
            """), {"e": body.email.lower(), "ph": ph,
                  "n": body.name or body.email.split("@")[0],
                  "u": uname, "fn": body.first_name, "ln": body.last_name})
            uid = conn.execute(text("SELECT last_insert_rowid()")).scalar()
            user = {"id": uid, "email": body.email.lower(), "name": body.name, "role": "user", "level": None, "username": uname}
        conn.commit()
        _create_default_topics(conn, user["id"])
    _send_otp(body.email.lower(), "register")
    return {"needs_otp": True, "email": body.email.lower(), "needs_level": True}


@app.post("/auth/login")
def login(body: LoginIn):
    with get_conn() as conn:
        try:
            row = conn.execute(
                text("SELECT id, email, name, role, level, password_hash FROM users WHERE email=:e"),
                {"e": body.email.lower()}
            ).mappings().one_or_none()
        except Exception:
            conn.rollback()
            row = conn.execute(
                text("SELECT id, email, name, role, password_hash FROM users WHERE email=:e"),
                {"e": body.email.lower()}
            ).mappings().one_or_none()
    if not row or not check_pw(body.password, row["password_hash"]):
        raise HTTPException(401, "Неверный email или пароль")
    # Send OTP
    _send_otp(body.email.lower(), "login")
    return {"needs_otp": True, "email": body.email.lower()}


@app.get("/auth/me")
def me(u=Depends(get_user)):
    with get_conn() as conn:
        try:
            row = conn.execute(
                text("SELECT id, email, name, role, level, username, first_name, last_name, bio, is_public, created_at FROM users WHERE id=:id"), {"id": u["sub"]}
            ).mappings().one_or_none()
        except Exception:
            conn.rollback()
            row = conn.execute(
                text("SELECT id, email, name, role, created_at FROM users WHERE id=:id"), {"id": u["sub"]}
            ).mappings().one_or_none()
    if not row:
        raise HTTPException(404, "Not found")
    result = dict(row)
    result.setdefault("level", None)
    return result

class LevelIn(BaseModel):
    level: str

@app.patch("/auth/level")
def set_level(body: LevelIn, u=Depends(get_user)):
    lvl = body.level.strip().upper()
    if lvl not in ("A1","A2","B1","B2","C1","C2"):
        raise HTTPException(400, "Неверный уровень")
    with get_conn() as conn:
        try:
            conn.execute(text("UPDATE users SET level=:l WHERE id=:id"), {"l": lvl, "id": u["sub"]})
            conn.commit()
        except Exception:
            conn.rollback()
            raise HTTPException(500, "Не удалось сохранить уровень")
    return {"level": lvl}


# ── Social endpoints ──────────────────────────────────────────────────────────

import re as _re

@app.get("/users/check-username")
def check_username(username: str):
    uname = username.lower().strip()
    if not _re.match(r"^[a-z0-9_]{3,30}$", uname):
        return {"available": False, "reason": "3-30 символов, только a-z 0-9 _"}
    with get_conn() as conn:
        exists = conn.execute(text("SELECT id FROM users WHERE username=:u"), {"u": uname}).one_or_none()
    return {"available": not exists}


@app.patch("/profile")
def update_profile(body: ProfileUpdate, u=Depends(get_user)):
    uid = u["sub"]
    updates = {}
    if body.first_name is not None: updates["first_name"] = body.first_name.strip()
    if body.last_name  is not None: updates["last_name"]  = body.last_name.strip()
    if body.bio        is not None: updates["bio"]        = body.bio.strip()[:300]
    if body.is_public  is not None: updates["is_public"]  = 1 if body.is_public else 0
    if body.username   is not None:
        uname = body.username.lower().strip()
        if not _re.match(r"^[a-z0-9_]{3,30}$", uname):
            raise HTTPException(400, "Никнейм: 3-30 символов, только a-z 0-9 _")
        with get_conn() as conn:
            conflict = conn.execute(
                text("SELECT id FROM users WHERE username=:u AND id!=:id"), {"u": uname, "id": uid}
            ).one_or_none()
        if conflict:
            raise HTTPException(400, "Никнейм уже занят")
        updates["username"] = uname
    if not updates:
        raise HTTPException(400, "Нет данных для обновления")
    with get_conn() as conn:
        set_clause = ", ".join(f"{k}=:{k}" for k in updates)
        updates["uid"] = uid
        conn.execute(text(f"UPDATE users SET {set_clause} WHERE id=:uid"), updates)
        conn.commit()
        row = conn.execute(
            text("SELECT id, email, name, role, level, username, first_name, last_name, bio, is_public FROM users WHERE id=:id"),
            {"id": uid}
        ).mappings().one()
    return dict(row)


@app.get("/users/search")
def search_users(q: str = "", u=Depends(get_user)):
    if len(q) < 2:
        return []
    q_like = f"%{q.lower()}%"
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT id, name, username, first_name, last_name, bio, is_public
            FROM users
            WHERE (LOWER(username) LIKE :q OR LOWER(name) LIKE :q OR LOWER(first_name) LIKE :q OR LOWER(last_name) LIKE :q)
            AND id != :me
            LIMIT 20
        """), {"q": q_like, "me": u["sub"]}).mappings().all()
    return [dict(r) for r in rows]


@app.get("/users/{username}")
def get_user_profile(username: str, u=Depends(get_user)):
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT id, name, username, first_name, last_name, bio, is_public, created_at FROM users WHERE username=:u"),
            {"u": username.lower()}
        ).mappings().one_or_none()
        if not row:
            raise HTTPException(404, "Пользователь не найден")
        profile = dict(row)
        if not profile["is_public"] and profile["id"] != u["sub"]:
            raise HTTPException(403, "Профиль закрыт")
        # Friend status
        fs = conn.execute(text("""
            SELECT status, sender_id FROM friendships
            WHERE (sender_id=:me AND receiver_id=:them) OR (sender_id=:them AND receiver_id=:me)
        """), {"me": u["sub"], "them": profile["id"]}).mappings().one_or_none()
        profile["friend_status"] = None
        if fs:
            profile["friend_status"] = fs["status"]
            profile["i_sent"] = fs["sender_id"] == u["sub"]
        # Word count if public
        wc = conn.execute(text("SELECT COUNT(*) FROM words WHERE user_id=:id"), {"id": profile["id"]}).scalar()
        profile["word_count"] = wc
    return profile


@app.post("/friends/{user_id}")
def send_friend_request(user_id: int, u=Depends(get_user)):
    if user_id == u["sub"]:
        raise HTTPException(400, "Нельзя добавить себя")
    with get_conn() as conn:
        existing = conn.execute(text("""
            SELECT id, status FROM friendships
            WHERE (sender_id=:me AND receiver_id=:them) OR (sender_id=:them AND receiver_id=:me)
        """), {"me": u["sub"], "them": user_id}).one_or_none()
        if existing:
            if existing[1] == "accepted":
                raise HTTPException(400, "Уже друзья")
            # Accept if other sent request
            conn.execute(text("UPDATE friendships SET status='accepted' WHERE id=:id"), {"id": existing[0]})
            conn.commit()
            return {"status": "accepted"}
        conn.execute(text("INSERT INTO friendships (sender_id, receiver_id) VALUES (:me, :them)"),
                     {"me": u["sub"], "them": user_id})
        conn.commit()
        # Email notification to receiver
        try:
            sender = conn.execute(text("SELECT name, username FROM users WHERE id=:id"), {"id": u["sub"]}).one_or_none()
            receiver_email = conn.execute(text("SELECT email, name FROM users WHERE id=:id"), {"id": user_id}).one_or_none()
            if sender and receiver_email:
                sname = sender[1] and f"@{sender[1]}" or sender[0] or "Пользователь"
                html_notif = f"""<div style="font-family:sans-serif;max-width:400px;margin:0 auto;padding:32px">
                  <h2 style="font-size:22px;margin-bottom:8px">Новая заявка в друзья</h2>
                  <p style="color:#555">{sname} отправил(а) вам заявку в друзья в WordsApp.</p>
                  <p style="color:#888;font-size:13px;margin-top:20px">Войдите в приложение чтобы принять или отклонить.</p>
                </div>"""
                _send_email(receiver_email[0], f"{sname} хочет добавить вас в друзья — WordsApp", html_notif)
        except Exception as ex:
            print(f"Friend notify email error: {ex}")
    return {"status": "pending"}


@app.delete("/friends/{user_id}")
def remove_friend(user_id: int, u=Depends(get_user)):
    with get_conn() as conn:
        conn.execute(text("""
            DELETE FROM friendships
            WHERE (sender_id=:me AND receiver_id=:them) OR (sender_id=:them AND receiver_id=:me)
        """), {"me": u["sub"], "them": user_id})
        conn.commit()
    return {"ok": True}


@app.get("/friends")
def get_friends(u=Depends(get_user)):
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT u.id, u.name, u.username, u.first_name, u.last_name, f.status, f.sender_id
            FROM friendships f
            JOIN users u ON (u.id = CASE WHEN f.sender_id=:me THEN f.receiver_id ELSE f.sender_id END)
            WHERE f.sender_id=:me OR f.receiver_id=:me
        """), {"me": u["sub"]}).mappings().all()
    return [dict(r) for r in rows]


@app.post("/messages")
def send_message(body: MessageIn, u=Depends(get_user)):
    if not body.content.strip():
        raise HTTPException(400, "Пустое сообщение")
    with get_conn() as conn:
        conn.execute(text("INSERT INTO messages (sender_id, receiver_id, content) VALUES (:s, :r, :c)"),
                     {"s": u["sub"], "r": body.receiver_id, "c": body.content.strip()[:1000]})
        conn.commit()
        # Email notification to receiver (only if they have no unread from sender already)
        try:
            unread = conn.execute(text(
                "SELECT COUNT(*) FROM messages WHERE sender_id=:s AND receiver_id=:r AND is_read=0"
            ), {"s": u["sub"], "r": body.receiver_id}).scalar()
            if unread <= 1:  # first unread from this sender
                sender = conn.execute(text("SELECT name, username FROM users WHERE id=:id"), {"id": u["sub"]}).one_or_none()
                recv   = conn.execute(text("SELECT email, name FROM users WHERE id=:id"), {"id": body.receiver_id}).one_or_none()
                if sender and recv:
                    sname = sender[1] and f"@{sender[1]}" or sender[0] or "Пользователь"
                    preview = body.content.strip()[:80]
                    html_notif = f"""<div style="font-family:sans-serif;max-width:400px;margin:0 auto;padding:32px">
                      <h2 style="font-size:22px;margin-bottom:8px">Новое сообщение</h2>
                      <p style="color:#555">{sname} написал(а) вам:</p>
                      <div style="background:#f5f4f0;border-radius:12px;padding:16px;margin:16px 0;color:#1c1b18;font-style:italic">
                        &ldquo;{preview}&rdquo;
                      </div>
                      <p style="color:#888;font-size:13px">Войдите в WordsApp чтобы ответить.</p>
                    </div>"""
                    _send_email(recv[0], f"Сообщение от {sname} — WordsApp", html_notif)
        except Exception as ex:
            print(f"Message notify email error: {ex}")
    return {"ok": True}


@app.get("/messages/{user_id}")
def get_messages(user_id: int, u=Depends(get_user)):
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT id, sender_id, receiver_id, content, is_read, created_at
            FROM messages
            WHERE (sender_id=:me AND receiver_id=:them) OR (sender_id=:them AND receiver_id=:me)
            ORDER BY created_at ASC LIMIT 100
        """), {"me": u["sub"], "them": user_id}).mappings().all()
        # Mark as read
        conn.execute(text("UPDATE messages SET is_read=1 WHERE receiver_id=:me AND sender_id=:them AND is_read=0"),
                     {"me": u["sub"], "them": user_id})
        conn.commit()
    return [dict(r) for r in rows]


@app.get("/messages")
def get_all_conversations(u=Depends(get_user)):
    """Last message per conversation + unread count"""
    me = u["sub"]
    with get_conn() as conn:
        # Get all partner IDs this user has conversed with
        partner_rows = conn.execute(text("""
            SELECT DISTINCT CASE WHEN sender_id=:me THEN receiver_id ELSE sender_id END AS pid
            FROM messages WHERE sender_id=:me OR receiver_id=:me
        """), {"me": me}).mappings().all()
        result = []
        for pr in partner_rows:
            pid = pr["pid"]
            # Get last message and unread count for this conversation
            row = conn.execute(text("""
                SELECT u.id, u.name, u.username, u.first_name, u.last_name,
                       m.content AS last_msg, m.created_at,
                       (SELECT COUNT(*) FROM messages
                        WHERE receiver_id=:me AND sender_id=:pid AND is_read=0) AS unread
                FROM users u, messages m
                WHERE u.id=:pid
                  AND ((m.sender_id=:me AND m.receiver_id=:pid)
                       OR (m.sender_id=:pid AND m.receiver_id=:me))
                ORDER BY m.created_at DESC LIMIT 1
            """), {"me": me, "pid": pid}).mappings().one_or_none()
            if row:
                result.append(dict(row))
        result.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return result


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
            INSERT INTO words (user_id, word, translation, example, topic_id, learned, level)
            VALUES (:uid,:word,:translation,:example,:topic_id,:learned,:level) RETURNING *
        """), {"uid": u["sub"], "word": body.word, "translation": body.translation,
               "example": body.example, "topic_id": body.topic_id,
               "learned": 1 if body.learned else 0, "level": body.level}).mappings().one()
        uid = u["sub"]
        xp = int(_get_s(conn, uid, "xp", "0"))
        _set_s(conn, uid, "xp", xp + 5)
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

def _update_streak(conn, uid, xp_gain=10):
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last_date   = _get_s(conn, uid, "last_study_date")
    streak      = int(_get_s(conn, uid, "streak", "0"))
    today_count = int(_get_s(conn, uid, "today_reviewed", "0"))
    daily_goal  = int(_get_s(conn, uid, "daily_goal", "10"))
    xp          = int(_get_s(conn, uid, "xp", "0"))
    if last_date == today:
        today_count += 1
    else:
        if last_date == yesterday and today_count >= daily_goal:
            streak += 1
            xp += 20  # бонус за сохранение стрика
        elif last_date != yesterday:
            streak = 0
        _set_s(conn, uid, "streak", streak)
        today_count = 1
        _set_s(conn, uid, "last_study_date", today)
    xp += xp_gain
    _set_s(conn, uid, "xp", xp)
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
        xp = int(_get_s(conn,uid,"xp","0"))
        level = max(1, int(xp ** 0.5) // 3 + 1)
        return {"next_review": nr, "interval": interval,
                "today_reviewed": int(_get_s(conn,uid,"today_reviewed","0")),
                "streak": int(_get_s(conn,uid,"streak","0")),
                "daily_goal": int(_get_s(conn,uid,"daily_goal","10")),
                "xp": xp, "level": level}


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
    xp = int(_get_s(conn,uid,"xp","0"))
    level = max(1, int(xp ** 0.5) // 3 + 1)  # плавная кривая уровней
    xp_for_level = ((level - 1) * 3) ** 2
    xp_next = (level * 3) ** 2
    return {"streak":streak,"today_reviewed":today_count,"daily_goal":daily_goal,
            "due_count":due,"xp":xp,"level":level,
            "xp_for_level":xp_for_level,"xp_next":xp_next}


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


# ── Friend requests ───────────────────────────────────────────────────────────

@app.get("/friends/requests")
def get_friend_requests(u=Depends(get_user)):
    with get_conn() as conn:
        sent = conn.execute(text("""
            SELECT u.id, u.name, u.username, u.first_name, u.last_name, f.created_at
            FROM friendships f JOIN users u ON u.id=f.receiver_id
            WHERE f.sender_id=:me AND f.status='pending'
        """), {"me": u["sub"]}).mappings().all()
        received = conn.execute(text("""
            SELECT u.id, u.name, u.username, u.first_name, u.last_name, f.created_at, f.id as fid
            FROM friendships f JOIN users u ON u.id=f.sender_id
            WHERE f.receiver_id=:me AND f.status='pending'
        """), {"me": u["sub"]}).mappings().all()
    return {"sent": [dict(r) for r in sent], "received": [dict(r) for r in received]}


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


# ── Change password ────────────────────────────────────────────────────────────

class PasswordChange(BaseModel):
    current_password: str
    new_password: str

@app.post("/profile/change-password")
def change_password(body: PasswordChange, u=Depends(get_user)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "Новый пароль минимум 6 символов")
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT password_hash FROM users WHERE id=:id"), {"id": u["sub"]}
        ).one_or_none()
        if not row or not check_pw(body.current_password, row[0]):
            raise HTTPException(400, "Неверный текущий пароль")
        new_hash = hash_pw(body.new_password)
        conn.execute(
            text("UPDATE users SET password_hash=:h WHERE id=:id"),
            {"h": new_hash, "id": u["sub"]}
        )
        conn.commit()
    return {"ok": True}


# ── Password Reset via Email ───────────────────────────────────────────────────

import smtplib, random, string
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)


def _send_email(to: str, subject: str, html: str):
    if not SMTP_HOST or not SMTP_USER:
        print(f"[EMAIL DEV] To: {to}\nSubject: {subject}\n{html}")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"WordsApp <{SMTP_FROM}>"
    msg["To"]      = to
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_FROM, to, msg.as_string())


def _gen_code(length=6) -> str:
    return "".join(random.choices(string.digits, k=length))


def _send_otp(email: str, purpose: str):
    """Generate 4-digit OTP, store in DB, send via email."""
    from datetime import datetime, timezone, timedelta
    code = "".join(random.choices(string.digits, k=4))
    expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    with get_conn() as conn:
        # Invalidate previous unused codes for this email+purpose
        conn.execute(
            text("UPDATE email_otp SET used=1 WHERE email=:e AND purpose=:p AND used=0"),
            {"e": email, "p": purpose}
        )
        conn.execute(
            text("INSERT INTO email_otp (email, code, purpose, expires_at) VALUES (:e, :c, :p, :x)"),
            {"e": email, "c": code, "p": purpose, "x": expires}
        )
        conn.commit()
    html_body = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
        max-width:480px;margin:0 auto;padding:32px 24px;background:#f9f9fb">
      <div style="background:#fff;border-radius:16px;padding:32px;border:1px solid #e5e7eb">
        <div style="font-size:22px;font-weight:800;color:#111;margin-bottom:8px">WordsApp</div>
        <div style="font-size:15px;color:#6b7280;margin-bottom:28px">
          {'Подтверждение входа' if purpose == 'login' else 'Подтверждение регистрации'}
        </div>
        <div style="font-size:13px;color:#374151;margin-bottom:16px">Твой код подтверждения:</div>
        <div style="font-size:42px;font-weight:900;letter-spacing:12px;color:#4f46e5;
            text-align:center;background:#f0f0ff;border-radius:12px;padding:18px 0;
            margin-bottom:20px">{code}</div>
        <div style="font-size:12px;color:#9ca3af">Код действителен 10 минут. Никому не сообщай его.</div>
      </div>
    </div>"""
    if not SMTP_HOST or not SMTP_USER:
        print(f"[OTP DEV] {email} → {code}")
        return
    _send_email(email, "Код подтверждения — WordsApp", html_body)


class OtpVerifyIn(BaseModel):
    email: str
    code:  str

@app.post("/auth/verify-otp")
def verify_otp(body: OtpVerifyIn):
    from datetime import datetime, timezone
    email = body.email.lower().strip()
    with get_conn() as conn:
        row = conn.execute(text("""
            SELECT id, code, expires_at, purpose FROM email_otp
            WHERE email=:e AND used=0
            ORDER BY id DESC LIMIT 1
        """), {"e": email}).one_or_none()
        if not row or row[1] != body.code.strip():
            raise HTTPException(400, "Неверный код")
        expires = row[2]
        if isinstance(expires, str):
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except:
                exp_dt = datetime.fromisoformat(expires)
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        else:
            exp_dt = expires
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp_dt:
            raise HTTPException(400, "Код истёк. Войдите снова")
        conn.execute(text("UPDATE email_otp SET used=1 WHERE id=:id"), {"id": row[0]})
        conn.commit()
        # Fetch user
        try:
            user = conn.execute(
                text("SELECT id, email, name, role, level FROM users WHERE email=:e"),
                {"e": email}
            ).mappings().one_or_none()
        except Exception:
            conn.rollback()
            user = conn.execute(
                text("SELECT id, email, name, role FROM users WHERE email=:e"),
                {"e": email}
            ).mappings().one_or_none()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    user = dict(user)
    token = make_token(user["id"], user["email"], user["role"])
    needs_level = not user.get("level")
    return {"token": token, "user": {k: user[k] for k in ("id","email","name","role","level") if k in user}, "needs_level": needs_level}


class ForgotPasswordIn(BaseModel):
    email: str

class ResetPasswordIn(BaseModel):
    email: str
    code: str
    new_password: str


@app.post("/auth/forgot-password")
def forgot_password(body: ForgotPasswordIn):
    email = body.email.lower().strip()
    with get_conn() as conn:
        user = conn.execute(
            text("SELECT id FROM users WHERE email=:e"), {"e": email}
        ).one_or_none()
        # Always return OK to not leak existence
        if not user:
            return {"ok": True}

        # Invalidate old codes for this email
        conn.execute(
            text("UPDATE password_reset_codes SET used=1 WHERE email=:e AND used=0"),
            {"e": email}
        )

        code = _gen_code()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        conn.execute(
            text("INSERT INTO password_reset_codes (email, code, expires_at) VALUES (:e, :c, :x)"),
            {"e": email, "c": code, "x": expires}
        )
        conn.commit()

    html_body = f"""
    <div style="font-family:sans-serif;max-width:400px;margin:0 auto;padding:32px">
      <h2 style="font-size:24px;margin-bottom:8px">Сброс пароля</h2>
      <p style="color:#555;margin-bottom:24px">Ваш код подтверждения для WordsApp:</p>
      <div style="font-size:40px;font-weight:800;letter-spacing:8px;text-align:center;
                  background:#f5f4f0;border-radius:12px;padding:20px;margin-bottom:24px;
                  color:#1c1b18;font-family:monospace">
        {code}
      </div>
      <p style="color:#888;font-size:13px">Код действителен 15 минут. Если вы не запрашивали сброс — проигнорируйте это письмо.</p>
    </div>
    """
    try:
        _send_email(email, "Код подтверждения WordsApp", html_body)
    except Exception as ex:
        print(f"Email error: {ex}")
        raise HTTPException(500, "Не удалось отправить письмо. Проверьте настройки SMTP.")

    return {"ok": True}


@app.post("/auth/reset-password")
def reset_password(body: ResetPasswordIn):
    if len(body.new_password) < 6:
        raise HTTPException(400, "Пароль минимум 6 символов")

    email = body.email.lower().strip()
    code  = body.code.strip()

    with get_conn() as conn:
        row = conn.execute(text("""
            SELECT id, expires_at FROM password_reset_codes
            WHERE email=:e AND code=:c AND used=0
            ORDER BY id DESC LIMIT 1
        """), {"e": email, "c": code}).one_or_none()

        if not row:
            raise HTTPException(400, "Неверный или просроченный код")

        expires_at = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(400, "Код истёк. Запросите новый")

        # Mark used
        conn.execute(
            text("UPDATE password_reset_codes SET used=1 WHERE id=:id"), {"id": row[0]}
        )
        new_hash = hash_pw(body.new_password)
        conn.execute(
            text("UPDATE users SET password_hash=:h WHERE email=:e"),
            {"h": new_hash, "e": email}
        )
        conn.commit()

    return {"ok": True}


# ── Password Change with Email Confirmation (logged-in user) ───────────────────

class InitPasswordChange(BaseModel):
    current_password: str
    new_password: str

@app.post("/profile/request-password-change")
def request_password_change(body: InitPasswordChange, u=Depends(get_user)):
    if len(body.new_password) < 6:
        raise HTTPException(400, "Новый пароль минимум 6 символов")
    uid = u["sub"]
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT email, password_hash FROM users WHERE id=:id"), {"id": uid}
        ).one_or_none()
        if not row or not check_pw(body.current_password, row[1]):
            raise HTTPException(400, "Неверный текущий пароль")
        email = row[0]
        # Store new hash + code
        new_hash = hash_pw(body.new_password)
        code     = _gen_code()
        expires  = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        # Reuse password_reset_codes table, store new_hash in code field prefixed
        conn.execute(
            text("UPDATE password_reset_codes SET used=1 WHERE email=:e AND used=0"),
            {"e": email}
        )
        conn.execute(
            text("INSERT INTO password_reset_codes (email, code, expires_at) VALUES (:e, :c, :x)"),
            {"e": email, "c": code + "||" + new_hash, "x": expires}
        )
        conn.commit()

    html_body = f"""
    <div style="font-family:sans-serif;max-width:400px;margin:0 auto;padding:32px">
      <h2 style="font-size:24px;margin-bottom:8px">Смена пароля</h2>
      <p style="color:#555;margin-bottom:24px">Подтвердите смену пароля в WordsApp:</p>
      <div style="font-size:40px;font-weight:800;letter-spacing:8px;text-align:center;
                  background:#f5f4f0;border-radius:12px;padding:20px;margin-bottom:24px;
                  color:#1c1b18;font-family:monospace">
        {code}
      </div>
      <p style="color:#888;font-size:13px">Код действителен 15 минут. Если вы не запрашивали смену пароля — немедленно войдите в аккаунт и проверьте безопасность.</p>
    </div>
    """
    try:
        _send_email(email, "Подтверждение смены пароля — WordsApp", html_body)
    except Exception as ex:
        print(f"Email error: {ex}")
        raise HTTPException(500, "Не удалось отправить письмо. Настройте SMTP в переменных окружения.")

    return {"ok": True, "email": email}


@app.post("/profile/confirm-password-change")
def confirm_password_change(body: ResetPasswordIn, u=Depends(get_user)):
    email = body.email.lower().strip()
    code  = body.code.strip()
    with get_conn() as conn:
        row = conn.execute(text("""
            SELECT id, expires_at, code FROM password_reset_codes
            WHERE email=:e AND used=0 AND code LIKE :c
            ORDER BY id DESC LIMIT 1
        """), {"e": email, "c": code + "||%"}).one_or_none()

        if not row:
            raise HTTPException(400, "Неверный или просроченный код")
        expires_at = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(400, "Код истёк. Запросите новый")

        stored_code, new_hash = row[2].split("||", 1)
        if stored_code != code:
            raise HTTPException(400, "Неверный код")

        conn.execute(text("UPDATE password_reset_codes SET used=1 WHERE id=:id"), {"id": row[0]})
        conn.execute(text("UPDATE users SET password_hash=:h WHERE email=:e"), {"h": new_hash, "e": email})
        conn.commit()

    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# ASSIGNMENTS (HOMEWORK)
# ═══════════════════════════════════════════════════════════════════════════════

import json as _json

class AssignmentIn(BaseModel):
    student_id:  int
    title:       str
    description: str = ""
    links:       list = []   # [{"url": "...", "label": "..."}]
    words:       list = []   # ["word1", "word2"]
    due_date:    str = None

class SubmissionIn(BaseModel):
    text: str = ""

class FeedbackIn(BaseModel):
    text: str

def _require_teacher(u):
    if u.get("role") not in ("teacher", "ceo"):
        raise HTTPException(403, "Только для учителя")

def _require_student_or_teacher(u, assignment):
    me = u["sub"]
    if u.get("role") in ("teacher", "ceo"):
        return
    if assignment["student_id"] != me:
        raise HTTPException(403, "Нет доступа")

# ── Teacher: create assignment ────────────────────────────────────────────────
@app.post("/assignments")
def create_assignment(body: AssignmentIn, u=Depends(get_user)):
    _require_teacher(u)
    with get_conn() as conn:
        if IS_PG:
            row = conn.execute(text("""
                INSERT INTO assignments (teacher_id, student_id, title, description, links, words, due_date)
                VALUES (:t, :s, :ti, :d, :l, :w, :du) RETURNING id
            """), {"t": u["sub"], "s": body.student_id, "ti": body.title,
                   "d": body.description, "l": _json.dumps(body.links, ensure_ascii=False),
                   "w": _json.dumps(body.words, ensure_ascii=False), "du": body.due_date}).one()
            aid = row[0]
        else:
            conn.execute(text("""
                INSERT INTO assignments (teacher_id, student_id, title, description, links, words, due_date)
                VALUES (:t, :s, :ti, :d, :l, :w, :du)
            """), {"t": u["sub"], "s": body.student_id, "ti": body.title,
                   "d": body.description, "l": _json.dumps(body.links, ensure_ascii=False),
                   "w": _json.dumps(body.words, ensure_ascii=False), "du": body.due_date})
            aid = conn.execute(text("SELECT last_insert_rowid()")).scalar()
        conn.commit()
    return {"id": aid, "ok": True}

# ── List assignments (teacher sees all they created; student sees their own) ──
@app.get("/assignments")
def list_assignments(u=Depends(get_user)):
    me = u["sub"]
    role = u.get("role", "user")
    with get_conn() as conn:
        if role in ("teacher", "ceo"):
            rows = conn.execute(text("""
                SELECT a.*, u.name AS student_name, u.username AS student_username
                FROM assignments a JOIN users u ON u.id=a.student_id
                WHERE a.teacher_id=:me ORDER BY a.created_at DESC
            """), {"me": me}).mappings().all()
        else:
            rows = conn.execute(text("""
                SELECT a.*, u.name AS teacher_name, u.username AS teacher_username
                FROM assignments a JOIN users u ON u.id=a.teacher_id
                WHERE a.student_id=:me ORDER BY a.created_at DESC
            """), {"me": me}).mappings().all()
        result = []
        for r in rows:
            d = dict(r)
            d["links"] = _json.loads(d.get("links") or "[]")
            d["words"] = _json.loads(d.get("words") or "[]")
            result.append(d)
    return result

# ── Get single assignment with submission ────────────────────────────────────
@app.get("/assignments/{aid}")
def get_assignment(aid: int, u=Depends(get_user)):
    me = u["sub"]
    with get_conn() as conn:
        row = conn.execute(text("SELECT * FROM assignments WHERE id=:id"), {"id": aid}).mappings().one_or_none()
        if not row:
            raise HTTPException(404, "Не найдено")
        a = dict(row)
        if u.get("role") not in ("teacher", "ceo") and a["student_id"] != me:
            raise HTTPException(403, "Нет доступа")
        a["links"] = _json.loads(a.get("links") or "[]")
        a["words"] = _json.loads(a.get("words") or "[]")
        # Get submission
        sub = conn.execute(text("""
            SELECT s.*, f.text AS feedback_text, f.created_at AS feedback_at
            FROM assignment_submissions s
            LEFT JOIN assignment_feedback f ON f.submission_id=s.id
            WHERE s.assignment_id=:aid AND s.student_id=:sid
            ORDER BY s.created_at DESC LIMIT 1
        """), {"aid": aid, "sid": a["student_id"]}).mappings().one_or_none()
        a["submission"] = dict(sub) if sub else None
    return a

# ── Student: submit answer ────────────────────────────────────────────────────
@app.post("/assignments/{aid}/submit")
def submit_assignment(aid: int, body: SubmissionIn, u=Depends(get_user)):
    me = u["sub"]
    with get_conn() as conn:
        a = conn.execute(text("SELECT * FROM assignments WHERE id=:id"), {"id": aid}).mappings().one_or_none()
        if not a or a["student_id"] != me:
            raise HTTPException(403, "Нет доступа")
        if IS_PG:
            conn.execute(text("""
                INSERT INTO assignment_submissions (assignment_id, student_id, text)
                VALUES (:a, :s, :t)
            """), {"a": aid, "s": me, "t": body.text})
        else:
            conn.execute(text("""
                INSERT INTO assignment_submissions (assignment_id, student_id, text)
                VALUES (:a, :s, :t)
            """), {"a": aid, "s": me, "t": body.text})
        conn.execute(text("UPDATE assignments SET status='submitted' WHERE id=:id"), {"id": aid})
        conn.commit()
    return {"ok": True}

# ── Teacher: leave feedback ───────────────────────────────────────────────────
@app.post("/assignments/{aid}/feedback")
def leave_feedback(aid: int, body: FeedbackIn, u=Depends(get_user)):
    _require_teacher(u)
    with get_conn() as conn:
        sub = conn.execute(text("""
            SELECT id FROM assignment_submissions WHERE assignment_id=:aid ORDER BY created_at DESC LIMIT 1
        """), {"aid": aid}).one_or_none()
        if not sub:
            raise HTTPException(404, "Ответ не найден")
        conn.execute(text("""
            INSERT INTO assignment_feedback (submission_id, teacher_id, text)
            VALUES (:s, :t, :tx)
        """), {"s": sub[0], "t": u["sub"], "tx": body.text})
        conn.execute(text("UPDATE assignments SET status='reviewed' WHERE id=:id"), {"id": aid})
        conn.commit()
    return {"ok": True}

# ── Teacher: delete assignment ────────────────────────────────────────────────
@app.delete("/assignments/{aid}", status_code=204)
def delete_assignment(aid: int, u=Depends(get_user)):
    _require_teacher(u)
    with get_conn() as conn:
        conn.execute(text("DELETE FROM assignment_feedback WHERE submission_id IN (SELECT id FROM assignment_submissions WHERE assignment_id=:id)"), {"id": aid})
        conn.execute(text("DELETE FROM assignment_submissions WHERE assignment_id=:id"), {"id": aid})
        conn.execute(text("DELETE FROM assignments WHERE id=:id AND teacher_id=:t"), {"id": aid, "t": u["sub"]})
        conn.commit()

# ── List students (for teacher to pick when creating assignment) ──────────────
@app.get("/teacher/students")
def list_students(u=Depends(get_user)):
    _require_teacher(u)
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT id, name, username, email FROM users
            WHERE role NOT IN ('teacher','ceo') ORDER BY name
        """)).mappings().all()
    return [dict(r) for r in rows]
