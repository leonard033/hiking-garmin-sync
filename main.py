import os
import time
import sqlite3
import threading
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from garminconnect import Garmin
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

# ---------- 配置（来自环境变量）----------
PORT = int(os.getenv("PORT", "8000"))
SYNC_TOKEN = os.getenv("SYNC_TOKEN", "")
GARMIN_USER = os.getenv("GARMIN_USER", "")
GARMIN_PASS = os.getenv("GARMIN_PASS", "")
GARMIN_TOKEN = os.getenv("GARMIN_TOKEN", "")  # 本地生成的 OAuth token base64
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
# 记录最近一次拉取的状态，供 /api/debug 排查
LAST_PULL = {"time": None, "ok": None, "error": None, "total": 0, "added": 0, "types": {}}

# Garmin OAuth token 持久化目录（重启/下次拉取可直接用 token，避免每次都输密码）
_TOKENSTORE = os.path.join(os.path.dirname(DB_PATH), ".garminconnect")


def get_garmin():
    """构造并登录 Garmin 客户端。
    优先级：1) GARMIN_TOKEN base64  2) 本地已保存 token  3) 账号密码登录
    """
    os.makedirs(_TOKENSTORE, exist_ok=True)

    # 方式 1: 环境变量里的 GARMIN_TOKEN（本地生成后填到 Railway）
    if GARMIN_TOKEN:
        try:
            api = Garmin()
            api.login(GARMIN_TOKEN)
            print("[login] 使用 GARMIN_TOKEN 登录成功")
            return api
        except Exception as e:
            print("[login] GARMIN_TOKEN 登录失败:", repr(e))

    # 方式 2: 容器内已保存的 token 目录
    try:
        api = Garmin()
        api.login(_TOKENSTORE)
        print("[login] 使用已保存 token 登录成功")
        return api
    except Exception as e:
        print("[login] 本地 token 登录失败:", repr(e))

    # 方式 3: 账号密码登录
    if not (GARMIN_USER and GARMIN_PASS):
        raise RuntimeError("未配置 GARMIN_USER / GARMIN_PASS，且没有可用 token")
    api = Garmin(
        email=GARMIN_USER,
        password=GARMIN_PASS,
        is_cn=GARMIN_IS_CN,
        prompt_mfa=False,
    )
    api.login()
    try:
        api.garth.dump(_TOKENSTORE)
        print("[login] 账号密码登录成功，token 已保存")
    except Exception as e2:
        print("[login] 保存 token 失败(可忽略):", repr(e2))
    return api


def pull_once():
    if not (GARMIN_TOKEN or (GARMIN_USER and GARMIN_PASS)):
        print("[pull] 未配置 GARMIN_USER / GARMIN_PASS / GARMIN_TOKEN，跳过")
        LAST_PULL.update(time=datetime.now().isoformat(), ok=False,
                        error="未配置 GARMIN_USER / GARMIN_PASS / GARMIN_TOKEN")
        return
    try:
        api = get_garmin()
        end = datetime.now()
        start = end - timedelta(days=LOOKBACK_DAYS)
        activities = api.get_activities_by_date(
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        )
        # 统计各活动类型出现次数（用于排查过滤是否正确）
        type_counts = {}
        for a in activities or []:
            atype = a.get("activityType") or {}
            k = atype.get("typeKey") or (a.get("activityTypeDTO", {}) or {}).get("typeKey") or "unknown"
            type_counts[k] = type_counts.get(k, 0) + 1
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
        LAST_PULL.update(time=datetime.now().isoformat(), ok=True, error=None,
                         total=len(activities or []), added=added, types=type_counts)
    except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
        # 登录/认证相关错误，单独标注，方便判断是密码错还是 MFA 问题
        msg = f"{type(e).__name__}: {e}"
        print("[pull] 认证失败:", msg)
        LAST_PULL.update(time=datetime.now().isoformat(), ok=False,
                         error="AUTH_FAIL: " + msg)
    except Exception as e:
        print("[pull] 拉取失败:", repr(e))
        LAST_PULL.update(time=datetime.now().isoformat(), ok=False, error=repr(e))


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

# 允许手机端 App（不同域名）跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/debug")
def debug(req: Request):
    """排查用：显示配置概览 + 最近一次拉取的状态/活动类型分布。"""
    if not auth_ok(req):
        raise HTTPException(status_code=401, detail="unauthorized")
    return {
        "config": {
            "garmin_user_set": bool(GARMIN_USER),
            "garmin_pass_set": bool(GARMIN_PASS),
            "garmin_token_set": bool(GARMIN_TOKEN),
            "is_cn": GARMIN_IS_CN,
            "trail_type": TRAIL_TYPE,
            "lookback_days": LOOKBACK_DAYS,
            "pull_interval_min": PULL_INTERVAL,
        },
        "last_pull": LAST_PULL,
    }


@app.post("/api/pull")
def manual_pull(req: Request):
    """手动触发一次拉取（省得等定时任务）。"""
    if not auth_ok(req):
        raise HTTPException(status_code=401, detail="unauthorized")
    pull_once()
    return {"ok": True, "last_pull": LAST_PULL}


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


@app.get("/api/decisions")
def list_decisions(req: Request):
    """查看已经确认(add)/忽略(ignore)过的活动，方便核对去重状态。"""
    if not auth_ok(req):
        raise HTTPException(status_code=401, detail="unauthorized")
    with _lock:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            """SELECT d.activity_id,
                      c.date,
                      c.activity_name,
                      c.distance_km,
                      c.duration_min,
                      d.decision,
                      d.name        AS mountain,
                      d.count,
                      d.decided_at
               FROM decisions d
               LEFT JOIN candidates c ON c.activity_id = d.activity_id
               ORDER BY d.decided_at DESC"""
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    return {"decisions": rows}


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
