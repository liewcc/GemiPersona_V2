const { app, BrowserWindow, ipcMain, dialog, shell, Tray, Menu, nativeImage, Notification } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn, execFile } = require('child_process');

let mainWindow;
let engineProcess;   // NOTE: this is the CONDUCTOR process in V2 (it supervises the engine itself)
let tray = null;

// ── Conductor log file (replaces stdio:inherit so CLI window is hidden) ────────
// V2 layout: config.json lives at project root; the conductor writes its own
// conductor.log via RotatingFileHandler — this file only captures piped stdout.
const ENGINE_LOG_PATH = path.join(__dirname, '..', 'conductor_ui.log');
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');
// V2 engine derives profiles from Local State; no login_lookup file. This path
// backs only the offline IPC fallback (rarely fires — conductor auto-spawns engine).
const LOGIN_LOOKUP_PATH = path.join(__dirname, '..', 'conductor', 'data', 'user_login_lookup.json');

function loadConfig() {
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      const content = fs.readFileSync(CONFIG_PATH, 'utf-8');
      return JSON.parse(content);
    }
  } catch (error) {
    console.error('Failed to load config in main.js:', error);
  }
  return {};
}

// ── Register IPC handlers for file dialogs ────────────────────────────────────
ipcMain.handle('select-file', async (event, options) => {
  const result = await dialog.showOpenDialog(mainWindow, options || {
    properties: ['openFile']
  });
  return result.filePaths;
});

ipcMain.handle('select-directory', async (event, options) => {
  const result = await dialog.showOpenDialog(mainWindow, options || {
    properties: ['openDirectory']
  });
  return result.filePaths;
});

ipcMain.handle('save-file', async (event, options) => {
  const result = await dialog.showSaveDialog(mainWindow, options);
  return result.filePath;
});

ipcMain.handle('open-file', async (event, filePath) => {
  return await shell.openPath(filePath);
});

ipcMain.handle('show-item-in-folder', async (event, filePath) => {
  shell.showItemInFolder(filePath);
});

ipcMain.handle('read-image-base64', async (event, filePath) => {
  try {
    const data = await fs.promises.readFile(filePath);
    const ext = path.extname(filePath).toLowerCase().replace('.', '');
    const mimeType = ext === 'png' ? 'image/png' : ext === 'webp' ? 'image/webp' : 'image/jpeg';
    return `data:${mimeType};base64,${data.toString('base64')}`;
  } catch (error) {
    console.error(error);
    return '';
  }
});

ipcMain.handle('read-png-metadata', async (event, filePath) => {
  try {
    const buf = await fs.promises.readFile(filePath);
    // Verify PNG signature
    if (buf.length < 8 || buf.readUInt32BE(0) !== 0x89504E47) return {};
    const meta = {};
    let offset = 8;
    while (offset + 12 <= buf.length) {
      const length = buf.readUInt32BE(offset);
      const type = buf.slice(offset + 4, offset + 8).toString('ascii');
      if (type === 'IEND') break;
      if (type === 'tEXt' && length > 0) {
        const data = buf.slice(offset + 8, offset + 8 + length);
        const nul = data.indexOf(0);
        if (nul !== -1) {
          meta[data.slice(0, nul).toString('latin1')] = data.slice(nul + 1).toString('latin1');
        }
      } else if (type === 'iTXt' && length > 0) {
        const data = buf.slice(offset + 8, offset + 8 + length);
        const nul = data.indexOf(0);
        if (nul !== -1) {
          const key = data.slice(0, nul).toString('utf8');
          // skip: compressionFlag(1) + compressionMethod(1) + languageTag(null) + translatedKey(null)
          let pos = nul + 3;
          while (pos < data.length && data[pos] !== 0) pos++;
          pos++;
          while (pos < data.length && data[pos] !== 0) pos++;
          pos++;
          meta[key] = data.slice(pos).toString('utf8');
        }
      }
      offset += 4 + 4 + length + 4;
    }
    return meta;
  } catch (e) {
    return {};
  }
});

