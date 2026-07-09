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

automation_manager = AutomationManager(get_engine_url)

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
    return automation_manager.automation_status

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
            return Response(content=resp.content, status_code=resp.status_code, media_type=resp.headers.get("content-type"))
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        return JSONResponse(status_code=502, content={"error": "Bad Gateway"})

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=18101, log_level="info")
