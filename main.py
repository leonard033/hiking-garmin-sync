import os
import time
import sqlite3
import threading
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
import uvicorn
from garminconnect import Garmin

# ---------- 配置（来自环境变量）----------
PORT = int(os.getenv("PORT", "8000"))
SYNC_TOKEN = os.getenv("SYNC_TOKEN", "")
GARMIN_USER = os.getenv("GARMIN_USER", "")
GARMIN_PASS = os.getenv("GARMIN_PASS", "")
GARMIN_IS_CN = os.getenv("GARMIN_IS_CN", "true").lower() in ("true", "1", "yes")
PULL_INTERVAL = int(os.getenv("PULL_INTERVAL_MIN", "30"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "21"))
TRAIL_TYPE = os.getenv("TRAIL_TYPE", "trail_running")
DB_PATH = os.getenv("DB_PATH", "data/sync.db")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ---------- 数据库 ----------
_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS candidates(
                activity_id INTEGER PRIMARY KEY,
                date TEXT,
                activity_name TEXT,
                distance_km REAL,
                duration_min REAL,
                suggested_name TEXT,
                fetched_at TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS decisions(
                activity_id INTEGER PRIMARY KEY,
                decision TEXT,
                name TEXT,
                count INTEGER,
                decided_at TEXT)"""
        )


# ---------- 鉴权 ----------
def auth_ok(req: Request) -> bool:
    if not SYNC_TOKEN:
        return True  # 未设置令牌则不校验（生产环境务必设置）
    ah = req.headers.get("Authorization", "")
    if ah.startswith("Bearer "):
        return ah[7:] == SYNC_TOKEN
    return False


# ---------- 拉取 Garmin ----------
def pull_once():
    if not (GARMIN_USER and GARMIN_PASS):
        print("[pull] 未配置 GARMIN_USER / GARMIN_PASS，跳过")
        return
    try:
        api = Garmin(
            email=GARMIN_USER,
            password=GARMIN_PASS,
            is_cn=GARMIN_IS_CN,
            prompt_mfa=False,
        )
        end = datetime.now()
        start = end - timedelta(days=LOOKBACK_DAYS)
        activities = api.get_activities_by_date(
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        )
        added = 0
        with _lock:
            conn = db()
            cur = conn.cursor()
            for a in activities or []:
                atype = a.get("activityType") or {}
                key = atype.get("typeKey") or a.get("activityTypeDTO", {}).get("typeKey")
                if key != TRAIL_TYPE:
                    continue
                aid = a.get("activityId")
                if aid is None:
                    continue
                cur.execute("SELECT 1 FROM decisions WHERE activity_id=?", (aid,))
                if cur.fetchone():
                    continue
                cur.execute("SELECT 1 FROM candidates WHERE activity_id=?", (aid,))
                if cur.fetchone():
                    continue
                st = a.get("startTimeLocal") or ""
                dist = a.get("distance")
                dur = a.get("duration")
                cur.execute(
                    """INSERT OR IGNORE INTO candidates
                       (activity_id, date, activity_name, distance_km, duration_min, suggested_name, fetched_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        aid,
                        st[:10],
                        a.get("activityName"),
                        round(dist / 1000, 2) if dist else None,
                        round(dur / 60, 1) if dur else None,
                        a.get("activityName"),
                        datetime.now().isoformat(),
                    ),
                )
                added += 1
            conn.commit()
            conn.close()
        print(f"[pull] 新增越野跑待确认 {added} 条")
    except Exception as e:
        print("[pull] 拉取失败:", repr(e))


def pull_loop():
    while True:
        pull_once()
        time.sleep(PULL_INTERVAL * 60)


# ---------- 应用 ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    threading.Thread(target=pull_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/pending")
def pending(req: Request):
    if not auth_ok(req):
        raise HTTPException(status_code=401, detail="unauthorized")
    with _lock:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """SELECT c.* FROM candidates c
               WHERE NOT EXISTS (SELECT 1 FROM decisions d WHERE d.activity_id = c.activity_id)
               ORDER BY c.date DESC"""
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    return {"pending": rows}


@app.post("/api/decide")
async def decide(req: Request):
    if not auth_ok(req):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")
    aid = body.get("activity_id")
    decision = body.get("decision")
    if aid is None or decision not in ("add", "ignore"):
        raise HTTPException(status_code=400, detail="bad request")
    name = (body.get("name") or "").strip()
    try:
        count = int(body.get("count") or 1)
    except Exception:
        count = 1
    count = max(1, min(999, count))
    date = body.get("date") or ""

    with _lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM candidates WHERE activity_id=?", (aid,))
        cand = cur.fetchone()
        if cand:
            cand = dict(cand)
        cur.execute(
            """INSERT OR REPLACE INTO decisions
               (activity_id, decision, name, count, decided_at)
               VALUES (?,?,?,?,?)""",
            (aid, decision, name, count, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

    record = None
    if decision == "add":
        if not name:
            name = (cand or {}).get("suggested_name") or (cand or {}).get(
                "activity_name"
            ) or "越野跑"
        if not date:
            date = (cand or {}).get("date") or datetime.now().strftime("%Y-%m-%d")
        record = {
            "id": int(time.time() * 1000),
            "date": date,
            "name": name,
            "count": count,
        }
    return {"ok": True, "record": record}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
