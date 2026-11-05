import os, re, uuid, time, tempfile
from datetime import date, timedelta
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
load_dotenv()
import agent as A

app = FastAPI(title="Cyncle")
VIEWS = {}

def tid(v): return v["thread"]["configurable"]["thread_id"]
def clean(v):
    keys = ["intent","mode","symptoms","messages_out","notes","reply","ai_reply","advice","cards","results","waiting_for_approval","stale","offline"]
    o = {k: v.get(k) for k in keys}
    o["thread"] = tid(v); o["why"] = A.why_no_plan(v)
    return o

CID = re.compile(r"[a-f0-9]{32}")
RESTORED = set(); STT = {}; DEFAULT_STT = A.STT_BACKEND
def me(): return A.VISITOR.get()
HITS = {}
LLM_LIMIT = int(os.environ.get("LLM_LIMIT_PER_HOUR", "120")); NEW_LIMIT = int(os.environ.get("NEW_VISITORS_PER_HOUR", "60"))
def allowed(key, limit):
    now = time.time(); q = [t for t in HITS.get(key, []) if now - t < 3600]
    ok = len(q) < limit
    if ok: q.append(now)
    HITS[key] = q
    return ok

@app.middleware("http")
async def visitor_mw(request, call_next):
    if not request.url.path.startswith("/api/"): return await call_next(request)
    cid = request.cookies.get("cid", ""); new = not CID.fullmatch(cid)
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")
    busy = JSONResponse({"error": "Too many requests. Please try again later."}, status_code=429)
    if request.method == "POST" and request.url.path in ("/api/chat", "/api/voice") and not allowed(("llm", ip), LLM_LIMIT): return busy
    if new and A.MULTI_USER and not allowed(("new", ip), NEW_LIMIT): return busy
    if new: cid = uuid.uuid4().hex
    if A.MULTI_USER: A.VISITOR.set(cid)
    if me() not in RESTORED:
        RESTORED.add(me())
        try:
            for v in A.restore_pending(): v["_owner"] = me(); VIEWS[tid(v)] = v
        except Exception: pass
    resp = await call_next(request)
    if new and A.MULTI_USER:
        resp.set_cookie("cid", cid, max_age=31536000, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return resp

class Chat(BaseModel):
    text: str = ""
    picked: Optional[list] = None
class Decide(BaseModel):
    thread: str
    decisions: list
class Period(BaseModel):
    start_date: Optional[str] = None
class Task(BaseModel):
    title: str
    days: int = 1
    priority: int = 2
    due: Optional[str] = None
class Stt(BaseModel):
    backend: str

@app.post("/api/chat")
def chat(b: Chat):
    v = A.chat(b.text, None, b.picked); v["_owner"] = me(); VIEWS[tid(v)] = v; return clean(v)

@app.post("/api/decide")
def decide(b: Decide):
    v = VIEWS.get(b.thread)
    if not v or v.get("_owner") != me():
        return {"stale": True, "cards": [], "results": [], "mode": "decided"}
    nv = A.decide(v, b.decisions); VIEWS.pop(b.thread, None); return clean(nv)

@app.get("/api/home")
def home():
    p = A.get_phase(); ts = A.get_tasks()
    pend = A.render_cards()
    th = next((t for t, x in VIEWS.items() if x.get("_owner") == me()), None)
    r = A.db.execute("SELECT COALESCE(SUM(status='done'),0) d, COUNT(*) n FROM tasks").fetchone()
    return {"phase": p, "tasks": ts, "pending": pend, "thread": th, "today": str(date.today()), "done": r["d"], "total": r["n"]}

class Done(BaseModel):
    id: int

@app.post("/api/tasks/delete")
def delete_task(b: Done):
    A.db.execute("UPDATE tasks SET status='deleted' WHERE id=?", (b.id,))
    A.db.execute("UPDATE proposals SET status='cancelled' WHERE task_id=? AND status='pending'", (b.id,))
    A.db.execute("DELETE FROM calendar_events WHERE task_id=?", (b.id,)); A.db.commit(); return {"ok": True}

@app.post("/api/tasks/done")
def done(b: Done):
    A.db.execute("UPDATE tasks SET status='done' WHERE id=?", (b.id,)); A.db.commit(); return {"ok": True}

@app.get("/api/week")
def week():
    return {"tasks": A.get_tasks(), "today": str(date.today())}

@app.post("/api/tasks")
def add(b: Task):
    days = b.days
    if b.due:
        try: days = max(0, (date.fromisoformat(b.due) - date.today()).days)
        except ValueError: pass
    return {"id": A.add_task(b.title, days, min(3, max(1, b.priority)))}

@app.get("/api/cycle")
def cycle():
    p = A.get_phase()
    g = lambda k: A.db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    lo, hi = g("cycle_min"), g("cycle_max")
    p["range"] = {"min": int(lo["value"]), "max": int(hi["value"])} if lo and hi else None
    p["history"] = [x["start_date"] for x in A.db.execute("SELECT start_date FROM period_logs ORDER BY start_date DESC LIMIT 6")]
    p["today"] = str(date.today()); p["min_date"] = str(date.today() - timedelta(days=60))
    return p

class Range(BaseModel):
    shortest: int
    longest: int

@app.post("/api/cycle/range")
def cycle_range(b: Range):
    lo, hi = b.shortest, b.longest
    if not (18 <= lo <= hi <= 60) or hi - lo > 20:
        return {"ok": False, "error": "Use days between 18 and 60, with the longest at most 20 days above the shortest."}
    for k, v in (("cycle_min", lo), ("cycle_max", hi), ("cycle_len", round((lo + hi) / 2))):
        A.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (k, str(v)))
    A.db.commit(); return {"ok": True, "used": round((lo + hi) / 2)}

