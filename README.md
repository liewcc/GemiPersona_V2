# GemiPersona V2

Electron-based persona image generation app built on the shared
[Gemi_Engine_V2](https://github.com/liewcc/Gemi_Engine_V2) atomic browser
engine (Playwright-driven Gemini web UI).

## Layers

- **Gemi_Engine_V2/** (submodule, port 18100) — dumb browser executor: atomic
  HTTP endpoints only (navigate, prompt, submit, download, ...), no automation
  logic.
- **conductor/** (port 18101) — orchestration layer: automation loop
  (new chat → apply settings → prompt → submit → wait → download → PNG
  metadata), keyword classification (refused/quota), loop-control thresholds,
  account rotation, SQLite health analytics. Proxies `/engine/*` and
  `/browser/*` to the engine.
- **app/** — Electron UI (setup, profile management, account health, system
  config, utilities).

## Run

```
setup.bat   # first-time install
run.bat     # start conductor + UI (run.vbs launches hidden when enabled)
```

Clone with `git clone --recurse-submodules` (setup does not auto-init the
engine submodule).
