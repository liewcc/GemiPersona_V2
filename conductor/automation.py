import asyncio
import time
import httpx
import os
import json
import re
import logging
import subprocess
import traceback
import health_db

logger = logging.getLogger('conductor')

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REFUSED_FILE = os.path.join(DATA_DIR, "refused_keywords.json")
QUOTA_FILE = os.path.join(DATA_DIR, "quota_full_keywords.json")

# conductor/automation.py -> conductor/ -> project root (same computation server.py uses for BASE_DIR)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = os.path.join(BASE_DIR, "Gemi_Engine_V2")

def load_keywords(filepath):
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)

def save_keywords(filepath, keywords):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(keywords, f, indent=4, ensure_ascii=False)

def load_refused_keywords():
    return load_keywords(REFUSED_FILE)

def save_refused_keywords(keywords):
    save_keywords(REFUSED_FILE, keywords)
    return True

def load_quota_keywords():
    return load_keywords(QUOTA_FILE)

def save_quota_keywords(keywords):
    save_keywords(QUOTA_FILE, keywords)
    return True

def classify_text(text: str) -> str:
    if not text:
        return "error"
    text_lower = text.lower()
    
    quota_kw = load_keywords(QUOTA_FILE)
    for kw in quota_kw:
        if kw.lower() in text_lower:
            return "quota"
            
    refused_kw = load_keywords(REFUSED_FILE)
    for kw in refused_kw:
        if kw.lower() in text_lower:
            return "refused"
            
    return "success"

def write_png_metadata(paths, config):
    """Re-save downloaded PNGs with whitelisted provenance metadata."""
    from PIL import Image, PngImagePlugin
    fields = {
        "aspect_ratio": config.get("aspect_ratio", ""),
        "prompt": config.get("prompt_clean", config.get("prompt", "")),
        "url": config.get("browser_url", ""),
        "upload_path": ", ".join(config.get("selected_files") or []),
    }
    for p in paths:
        try:
            with Image.open(p) as img:
                meta = PngImagePlugin.PngInfo()
                for k in ("aspect_ratio", "prompt", "url", "upload_path"):
                    v = fields.get(k) or img.info.get(k, "")
                    if v:
                        meta.add_text(k, str(v))
                img.save(p, "PNG", pnginfo=meta)
        except Exception as e:
            logger.warning(f"PNG metadata write failed for {p}: {e}")

def _resolve_aspect_ratio(cfg):
    """Mirror of the manual Submit resolution in setup.html: dynamic mode
    (prompt_matrix.enabled) uses the first matrix row with current < target;
    fixed mode uses fixed_aspect_ratio unless it is 'None'.
    Returns (ratio, dynamic, idx); ratio is '' when nothing should be injected."""
    pm = cfg.get("prompt_matrix") or {}
    if pm.get("enabled"):
        for i, it in enumerate(pm.get("items") or []):
            if (it.get("current") or 0) < (it.get("target") or 1):
                return (it.get("ratio") or "", True, i)
        return ("", True, -1)
    fixed = cfg.get("fixed_aspect_ratio") or ""
    return (fixed if fixed != "None" else "", False, -1)


def _inject_ratio(prompt, ratio):
    """Strip any existing 'Aspect Ratio:' prefix, prepend the active one.
    Same regex as the manual path in setup.html.
    Returns (final_prompt, clean_prompt)."""
    clean = re.sub(r"^Aspect Ratio:.*?\n\n", "", prompt or "", flags=re.IGNORECASE | re.DOTALL)
    return (f"Aspect Ratio: {ratio}\n\n{clean}" if ratio else clean, clean)


def filter_ghost_profiles(profiles: list) -> list:
    """Drop profile entries whose browser_user_data directory doesn't exist on disk."""
    user_data_root = os.path.join(ENGINE_DIR, "browser_user_data")
    return [
        p for p in profiles
        if p.get("dir") and os.path.isdir(os.path.join(user_data_root, p["dir"]))
    ]

def _load_root_config():
    try:
        with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