@app.post("/api/period")
def period(b: Period): return A.log_period_start(b.start_date)

@app.post("/api/period/delete")
def delete_period(b: Period):
    cur = A.db.execute("DELETE FROM period_logs WHERE start_date=?", (b.start_date,)); A.db.commit()
    return {"ok": True} if cur.rowcount else {"ok": False, "error": "That period was not found"}

@app.post("/api/voice")
async def voice(file: UploadFile = File(...)):
    os.makedirs(A.VOICE_DIR, exist_ok=True)
    path = os.path.join(A.VOICE_DIR, "v" + os.urandom(4).hex() + os.path.splitext(file.filename or ".webm")[1])
    with open(path, "wb") as f: f.write(await file.read())
    A.STT_BACKEND = STT.get(me(), DEFAULT_STT)
    return A.transcribe(path)

def pattern():
    starts = [r["start_date"] for r in A.db.execute("SELECT start_date FROM period_logs ORDER BY start_date")]
    by = {}
    for r in A.db.execute("SELECT day,symptom,severity FROM symptoms"):
        if r["symptom"] not in A.PLANNING_CODES: continue
        s = max((x for x in starts if x <= r["day"]), default=None)
        if s: by.setdefault((date.fromisoformat(r["day"]) - date.fromisoformat(s)).days + 1, []).append(r["severity"])
    if len(by) < 2: return None
    top = sorted(by, key=lambda d: -sum(by[d]) / len(by[d]))[:2]
    return {"text": f"Symptoms were highest on cycle days {' and '.join(map(str, sorted(top)))}.", "cycles": len(starts)}

@app.get("/api/insights")
def insights():
    c = {r["status"]: r["n"] for r in A.db.execute("SELECT status,COUNT(*) n FROM proposals WHERE status IN ('approved','rejected') GROUP BY status")}
    ap, rj = c.get("approved", 0), c.get("rejected", 0)
    return {"approved": ap, "decided": ap + rj, "rejections": A.rejection_memory(), "phase": A.get_phase(), "pattern": pattern()}

@app.get("/api/privacy")
def privacy():
    return {"offline": not A.llm_available(), "stt": STT.get(me(), DEFAULT_STT), "rejections": A.rejection_memory(), "cooldown": A.REJECT_COOLDOWN_DAYS}

@app.post("/api/privacy/stt")
def set_stt(b: Stt):
    if b.backend in ("local", "groq"): STT[me()] = b.backend
    return {"stt": STT.get(me(), DEFAULT_STT)}

@app.post("/api/delete")
def delete_me():
    vid = me()
    mine = [r["thread_id"] for r in A.db.execute("SELECT thread_id FROM paused_checkins")] + [t for t, x in VIEWS.items() if x.get("_owner") == vid]
    for t in set(mine):
        try: A._forget(t)
        except Exception: pass
        VIEWS.pop(t, None)
    if vid == "default":
        A.db.executescript("DELETE FROM period_logs; DELETE FROM tasks; DELETE FROM symptoms; DELETE FROM proposals; DELETE FROM calendar_events; DELETE FROM paused_checkins; DELETE FROM settings;")
    else:
        c = A._conns.pop(vid, None)
        if c: c.close()
        for ext in (".db", ".db-wal", ".db-shm"):
            try: os.remove(os.path.join(A.DATA_DIR, f"u_{vid}{ext}"))
            except OSError: pass
    try: os.remove(os.path.join(A.DATA_DIR, vid + ".ics"))
    except OSError: pass
    RESTORED.discard(vid); STT.pop(vid, None)
    return {"ok": True}

@app.get("/api/catalog")
def catalog():
    return {c: [{"code": k, "label": d["label"], "plans": d["plans"]} for k, d in items.items()] for c, items in A.SYMPTOM_CATALOG.items()}

def _ics():
    os.makedirs(A.DATA_DIR, exist_ok=True); return A.export_ics(os.path.join(A.DATA_DIR, me() + ".ics"))

@app.get("/api/ics")
def ics(): return FileResponse(_ics(), filename="cyncle.ics", media_type="text/calendar")

app.mount("/", StaticFiles(directory="static", html=True), name="static")
