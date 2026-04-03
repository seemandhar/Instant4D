// CARLA 3D Scene Generator v2 - Interactive Frontend with Incremental Builder

const STREAM_URL = window.location.protocol + '//' + window.location.hostname + ':5556';
const API_URL = '';  // Same origin for Flask API

let currentJobId = null;
let eventSource = null;
let captured = false;  // Mouse captured for camera control
let keysDown = new Set();
let controlInterval = null;
let infoInterval = null;

// Build progress tracking
let buildState = {
    active: false,
    phase: '',
    totalSpawns: 0,
    completedSpawns: 0,
    categories: {},  // { vehicles: { total: 0, done: 0 }, props: {...}, walkers: {...} }
};

// ─── Stream ──────────────────────────────────────────────────────────────────

function initStream() {
    const img = document.getElementById('stream-img');
    const placeholder = document.getElementById('stream-placeholder');

    img.onerror = () => {
        placeholder.classList.remove('hidden');
        img.style.opacity = '0';
        setTimeout(initStream, 3000);  // Retry
    };
    img.onload = () => {
        placeholder.classList.add('hidden');
        img.style.opacity = '1';
    };
    img.src = STREAM_URL + '/stream';
}

// ─── Interactive Camera Controls ─────────────────────────────────────────────

function initViewportControls() {
    const viewport = document.getElementById('viewport-3d');

    // Click to capture pointer lock
    viewport.addEventListener('click', (e) => {
        if (!captured) {
            viewport.requestPointerLock();
        }
    });

    // Pointer lock change events
    document.addEventListener('pointerlockchange', () => {
        if (document.pointerLockElement === viewport) {
            captured = true;
            viewport.classList.add('captured');
            document.getElementById('controls-hint').textContent =
                'ESC to release | WASD: Move | Mouse: Look | Q/E: Up/Down | Scroll: Zoom';
        } else {
            releaseCursor();
        }
    });

    // Keyboard: always listen on document, but only act when captured
    document.addEventListener('keydown', (e) => {
        if (!captured) return;
        const key = e.key.toLowerCase();
        // Don't capture if user is typing in an input/textarea
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
        keysDown.add(key);
        e.preventDefault();
    });

    document.addEventListener('keyup', (e) => {
        keysDown.delete(e.key.toLowerCase());
    });

    // Mouse movement for rotation (uses movementX/Y from pointer lock)
    document.addEventListener('mousemove', (e) => {
        if (!captured) return;

        const dx = e.movementX || 0;
        const dy = e.movementY || 0;

        if (dx !== 0 || dy !== 0) {
            streamControl({ command: 'rotate', dyaw: dx * 0.3, dpitch: -dy * 0.3 });
        }
    });

    // Scroll for zoom
    viewport.addEventListener('wheel', (e) => {
        e.preventDefault();
        const delta = -Math.sign(e.deltaY);
        streamControl({ command: 'zoom', delta: delta });
    }, { passive: false });

    // Continuous key movement (30 Hz)
    controlInterval = setInterval(() => {
        if (!captured || keysDown.size === 0) return;

        const speed = keysDown.has('shift') ? 4.0 : 1.5;

        if (keysDown.has('w')) streamControl({ command: 'move_forward', speed });
        if (keysDown.has('s')) streamControl({ command: 'move_backward', speed });
        if (keysDown.has('a')) streamControl({ command: 'move_left', speed });
        if (keysDown.has('d')) streamControl({ command: 'move_right', speed });
        if (keysDown.has('q') || keysDown.has(' ')) streamControl({ command: 'move_up', speed: speed * 0.7 });
        if (keysDown.has('e') || keysDown.has('c')) streamControl({ command: 'move_down', speed: speed * 0.7 });
    }, 33);  // ~30 Hz
}

function releaseCursor() {
    captured = false;
    keysDown.clear();
    const viewport = document.getElementById('viewport-3d');
    viewport.classList.remove('captured');
    // Exit pointer lock if still active
    if (document.pointerLockElement === viewport) {
        document.exitPointerLock();
    }
    document.getElementById('controls-hint').textContent =
        'Click viewport to enable controls | WASD: Move | Mouse: Look | Q/E: Up/Down | Scroll: Zoom';
}