async def do_account_switch(get_engine_url_fn, ensure_service_fn, username: str, profile_dir: str | None = None) -> dict:
    """The proven 6-step account switch (stop -> persist config -> kill orphan
    chromium -> remove sandbox junctions -> sleep -> start), shared by the
    /account/switch HTTP endpoint (server.py) and the automation loop's
    rotation/quota paths, so both go through identical, tested logic."""
    if ensure_service_fn:
        await ensure_service_fn()
    base = get_engine_url_fn()
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            await client.post(f"{base}/engine/stop")
        except Exception:
            pass

        await client.post(
            f"{base}/engine/config",
            json={"active_profile": profile_dir, "active_user": username},
        )

        try:
            subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-Process -Name chrome -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.Path -like '*playwright*' } | "
                    "Stop-Process -Force -ErrorAction SilentlyContinue",
                ],
                timeout=15,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

        for sandbox_root in (BASE_DIR, ENGINE_DIR):
            junction = os.path.join(sandbox_root, "browser_session_sandbox", "Default")
            try:
                if os.path.exists(junction):
                    os.rmdir(junction)
            except Exception:
                pass

        await asyncio.sleep(1)

        cfg = _load_root_config()
        headless = bool(cfg.get("headless", False))
        active_service = cfg.get("active_service")
        start_payload = {
            "headless": headless,
            "active_user": username,
            "profile_name": profile_dir,
        }
        if active_service:
            start_payload["active_service"] = active_service
        start_resp = await client.post(f"{base}/engine/start", json=start_payload)
        engine_start = start_resp.json() if start_resp.status_code == 200 else {"error": start_resp.text}

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
        "active_user": username,
        "engine_start": engine_start,
        "junction_target": junction_target,
    }

