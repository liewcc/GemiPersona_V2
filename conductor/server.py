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
    level=logging.INFO,
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

# Config
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = os.path.join(BASE_DIR, "Gemi_Engine_V2")
CONFIG_PATH = os.path.join(BASE_DIR, "engine_config.json")
ENGINE_VENV_PYTHON = os.path.join(ENGINE_DIR, ".venv", "Scripts", "pythonw.exe")
ENGINE_SCRIPT = "engine_service.py"
ENGINE_OUT = os.path.join(BASE_DIR, "engine_svc.out")

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

    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            resp = await client.get(f"{url}/health")
            if resp.status_code == 200:
                await _check_and_register(resp)
                return True
    except Exception:
        pass
    
    logger.info("Engine unreachable. Spawning new engine process...")
    try:
        out_file = open(ENGINE_OUT, "a")
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
    engine_up = False
    while time.time() - start_time < 20:
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.get(f"{url}/health")
                if resp.status_code == 200:
                    await _check_and_register(resp)
                    engine_up = True
                    break
        except Exception:
            pass
        await asyncio.sleep(1)
        
    if engine_up:
        return True
    else:
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
from automation import AutomationManager, load_refused_keywords, save_refused_keywords, load_quota_keywords, save_quota_keywords

automation_manager = AutomationManager(get_engine_url, ensure_service)

class AutomationRequest(BaseModel):
    mode: str = "rounds"
    goal: int = 100
    config: dict = {}
    clear_pending: bool = True

@app.post("/browser/automation/start")
async def start_automation(req: AutomationRequest):
    automation_manager.start(req.mode, req.goal, req.config, req.clear_pending)
    return {"status": "success"}

@app.post("/browser/automation/continue")
async def continue_automation(req: AutomationRequest):
    req.clear_pending = False
    automation_manager.start(req.mode, req.goal, req.config, req.clear_pending)
    return {"status": "success"}

@app.post("/browser/automation/stop")
async def stop_automation():
    automation_manager.stop()
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

@app.post("/engine/clear_logs")
async def clear_logs_ep():
    # V2 auto-rotates via RotatingFileHandler; nothing to clear.
    return {"status": "success"}

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

class ProcessRequest(BaseModel):
    paths: list = []
    save_dir: str = ""

@app.post("/browser/process")
async def process_images_ep(req: ProcessRequest):
    try:
        from processing_utils import get_shared_processor, save_with_metadata
        from PIL import Image
        # ponytail: GPU flag not wired to V2 config yet; default True. Upgrade: read automation.use_gpu.
        processor = get_shared_processor(use_gpu=True)
        p_dir = os.path.join(req.save_dir, "processed")
        os.makedirs(p_dir, exist_ok=True)
        processed_count = 0
        for path in req.paths:
            if os.path.exists(path):
                with Image.open(path) as img:
                    final_img = processor.hybrid_process(img)
                    p_path = os.path.join(p_dir, os.path.basename(path))
                    save_with_metadata(final_img, img, p_path)
                    processed_count += 1
        return {"status": "success", "processed_count": processed_count, "processed_dir": p_dir}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

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
    """Switch the active Chrome profile. Implements the proven 6-step sequence
    from Gemi_MCP_V2 doSwitchAccount: stop → config → kill Chromium → remove
    sandbox junctions → sleep → start."""
    await ensure_service()
    base = get_engine_url()
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            # 1. Stop current browser (ignore failure if already stopped)
            try:
                await client.post(f"{base}/engine/stop")
            except Exception:
                pass

            # 2. Persist chosen profile + user to engine config
            await client.post(
                f"{base}/engine/config",
                json={"active_profile": req.profile_dir, "active_user": req.username},
            )

            # 3. Kill Playwright Chromium to release file locks
            try:
                subprocess.run(
                    [
                        "powershell", "-NoProfile", "-Command",
                        "Get-Process -Name chrome -ErrorAction SilentlyContinue | "
                        "Where-Object { $_.Path -like '*ms-playwright*' } | "
                        "Stop-Process -Force -ErrorAction SilentlyContinue",
                    ],
                    timeout=15,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass

            # 4. Remove BOTH sandbox junctions (os.rmdir only — NTFS junction safe)
            for sandbox_root in (BASE_DIR, ENGINE_DIR):
                junction = os.path.join(sandbox_root, "browser_session_sandbox", "Default")
                try:
                    if os.path.exists(junction):
                        os.rmdir(junction)
                except Exception:
                    pass

            # 5. Let FS handles settle
            await asyncio.sleep(1)

            # 6. Read config.json and start browser into the target profile
            cfg = {}
            try:
                with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                pass
            headless = bool(cfg.get("headless", False))
            active_service = cfg.get("active_service")
            start_payload = {"headless": headless, "active_user": req.username}
            if active_service:
                start_payload["active_service"] = active_service
            start_resp = await client.post(f"{base}/engine/start", json=start_payload)
            engine_start = start_resp.json() if start_resp.status_code == 200 else {"error": start_resp.text}

        # Diagnostic: resolve what the sandbox junction points to after restart
        junction_target = None
        try:
            junction_target = os.readlink(
                os.path.join(ENGINE_DIR, "browser_session_sandbox", "Default")
            )
        except Exception:
            try:
                junction_target = os.readlink(
                    os.path.join(BASE_DIR, "browser_session_sandbox", "Default")
                )
            except Exception:
                pass

        return {
            "ok": True,
            "active_user": req.username,
            "engine_start": engine_start,
            "junction_target": junction_target,
        }
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
