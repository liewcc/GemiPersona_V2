import os
import json
import time
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import subprocess
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Setup logging
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'conductor.log')
handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
handler.setFormatter(formatter)

logging.basicConfig(
    handlers=[handler],
    level=os.environ.get('GEMI_LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger('conductor')

app = FastAPI()

# The Electron renderer loads via file:// (Origin: null) and calls this API over
# http://127.0.0.1:18101 — a cross-origin request. Without CORS headers Chromium
# blocks the response and every api.js fetch fails. Allow all origins (local app).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_registered_service_pid = None
_spawn_lock = asyncio.Lock()

# Config
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = os.path.join(BASE_DIR, "Gemi_Engine_V2")
CONFIG_PATH = os.path.join(BASE_DIR, "engine_config.json")
ENGINE_VENV_PYTHON = os.path.join(BASE_DIR, ".python_venv", "pythonw.exe")
ENGINE_SCRIPT = "engine_service.py"
ENGINE_OUT = os.path.join(BASE_DIR, "engine_svc.out")
MAX_LOG_BYTES = 5 * 1024 * 1024

def rotate_if_oversized(path):
    """Keep one generation of `path` and start fresh once it passes MAX_LOG_BYTES.

    ponytail: the size is only checked when we are about to spawn an engine, so an
    engine that stays up for weeks can still overshoot the cap. Upgrade path: pipe
    the subprocess output through a RotatingFileHandler instead of handing it a raw
    file handle.
    """
    try:
        if os.path.getsize(path) < MAX_LOG_BYTES:
            return
        os.replace(path, path + ".1")
    except FileNotFoundError:
        pass
    except OSError as e:
        # A stale handle on the old file loses us the rotation, not the log line.
        logger.error(f"Failed to rotate {path}: {e}")

def get_engine_port():
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
                return data.get("port", 18100)
    except Exception as e:
        logger.error(f"Error reading engine config: {e}")
    return 18100

def get_engine_url():
    port = get_engine_port()
    return f"http://127.0.0.1:{port}"

async def ensure_service():
    global _registered_service_pid
    url = get_engine_url()

    async def _check_and_register(resp):
        global _registered_service_pid
        data = resp.json()
        service_pid = data.get("service_pid")
        if service_pid and service_pid != _registered_service_pid:
            logger.info("New engine process detected. Registering conductor PID.")
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.post(f"{url}/tui/register", json={"pid": os.getpid()})
                _registered_service_pid = service_pid
            except Exception as e:
                logger.error(f"Failed to register with engine: {e}")

    async def _probe(timeout=1.0):
        """Return True if the engine answers /health (and register if it's new)."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{url}/health")
                if resp.status_code == 200:
                    await _check_and_register(resp)
                    return True
        except Exception:
            pass
        return False

    async def _port_open():
        """True if something is already listening on the engine port.

        A long-running browser step (wait_response, redo, ...) can block the
        engine's event loop past the /health timeout even though the process
        is alive — a TCP connect still succeeds because the listener socket
        itself isn't blocked. Spawning a second engine in that case can't
        bind the port anyway (WinError 10048) and just wastes a process.
        """
        port = get_engine_port()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=1.0
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    # Fast path: engine already up — no lock, no contention on the hot proxy path.
    if await _probe():
        return True

    # Slow path: serialize spawning so concurrent callers don't each spawn a
    # duplicate engine process (the previous behaviour spawned N ghosts on races).
    async with _spawn_lock:
        # Re-check: another caller may have spawned while we waited for the lock.
        if await _probe(timeout=1.5):
            return True

        if await _port_open():
            # Engine process is alive and holding the port, just still busy.
            # Keep polling instead of spawning a duplicate that can't bind.
            start_time = time.time()
            while time.time() - start_time < 20:
                if await _probe(timeout=1.5):
                    return True
                await asyncio.sleep(1)
            logger.error("Engine port is open but /health never responded within 20s.")
            return False

        logger.info("Engine unreachable. Spawning new engine process...")
        try:
            rotate_if_oversized(ENGINE_OUT)
            # Popen duplicates the handle into the child, so closing our copy here
            # keeps the conductor from leaking one fd per spawn — and leaving that
            # fd open would also block the next rotation on Windows.
            with open(ENGINE_OUT, "a") as out_file:
                subprocess.Popen(
                    [ENGINE_VENV_PYTHON, ENGINE_SCRIPT],
                    cwd=ENGINE_DIR,
                    stdout=out_file,
                    stderr=out_file
                )
        except Exception as e:
            logger.error(f"Failed to spawn engine: {e}")
            return False

        # Poll /health up to ~20s
        start_time = time.time()
        while time.time() - start_time < 20:
            if await _probe():
                return True
            await asyncio.sleep(1)
        logger.error("Engine failed to start within 20s.")
        return False

@app.get("/health")
async def health_check():
    engine_health = None
    url = get_engine_url()
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            resp = await client.get(f"{url}/health")
            if resp.status_code == 200:
                engine_health = resp.json()
    except Exception:
        pass
    
    return {"conductor": "ok", "engine": engine_health}

from pydantic import BaseModel
from fastapi import Body
from automation import AutomationManager, load_refused_keywords, save_refused_keywords, load_quota_keywords, save_quota_keywords, do_account_switch
import health_db

automation_manager = AutomationManager(get_engine_url, ensure_service)

class AutomationRequest(BaseModel):
    mode: str = "rounds"
    goal: int = 100
    config: dict = {}
    clear_pending: bool = True

@app.post("/browser/automation/start")
async def start_automation(req: AutomationRequest):
    ok = automation_manager.start(req.mode, req.goal, req.config, req.clear_pending, is_continue=False)
    return {"status": "success" if ok else "already_running"}

@app.post("/browser/automation/continue")
async def continue_automation(req: AutomationRequest):
    # Goal Protection Check
    mode = req.mode
    goal = req.goal
    cycles = automation_manager.automation_status.get("cycles", 0)
    successes = automation_manager.automation_status.get("successes", 0)
    
    if mode == "rounds" and cycles >= goal:
        return {
            "status": "goal_reached",
            "message": f"Goal Protection: The target goal of {goal} Cycle(s) has already been reached (Current Cycles: {cycles}). Please increase the Target Goal."
        }
    if mode == "images" and successes >= goal:
        return {
            "status": "goal_reached",
            "message": f"Goal Protection: The target goal of {goal} Image(s) has already been reached (Current Images: {successes}). Please increase the Target Goal."
        }

    ok = automation_manager.start(req.mode, req.goal, req.config, req.clear_pending, is_continue=True)
    return {"status": "success" if ok else "already_running"}

@app.post("/browser/automation/stop")
async def stop_automation():
    automation_manager.stop()
    # Unblock any in-flight engine wait so the loop exits within ~2s instead of
    # up to the wait_response timeout (180s). Best-effort: the loop's own stop
    # flag is authoritative; this just makes it react fast.
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(get_engine_url() + "/engine/interrupt")
    except Exception as e:
        logger.warning(f"automation stop: interrupt post failed: {e}")
    return {"status": "success"}

@app.post("/browser/automation/request_new_chat")
async def request_new_chat():
    automation_manager.request_new_chat()
    return {"status": "success"}

@app.get("/browser/automation/stats")
async def get_automation_stats():
    stats = dict(automation_manager.automation_status)
    stats["lc_time_threshold_start_ts"] = getattr(automation_manager, '_lc_time_threshold_start_time', None)
    
    # V1 clears only the dict mirror on read; internal _pending_* counters keep
    # accumulating for loop-control thresholds (V1 engine_service.py:689-692)
    if "pending_refused" in automation_manager.automation_status:
        automation_manager.automation_status["pending_refused"] = 0
    if "pending_resets" in automation_manager.automation_status:
        automation_manager.automation_status["pending_resets"] = 0
        
    return stats

@app.get("/health/events")
async def health_events(date_from: str = None, date_to: str = None,
                        account: str = None, run_id: str = None,
                        limit: int = 2000):
    return {"events": health_db.query_events(date_from, date_to, account, run_id, limit)}

@app.get("/health/summary")
async def health_summary(date_from: str = None, date_to: str = None,
                         group_by: str = "account"):
    if group_by not in ("account", "day"):
        return JSONResponse(status_code=422, content={"detail": "group_by must be 'account' or 'day'"})
    return {"summary": health_db.summary(date_from, date_to, group_by)}

@app.get("/health/runs")
async def health_runs(limit: int = 50):
    return {"runs": health_db.list_runs(limit)}

@app.post("/health/runs/delete")
async def health_run_delete(data: dict = Body(...)):
    run_id = data.get("run_id")
    if not run_id:
        return JSONResponse(status_code=422, content={"detail": "'run_id' required"})
    return {"deleted": health_db.delete_run(run_id)}

@app.post("/health/runs/delete_all")
async def health_runs_delete_all():
    return {"deleted": health_db.delete_all_runs()}

@app.get("/engine/refused_keywords")
async def get_refused_keywords_ep():
    return {"keywords": load_refused_keywords()}

@app.post("/engine/refused_keywords")
async def save_refused_keywords_ep(data: dict = Body(...)):
    keywords = data.get("keywords", [])
    save_refused_keywords(keywords)
    return {"status": "success", "count": len(keywords)}

@app.get("/engine/quota_keywords")
async def get_quota_keywords_ep():
    return {"keywords": load_quota_keywords()}

@app.post("/engine/quota_keywords")
async def save_quota_keywords_ep(data: dict = Body(...)):
    keywords = data.get("keywords", [])
    save_quota_keywords(keywords)
    return {"status": "success", "count": len(keywords)}

@app.post("/engine/reset_time_timer")
async def reset_time_timer():
    automation_manager.reset_time_timer()
    return {"status": "success"}

# ── Phase 4 compat-shim endpoints ────────────────────────────────────────────
# These present the V1 (DT) API surface the Electron UI expects, remapping to the
# V2 engine or handling locally. MUST be declared BEFORE the catch-all proxy below.

@app.get("/engine/preset")
async def get_preset_ep(path: str):
    if not os.path.exists(path):
        return JSONResponse(status_code=404, content={"detail": "Preset file not found"})
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

@app.post("/engine/preset")
async def save_preset_ep(path: str, data: dict = Body(...)):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

@app.get("/engine/image_metadata")
async def image_metadata_ep(path: str):
    if not os.path.exists(path):
        return JSONResponse(status_code=404, content={"detail": "Image file not found"})
    try:
        from PIL import Image
        with Image.open(path) as img:
            return {k: v for k, v in img.info.items() if isinstance(v, str)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

@app.post("/engine/profiles/save")
async def profiles_save_ep(data: dict = Body(...)):
    # Vestigial in V2 (profiles derive from Local State); persist the UI's list anyway.
    profiles = data.get("profiles", [])
    valid = [r for r in profiles if str(r.get("username", "")).strip()]
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "user_login_lookup.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(valid, f, indent=4, ensure_ascii=False)
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

@app.post("/browser/discover")
async def discover_ep(request: Request):
    # Explicit handler rather than the catch-all proxy for two reasons: the scan
    # drives the same page the automation loop does (opening the model drawer
    # mid-run would fight it, and the Escape it presses to close can dismiss a
    # dialog the loop is waiting on), and the result has to be persisted for the
    # setup UI's dropdowns. The automation loop posts to the engine directly, so
    # it does its own persisting — this covers the manual Discover button.
    if automation_manager.automation_status["is_running"]:
        # 200 with status:"error" mirrors the engine's own discover contract —
        # api.js turns any non-2xx into a bare "HTTP error! status: N" and drops
        # the body, which would hide the reason from the user.
        return JSONResponse(content={
            "status": "error",
            "message": "Automation is running — its own scan owns the page. Stop the loop to rescan."
        })

    await ensure_service()
    url = get_engine_url() + "/browser/discover"
    query = request.url.query
    if query:
        url += f"?{query}"
    try:
        async with httpx.AsyncClient(timeout=320.0) as client:
            resp = await client.post(url, content=await request.body(),
                                     headers={"content-type": "application/json"})
            payload = resp.json()

            # A scan that throws comes back as HTTP 500 {"detail": ...}. api.js
            # drops the body of any non-2xx, so restate it in the envelope the
            # UI already reads or the user just sees "HTTP error! status: 500".
            if "status" not in payload:
                return JSONResponse(content={
                    "status": "error",
                    "message": payload.get("detail", "Discovery failed")
                })

            # Persist only a scan that found models: a success with empty results
            # means selector drift, and overwriting would leave the UI with a
            # blank menu. Written through the engine's config endpoint so every
            # config.json write shares one code path.
            data = payload.get("data") or {}
            if payload.get("status") == "success" and data.get("models"):
                try:
                    await client.post(get_engine_url() + "/engine/config", json={"discovery": {
                        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "available_models": data.get("models", []),
                        "available_thinking_levels": data.get("thinking_levels", []),
                        "available_tools": data.get("main_tools", []),
                        "sub_tools": data.get("sub_tools", {})
                    }})
                except Exception as e:
                    logger.warning(f"Failed to persist discovery for the UI: {e}")
    except Exception as e:
        logger.error(f"Discover proxy error: {e}")
        return JSONResponse(status_code=502, content={"status": "error", "message": str(e)})

    return JSONResponse(status_code=resp.status_code, content=payload)

@app.post("/browser/attach_files")
async def attach_files_ep(file_paths: list = Body(...)):
    # Sync wrapper: converge engine attachments to the desired list via file/add + file/remove.
    base = get_engine_url()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            cur_resp = await client.get(base + "/browser/current_attachments")
            current = cur_resp.json().get("attachments", []) if cur_resp.status_code == 200 else []
            desired = list(file_paths)
            # ponytail: compare by basename (engine may report names, not full paths); add-only
            # fallback would over-attach — acceptable, but we attempt removes too.
            cur_names = {os.path.basename(str(c)): c for c in current}
            des_names = {os.path.basename(str(d)): d for d in desired}
            for name, full in des_names.items():
                if name not in cur_names:
                    await client.post(base + "/browser/file/add", json={"path": full})
            for name, orig in cur_names.items():
                if name not in des_names:
                    await client.post(base + "/browser/file/remove", json={"path": orig})
        return {"status": "success", "count": len(desired)}
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": str(e)})

# ── Account switching (single endpoint, 6-step sequence from Gemi_MCP_V2) ────

class AccountSwitchRequest(BaseModel):
    username: str
    profile_dir: str | None = None

@app.post("/account/switch")
async def account_switch_ep(req: AccountSwitchRequest):
    """Switch the active Chrome profile. Delegates to automation.do_account_switch
    (the proven 6-step sequence: stop -> config -> kill Chromium -> remove sandbox
    junctions -> sleep -> start) so this HTTP endpoint and the automation loop's
    rotation/quota paths share one implementation."""
    try:
        result = await do_account_switch(get_engine_url, ensure_service, req.username, req.profile_dir)
        return result
    except Exception as e:
        logger.error(f"Account switch failed: {e}")
        return JSONResponse(status_code=502, content={"ok": False, "detail": str(e)})

@app.get("/engine/profiles")
async def get_engine_profiles_ep():
    """Fetch profiles from the engine and filter out ghost entries whose
    browser_user_data directory does not actually exist on disk."""
    await ensure_service()
    base = get_engine_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}/engine/profiles")
            data = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch engine profiles: {e}")
        return JSONResponse(status_code=502, content={"error": "Bad Gateway"})
    user_data_root = os.path.join(ENGINE_DIR, "browser_user_data")
    profiles = [
        p for p in data.get("profiles", [])
        if p.get("dir") and os.path.isdir(os.path.join(user_data_root, p["dir"]))
    ]
    return {"profiles": profiles}

class ResetSessionRequest(BaseModel):
    config: dict = {}

@app.post("/browser/reset_session")
async def reset_session(req: ResetSessionRequest):
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(get_engine_url() + "/engine/interrupt")
    except Exception as e:
        logger.warning(f"reset_session: interrupt post failed: {e}")

    if automation_manager.automation_status["is_running"]:
        automation_manager.stop()
        for _ in range(20):
            if not automation_manager.automation_status["is_running"]:
                break
            await asyncio.sleep(0.1)

    try:
        res = await automation_manager._init_session(req.config)
        return {"status": "success", "session": res}
    except Exception as e:
        logger.error(f"reset_session failed: {e}")
        return JSONResponse(status_code=500, content={"detail": str(e)})

@app.api_route("/engine/{path:path}", methods=["GET", "POST"])
@app.api_route("/browser/{path:path}", methods=["GET", "POST"])
async def proxy_to_engine(request: Request, path: str):
    await ensure_service()
    
    url = get_engine_url() + request.url.path
    query = request.url.query
    if query:
        url += f"?{query}"
        
    body = await request.body()
    
    # Filter headers
    excluded_headers = {"host", "content-length"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in excluded_headers}
    
    try:
        async with httpx.AsyncClient(timeout=320.0) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers
            )
            ct = resp.headers.get("content-type", "") or ""
            if ct.startswith("application/json") and "charset" not in ct.lower():
                ct = "application/json; charset=utf-8"
            return Response(content=resp.content, status_code=resp.status_code, media_type=ct)
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return JSONResponse(status_code=502, content={"error": "Bad Gateway"})

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=18101, log_level="info")