function streamControl(data) {
    fetch(STREAM_URL + '/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).catch(() => {});  // Fire and forget for responsiveness
}

// ─── HUD / Info polling ──────────────────────────────────────────────────────

function pollStreamInfo() {
    fetch(STREAM_URL + '/info')
        .then(r => r.json())
        .then(data => {
            // Status
            const statusEl = document.getElementById('carla-status');
            if (data.connected) {
                statusEl.className = 'status-dot connected';
                statusEl.textContent = 'CARLA Connected';
            } else {
                statusEl.className = 'status-dot disconnected';
                statusEl.textContent = 'CARLA Disconnected';
            }

            // FPS
            document.getElementById('stream-fps').textContent = (data.fps || 0).toFixed(0) + ' FPS';
            document.getElementById('hud-fps').textContent = (data.fps || 0).toFixed(0) + ' FPS';

            // HUD
            document.getElementById('hud-map').textContent = data.map || '--';
            document.getElementById('hud-pos').textContent =
                `X: ${data.x?.toFixed(1)} Y: ${data.y?.toFixed(1)} Z: ${data.z?.toFixed(1)} | P: ${data.pitch?.toFixed(0)} Y: ${data.yaw?.toFixed(0)}`;

            // Camera info panel
            document.getElementById('cam-pos').textContent =
                `${data.x?.toFixed(1)}, ${data.y?.toFixed(1)}, ${data.z?.toFixed(1)}`;
            document.getElementById('cam-rot').textContent =
                `P: ${data.pitch?.toFixed(0)} Y: ${data.yaw?.toFixed(0)}`;

            // Update input fields
            if (!document.activeElement || document.activeElement.tagName !== 'INPUT') {
                document.getElementById('cam-x').value = data.x?.toFixed(0);
                document.getElementById('cam-y').value = data.y?.toFixed(0);
                document.getElementById('cam-z').value = data.z?.toFixed(0);
                document.getElementById('cam-pitch').value = data.pitch?.toFixed(0);
                document.getElementById('cam-yaw').value = data.yaw?.toFixed(0);
            }

            // Scene info
            updateSceneInfo(data);
        })
        .catch(() => {
            document.getElementById('carla-status').className = 'status-dot disconnected';
            document.getElementById('carla-status').textContent = 'Stream Offline';
        });
}

function updateSceneInfo(data) {
    // Also fetch actor counts from stream server
    fetch(STREAM_URL + '/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'get_actors' }),
    })
    .then(r => r.json())
    .then(actors => {
        const infoDiv = document.getElementById('scene-info');
        infoDiv.innerHTML = `
            <div class="info-row"><span class="info-label">Map</span><span class="info-value">${data.map || '--'}</span></div>
            <div class="info-row"><span class="info-label">Vehicles</span><span class="info-value">${actors.vehicles || 0}</span></div>
            <div class="info-row"><span class="info-label">Walkers</span><span class="info-value">${actors.walkers || 0}</span></div>
            <div class="info-row"><span class="info-label">Props</span><span class="info-value">${actors.props || 0}</span></div>
            <div class="info-row"><span class="info-label">Sun</span><span class="info-value">${data.weather?.sun_altitude?.toFixed(0) || '--'}°</span></div>
        `;
    })
    .catch(() => {});
}

// ─── Build Progress Bar ─────────────────────────────────────────────────────

function showBuildProgress() {
    buildState.active = true;
    buildState.completedSpawns = 0;
    buildState.totalSpawns = 0;
    buildState.categories = {};
    const el = document.getElementById('build-progress');
    el.classList.add('visible');
    updateBuildProgressBar(0, '⏳ Starting build...');
}

function hideBuildProgress() {
    buildState.active = false;
    const el = document.getElementById('build-progress');
    // Brief delay so user sees 100%
    setTimeout(() => {
        el.classList.remove('visible');
        updateBuildProgressBar(0, '');
        document.getElementById('build-progress-count').textContent = '';
    }, 1500);
}

function updateBuildProgressBar(percent, phaseText, countText) {
    const fill = document.getElementById('build-progress-fill');
    const phaseEl = document.getElementById('build-progress-phase');
    const countEl = document.getElementById('build-progress-count');

    fill.style.width = Math.min(100, Math.max(0, percent)) + '%';
    if (phaseText !== undefined) phaseEl.textContent = phaseText;
    if (countText !== undefined) countEl.textContent = countText;
}

function calcBuildPercent() {
    if (buildState.totalSpawns === 0) return 0;
    return (buildState.completedSpawns / buildState.totalSpawns) * 100;
}

// ─── Map Loading Overlay ────────────────────────────────────────────────────

