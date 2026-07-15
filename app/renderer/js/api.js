const API_BASE_URL = 'http://127.0.0.1:18101';

const api = {
    async request(endpoint, method = 'GET', body = null) {
        const options = {
            method,
            headers: {
                'Content-Type': 'application/json'
            }
        };

        if (body) {
            options.body = JSON.stringify(body);
        }

        try {
            const response = await fetch(`${API_BASE_URL}${endpoint}`, options);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            console.error(`API Error on ${endpoint}:`, error);
            throw error;
        }
    },

    // Health and Status
    checkHealth() {
        return this.request('/health');
    },

    getBrowserStatus() {
        return this.request('/browser/status');
    },
    
    getEngineLogs(history = false) {
        return this.request(`/engine/logs${history ? '?history=true' : ''}`);
    },

    // Engine Control
    startEngine(payload = {}) {
        return this.request('/engine/start', 'POST', payload);
    },

    stopEngine() {
        return this.request('/engine/stop', 'POST');
    },

    startRegistrationMode(payload = {}) {
        return this.request('/engine/start_registration', 'POST', payload);
    },

    stopRegistrationMode() {
        return this.request('/engine/stop_registration', 'POST');
    },

    // Account switching (single endpoint, 6-step sequence)
    accountSwitch(username, profileDir = null) {
        return this.request('/account/switch', 'POST', {
            username,
            profile_dir: profileDir,
        });
    },


    // Browser Actions
    navigate(url) {
        return this.request('/browser/navigate', 'POST', { url });
    },



    getAccountInfo() {
        return this.request('/browser/account');
    },

    // Automation
    startAutomation(mode, goal, config) {
        return this.request('/browser/automation/start', 'POST', { mode, goal, config });
    },

    stopAutomation() {
        return this.request('/browser/automation/stop', 'POST');
    },

    requestNewChat() {
        return this.request('/browser/automation/request_new_chat', 'POST');
    },

    getAutomationStats() {
        return this.request('/browser/automation/stats');
    },
    
    // Configuration & Setup
    applySettings(settings, service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request('/browser/apply_settings', 'POST', { ...settings, service: activeService });
    },
    
    setPrompt(prompt_text, prefix_mode, service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request('/browser/prompt', 'POST', { text: prompt_text, service: activeService });
    },
    
    getGemTitle(url) {
        const query = url ? `?url=${encodeURIComponent(url)}` : '';
        return this.request(`/browser/gem_title${query}`);
    },
    
    clearEngineLogs() {
        return this.request('/engine/clear_logs', 'POST');
    },

    captureDom(service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request(`/browser/capture_dom?service=${encodeURIComponent(activeService)}`, 'POST');
    },
    
    resetTimer() {
        return this.request('/engine/reset_time_timer', 'POST');
    },

    getConfig() {
        // Try the live engine first; fall back to direct Electron file read when offline
        return this.request('/engine/config').catch(() => {
            if (window.electronAPI && window.electronAPI.readConfig) {
                return window.electronAPI.readConfig();
            }
            throw new Error('Engine offline and no Electron IPC available');
        });
    },

    saveConfig(updates) {
        // Try the live engine first (so the running engine gets the update too);
        // fall back to direct Electron file write when offline
        return this.request('/engine/config', 'POST', updates).catch(() => {
            if (window.electronAPI && window.electronAPI.writeConfig) {
                return window.electronAPI.writeConfig(updates);
            }
            throw new Error('Engine offline and no Electron IPC available');
        });
    },

    getPreset(path) {
        return this.request(`/engine/preset?path=${encodeURIComponent(path)}`);
    },

    savePreset(path, data) {
        return this.request(`/engine/preset?path=${encodeURIComponent(path)}`, 'POST', data);
    },

    getImageMetadata(path) {
        return this.request(`/engine/image_metadata?path=${encodeURIComponent(path)}`);
    },

    submitResponse(text, service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request('/browser/submit', 'POST', { text, service: activeService });
    },

    stopResponse(service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request(`/browser/stop?service=${encodeURIComponent(activeService)}`, 'POST');
    },

    redoResponse(service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request(`/browser/redo?service=${encodeURIComponent(activeService)}`, 'POST');
    },

    newChat(service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request(`/browser/new_chat?service=${encodeURIComponent(activeService)}`, 'POST');
    },

    downloadImages(saveDir, naming, meta, service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request('/browser/download', 'POST', { save_dir: saveDir, naming, meta, service: activeService });
    },

    attachFiles(filePaths, service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request(`/browser/attach_files?service=${encodeURIComponent(activeService)}`, 'POST', filePaths);
    },

    clearAttachments(service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request(`/browser/clear_attachments?service=${encodeURIComponent(activeService)}`, 'POST');
    },

    discoverCapabilities(service = null) {
        const activeService = service || window.localConfig?.prewarm_tab || 'gemini';
        return this.request(`/browser/discover?service=${encodeURIComponent(activeService)}`, 'POST');
    },

    getProfiles() {
        // Try live engine first; fall back to direct file read when offline
        return this.request('/engine/profiles').catch(() => {
            if (window.electronAPI && window.electronAPI.readLoginLookup) {
                return window.electronAPI.readLoginLookup().then(data => {
                    return { profiles: data.map(u => ({ dir: u.dir, email: u.email, name: u.name })) };
                });
            }
            return { profiles: [] };
        });
    },

    getHealthEvents(params = {}) {
        const q = new URLSearchParams(params).toString();
        return this.request(`/health/events${q ? '?' + q : ''}`);
    },

    getHealthSummary(params = {}) {
        const q = new URLSearchParams(params).toString();
        return this.request(`/health/summary${q ? '?' + q : ''}`);
    },

    getProfilesStatus() {
        // Direct local file read of Chrome's Local State via IPC is instant and doesn't wake the engine
        if (window.electronAPI && window.electronAPI.readLoginLookup) {
            return window.electronAPI.readLoginLookup().then(data => ({ profiles: data }));
        }
        // Fallback to HTTP request if running in standard browser/development context
        return this.request('/engine/profiles/status');
    },

    reorderProfiles(renameMap) {
        if (window.electronAPI && window.electronAPI.reorderProfiles) {
            return window.electronAPI.reorderProfiles(renameMap);
        }
        return Promise.reject(new Error('Electron IPC not available for reordering'));
    },

    saveProfiles(profiles) {
        // Try live engine first; fall back to direct file write when offline
        return this.request('/engine/profiles/save', 'POST', { profiles }).catch(() => {
            if (window.electronAPI && window.electronAPI.writeLoginLookup) {
                return window.electronAPI.writeLoginLookup(profiles).then(success => {
                    if (!success) throw new Error('Failed to write profiles to disk');
                    return { status: 'success' };
                });
            }
            throw new Error('Engine offline and no Electron IPC available');
        });
    },

    repackProfiles() {
        return this.request('/engine/profiles/repack', 'POST');
    },

    deleteProfile(profileName) {
        // Try the live engine first; fall back to direct Electron delete when offline
        return this.request('/engine/profiles/delete', 'POST', { profile: profileName }).catch((err) => {
            if (window.electronAPI && window.electronAPI.deleteProfile) {
                return window.electronAPI.deleteProfile(profileName).then(res => {
                    if (res && res.success) return { status: 'success' };
                    throw new Error(res ? res.error : 'IPC deletion failed');
                });
            }
            throw err;
        });
    },


    getHealthRuns(limit = 50) {
        return this.request(`/health/runs?limit=${limit}`);
    },

    clearHealthBefore(dateStr) {
        return this.request('/health/clear', 'POST', { before: dateStr });
    },

    continueAutomation(mode, goal, config, clearPending = false) {
        return this.request('/browser/automation/continue', 'POST', { mode, goal, config, clear_pending: clearPending });
    },

    // Keyword Management
    getRefusedKeywords() {
        return this.request('/engine/refused_keywords');
    },

    saveRefusedKeywords(keywords) {
        return this.request('/engine/refused_keywords', 'POST', { keywords });
    },

    getQuotaKeywords() {
        return this.request('/engine/quota_keywords');
    },

    saveQuotaKeywords(keywords) {
        return this.request('/engine/quota_keywords', 'POST', { keywords });
    }
    // 4K Upscaler removed in V2 (obsolete).
};

window.api = api;
