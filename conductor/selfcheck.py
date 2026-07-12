import sys
import os
import json
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
            if e.name in ("torch", "cv2", "playwright"):
                print(f"[NOTE] {name} skipped (heavy dep '{e.name}' not installed yet)")
            else:
                print(f"[FAIL] Failed to import {name}: {e}")
                sys.exit(1)
        except Exception as e:
            print(f"[FAIL] Failed to import {name}: {e}")
            sys.exit(1)

    for mod in ("gemini_api_client", "image_notifier", "automation"):
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
    import time as _time
    mgr.config = {"automation": {"loop_control": loop_ctrl}}
    mgr._lc_time_threshold_start_time = _time.time()
    mgr._pending_refused = 3
    switch, action = mgr._should_switch()
    assert switch is True and action == "next_profile", f"got {switch}, {action}"
    mgr._pending_refused = 2
    switch, action = mgr._should_switch()
    assert switch is False, f"got {switch}, {action}"
    print("[OK] _should_switch passed")

    # Stats endpoint test
    print("Testing automation stats endpoint...")
    response = client.get("/browser/automation/stats")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    assert response.json()["is_running"] is False
    print("[OK] Automation stats endpoint passed")

    print("[OK] All selfchecks passed!")

if __name__ == "__main__":
    main()