// ── Find latest image helper ──────────────────────────────────────────────────
function findLatestImage(saveDir) {
  try {
    if (!saveDir || !fs.existsSync(saveDir)) return null;
    const files = fs.readdirSync(saveDir);
    let latestFile = null;
    let latestMtime = 0;
    for (const file of files) {
      const ext = path.extname(file).toLowerCase();
      if (['.png', '.jpg', '.jpeg', '.webp'].includes(ext)) {
        const fullPath = path.join(saveDir, file);
        const stat = fs.statSync(fullPath);
        if (stat.isFile() && stat.mtimeMs > latestMtime) {
          latestMtime = stat.mtimeMs;
          latestFile = fullPath;
        }
      }
    }
    return latestFile;
  } catch (e) {
    console.error('Failed to find latest image:', e);
    return null;
  }
}

// ── IPC: Renderer → Main: trigger tray balloon for new images ─────────────────
ipcMain.on('notify-download', (event, { count, saveDir, silent, imagePath }) => {
  showDownloadNotification(count, saveDir, silent, imagePath);
});

// ── Tray balloon / system notification helper ─────────────────────────────────
function showDownloadNotification(count, saveDir, silent = false, imagePath = '') {
  if (!tray) return;

  const title = `${count} image${count > 1 ? 's' : ''} downloaded`;
  const body = saveDir ? `Saved to: ${saveDir}` : 'Images downloaded successfully';

  // Windows 10+: use native Notification API via Electron
  if (Notification.isSupported()) {
    const notif = new Notification({
      title,
      body,
      icon: path.join(__dirname, '..', 'assets', 'sys_img', 'icon_no_BG.png'),
      silent: silent
    });
    notif.on('click', () => {
      const config = loadConfig();
      const clickAction = config.notification_click_action || 'download_folder';
      
      if (clickAction === 'default_viewer') {
        let targetPath = imagePath;
        if (!targetPath && saveDir) {
          targetPath = findLatestImage(saveDir);
        }
        if (targetPath && fs.existsSync(targetPath)) {
          shell.openPath(targetPath);
        } else if (saveDir) {
          shell.openPath(saveDir);
        }
      } else {
        if (saveDir) shell.openPath(saveDir);
      }
    });
    notif.show();
  } else {
    // Fallback: Windows tray balloon (Electron < 20 / older Windows)
    tray.displayBalloon({
      title,
      content: body,
      iconType: 'info'
    });
  }
}

// ── Window helpers ────────────────────────────────────────────────────────────
function showMainWindow() {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
  // Update tray context menu state
  updateTrayMenu();
}

function hideMainWindow() {
  if (!mainWindow) return;
  mainWindow.hide();
  updateTrayMenu();
}

// ── Tray context menu (rebuilt dynamically) ───────────────────────────────────
function buildTrayMenu() {
  const isVisible = mainWindow && mainWindow.isVisible();
  return Menu.buildFromTemplate([
    {
      label: isVisible ? 'Hide Window' : 'Show Window',
      click: () => {
        if (isVisible) hideMainWindow(); else showMainWindow();
      }
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => {
        app.isQuitting = true;
        app.quit();
      }
    }
  ]);
}

function updateTrayMenu() {
  if (tray) tray.setContextMenu(buildTrayMenu());
}

// ── Create system tray ────────────────────────────────────────────────────────
function createTray() {
  // Prefer .ico for Windows tray (sharper at 16×16); fallback to .png
  const icoPath = path.join(__dirname, '..', 'assets', 'sys_img', 'icon_no_BG.ico');
  const pngPath = path.join(__dirname, '..', 'assets', 'sys_img', 'icon.png');
  const iconPath = fs.existsSync(icoPath) ? icoPath : pngPath;

  tray = new Tray(iconPath);
  tray.setToolTip('Gemipersona Pro');
  tray.setContextMenu(buildTrayMenu());

  // Double-click tray icon → toggle window visibility
  tray.on('double-click', () => {
    if (mainWindow && mainWindow.isVisible()) {
      hideMainWindow();
    } else {
      showMainWindow();
    }
  });

  // Single-click (Windows convention: left-click shows window)
  tray.on('click', () => {
    showMainWindow();
  });
}