class AutomationManager:
    def __init__(self, get_engine_url_fn, ensure_service_fn=None):
        self.get_engine_url = get_engine_url_fn
        self.ensure_service = ensure_service_fn
        self.automation_status = {
            "is_running": False,
            "mode": "rounds",
            "goal": 0,
            "cycles": 0,
            "current_step": "idle",
            "successes": 0,
            "refusals": 0,
            "resets": 0,
            "pending_refused": 0,
            "pending_resets": 0,
            "current_account_id": None,
            "initial_user": None,
            "start_time": None,
            "current_cycle_start_ts": None,
            "inter_cycle_start_ts": None
        }
        self.config = {}
        self._stop_event = asyncio.Event()
        self._task = None
        
        self._cycle_start_time = None
        
        self._pending_refused = 0
        self._pending_resets = 0
        
        self._lc_time_threshold_start_time = None
        self._needs_new_chat = True
        self._session_lost = False
        self._run_id = None
        self._ar_ratio = ""
        self._ar_dynamic = False
        self._ar_idx = -1
        health_db.init_db()
        
    async def _post(self, path, json_data=None):
        url = self.get_engine_url() + path
        async with httpx.AsyncClient(timeout=300.0) as client:
            return await client.post(url, json=json_data)

    async def _get(self, path):
        url = self.get_engine_url() + path
        async with httpx.AsyncClient(timeout=300.0) as client:
            return await client.get(url)

    async def log_to_engine(self, message: str, level: str = "info"):
        try:
            await self._post("/engine/log", {"message": message, "level": level})
        except Exception as e:
            logger.error(f"Failed to log to engine: {e}")

    def start(self, mode: str, goal: int, config: dict, clear_pending: bool) -> bool:
        if self.automation_status["is_running"]:
            return False
            
        self._stop_event.clear()
        
        self.automation_status.update({
            "is_running": True,
            "mode": mode,
            "goal": goal,
            "cycles": 0 if clear_pending else self.automation_status.get("cycles", 0),
            "successes": 0 if clear_pending else self.automation_status.get("successes", 0),
            "refusals": 0 if clear_pending else self.automation_status.get("refusals", 0),
            "resets": 0 if clear_pending else self.automation_status.get("resets", 0)
        })
        
        if clear_pending:
            self._pending_refused = 0
            self._pending_resets = 0
            self.automation_status["pending_refused"] = 0
            self.automation_status["pending_resets"] = 0
            self._lc_time_threshold_start_time = time.time()
            
        if self._lc_time_threshold_start_time is None:
            self._lc_time_threshold_start_time = time.time()
        self.config = config
        
        if self.automation_status.get("start_time") is None or clear_pending:
            from datetime import datetime
            self.automation_status["start_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.automation_status["initial_user"] = config.get("active_user")
        self.automation_status["current_account_id"] = config.get("active_user")
        self._run_id = time.strftime("%Y%m%d-%H%M%S")
        health_db.record_event(self._run_id, "run_start",
                               account=config.get("active_user"),
                               extra={"mode": mode, "goal": goal})
            
        self._task = asyncio.create_task(self._run_loop())
        return True

    def stop(self):
        self._stop_event.set()

    def request_new_chat(self):
        self._needs_new_chat = True
        
    def reset_time_timer(self):
        self._lc_time_threshold_start_time = time.time()
        
    async def _run_loop(self):
        try:
            def _step(name):
                self.automation_status["current_step"] = name
                logger.info(f"[automation] step: {name}")
                asyncio.create_task(self.log_to_engine(f"[automation] step: {name}"))

            if self.ensure_service:
                ok = await self.ensure_service()
                if not ok:
                    logger.error("Engine service could not be started; aborting automation run.")
                    return
            self._session_lost = False
            while self.automation_status["is_running"]:
                if self._session_lost:
                    logger.warning("Session lost detected. Aborting loop.")
                    break
                    
                if self._stop_event.is_set():
                    break
                    
                mode = self.automation_status.get("mode", "rounds")
                goal = self.automation_status.get("goal", 0)
                cycles = self.automation_status.get("cycles", 0)
                successes = self.automation_status.get("successes", 0)
                
                if mode == "rounds" and cycles >= goal:
                    break
                if mode == "images" and successes >= goal:
                    break
                    
                if self._cycle_start_time is None:
                    self._cycle_start_time = time.time()
                    self.automation_status["current_cycle_start_ts"] = self._cycle_start_time
                    self.automation_status["inter_cycle_start_ts"] = None

                # Mid-run loop-control check: reacts to refusal/reset streaks
                # and elapsed time even when no cycle ever succeeds (the
                # success-branch check further down never runs in that case).
                try:
                    switch, action = self._should_switch()
                    if switch:
                        self._pending_refused = 0
                        self._pending_resets = 0
                        self.automation_status["pending_refused"] = 0
                        self.automation_status["pending_resets"] = 0
                        await self._handle_switch_action(action)
                except Exception as e:
                    logger.warning(f"Loop-control switch failed: {e}", exc_info=True)
                    self._needs_new_chat = True
                
                is_initial = (cycles == 0) or self._needs_new_chat
                if is_initial and self.ensure_service:
                    await self.ensure_service()
                
                try:
                    if is_initial:
                        cfg = _load_root_config()
                        _step("engine_start")
                        await self._post("/engine/start", {
                            "headless": bool(cfg.get("headless", False)),
                            "active_user": self.automation_status["current_account_id"],
                            "profile_name": cfg.get("active_profile"),
                            "active_service": cfg.get("active_service") or "gemini"
                        })
                        _step("new_chat")
                        await self._post("/browser/new_chat", {})
                        if self._stop_event.is_set(): break
                        await asyncio.sleep(2)
                        
                        try:
                            logger.debug("Performing live discovery scan at new chat...")
                            _step("discover")
                            discovery_resp = await self._post("/browser/discover")
                            discovery_res = discovery_resp.json()
                            
                            sel_model = self.config.get("selected_model")
                            sel_tool = self.config.get("selected_tool")
                            sel_sub_tool = self.config.get("selected_sub_tool")
                            sel_thinking = self.config.get("selected_thinking_level")
                            
                            model_to_apply = sel_model
                            tool_to_apply = sel_sub_tool or sel_tool
                            thinking_to_apply = sel_thinking
                            
                            if discovery_res.get("status") == "success":
                                discovered = discovery_res.get("data", {})
                                models = discovered.get("models", [])
                                main_tools = discovered.get("main_tools", [])
                                sub_tools = discovered.get("sub_tools", {})
                                thinking_levels = discovered.get("thinking_levels", [])
                                
                                if sel_model and models:
                                    if sel_model not in models:
                                        logger.debug(f"Selected model '{sel_model}' not found in live scan. Leaving empty.")
                                        model_to_apply = None
                                        
                                if sel_thinking and thinking_levels:
                                    if sel_thinking not in thinking_levels:
                                        logger.debug(f"Selected thinking level '{sel_thinking}' not found in live scan. Leaving empty.")
                                        thinking_to_apply = None
                                        
                                if sel_tool and main_tools:
                                    if sel_tool in main_tools:
                                        if sel_tool in sub_tools:
                                            if sel_sub_tool and sub_tools.get(sel_tool):
                                                if sel_sub_tool in sub_tools[sel_tool]:
                                                    tool_to_apply = sel_sub_tool
                                                else:
                                                    logger.debug(f"Selected sub-tool '{sel_sub_tool}' not found under '{sel_tool}'. Leaving empty.")
                                                    tool_to_apply = None
                                            else:
                                                tool_to_apply = sel_sub_tool
                                        else:
                                            tool_to_apply = sel_tool
                                    else:
                                        logger.debug(f"Selected tool '{sel_tool}' not found in live scan. Leaving empty.")
                                        tool_to_apply = None
                            else:
                                logger.warning(f"Discovery scan failed: {discovery_res.get('message')}. Applying settings directly from config.")
                            
                            _step("apply_settings")
                            await self._post("/browser/apply_settings", {
                                "model": model_to_apply,
                                "tool": tool_to_apply,
                                "thinking_level": thinking_to_apply
                            })
                            
                            has_files = self.config.get("selected_files")
                            if has_files:
                                _step("attach_files")
                                for f_path in has_files:
                                    await self._post("/browser/file/add", {"path": f_path})
                        except Exception as e:
                            logger.warning(f"Settings setup failed: {e}")

                        _step("prompt")
                        ratio, ar_dynamic, ar_idx = _resolve_aspect_ratio(_load_root_config())
                        self._ar_ratio, self._ar_dynamic, self._ar_idx = ratio, ar_dynamic, ar_idx
                        final_prompt, clean_prompt = _inject_ratio(self.config.get("prompt", ""), ratio)
                        self.config["aspect_ratio"] = ratio
                        self.config["prompt_clean"] = clean_prompt
                        await self._post("/browser/prompt", {"text": final_prompt})
                        if self._stop_event.is_set(): break
                        
                        _step("submit")
                        await self._post("/browser/submit", {})
                        if self._stop_event.is_set(): break
                        self._needs_new_chat = False
                    else:
                        _step("redo")
                        redo_resp = await self._post("/browser/redo", {})
                        if redo_resp.status_code != 200 or redo_resp.json().get("status") != "success":
                            self._record_reset()
                            continue
                    
                    _step("wait_response")
                    wait_resp = await self._post("/browser/wait_response", {"timeout": 180})
                    wait_data = wait_resp.json()
                    
                    status = wait_data.get("status")
                    if status == "done":
                        if wait_data.get("has_image"):
                            dl_cfg = {
                                "save_dir": self.config.get("save_dir", ""),
                                "prefix": self.config.get("name_prefix", ""),
                                "padding": self.config.get("name_padding", 2),
                                "start": self.config.get("name_start", 1)
                            }
                            _step("download")
                            dl_resp = await self._post("/browser/download", dl_cfg)
                            dl_data = dl_resp.json()
                            
                            if dl_data.get("status") == "success":
                                saved_paths = dl_data.get("saved_paths", [])
                                if saved_paths:
                                    write_png_metadata(saved_paths, self.config)
                                    self.config["name_start"] = dl_data.get("next_start", dl_cfg["start"])
                                    self.automation_status["successes"] += 1
                                    self.automation_status["cycles"] += 1

                                    cycle_dur = time.time() - self._cycle_start_time
                                    health_db.record_event(
                                        self._run_id, "success",
                                        account=self.automation_status["current_account_id"],
                                        cycle_index=self.automation_status["cycles"],
                                        duration_sec=cycle_dur,
                                        filename=os.path.basename(saved_paths[0]) if saved_paths else None)

                                    if self._ar_dynamic and self._ar_idx >= 0:
                                        # Advance the dynamic ratio matrix; persist through the
                                        # engine so writes share one code path with the UI.
                                        try:
                                            pm = _load_root_config().get("prompt_matrix") or {}
                                            items = pm.get("items") or []
                                            if self._ar_idx < len(items):
                                                it = items[self._ar_idx]
                                                it["current"] = (it.get("current") or 0) + 1
                                                await self._post("/engine/config", {"prompt_matrix": pm})
                                                if it["current"] >= (it.get("target") or 1):
                                                    # Row complete: force a new chat so the next
                                                    # cycle re-resolves and injects the next ratio.
                                                    self._needs_new_chat = True
                                        except Exception as e:
                                            logger.warning(f"prompt_matrix advance failed: {e}")
                                    
                                    cycle_refused_snap = self._pending_refused
                                    cycle_resets_snap = self._pending_resets
                                    
                                    self._pending_refused = 0
                                    self._pending_resets = 0
                                    self.automation_status["pending_refused"] = 0
                                    self.automation_status["pending_resets"] = 0
                                    
                                    self._cycle_start_time = None
                                    self.automation_status["current_cycle_start_ts"] = None
                                    self.automation_status["inter_cycle_start_ts"] = time.time()
                                    
                                    result = {
                                        "cycle_duration_sec": cycle_dur,
                                        "cycle_refused": cycle_refused_snap,
                                        "cycle_resets": cycle_resets_snap,
                                        "time_threshold_duration_sec": time.time() - (self._lc_time_threshold_start_time or time.time())
                                    }
                                    
                                    loop_ctrl = self.config.get("automation", {}).get("loop_control", {})
                                    switch, action = self._check_loop_control_thresholds(loop_ctrl, result)
                                    if switch:
                                        await self._handle_switch_action(action)
                                else:
                                    self._record_reset()
                            else:
                                self._record_reset()
                        else:
                            resp_data = await self._get("/browser/last_response")
                            text = resp_data.json().get("text", "")
                            cls_status = classify_text(text)
                            if cls_status == "refused":
                                self.automation_status["cycles"] += 1
                                self.automation_status["refusals"] += 1
                                health_db.record_event(
                                    self._run_id, "refused",
                                    account=self.automation_status["current_account_id"],
                                    cycle_index=self.automation_status["cycles"],
                                    extra={"text": (text or "")[:200]})
                                self._pending_refused += 1
                                self.automation_status["pending_refused"] = self._pending_refused
                            elif cls_status == "quota":
                                await self._handle_quota()
                            else:
                                self._record_reset()
                    elif status == "error":
                        if "quota" in str(wait_data.get("message", "")).lower():
                            await self._handle_quota()
                        else:
                            self._record_reset()
                    elif status == "timeout":
                        self._record_reset()
                    elif status == "reset":
                        self._record_reset()
                    else:
                        logger.warning(f"Unhandled wait status: {status}; treating as reset")
                        self._record_reset()
                        
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.warning(f"Recoverable cycle error: {e}", exc_info=True)
                    self._record_reset()
                    
        except Exception as e:
            logger.error(f"Automation loop error: {traceback.format_exc()}")
        finally:
            self.automation_status["is_running"] = False
            self.automation_status["current_step"] = "idle"
            s = self.automation_status
            health_db.record_event(
                self._run_id, "run_end", account=s.get("current_account_id"),
                extra={"cycles": s.get("cycles"), "successes": s.get("successes"),
                       "refusals": s.get("refusals"), "resets": s.get("resets")})

    def _record_reset(self):
        self.automation_status["resets"] += 1
        self.automation_status["cycles"] += 1
        self._pending_resets += 1
        self.automation_status["pending_resets"] = self._pending_resets
        self._needs_new_chat = True
        health_db.record_event(
            self._run_id, "reset",
            account=self.automation_status["current_account_id"],
            cycle_index=self.automation_status["cycles"])
        
    def _check_loop_control_thresholds(self, loop_ctrl: dict, result: dict):
        if not loop_ctrl:
            return False, "next_profile"

        dur_min = result.get("cycle_duration_sec", 0) / 60.0
        time_dur_min = result.get("time_threshold_duration_sec", 0) / 60.0
        refused = result.get("cycle_refused", 0)
        resets = result.get("cycle_resets", 0)

        if loop_ctrl.get("time_enabled") and time_dur_min >= loop_ctrl.get("time_minutes", 999):
            return True, loop_ctrl.get("time_action", "next_profile")
        if loop_ctrl.get("refused_enabled") and refused >= loop_ctrl.get("refused_threshold", 999):
            return True, loop_ctrl.get("refused_action", "next_profile")
        if loop_ctrl.get("reset_enabled") and resets >= loop_ctrl.get("reset_threshold", 999):
            return True, loop_ctrl.get("reset_action", "next_profile")

        return False, "next_profile"
        
    def _should_switch(self):
        """Evaluate loop-control thresholds against the LIVE pending
        counters. The pre-existing check in the success branch of _run_loop
        only runs after a successful download; this helper lets the loop also
        react to pure refusal/reset streaks (and elapsed time) that never
        produce a success."""
        loop_ctrl = self.config.get("automation", {}).get("loop_control", {})
        elapsed = time.time() - self._lc_time_threshold_start_time if self._lc_time_threshold_start_time else 0
        return self._check_loop_control_thresholds(loop_ctrl, {
            "cycle_refused": self._pending_refused,
            "cycle_resets": self._pending_resets,
            "time_threshold_duration_sec": elapsed,
        })

    async def _handle_switch_action(self, action):
        self._lc_time_threshold_start_time = time.time()
        if action == "next_profile":
            await self._switch_to_next_profile()
        elif action == "re_login":
            await self._post("/engine/re_login", {})
            self._needs_new_chat = True
            health_db.record_event(
                self._run_id, "re_login",
                account=self.automation_status["current_account_id"])

    async def _switch_to_next_profile(self):
        resp = await self._get("/engine/profiles")
        profiles = filter_ghost_profiles(resp.json().get("profiles", []))
        if not profiles:
            return
            
        cur = self.automation_status["current_account_id"]
        idx = -1
        for i, p in enumerate(profiles):
            if p.get("email") == cur or p.get("name") == cur:
                idx = i
                break
        
        next_idx = (idx + 1) % len(profiles)
        next_profile = profiles[next_idx]
        next_user = next_profile.get("email") or next_profile.get("name")
        
        await do_account_switch(self.get_engine_url, self.ensure_service, next_user, next_profile.get("dir"))
        health_db.record_event(
            self._run_id, "switch", account=next_user,
            extra={"from": cur, "reason": "loop_control"})
        self.automation_status["current_account_id"] = next_user
        self._needs_new_chat = True

    async def _handle_quota(self):
        health_db.record_event(
            self._run_id, "quota",
            account=self.automation_status["current_account_id"])
        await self._post("/engine/stop", {})
        
        resp = await self._get("/engine/profiles")
        profiles = filter_ghost_profiles(resp.json().get("profiles", []))
        if not profiles:
            logger.error("Quota hit but no usable profiles exist; ending run.")
            self._session_lost = True
            return
        
        cur = self.automation_status["current_account_id"]
        idx = -1
        for i, p in enumerate(profiles):
            if p.get("email") == cur or p.get("name") == cur:
                idx = i
                break
        
        next_idx = (idx + 1) % len(profiles)
        next_profile = profiles[next_idx]
        next_user = next_profile.get("email") or next_profile.get("name")
        
        initial = self.automation_status["initial_user"]
        
        if next_user == initial:
            loop_ctrl = self.config.get("automation", {}).get("loop_control", {})
            inf_en = loop_ctrl.get("infinite_loop_enabled", False)
            if not inf_en:
                self._session_lost = True
                return
            else:
                sleep_min = loop_ctrl.get("infinite_loop_minutes", 60)
                for _ in range(int(sleep_min * 60)):
                    if self._stop_event.is_set():
                        break
                    await asyncio.sleep(1)
                if self._stop_event.is_set():
                    return
        
        await do_account_switch(self.get_engine_url, self.ensure_service, next_user, next_profile.get("dir"))
        health_db.record_event(
            self._run_id, "switch", account=next_user,
            extra={"from": cur, "reason": "quota"})
        self.automation_status["current_account_id"] = next_user
        self._needs_new_chat = True
