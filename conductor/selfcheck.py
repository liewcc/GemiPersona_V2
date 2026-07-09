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

    for mod in ("gemini_api_client", "processing_utils", "shared_state",
                "inverse_alpha_compositing", "image_notifier", "lama_refiner"):
        try_import(mod)

    print("[OK] All selfchecks passed!")

if __name__ == "__main__":
    main()