// ── Create main window ────────────────────────────────────────────────────────
function createWindow() {
  const winIcoPath = path.join(__dirname, '..', 'assets', 'sys_img', 'icon_no_BG.ico');
  const winPngPath = path.join(__dirname, '..', 'assets', 'sys_img', 'icon_no_BG.png');
  const winIconPath = fs.existsSync(winIcoPath) ? winIcoPath : winPngPath;

  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
      backgroundThrottling: false
    },
    icon: winIconPath
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  // Intercept minimize → send to tray instead (only if from minimize button click)
  mainWindow.on('minimize', (event) => {
    const { screen } = require('electron');
    const cursor = screen.getCursorScreenPoint();
    const bounds = mainWindow.getBounds();

    // The minimize button is physically located in the top-right corner of the window frame
    const isNearTop = (cursor.y >= bounds.y && cursor.y <= bounds.y + 45);
    const isNearRight = (cursor.x >= bounds.x + bounds.width - 150 && cursor.x <= bounds.x + bounds.width);

    if (isNearTop && isNearRight) {
      hideMainWindow();
    }
  });

  // Intercept close button → hide to tray or quit based on config (unless app.isQuitting)
  mainWindow.on('close', (event) => {
    if (!app.isQuitting) {
      const config = loadConfig();
      if (config.close_to_tray !== false) {
        event.preventDefault();
        hideMainWindow();
      } else {
        app.isQuitting = true;
        app.quit();
      }
    }
  });

  mainWindow.on('show', updateTrayMenu);
  mainWindow.on('hide', updateTrayMenu);

  // mainWindow.webContents.openDevTools(); // Disabled: remove to suppress Autofill.enable error
}

let isStoppingEngine = false;

// ── Start the CONDUCTOR (pipe stdio → no CLI window visible) ───────────────────
// V2: main.js spawns the conductor (18101), NOT the engine. The conductor's
// ensure_service() spawns and supervises the engine (18100) itself, so no
// BROWSER_ENGINE_* env hack is needed (the engine reads engine_config.json).
function startEngine() {
  console.log('Starting FastAPI conductor service...');
  const projectRoot = path.join(__dirname, '..');
  const pythonExecutable = path.join(projectRoot, '.venv', 'Scripts', 'pythonw.exe');

  // Open log file for appending conductor output
  let logStream;
  try {
    logStream = fs.createWriteStream(ENGINE_LOG_PATH, { flags: 'a' });
  } catch (e) {
    console.error('Cannot open conductor log file:', e);
  }

  engineProcess = spawn(pythonExecutable, [path.join('conductor', 'server.py')], {
    cwd: projectRoot,
    env: {
      ...process.env,
    },
    stdio: ['ignore', 'pipe', 'pipe']   // stdin=ignore, stdout/stderr=pipe
  });

  // Pipe engine output to log file (and optionally to console for debugging)
  if (engineProcess.stdout) {
    if (logStream) engineProcess.stdout.pipe(logStream, { end: false });
    engineProcess.stdout.on('data', (data) => process.stdout.write(data));
  }
  if (engineProcess.stderr) {
    if (logStream) engineProcess.stderr.pipe(logStream, { end: false });
    engineProcess.stderr.on('data', (data) => process.stderr.write(data));
  }

  engineProcess.on('error', (err) => {
    console.error('Failed to start engine service:', err);
  });

  engineProcess.on('close', (code) => {
    console.log(`Engine service exited with code ${code}`);
    if (logStream) logStream.end();
    const wasExpectedStop = isStoppingEngine;
    isStoppingEngine = false;
    // If the CLI/engine exits unexpectedly, quit the Electron app too
    if (!wasExpectedStop && !app.isQuitting) {
      app.quit();
    }
  });
}

// ── Register IPC handlers for starting/stopping the engine service ───────────
ipcMain.handle('start-engine-service', async () => {
  if (engineProcess) {
    return { status: 'already_running' };
  }
  try {
    isStoppingEngine = false;
    startEngine();
    return { status: 'success' };
  } catch (error) {
    return { status: 'error', error: error.message };
  }
});

ipcMain.handle('stop-engine-service', async () => {
  if (!engineProcess) {
    return { status: 'not_running' };
  }
  isStoppingEngine = true;
  try {
    const pid = engineProcess.pid;
    if (process.platform === 'win32' && pid) {
      await new Promise((resolve) => {
        const killer = spawn('taskkill', ['/F', '/T', '/PID', pid.toString()]);
        killer.on('close', () => {
          resolve();
        });
      });
    } else {
      engineProcess.kill();
    }
    engineProcess = null;
    return { status: 'success' };
  } catch (error) {
    return { status: 'error', error: error.message };
  }
});

