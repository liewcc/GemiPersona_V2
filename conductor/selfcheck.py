import sys
import os
import json
import time
import logging
from fastapi.testclient import TestClient

# Add current dir to path to allow imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import app

def main():
    print("Running selfcheck...")

    # 1. Test /health
    print("Testing /health route...")
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert data.get("conductor") == "ok", f"Expected conductor: ok, got {data}"
    print(f"Health check passed: {data}")

    # 2. Test imports — heavy deps (torch/cv2) arrive in Phase 3, skip modules that need them
    print("Testing imports...")
    import importlib

    def try_import(name):
        try:
            importlib.import_module(name)
            print(f"[OK] {name} imported successfully")
        except ModuleNotFoundError as e:
            if e.name in ("torch", "cv2", "playwright", "tkinter", "_tkinter", "pystray"):
                print(f"[NOTE] {name} skipped (heavy dep '{e.name}' not installed yet)")
            else:
                print(f"[FAIL] Failed to import {name}: {e}")
                sys.exit(1)
        except Exception as e:
            print(f"[FAIL] Failed to import {name}: {e}")
            sys.exit(1)

    for mod in ("image_notifier", "automation"):
        try_import(mod)

    # Classification test
    print("Testing classification...")
    from automation import classify_text
    assert classify_text("I cannot do this") == "refused"
    assert classify_text("quota exceeded") == "quota"
    assert classify_text("here is your request") == "success"
    print("[OK] Classification test passed")

    # Threshold logic test
    print("Testing threshold logic...")
    from automation import AutomationManager
    mgr = AutomationManager(lambda: "http://localhost")
    loop_ctrl = {
        "time_enabled": True, "time_minutes": 10, "time_action": "re_login",
        "refused_enabled": True, "refused_threshold": 3, "refused_action": "next_profile",
        "reset_enabled": True, "reset_threshold": 5, "reset_action": "next_profile"
    }
    # Time threshold hit
    switch, action = mgr._check_loop_control_thresholds(loop_ctrl, {"time_threshold_duration_sec": 601, "cycle_refused": 0, "cycle_resets": 0})
    assert switch is True and action == "re_login"
    # Refused threshold hit
    switch, action = mgr._check_loop_control_thresholds(loop_ctrl, {"time_threshold_duration_sec": 0, "cycle_refused": 3, "cycle_resets": 0})
    assert switch is True and action == "next_profile"
    # No threshold hit
    switch, action = mgr._check_loop_control_thresholds(loop_ctrl, {"time_threshold_duration_sec": 0, "cycle_refused": 1, "cycle_resets": 1})
    assert switch is False
    print("[OK] Threshold logic passed")

    # Mid-run switch decision (live pending counters)
    print("Testing _should_switch...")
    mgr.config = {"automation": {"loop_control": loop_ctrl}}
    mgr._lc_time_threshold_start_time = time.time()
    mgr._pending_refused = 3
    switch, action = mgr._should_switch()
    assert switch is True and action == "next_profile", f"got {switch}, {action}"
    mgr._pending_refused = 2
    switch, action = mgr._should_switch()
    assert switch is False, f"got {switch}, {action}"
    print("[OK] _should_switch passed")

    # Aspect ratio resolution + injection
    print("Testing aspect ratio helpers...")
    from automation import _resolve_aspect_ratio, _inject_ratio
    r, dyn, idx = _resolve_aspect_ratio({"fixed_aspect_ratio": "16:9 (Landscape)"})
    assert r == "16:9 (Landscape)" and dyn is False, f"got {r}, {dyn}"
    r, dyn, idx = _resolve_aspect_ratio({"fixed_aspect_ratio": "None"})
    assert r == "", f"got {r}"
    pm = {"enabled": True, "items": [
        {"ratio": "16:9 (Landscape)", "target": 2, "current": 2},
        {"ratio": "1:1 (Square)", "target": 3, "current": 1},
    ]}
    r, dyn, idx = _resolve_aspect_ratio({"prompt_matrix": pm, "fixed_aspect_ratio": "4:3"})
    assert r == "1:1 (Square)" and dyn is True and idx == 1, f"got {r}, {dyn}, {idx}"
    pm["items"][1]["current"] = 3
    r, dyn, idx = _resolve_aspect_ratio({"prompt_matrix": pm})
    assert r == "" and idx == -1, f"got {r}, {idx}"
    final, clean = _inject_ratio("Aspect Ratio: old\n\nA cat", "1:1 (Square)")
    assert final == "Aspect Ratio: 1:1 (Square)\n\nA cat" and clean == "A cat", f"got {final!r}"
    final, clean = _inject_ratio("A cat", "")
    assert final == "A cat" and clean == "A cat", f"got {final!r}"
    print("[OK] Aspect ratio helpers passed")

    # Stats endpoint test
    print("Testing automation stats endpoint...")
    response = client.get("/browser/automation/stats")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    assert response.json()["is_running"] is False
    print("[OK] Automation stats endpoint passed")

    # Time threshold reset-on-success: behavioural checks
    print("Testing time threshold reset-on-success...")

    # (b) Behavioural check: _check_loop_control_thresholds must fire True
    #     when the elapsed time exceeds the threshold (strict timeout).
    mgr2 = AutomationManager(lambda: "http://localhost")
    loop_ctrl_tt = {
        "time_enabled": True, "time_minutes": 1, "time_action": "re_login",
        "refused_enabled": False, "refused_threshold": 99,
        "reset_enabled": False, "reset_threshold": 99,
    }
    mgr2._lc_time_threshold_start_time = time.time() - 90  # 90 s elapsed, threshold = 60 s
    result_tt = {
        "cycle_duration_sec": 5,
        "cycle_refused": 0,
        "cycle_resets": 0,
        "time_threshold_duration_sec": time.time() - mgr2._lc_time_threshold_start_time,
    }
    switch_tt, action_tt = mgr2._check_loop_control_thresholds(loop_ctrl_tt, result_tt)
    assert switch_tt is True and action_tt == "re_login", (
        f"(b) strict timeout: expected switch=True/re_login, got {switch_tt}/{action_tt}"
    )
    print("[OK] (b) strict-timeout behavioural check passed")

    # Ordering rule: on a cycle that succeeded but already overran the time
    # threshold, _on_cycle_success must still trigger a switch BEFORE it
    # restarts the timer. Resetting first would erase the overrun and the
    # switch would never fire.
    print("Testing strict-timeout ordering on cycle success...")
    import asyncio as _asyncio
    mgr3 = AutomationManager(lambda: "http://localhost")
    mgr3.config = {"automation": {"loop_control": {
        "time_enabled": True, "time_minutes": 1, "time_action": "re_login",
        "refused_enabled": False, "refused_threshold": 99,
        "reset_enabled": False, "reset_threshold": 99,
    }}}
    _switch_calls = []
    _elapsed_seen = []

    async def _fake_switch(action):
        # Record what the timer looked like at the moment the switch fired.
        _elapsed_seen.append(time.time() - mgr3._lc_time_threshold_start_time)
        _switch_calls.append(action)

    mgr3._handle_switch_action = _fake_switch
    mgr3._lc_time_threshold_start_time = time.time() - 90  # 90 s elapsed, threshold = 60 s
    _result3 = {
        "cycle_duration_sec": 5,
        "cycle_refused": 0,
        "cycle_resets": 0,
        "time_threshold_duration_sec": 90,
    }
    _asyncio.run(mgr3._on_cycle_success(_result3))

    assert _switch_calls == ["re_login"], (
        "REGRESSION (Strict Timeout): a successful cycle that overran the time "
        f"threshold did not trigger a switch. Expected ['re_login'], got {_switch_calls}. "
        "The most likely cause is the timer reset in _on_cycle_success being moved "
        "ABOVE the _check_loop_control_thresholds call."
    )
    assert _elapsed_seen and _elapsed_seen[0] >= 60, (
        "REGRESSION (Strict Timeout): the switch fired but the timer had already "
        f"been reset first (elapsed at switch time was {_elapsed_seen[0]:.1f}s, expected >= 60s)."
    )
    assert time.time() - mgr3._lc_time_threshold_start_time < 5, (
        "_on_cycle_success must restart the time-threshold timer after the check."
    )
    print("[OK] strict-timeout ordering check passed")

    print("[OK] Time threshold reset-on-success and strict-timeout checks passed")

    # Engine-log rotation
    print("Testing engine_svc.out rotation...")
    import tempfile
    from server import rotate_if_oversized, MAX_LOG_BYTES
    with tempfile.TemporaryDirectory() as _tmp:
        _p = os.path.join(_tmp, "engine_svc.out")
        rotate_if_oversized(_p)   # a missing log must be a no-op, not a crash
        with open(_p, "wb") as f:
            f.write(b"x" * 16)
        rotate_if_oversized(_p)
        assert os.path.exists(_p) and not os.path.exists(_p + ".1"), (
            "a log under MAX_LOG_BYTES must be left alone"
        )
        with open(_p, "wb") as f:
            f.write(b"x" * MAX_LOG_BYTES)
        rotate_if_oversized(_p)
        assert not os.path.exists(_p), "an oversized log must be moved aside"
        assert os.path.getsize(_p + ".1") == MAX_LOG_BYTES, "rotated content was lost"
        with open(_p, "wb") as f:
            f.write(b"y" * MAX_LOG_BYTES)
        rotate_if_oversized(_p)
        assert not os.path.exists(_p) and os.path.getsize(_p + ".1") == MAX_LOG_BYTES, (
            "rotating a second time must replace the previous generation, not fail"
        )
    print("[OK] engine_svc.out rotation passed")

    # ── Attach guard: never submit a cycle with missing reference images ────────
    # A signed-out profile fails every upload while answering 200 elsewhere, so the
    # guard trusts the page's attachment count, not the POST results.
    print("Testing attach guard...")
    import asyncio as _asyncio
    import automation as _auto
    from automation import AutomationManager

    # This check deliberately drives the failure path, which logs at WARNING on the
    # shared 'conductor' logger. Left alone it writes convincing "attach failed"
    # lines into the live conductor.log — a false trail for whoever debugs next.
    logging.getLogger('conductor').setLevel(logging.CRITICAL)

    class _Resp:
        def __init__(self, payload): self._payload = payload
        def json(self): return self._payload

    def _run_guard(desired, on_page, raise_on_get=False, break_settings=False):
        """Drive _init_session with a stubbed engine; return its verdict."""
        mgr = AutomationManager.__new__(AutomationManager)
        mgr.automation_status = {"current_step": None, "current_account_id": None}
        mgr._stop_event = _asyncio.Event()
        mgr._needs_discovery = False
        mgr._ar_ratio = mgr._ar_dynamic = mgr._ar_idx = None
        posted = []

        async def _post(path, json_data=None):
            posted.append(path)
            if break_settings and path == "/browser/apply_settings":
                raise RuntimeError("settings step blew up")
            return _Resp({"status": "success"})

        async def _get(path):
            if raise_on_get:
                raise RuntimeError("engine unreachable")
            return _Resp({"attachments": ["a"] * on_page})

        async def _log(msg, level="info"): return None
        mgr._post, mgr._get, mgr.log_to_engine = _post, _get, _log
        mgr.get_engine_url = lambda: "http://127.0.0.1:0"
        mgr.ensure_service = None

        # _init_session live-reads config.json; pin it so the check does not
        # depend on whatever the user currently has selected.
        real_loader = _auto._load_root_config
        _auto._load_root_config = lambda: {"selected_files": list(desired)}
        try:
            out = _asyncio.new_event_loop().run_until_complete(
                mgr._init_session({"selected_files": list(desired), "prompt": "p"}))
        finally:
            _auto._load_root_config = real_loader
        return out, posted

    _files = ["a.png", "b.png", "c.png"]
    out, posted = _run_guard(_files, on_page=0)
    assert out.get("aborted") == "attach_failed", "0 of 3 attached must abort the cycle"
    assert "/browser/submit" not in posted, "aborted cycle must not submit"

    out, posted = _run_guard(_files, on_page=2)
    assert out.get("aborted") == "attach_failed", "a partial attach must abort too"

    out, posted = _run_guard(_files, on_page=3)
    assert not out.get("aborted"), "a complete attach must proceed"
    assert "/browser/submit" in posted, "a complete attach must reach submit"

    out, posted = _run_guard([], on_page=0)
    assert not out.get("aborted"), "no reference images configured is not a failure"

    out, posted = _run_guard(_files, on_page=3, raise_on_get=True)
    assert out.get("aborted") == "attach_failed", (
        "an unverifiable attach must abort, not submit blind"
    )

    out, posted = _run_guard(_files, on_page=0, break_settings=True)
    assert out.get("aborted") == "attach_failed", (
        "the guard must still fire when the settings block throws before attaching"
    )
    assert "/browser/submit" not in posted, "aborted cycle must not submit"
    print("[OK] Attach guard passed")

    print("Testing discovery persistence...")

    def _run_discovery(scan_data):
        """Drive _init_session with a scan pending; return what got POSTed to
        /engine/config (the setup UI reads config.json for its menu options)."""
        mgr = AutomationManager.__new__(AutomationManager)
        mgr.automation_status = {"current_step": None, "current_account_id": None}
        mgr._stop_event = _asyncio.Event()
        mgr._needs_discovery = True
        mgr._ar_ratio = mgr._ar_dynamic = mgr._ar_idx = None
        written = []

        async def _post(path, json_data=None):
            if path == "/engine/config" and "discovery" in (json_data or {}):
                written.append(json_data["discovery"])
            if path == "/browser/discover":
                return _Resp({"status": "success", "data": scan_data})
            return _Resp({"status": "success"})

        async def _get(path):
            return _Resp({"attachments": []})

        async def _log(msg, level="info"): return None
        mgr._post, mgr._get, mgr.log_to_engine = _post, _get, _log
        mgr.get_engine_url = lambda: "http://127.0.0.1:0"
        mgr.ensure_service = None

        real_loader = _auto._load_root_config
        _auto._load_root_config = lambda: {"selected_files": []}
        try:
            _asyncio.new_event_loop().run_until_complete(
                mgr._init_session({"selected_files": [], "prompt": "p"}))
        finally:
            _auto._load_root_config = real_loader
        return written

    _good = {"models": ["3.0 Pro", "3.0 Flash"], "thinking_levels": ["High"],
             "main_tools": ["Create images"], "sub_tools": {"More tools": ["Deep Research"]}}
    written = _run_discovery(_good)
    assert len(written) == 1, "a good scan must be persisted for the UI exactly once"
    assert written[0]["available_models"] == _good["models"], "model list must reach config.json"
    assert written[0]["available_tools"] == _good["main_tools"], "tool list must reach config.json"
    assert written[0]["sub_tools"] == _good["sub_tools"], "sub-tool map must reach config.json"
    assert written[0].get("last_updated"), "the UI shows this stamp to flag a stale menu"

    # Selector drift returns success with nothing found. Persisting that would
    # blank the menu on next launch — the exact thing this feature prevents.
    assert _run_discovery({"models": [], "thinking_levels": [],
                           "main_tools": [], "sub_tools": {}}) == [], (
        "an empty scan must not overwrite the last good menu")

    # The gemini provider used to return its own {"status","data"} envelope,
    # which engine_service wrapped again. One unwrap then landed on the inner
    # envelope, so models was always empty: no menu was ever saved and every
    # model/tool check below silently passed. Nothing asserted on the payload's
    # shape, so it went unnoticed — pin it here.
    assert _run_discovery({"status": "success", "data": _good}) == [], (
        "a double-wrapped payload has no models at the top level and must not "
        "be mistaken for a good scan")
    print("[OK] Discovery persistence passed")

    print("[OK] All selfchecks passed!")

if __name__ == "__main__":
    main()