function showMapLoading(mapName) {
    const overlay = document.getElementById('map-loading-overlay');
    const textEl = overlay.querySelector('.map-loading-text');
    textEl.textContent = mapName ? `🗺️ Loading ${mapName}...` : '🗺️ Loading map...';
    overlay.classList.add('visible');
}

function hideMapLoading() {
    const overlay = document.getElementById('map-loading-overlay');
    overlay.classList.remove('visible');
}

// ─── Tab Switching ───────────────────────────────────────────────────────────

function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    // Activate matching tab button
    const tabs = document.querySelectorAll('.tab');
    tabs.forEach(t => {
        if (t.textContent.toLowerCase().includes(name.substring(0, 4))) {
            t.classList.add('active');
        }
    });

    if (name === 'gallery') refreshImages();
}

// ─── Scene Controls (via stream server) ──────────────────────────────────────

function setWeather() {
    const preset = document.getElementById('weather-preset').value;
    streamControl({ command: 'set_weather', preset });
    logEntry(`Weather: ${preset}`, 'success');
}

function loadMap() {
    const map = document.getElementById('map-select').value;
    logEntry(`Loading map ${map}...`, 'info');
    streamControl({ command: 'load_map', map });
}

function teleportCamera() {
    const x = parseFloat(document.getElementById('cam-x').value) || 0;
    const y = parseFloat(document.getElementById('cam-y').value) || 0;
    const z = parseFloat(document.getElementById('cam-z').value) || 20;
    const pitch = parseFloat(document.getElementById('cam-pitch').value) || -30;
    const yaw = parseFloat(document.getElementById('cam-yaw').value) || 0;
    streamControl({ command: 'set_position', x, y, z, pitch, yaw });
    logEntry(`Camera teleported to (${x}, ${y}, ${z})`, 'success');
}

function spawnVehicle() {
    const blueprint = document.getElementById('vehicle-bp').value;
    const x = parseFloat(document.getElementById('spawn-x').value) || 0;
    const y = parseFloat(document.getElementById('spawn-y').value) || 0;
    const yaw = parseFloat(document.getElementById('spawn-yaw').value) || 0;

    fetch(API_URL + '/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'spawn_vehicle', blueprint, x, y, z: 0.5, yaw }),
    })
    .then(r => r.json())
    .then(r => {
        if (r.success) logEntry(`Spawned ${blueprint} (id: ${r.actor_id})`, 'success');
        else logEntry(`Spawn error: ${r.error}`, 'error');
    })
    .catch(err => logEntry(`Spawn failed: ${err}`, 'error'));
}

function saveSnapshot() {
    fetch(API_URL + '/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'snapshot' }),
    })
    .then(r => r.json())
    .then(r => {
        if (r.success) {
            logEntry(`Snapshot saved: ${r.image}`, 'success');
            refreshImages();
        } else {
            logEntry(`Snapshot error: ${r.error}`, 'error');
        }
    });
}

function clearScene() {
    fetch(API_URL + '/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'clear_scene' }),
    })
    .then(r => r.json())
    .then(r => {
        if (r.success) logEntry(`Scene cleared: ${r.destroyed} actors`, 'success');
        else logEntry(`Clear error: ${r.error}`, 'error');
    });
}

// ─── Scene Presets ──────────────────────────────────────────────────────────

function applyPreset(value) {
    const textarea = document.getElementById('prompt');
    if (value) {
        textarea.value = value;
    }
}

// ─── Load Demo Scene (Incremental Builder with SSE) ────────────────────────

function loadDemo() {
    const btn = document.getElementById('btn-demo');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Building demo scene...';
    logEntry('🚀 Starting demo: Rainy Night Police Roadblock (live build)...', 'agent');
    showBuildProgress();

    fetch(API_URL + '/api/load_demo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) {
            logEntry('Demo error: ' + data.error, 'error');
            resetDemoBtn();
            hideBuildProgress();
            return;
        }
        logEntry('📋 Demo job started: ' + data.job_id, 'info');
        connectSSE(data.job_id, true);
    })
    .catch(err => {
        logEntry('Demo request failed: ' + err, 'error');
        resetDemoBtn();
        hideBuildProgress();
    });
}

function resetDemoBtn() {
    const btn = document.getElementById('btn-demo');
    btn.disabled = false;
    btn.innerHTML = '⚡ Load Demo (Live Build)';
}

// ─── Generate Scene (via Flask pipeline API) ─────────────────────────────────