ipcMain.handle('get-engine-service-status', async () => {
  return { running: !!engineProcess };
});

// ── IPC: Direct config read/write (works even when engine is offline) ─────────
ipcMain.handle('read-config', async () => {
  return loadConfig();
});

ipcMain.handle('write-config', async (event, updates) => {
  try {
    // Deep merge only the 'automation' sub-object; top-level keys are shallow-merged
    // (upscaler removed in V2)
    const current = loadConfig();
    const merged = Object.assign({}, current);
    for (const key of Object.keys(updates)) {
      if (
        key === 'automation' &&
        typeof updates[key] === 'object' &&
        updates[key] !== null &&
        !Array.isArray(updates[key])
      ) {
        merged[key] = Object.assign({}, current[key] || {}, updates[key]);
      } else {
        merged[key] = updates[key];
      }
    }
    const dir = path.dirname(CONFIG_PATH);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(merged, null, 4), 'utf-8');
    return merged;
  } catch (error) {
    console.error('write-config IPC error:', error);
    throw error;
  }
});


ipcMain.handle('set-hide-cli', (event, hide) => {
  const flagPath = path.join(__dirname, '..', 'data', 'hide_cli.flag');
  if (hide) {
    const dir = path.dirname(flagPath);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(flagPath, 'true');
  } else {
    try {
      if (fs.existsSync(flagPath)) {
        fs.unlinkSync(flagPath);
      }
    } catch (e) {
      console.error('Failed to delete hide_cli.flag:', e);
    }
  }
  return true;
});

ipcMain.handle('get-hide-cli', () => {
  const flagPath = path.join(__dirname, '..', 'data', 'hide_cli.flag');
  return fs.existsSync(flagPath);
});


// ── IPC: Direct login lookup read (works even when engine is offline) ──────────
ipcMain.handle('read-login-lookup', async () => {
  try {
    if (fs.existsSync(LOGIN_LOOKUP_PATH)) {
      const content = fs.readFileSync(LOGIN_LOOKUP_PATH, 'utf-8');
      if (content.trim().length > 0) {
        const data = JSON.parse(content);
        return Array.isArray(data) ? data : [];
      }
    }
  } catch (error) {
    console.error('read-login-lookup IPC error:', error);
  }
  return [];
});

ipcMain.handle('write-login-lookup', async (event, data) => {
  try {
    const parentDir = path.dirname(LOGIN_LOOKUP_PATH);
    if (!fs.existsSync(parentDir)) {
      fs.mkdirSync(parentDir, { recursive: true });
    }
    // Write atomically via temp file
    const tmpPath = LOGIN_LOOKUP_PATH + '.tmp';
    fs.writeFileSync(tmpPath, JSON.stringify(data, null, 4), 'utf-8');
    fs.renameSync(tmpPath, LOGIN_LOOKUP_PATH);
    return true;
  } catch (error) {
    console.error('write-login-lookup IPC error:', error);
    return false;
  }
});

// ── Git update IPC ─────────────────────────────────────────────────────────────
function gitExec(args, cwd) {
  return new Promise((resolve) => {
    execFile('git', args, { cwd, timeout: 30000 }, (err, stdout, stderr) => {
      resolve({ ok: !err, stdout: stdout || '', stderr: stderr || '' });
    });
  });
}

