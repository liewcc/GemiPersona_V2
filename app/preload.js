const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  selectFile: (options) => ipcRenderer.invoke('select-file', options),
  selectDirectory: (options) => ipcRenderer.invoke('select-directory', options),
  saveFile: (options) => ipcRenderer.invoke('save-file', options),
  openFile: (filePath) => ipcRenderer.invoke('open-file', filePath),
  showItemInFolder: (filePath) => ipcRenderer.invoke('show-item-in-folder', filePath),
  readImageBase64: (filePath) => ipcRenderer.invoke('read-image-base64', filePath),
  readPngMetadata: (filePath) => ipcRenderer.invoke('read-png-metadata', filePath),

  // Trigger a system tray notification when images are downloaded
  // count: number of images, saveDir: destination folder path (optional), silent: mute alert sound (optional), imagePath: exact image path (optional)
  notifyDownload: (count, saveDir, silent = false, imagePath = '') => ipcRenderer.send('notify-download', { count, saveDir, silent, imagePath }),

  startEngineService: () => ipcRenderer.invoke('start-engine-service'),
  stopEngineService: () => ipcRenderer.invoke('stop-engine-service'),
  getEngineServiceStatus: () => ipcRenderer.invoke('get-engine-service-status'),

  // Direct config file access — works even when the Python engine is offline
  readConfig: () => ipcRenderer.invoke('read-config'),
  writeConfig: (updates) => ipcRenderer.invoke('write-config', updates),

  // CLI Visibility flags
  setHideCliFlag: (hide) => ipcRenderer.invoke('set-hide-cli', hide),
  getHideCliFlag: () => ipcRenderer.invoke('get-hide-cli'),

  // Direct profile list read — works even when the Python engine is offline
  readLoginLookup: () => ipcRenderer.invoke('read-login-lookup'),
  writeLoginLookup: (data) => ipcRenderer.invoke('write-login-lookup', data),
  checkForUpdates: () => ipcRenderer.invoke('check-for-updates'),
  runUpdate: () => ipcRenderer.invoke('run-update'),
  relaunchApp: () => ipcRenderer.invoke('relaunch-app')
});