function generateScene() {
    const prompt = document.getElementById('prompt').value.trim();
    if (!prompt) { logEntry('Enter a scene description.', 'error'); return; }

    const model = document.getElementById('model').value;
    const btn = document.getElementById('btn-generate');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Generating...';

    logEntry('🚀 Starting scene generation pipeline...', 'agent');
    showBuildProgress();

    fetch(API_URL + '/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, model }),
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) {
            logEntry('Error: ' + data.error, 'error');
            resetGenerateBtn();
            hideBuildProgress();
            return;
        }
        currentJobId = data.job_id;
        logEntry(`📋 Job started: ${data.job_id}`, 'info');
        connectSSE(data.job_id, false);
    })
    .catch(err => {
        logEntry('Request failed: ' + err, 'error');
        resetGenerateBtn();
        hideBuildProgress();
    });
}

function connectSSE(jobId, isDemo) {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(API_URL + `/api/events/${jobId}`);
    eventSource.onmessage = (e) => handlePipelineEvent(JSON.parse(e.data), isDemo);
    eventSource.onerror = () => {
        logEntry('SSE connection lost', 'error');
        eventSource.close();
        if (isDemo) resetDemoBtn();
        else resetGenerateBtn();
        hideBuildProgress();
        hideMapLoading();
    };
}

function handlePipelineEvent(event, isDemo) {
    const t = event.type;
    const d = event.data || {};

    switch (t) {
        // ─── Existing pipeline events ───
        case 'pipeline_start':
            logEntry(`🎬 Pipeline: "${d.prompt}"`, 'agent');
            break;
        case 'agents_loaded':
            logEntry(`🤖 Agents: ${(d.agents || []).join(', ')}`, 'info');
            break;
        case 'text':
            if (d.message) logEntry(d.message, 'text');
            break;
        case 'tool_use':
            logEntry(`🔧 ${d.name}: ${d.input_preview || ''}`, 'tool');
            break;
        case 'subagent_start':
            logEntry(`>> ${d.agent}`, 'agent');
            break;
        case 'subagent_done':
            logEntry(`<< ${d.status}`, 'success');
            break;
        case 'pipeline_complete':
            logEntry(`✅ Complete! (${(d.elapsed || 0).toFixed(0)}s)`, 'success');
            if (isDemo) resetDemoBtn();
            else resetGenerateBtn();
            hideBuildProgress();
            hideMapLoading();
            refreshImages();
            break;
        case 'error':
            logEntry(`❌ Error: ${d.message}`, 'error');
            if (isDemo) resetDemoBtn();
            else resetGenerateBtn();
            hideBuildProgress();
            hideMapLoading();
            break;
        case 'done':
            if (eventSource) eventSource.close();
            if (isDemo) resetDemoBtn();
            else resetGenerateBtn();
            hideBuildProgress();
            hideMapLoading();
            refreshImages();
            break;

        // ─── Incremental Builder events ───
        case 'plan_parsed':
            logEntry('📐 Plan ready, starting live build...', 'agent');
            // Precompute total spawns from plan data if available
            if (d.vehicles !== undefined || d.props !== undefined || d.walkers !== undefined) {
                const vCount = d.vehicles || 0;
                const pCount = d.props || 0;
                const wCount = d.walkers || 0;
                buildState.totalSpawns = vCount + pCount + wCount;
                buildState.categories = {
                    vehicles: { total: vCount, done: 0 },
                    props: { total: pCount, done: 0 },
                    walkers: { total: wCount, done: 0 },
                };
                logEntry(`📦 Plan: ${vCount} vehicles, ${pCount} props, ${wCount} walkers`, 'info');
            }
            updateBuildProgressBar(5, '📐 Plan parsed');
            break;

        case 'build_phase': {
            const phase = d.phase || d.name || '';
            const phaseLabels = {
                'map_load': '🗺️ Loading map',
                'weather': '🌤️ Setting weather',
                'camera': '📷 Positioning camera',
                'clear_scene': '🧹 Clearing scene',
                'spawning': '🏗️ Spawning actors',
                'capture': '📸 Capturing renders',
                'vehicles': '🚗 Spawning vehicles',
                'props': '🪑 Spawning props',
                'walkers': '🚶 Spawning walkers',
            };
            const label = phaseLabels[phase] || `⚙️ ${phase}`;
            logEntry(label, 'info');

            // Show/hide map loading overlay
            if (phase === 'map_load') {
                showMapLoading(d.map || '');
            } else {
                hideMapLoading();
            }

            // Update progress bar phase text
            let percent = calcBuildPercent();
            if (phase === 'map_load') percent = 5;
            else if (phase === 'weather') percent = 10;
            else if (phase === 'camera') percent = 15;
            else if (phase === 'clear_scene') percent = 8;
            else if (phase === 'capture') percent = 95;
            updateBuildProgressBar(Math.max(percent, calcBuildPercent()), label);
            break;
        }

        case 'build_spawn': {
            const category = d.category || 'unknown';
            const index = (d.index !== undefined) ? d.index + 1 : '?';
            const total = d.total || '?';
            const blueprint = d.blueprint || d.name || '';
            const categoryEmojis = {
                'vehicle': '🚗', 'vehicles': '🚗',
                'prop': '🪑', 'props': '🪑',
                'walker': '🚶', 'walkers': '🚶',
            };
            const emoji = categoryEmojis[category] || '📦';
            const shortBp = blueprint.split('.').pop() || blueprint;

            logEntry(`${emoji} ${category} ${index}/${total}: ${shortBp}`, 'info');

            // Update build state
            buildState.completedSpawns++;
            const catKey = category.endsWith('s') ? category : category + 's';
            if (buildState.categories[catKey]) {
                buildState.categories[catKey].done++;
            }

            // Recalculate percentage: spawning is 20%-90% of the bar
            const spawnPercent = buildState.totalSpawns > 0
                ? 20 + (buildState.completedSpawns / buildState.totalSpawns) * 70
                : 50;
            updateBuildProgressBar(
                spawnPercent,
                `${emoji} Spawning ${category}`,
                `${index}/${total}`
            );
            break;
        }

        case 'build_warning':
            logEntry(`⚠️ ${d.message || d.warning || 'Warning'}`, 'error');
            break;

        case 'build_complete': {
            const elapsed = d.elapsed ? `(${d.elapsed.toFixed(1)}s)` : '';
            const actorCount = d.actors_spawned || d.total || '';
            logEntry(`🎉 Build complete! ${actorCount ? actorCount + ' actors spawned ' : ''}${elapsed}`, 'success');
            updateBuildProgressBar(100, '✅ Build complete!', '');
            hideBuildProgress();
            hideMapLoading();
            refreshImages();
            if (isDemo) resetDemoBtn();
            else resetGenerateBtn();
            break;
        }

        // ─── Quiet events ───
        case 'heartbeat':
        case 'subagent_progress':
            break;
        default:
            break;
    }
}