ipcMain.handle('check-for-updates', async () => {
  const https = require('https');
  const fs    = require('fs');
  const root  = path.join(__dirname, '..');

  function fetchJson(url) {
    return new Promise((resolve) => {
      https.get(url, { headers: { 'User-Agent': 'GemiPersona_V2' } }, (res) => {
        let data = '';
        res.on('data', chunk => data += chunk);
        res.on('end', () => { try { resolve(JSON.parse(data)); } catch { resolve(null); } });
      }).on('error', () => resolve(null));
    });
  }

  function parseVer(v) {
    return (v || '0.0.0').split('.').map(n => parseInt(n, 10) || 0);
  }

  function readLocalVer(filePath) {
    try { return JSON.parse(fs.readFileSync(filePath, 'utf8')).version || '0.0.0'; } catch { return '0.0.0'; }
  }

  function verLabel(local, remote) {
    const l = parseVer(local), r = parseVer(remote);
    for (let i = 0; i < 3; i++) if (r[i] > l[i]) return `${local}→${remote}`;
    return null;
  }

  try {
    const [remoteApp, remoteEngine] = await Promise.all([
      fetchJson('https://raw.githubusercontent.com/liewcc/GemiPersona_V2/master/version.json'),
      fetchJson('https://raw.githubusercontent.com/liewcc/Gemi_Engine_V2/master/version.json'),
    ]);

    const localApp    = readLocalVer(path.join(root, 'version.json'));
    const localEngine = readLocalVer(path.join(root, 'Gemi_Engine_V2', 'version.json'));

    return {
      main:   verLabel(localApp,    remoteApp?.version    || '0.0.0'),
      engine: verLabel(localEngine, remoteEngine?.version || '0.0.0'),
    };
  } catch (e) {
    return { main: null, engine: null, error: String(e) };
  }
});

ipcMain.handle('run-update', async () => {
  const root = path.join(__dirname, '..');
  const r1 = await gitExec(['pull'], root);
  const r2 = await gitExec(['submodule', 'update', '--remote', 'Gemi_Engine_V2'], root);
  return {
    ok: r1.ok && r2.ok,
    log: [r1.stdout, r1.stderr, r2.stdout, r2.stderr].filter(Boolean).join('\n')
  };
});

// Returns { ui, engine } version strings from both local version.json files
ipcMain.handle('get-versions', () => {
  const fs   = require('fs');
  const root = path.join(__dirname, '..');

  function readLocalVer(filePath) {
    try { return JSON.parse(fs.readFileSync(filePath, 'utf8')).version || '?.?.?'; } catch { return '?.?.?'; }
  }

  return {
    ui:     readLocalVer(path.join(root, 'version.json')),
    engine: readLocalVer(path.join(root, 'Gemi_Engine_V2', 'version.json')),
  };
});

ipcMain.handle('relaunch-app', async () => {
  if (engineProcess) {
    isStoppingEngine = true;
    const pid = engineProcess.pid;
    if (process.platform === 'win32' && pid) {
      try { require('child_process').execSync(`taskkill /F /T /PID ${pid}`); } catch (_) {}
    } else {
      try { engineProcess.kill(); } catch (_) {}
    }
    engineProcess = null;
  }
  app.relaunch();
  app.exit(0);
});

// ── Single Instance Lock ──────────────────────────────────────────────────────
const gotTheLock = app.requestSingleInstanceLock();

if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', (event, commandLine, workingDirectory) => {
    // Someone tried to run a second instance, restore and focus the main window.
    showMainWindow();
  });

  // ── App lifecycle ─────────────────────────────────────────────────────────────
  app.isQuitting = false;

  app.whenReady().then(() => {
    // Set app user model ID for Windows notifications to work properly
    if (process.platform === 'win32') {
      app.setAppUserModelId('com.liewcc.gemipersona.v2');
    }

    startEngine();
    // Tray is non-essential; a tray/icon failure must never block window creation.
    try { createTray(); } catch (e) { console.error('createTray failed (non-fatal):', e); }
    createWindow();

    app.on('activate', function () {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', function () {
    // On Windows/Linux: only quit when user explicitly chose Quit from tray
    // Do NOT quit here; quitting is handled by app.isQuitting flag
    if (process.platform === 'darwin') app.quit();
  });

  app.on('before-quit', () => {
    app.isQuitting = true;
  });

  app.on('will-quit', () => {
    if (engineProcess) {
      console.log('Stopping engine service...');
      const pid = engineProcess.pid;
      if (process.platform === 'win32' && pid) {
        try {
          require('child_process').execSync(`taskkill /F /T /PID ${pid}`);
        } catch (e) {
          console.error('Failed to kill engine process tree on quit:', e);
        }
      } else {
        engineProcess.kill();
      }
    }
    if (tray) {
      tray.destroy();
      tray = null;
    }
  });
}