function resetGenerateBtn() {
    const btn = document.getElementById('btn-generate');
    btn.disabled = false;
    btn.innerHTML = '🤖 Generate Scene';
}

// ─── Images / Gallery ────────────────────────────────────────────────────────

function refreshImages() {
    fetch(API_URL + '/api/images')
        .then(r => r.json())
        .then(data => {
            const gallery = document.getElementById('image-gallery');
            const images = data.images || [];
            if (images.length === 0) {
                gallery.innerHTML = '<p style="color:var(--text-muted);font-size:12px;">No renders yet. Generate a scene first.</p>';
                return;
            }
            gallery.innerHTML = images.map(img =>
                `<img src="/renders/${img}?t=${Date.now()}" alt="${img}" title="${img}" onclick="window.open('/renders/${img}', '_blank')">`
            ).join('');
        });
}

// ─── Logging ─────────────────────────────────────────────────────────────────

function logEntry(message, type = 'info') {
    const log = document.getElementById('pipeline-log');
    const entry = document.createElement('div');
    entry.className = `log-entry log-${type}`;
    const time = new Date().toLocaleTimeString('en-US', { hour12: false });
    const prefixes = {
        'info': '[i]', 'text': '[>]', 'agent': '[A]', 'tool': '[T]',
        'error': '[!]', 'success': '[+]', 'thinking': '[~]',
    };
    entry.textContent = `${time} ${prefixes[type] || '[?]'} ${message}`;
    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;

    // Keep log manageable
    while (log.children.length > 200) log.removeChild(log.firstChild);
}

function clearLog() {
    document.getElementById('pipeline-log').innerHTML = '';
}

// ─── Init ────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    initStream();
    initViewportControls();

    // Poll stream info at 2 Hz
    infoInterval = setInterval(pollStreamInfo, 500);
    pollStreamInfo();

    // Window blur releases controls
    window.addEventListener('blur', releaseCursor);
});
