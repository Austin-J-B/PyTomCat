/**
 * Image Labeling Tool for TomCat
 * 
 * Keyboard shortcuts:
 *   Detector Mode:
 *     WASD       - Nudge box top-left corner
 *     Arrows     - Nudge box bottom-right corner
 *     2          - Add new box
 *     X          - Delete selected box
 *     E          - Run SAM refinement
 *     Y/Enter    - Save and advance
 *     N          - Reject image
 *     Backspace  - Undo last action
 *     
 *   Classifier Mode:
 *     1-9        - Select prediction
 *     0          - Mark as NeedsReview
 *     X          - Reject crop
 *     Enter      - Confirm and advance
 *     Backspace  - Undo
 */
(function () {
    'use strict';

    const NUDGE_PX = 2; //Pixels per WASD/arrow press
    const CROP_PAD_PCT = 0.03;
    const ZOOM_MIN = 0.5;
    const ZOOM_MAX = 6.0;
    const ZOOM_STEP = 0.12;
    const PREFETCH_AHEAD = 25;
    const PREFETCH_CONCURRENCY = 2;
    const IMAGE_PREFETCH_AHEAD = 25;
    const IMAGE_PREFETCH_MAX = 80;
    const DETECT_PREFETCH_AHEAD = 25;
    const DETECT_PREFETCH_CONCURRENCY = 1;
    const DETECT_PREFETCH_COOLDOWN_MS = 8000;
    const DETECT_REFINE_PREFETCH_AHEAD = 25;
    const DETECT_REFINE_COOLDOWN_MS = 12000;
    const REF_CACHE_VERSION = 'refs_v2';
    const ACTION_COOLDOWN_MS = 250;
    const API_POST_TIMEOUT_MS = 45000;
    const API_PREFETCH_TIMEOUT_MS = 15000;
    const CLAIM_HEARTBEAT_MS = 15000;
    const SESSION_WARM_TARGET = 25;
    const SESSION_WARM_TICK_MS = 1500;
    const SESSION_REFRESH_QUEUES_MS = 30000;
    const DETECTOR_PREFETCH_REFINE_PASSES = 2;

    function getApiBase() {
        let base = '';
        if (typeof apiBase === 'string') {
            base = apiBase;
        } else if (typeof window.apiBase === 'string') {
            base = window.apiBase;
        }
        base = base.toString().trim();
        if (base) return base.replace(/\/$/, '');
        return window.location.origin;
    }

    function buildApiUrl(path) {
        const base = getApiBase();
        if (!path) return base;
        if (/^https?:\/\//i.test(path)) return path;
        if (!path.startsWith('/')) path = `/${path}`;
        return `${base}${path}`;
    }

    //State
    let labelerMode = 'detect'; //'detect' or 'classify'
    let queue = [];
    let queueTotal = 0;
    let queueIndex = 0;
    let currentSerial = null;
    let currentImageUrl = null;
    let currentItem = null;
    let currentBoxes = []; //Array of {x, y, w, h} in normalized coords
    let selectedBoxIdx = 0;
    let currentLabels = []; //Array of cat names per box
    let currentCropIdx = 0;
    let allCats = []; //Dropdown options
    let history = []; //Undo stack
    let pendingUpdates = []; //Batch save queue
    let imageElement = null;
    let canvasEl = null;
    let ctxCanvas = null;
    let canvasAreaEl = null;
    let currentPredictions = [];
    let zoomLevel = 1.0;
    let panX = 0;
    let panY = 0;
    let baseScale = 1.0;
    let isPanning = false;
    let panMoved = false;
    let suppressClick = false;
    let lastPan = { x: 0, y: 0 };
    let refPollId = null;
    let predCache = new Map();
    let prefetchInFlight = new Set();
    let prefetchRunning = false;
    let prefetchRequested = false;
    let predCacheEpoch = 0;
    let prefetchTimer = null;
    let imagePrefetch = new Map();
    let detectPrefetch = new Map();
    let detectPrefetchInFlight = new Set();
    let detectPrefetchRunning = false;
    let detectPrefetchRequested = false;
    let detectPrefetchEpoch = 0;
    let lastDetectPrefetch = 0;
    let lastDetectRefinePrefetch = 0;
    let detectRefineInFlight = new Set();
    let boxesTouched = false;
    let pressedKeys = new Set();
    let moveIntervalId = null;
    let actionCooldowns = new Map();
    let labelerInitialized = false;
    let labelerActive = false;
    let warmTimer = null;
    let warmLoopRunning = false;
    let currentClaim = null;
    let claimTimer = null;
    let retrainPollId = null;
    let lastQueueRefreshTs = 0;
    let queueRefreshPromise = null;
    let detectQueue = [];
    let detectQueueTotal = 0;
    let classifyQueue = [];
    let classifyQueueTotal = 0;
    let modePositions = { detect: 0, classify: 0 };
    let detectWarmInFlight = new Set();
    let classifyWarmInFlight = new Set();

    //DOM references (set after init)
    let containerEl = null;
    let statusEl = null;
    let infoEl = null;
    let cropDisplayEl = null;
    let predictionsEl = null;

    //---------- Initialization ----------

    function initLabeler() {
        containerEl = document.getElementById('labeler-container');
        if (!containerEl) {
            console.warn('[Labeler] No labeler-container found');
            return;
        }
        if (labelerInitialized) {
            labelerActive = true;
            startRetrainStatusPoll();
            togglePrefetchTimer(true);
            startWarmLoop();
            loadQueue({ forceRefresh: false });
            return;
        }
        labelerInitialized = true;
        labelerActive = true;
        containerEl.classList.remove('labeler-mode-detect', 'labeler-mode-classify');
        containerEl.classList.add('labeler-mode-detect');

        //Create UI elements
        containerEl.innerHTML = `
            <div class="labeler-wrapper">
                <div class="labeler-header">
                    <div class="labeler-nav">
                        <button class="labeler-menu-btn" id="labeler-menu-btn" title="Open Menu" type="button">
                            <span></span>
                            <span></span>
                            <span></span>
                        </button>
                        <div class="labeler-nav-title">Campus Cat Coalition</div>
                    </div>
                    <div class="labeler-tabs">
                        <button class="labeler-tab active" data-mode="detect">Detector</button>
                        <button class="labeler-tab" data-mode="classify">Classifier</button>
                    </div>
                    <div class="labeler-status" id="labeler-status">Loading...</div>
                    <div class="labeler-actions">
                        <button class="labeler-btn" id="btn-save-all" title="Save pending annotations">Save All</button>
                        <button class="labeler-btn" id="btn-retrain-4am" title="Queue full gallery retrain for next 4 AM">Tonight 4 AM Retrain</button>
                        <span class="pending-count" id="pending-count">0 pending</span>
                        <span class="retrain-status" id="retrain-status">Retrain: not scheduled</span>
                    </div>
                </div>

                <div class="labeler-banner">
                    <div class="labeler-info" id="labeler-info">
                        <div class="info-row"><span class="info-label">Serial:</span> <span id="info-serial">-</span></div>
                        <div class="info-row"><span class="info-label">Queue:</span> <span id="info-queue">-</span></div>
                        <div class="info-row"><span class="info-label">Boxes:</span> <span id="info-boxes">-</span></div>
                        <div class="info-row"><span class="info-label">Crop:</span> <span id="info-crop">-</span></div>
                        <div class="info-row"><span class="info-label">Zoom:</span> <span id="info-zoom">100%</span></div>
                    </div>
                    <div class="labeler-help">
                        <div id="shortcuts-detect" class="shortcuts-row">
                            <span class="shortcut-item"><kbd>WASD</kbd> Move top-left</span>
                            <span class="shortcut-item"><kbd>Arrows</kbd> Move bottom-right</span>
                            <span class="shortcut-item"><kbd>2</kbd> Add box</span>
                            <span class="shortcut-item"><kbd>X</kbd> Delete box</span>
                            <span class="shortcut-item"><kbd>E</kbd> SAM refine</span>
                            <span class="shortcut-item"><kbd>Y</kbd> Save & next</span>
                            <span class="shortcut-item"><kbd>N</kbd> Reject</span>
                            <span class="shortcut-item"><kbd>Tab</kbd>/<kbd>Space</kbd> Next box</span>
                            <span class="shortcut-item"><kbd>Backspace</kbd> Undo</span>
                            <span class="shortcut-item"><kbd>Wheel</kbd> Zoom</span>
                            <span class="shortcut-item"><kbd>Left-drag</kbd> Pan</span>
                        </div>
                        <div id="shortcuts-classify" class="shortcuts-row" style="display:none">
                            <span class="shortcut-item"><kbd>1-9</kbd> Pick prediction</span>
                            <span class="shortcut-item"><kbd>0</kbd> Needs Review</span>
                            <span class="shortcut-item"><kbd>X</kbd> Reject crop</span>
                            <span class="shortcut-item"><kbd>Enter</kbd> Next crop</span>
                            <span class="shortcut-item"><kbd>Backspace</kbd> Undo</span>
                        </div>
                    </div>
                </div>
                
                <div class="labeler-main">
                    <div class="labeler-canvas-area">
                        <canvas id="labeler-canvas" width="800" height="600"></canvas>
                        <img id="labeler-image" style="display:none">
                    </div>
                    
                    <div class="labeler-sidebar">
                        <div class="labeler-predictions" id="labeler-predictions" style="display:none">
                            <h4>Predictions</h4>
                            <div id="predictions-list"></div>
                        </div>
                    </div>
                </div>
            </div>
        `;

        //Get references
        canvasEl = document.getElementById('labeler-canvas');
        ctxCanvas = canvasEl.getContext('2d');
        imageElement = document.getElementById('labeler-image');
        statusEl = document.getElementById('labeler-status');
        infoEl = document.getElementById('labeler-info');
        predictionsEl = document.getElementById('labeler-predictions');
        cropDisplayEl = document.getElementById('labeler-crop-display');
        canvasAreaEl = containerEl.querySelector('.labeler-canvas-area');
        if (canvasEl) {
            canvasEl.tabIndex = 0;
            canvasEl.style.outline = 'none';
            canvasEl.addEventListener('mouseenter', () => canvasEl.focus());
        }
        const predictionsList = document.getElementById('predictions-list');
        predictionsList?.addEventListener('click', (e) => {
            const item = e.target.closest('.prediction-item');
            if (!item) return;
            const idx = parseInt(item.dataset.idx || '', 10);
            if (idx) selectPrediction(idx);
        });

        //Event listeners
        const menuBtn = document.getElementById('labeler-menu-btn');
        if (menuBtn) {
            menuBtn.addEventListener('click', () => {
                if (typeof window.toggleMenu === 'function') {
                    window.toggleMenu();
                }
            });
        }
        containerEl.querySelectorAll('.labeler-tab').forEach(tab => {
            tab.addEventListener('click', () => switchMode(tab.dataset.mode));
        });

        document.getElementById('btn-save-all').addEventListener('click', saveAllPending);
        document.getElementById('btn-retrain-4am').addEventListener('click', scheduleRetrain4am);

        imageElement.addEventListener('load', onImageLoad);
        imageElement.addEventListener('error', onImageError);

        //Canvas click to select box
        canvasEl.addEventListener('click', onCanvasClick);
        canvasEl.addEventListener('wheel', onCanvasWheel, { passive: false });
        canvasAreaEl?.addEventListener('wheel', onCanvasWheel, { passive: false });
        canvasEl.addEventListener('mousedown', onCanvasMouseDown);
        window.addEventListener('mousemove', onCanvasMouseMove);
        window.addEventListener('mouseup', onCanvasMouseUp);
        canvasEl.addEventListener('contextmenu', (e) => e.preventDefault());

        //Keyboard
        document.addEventListener('keydown', onKeyDown);
        document.addEventListener('keyup', onKeyUp);

        if (canvasAreaEl) {
            resizeCanvasToContainer();
            if (typeof ResizeObserver !== 'undefined') {
                const observer = new ResizeObserver(() => {
                    resizeCanvasToContainer();
                    drawCanvas();
                });
                observer.observe(canvasAreaEl);
            } else {
                window.addEventListener('resize', () => {
                    resizeCanvasToContainer();
                    drawCanvas();
                });
            }
        }

        //Load cat list for classifier dropdown
        loadCatList();
        warmRefCache();
        startRetrainStatusPoll();
        togglePrefetchTimer(true);
        startWarmLoop();

        //Load initial queue
        loadQueue({ forceRefresh: true });
    }

    //---------- Mode Switching ----------

    function switchMode(mode) {
        void releaseCurrentClaim();
        labelerMode = mode;
        containerEl.querySelectorAll('.labeler-tab').forEach(tab => {
            tab.classList.toggle('active', tab.dataset.mode === mode);
        });

        containerEl.classList.remove('labeler-mode-detect', 'labeler-mode-classify');
        containerEl.classList.add(`labeler-mode-${mode}`);

        document.getElementById('shortcuts-detect').style.display = mode === 'detect' ? 'block' : 'none';
        document.getElementById('shortcuts-classify').style.display = mode === 'classify' ? 'block' : 'none';
        predictionsEl.style.display = mode === 'classify' ? 'block' : 'none';
        if (cropDisplayEl) cropDisplayEl.style.display = 'none';

        pressedKeys.clear();
        if (moveIntervalId !== null) {
            clearInterval(moveIntervalId);
            moveIntervalId = null;
        }
        actionCooldowns.clear();

        togglePrefetchTimer(true);
        startWarmLoop();
        loadQueue({ forceRefresh: false });
    }

    async function warmRefCache() {
        try {
            const needsForce = (() => {
                try {
                    return localStorage.getItem('labelerRefCacheVersion') !== REF_CACHE_VERSION;
                } catch (e) {
                    return true;
                }
            })();
            await apiPost('/api/labeler/refs/warm', { force: needsForce });
            try {
                localStorage.setItem('labelerRefCacheVersion', REF_CACHE_VERSION);
            } catch (e) {
                //ignore storage errors
            }
            startRefPoll();
        } catch (e) {
            console.warn('[Labeler] Ref cache warm failed:', e);
        }
    }

    function startRefPoll() {
        if (refPollId) return;
        refPollId = setInterval(async () => {
            try {
                const status = await apiGet('/api/labeler/refs/status');
                if (status && status.ready) {
                    clearInterval(refPollId);
                    refPollId = null;
                    resetPredCache();
                    if (labelerMode === 'classify') {
                        loadPredictions(true);
                        prefetchPredictions();
                    }
                }
            } catch (e) {
                //Ignore transient errors
            }
        }, 5000);
    }

    //---------- API Calls ----------

    async function apiGet(endpoint) {
        const resp = await fetch(buildApiUrl(endpoint), {
            credentials: 'include',
        });
        if (!resp.ok) throw new Error(`API error: ${resp.status}`);
        return resp.json();
    }

    async function apiPost(endpoint, data, opts = {}) {
        const timeoutMs = Number(opts.timeoutMs || API_POST_TIMEOUT_MS);
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        try {
            const resp = await fetch(buildApiUrl(endpoint), {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
                signal: controller.signal,
            });
            if (!resp.ok) throw new Error(`API error: ${resp.status}`);
            return resp.json();
        } catch (e) {
            if (e && e.name === 'AbortError') {
                throw new Error(`API timeout: ${timeoutMs}ms`);
            }
            throw e;
        } finally {
            clearTimeout(timer);
        }
    }

    function stopClaimHeartbeat() {
        if (claimTimer) {
            clearInterval(claimTimer);
            claimTimer = null;
        }
    }

    function formatRetrainStatus(status) {
        if (!status) return 'Retrain: unknown';
        if (status.running) return 'Retrain: running now';
        const date = status.scheduled_date || '';
        if (status.enabled && date) return `Retrain: scheduled ${date} at 4:00 AM`;
        if (status.last_run_date) return `Retrain: last run ${status.last_run_date}`;
        return 'Retrain: not scheduled';
    }

    async function refreshRetrainStatus() {
        const el = document.getElementById('retrain-status');
        if (!el) return;
        try {
            const status = await apiGet('/api/labeler/gallery_retrain/status');
            el.textContent = formatRetrainStatus(status);
        } catch (e) {
            el.textContent = 'Retrain: status unavailable';
        }
    }

    function startRetrainStatusPoll() {
        if (retrainPollId) return;
        void refreshRetrainStatus();
        retrainPollId = setInterval(() => { void refreshRetrainStatus(); }, 30000);
    }

    function stopRetrainStatusPoll() {
        if (retrainPollId) {
            clearInterval(retrainPollId);
            retrainPollId = null;
        }
    }

    async function scheduleRetrain4am() {
        setStatus('Scheduling 4 AM retrain...');
        try {
            const status = await apiPost('/api/labeler/gallery_retrain/schedule', {});
            const el = document.getElementById('retrain-status');
            if (el) el.textContent = formatRetrainStatus(status);
            setStatus('Retrain scheduled.');
        } catch (e) {
            setStatus(`Retrain schedule failed: ${e.message}`);
        }
    }

    function startClaimHeartbeat() {
        stopClaimHeartbeat();
        claimTimer = setInterval(() => {
            void heartbeatCurrentClaim();
        }, CLAIM_HEARTBEAT_MS);
    }

    async function heartbeatCurrentClaim() {
        if (!currentClaim) return;
        try {
            await apiPost('/api/labeler/claim', {
                action: 'heartbeat',
                mode: currentClaim.mode,
                serial: currentClaim.serial,
            }, { timeoutMs: 6000 });
        } catch (e) {
            //Ignore heartbeat errors; TTL expiry is the backstop.
        }
    }

    async function releaseCurrentClaim() {
        stopClaimHeartbeat();
        const claim = currentClaim;
        currentClaim = null;
        if (!claim) return;
        try {
            await apiPost('/api/labeler/claim', {
                action: 'release',
                mode: claim.mode,
                serial: claim.serial,
            }, { timeoutMs: 6000 });
        } catch (e) {
            //Ignore release failures; server TTL will expire stale claims.
        }
    }

    async function claimQueueItem(item, mode) {
        if (!item || !item.serial) return false;
        try {
            const data = await apiPost('/api/labeler/claim', {
                action: 'acquire',
                mode,
                serial: item.serial,
            }, { timeoutMs: 8000 });
            if (data && data.granted) {
                currentClaim = { mode, serial: item.serial };
                startClaimHeartbeat();
                return true;
            }
            return false;
        } catch (e) {
            return false;
        }
    }

    async function loadCatList() {
        try {
            const data = await apiGet('/api/labeler/cats');
            allCats = data.cats || [];
            console.log(`[Labeler] Loaded ${allCats.length} cats`);
        } catch (e) {
            console.error('[Labeler] Failed to load cats:', e);
        }
    }

    function queueForMode(mode) {
        return mode === 'detect' ? detectQueue : classifyQueue;
    }

    function queueTotalForMode(mode) {
        return mode === 'detect' ? detectQueueTotal : classifyQueueTotal;
    }

    async function refreshQueues(force = false) {
        const now = Date.now();
        const stale = (now - lastQueueRefreshTs) > SESSION_REFRESH_QUEUES_MS;
        if (!force && !stale && detectQueue.length && classifyQueue.length) {
            return;
        }
        if (queueRefreshPromise) {
            await queueRefreshPromise;
            return;
        }
        queueRefreshPromise = (async () => {
            const [detectData, classifyData] = await Promise.all([
                apiGet('/api/labeler/queue/detect'),
                apiGet('/api/labeler/queue/classify'),
            ]);
            detectQueue = detectData.queue || [];
            detectQueueTotal = typeof detectData.total === 'number' ? detectData.total : detectQueue.length;
            classifyQueue = classifyData.queue || [];
            classifyQueueTotal = typeof classifyData.total === 'number' ? classifyData.total : classifyQueue.length;
            lastQueueRefreshTs = Date.now();
        })();
        try {
            await queueRefreshPromise;
        } finally {
            queueRefreshPromise = null;
        }
    }

    function applyModeQueue() {
        queue = queueForMode(labelerMode);
        queueTotal = queueTotalForMode(labelerMode);
        const savedPos = Number(modePositions[labelerMode] || 0);
        queueIndex = Math.max(0, Math.min(savedPos, Math.max(0, queue.length - 1)));
        modePositions[labelerMode] = queueIndex;
    }

    async function primeCurrentItemForMode() {
        const item = queue[queueIndex];
        if (!item) return;
        prefetchImageSerial(item.serial);
        try {
            if (labelerMode === 'detect' && !item.boxes) {
                setStatus('Warming detector cache...');
                await ensureDetectItemReady(item, true);
            } else if (labelerMode === 'classify' && item.boxes) {
                setStatus('Warming classifier cache...');
                await ensureClassifyItemReady(item, true);
            }
        } catch (e) {
            //Prime failures should not block the UI from showing current item.
        }
    }

    async function loadQueue(opts = {}) {
        const forceRefresh = !!opts.forceRefresh;
        setStatus('Loading queue...');
        try {
            await refreshQueues(forceRefresh);
            applyModeQueue();
            setStatus(`Queue: ${queueTotal} items`);
            if (queue.length > 0) {
                startWarmLoop();
                await primeCurrentItemForMode();
                await loadCurrentItem();
            } else {
                await releaseCurrentClaim();
                setStatus('Queue empty - all done!');
                clearCanvas();
            }
        } catch (e) {
            setStatus(`Error: ${e.message}`);
            console.error('[Labeler] Queue load error:', e);
        }
    }

    async function loadCurrentItem() {
        console.log('[Labeler] loadCurrentItem called, queueIndex:', queueIndex, 'queue.length:', queue.length);
        let skippedClaims = 0;
        while (queueIndex < queue.length) {
            modePositions[labelerMode] = queueIndex;
            const item = queue[queueIndex];
            if (!item) break;
            const claimed = await claimQueueItem(item, labelerMode);
            if (!claimed) {
                queueIndex++;
                skippedClaims++;
                continue;
            }
            console.log('[Labeler] Loading item:', item);
            currentItem = item;
            currentSerial = item.serial;
            //Use cached endpoint for fast loading (falls back to Google Drive if not cached)
            currentImageUrl = buildApiUrl(`/api/labeler/cached_image/${item.serial}`);
            console.log('[Labeler] Built cached URL:', currentImageUrl);

            //Parse existing boxes if any (for classifier mode)
            if (item.boxes) {
                currentBoxes = parseYoloBoxes(item.boxes);
            } else {
                currentBoxes = [];
                if (labelerMode === 'detect') {
                    const cachedDet = detectPrefetch.get(String(currentSerial));
                    if (cachedDet && cachedDet.refined) {
                        currentBoxes = parseYoloBoxes(cachedDet.refined);
                    }
                }
            }

            //Parse existing labels
            if (item.labels) {
                currentLabels = item.labels.split('|');
            } else {
                currentLabels = [];
            }
            while (currentLabels.length < currentBoxes.length) {
                currentLabels.push('');
            }

            selectedBoxIdx = 0;
            currentCropIdx = 0;
            currentPredictions = [];
            boxesTouched = false;
            const listEl = document.getElementById('predictions-list');
            if (listEl) listEl.innerHTML = '';

            if (labelerMode === 'classify' && currentBoxes.length) {
                const firstUnlabeled = currentLabels.findIndex(lbl => !lbl || !lbl.trim());
                if (firstUnlabeled >= 0) currentCropIdx = firstUnlabeled;
                const cached = predCache.get(getPredCacheKey(currentItem));
                if (cached) {
                    currentPredictions = cached;
                }
            }
            updateInfo();
            loadImage(currentImageUrl);
            prefetchPredictions();
            prefetchImages();
            prefetchDetection();
            prefetchDetectionRefine();
            return;
        }
        await releaseCurrentClaim();
        if (skippedClaims > 0) {
            setStatus('No unlocked items right now. Try again shortly.');
        } else {
            setStatus('Queue complete!');
        }
        clearCanvas();
    }

    function togglePrefetchTimer(enabled) {
        if (prefetchTimer) {
            clearInterval(prefetchTimer);
            prefetchTimer = null;
        }
        if (enabled) {
            prefetchTimer = setInterval(() => {
                prefetchPredictions();
                prefetchImages();
                prefetchDetection();
                prefetchDetectionRefine();
            }, 3000);
        }
    }

    function resetPredCache() {
        predCacheEpoch += 1;
        predCache.clear();
        prefetchInFlight.clear();
        prefetchRunning = false;
        prefetchRequested = false;
        imagePrefetch.clear();
        detectPrefetchEpoch += 1;
        detectPrefetch.clear();
        detectPrefetchInFlight.clear();
        detectPrefetchRunning = false;
        detectPrefetchRequested = false;
        detectRefineInFlight.clear();
    }

    function getPredCacheKey(item) {
        if (!item) return '';
        return `${item.serial || ''}|${item.boxes || ''}`;
    }

    function parseYoloBoxes(boxStr) {
        //Format: "cx cy w h|cx cy w h|..."
        return boxStr.split('|').map(b => {
            const [cx, cy, w, h] = b.trim().split(/\s+/).map(parseFloat);
            return { cx, cy, w, h };
        }).filter(b => !isNaN(b.cx));
    }

    function formatYoloBoxes(boxes) {
        return boxes.map(b => `${b.cx.toFixed(6)} ${b.cy.toFixed(6)} ${b.w.toFixed(6)} ${b.h.toFixed(6)}`).join('|');
    }

    function prefetchImageSerial(serial) {
        if (!serial) return;
        const url = buildApiUrl(`/api/labeler/cached_image/${serial}`);
        if (imagePrefetch.has(url)) return;
        const img = new Image();
        img.decoding = 'async';
        img.loading = 'eager';
        img.src = url;
        imagePrefetch.set(url, img);
        if (imagePrefetch.size > IMAGE_PREFETCH_MAX) {
            const overflow = imagePrefetch.size - IMAGE_PREFETCH_MAX;
            const keys = imagePrefetch.keys();
            for (let i = 0; i < overflow; i++) {
                const key = keys.next().value;
                if (key) imagePrefetch.delete(key);
            }
        }
    }

    function warmWindowForMode(mode) {
        const q = queueForMode(mode);
        const start = Math.max(0, Number(modePositions[mode] || 0));
        return q.slice(start, start + SESSION_WARM_TARGET);
    }

    async function _waitFor(predicate, timeoutMs = 30000, stepMs = 120) {
        const end = Date.now() + timeoutMs;
        while (Date.now() < end) {
            if (predicate()) return true;
            await new Promise((resolve) => setTimeout(resolve, stepMs));
        }
        return false;
    }

    async function ensureDetectItemReady(item, waitForInFlight = false) {
        if (!item || !item.serial) return false;
        const key = String(item.serial);
        const cached = detectPrefetch.get(key);
        if (cached && cached.refined) return true;

        if (detectWarmInFlight.has(key)) {
            return waitForInFlight
                ? _waitFor(() => {
                    const hit = detectPrefetch.get(key);
                    return !!(hit && hit.refined);
                })
                : false;
        }

        detectWarmInFlight.add(key);
        try {
            prefetchImageSerial(item.serial);
            let raw = (cached && cached.raw) ? cached.raw : '';
            if (!raw) {
                const det = await apiPost('/api/labeler/detect', {
                    serial: item.serial,
                    url: item.url || null,
                    fast: false,
                    prefetch: true,
                }, { timeoutMs: API_POST_TIMEOUT_MS });
                raw = det.boxes_yolo || '';
            }

            let refined = raw;
            const parsed = parseYoloBoxes(raw || '');
            if (parsed.length) {
                const ref = await apiPost('/api/labeler/refine', {
                    serial: item.serial,
                    url: item.url || null,
                    boxes: parsed.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                    passes: DETECTOR_PREFETCH_REFINE_PASSES,
                    prefetch: true,
                }, { timeoutMs: API_POST_TIMEOUT_MS });
                refined = ref.boxes_yolo || raw;
            }

            detectPrefetch.set(key, { raw: raw || refined || '', refined: refined || raw || '' });
            return !!(refined || raw);
        } catch (e) {
            return false;
        } finally {
            detectWarmInFlight.delete(key);
        }
    }

    async function ensureClassifyItemReady(item, waitForInFlight = false) {
        if (!item || !item.boxes) return false;
        const key = getPredCacheKey(item);
        if (!key) return false;
        if (predCache.has(key)) return true;
        if (classifyWarmInFlight.has(key)) {
            return waitForInFlight ? _waitFor(() => predCache.has(key)) : false;
        }

        classifyWarmInFlight.add(key);
        try {
            prefetchImageSerial(item.serial);
            const parsed = parseYoloBoxes(item.boxes || '');
            if (!parsed.length) return false;
            const data = await apiPost('/api/labeler/identify', {
                serial: item.serial,
                url: item.url || null,
                boxes: parsed.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                prefetch: true,
            }, { timeoutMs: API_POST_TIMEOUT_MS });
            predCache.set(key, data.results || []);
            return true;
        } catch (e) {
            return false;
        } finally {
            classifyWarmInFlight.delete(key);
        }
    }

    async function runWarmTick() {
        if (!labelerActive || warmLoopRunning) return;
        warmLoopRunning = true;
        try {
            await refreshQueues(false);
            const detectTargets = warmWindowForMode('detect');
            const classifyTargets = warmWindowForMode('classify');
            detectTargets.forEach((item) => prefetchImageSerial(item.serial));
            classifyTargets.forEach((item) => prefetchImageSerial(item.serial));

            const detectTodo = detectTargets.filter((item) => {
                const key = String(item.serial || '');
                const cached = detectPrefetch.get(key);
                return !!item.serial && !(cached && cached.refined) && !detectWarmInFlight.has(key);
            }).slice(0, DETECT_PREFETCH_CONCURRENCY);

            const classifyTodo = classifyTargets.filter((item) => {
                const key = getPredCacheKey(item);
                return !!key && !predCache.has(key) && !classifyWarmInFlight.has(key);
            }).slice(0, PREFETCH_CONCURRENCY);

            const tasks = [];
            detectTodo.forEach((item) => tasks.push(ensureDetectItemReady(item, false)));
            classifyTodo.forEach((item) => tasks.push(ensureClassifyItemReady(item, false)));
            if (tasks.length) {
                await Promise.all(tasks);
            }
        } catch (e) {
            //Warm loop failures should not interrupt labeling.
        } finally {
            warmLoopRunning = false;
        }
    }

    function startWarmLoop() {
        stopWarmLoop();
        if (!labelerActive) return;
        warmTimer = setInterval(() => { runWarmTick(); }, SESSION_WARM_TICK_MS);
        runWarmTick();
    }

    function stopWarmLoop() {
        if (warmTimer) {
            clearInterval(warmTimer);
            warmTimer = null;
        }
    }

    function teardownLabeler() {
        labelerActive = false;
        void releaseCurrentClaim();
        stopRetrainStatusPoll();
        stopWarmLoop();
        togglePrefetchTimer(false);
        if (refPollId) {
            clearInterval(refPollId);
            refPollId = null;
        }
        resetPredCache();
        detectWarmInFlight.clear();
        classifyWarmInFlight.clear();
        detectQueue = [];
        classifyQueue = [];
        detectQueueTotal = 0;
        classifyQueueTotal = 0;
        queue = [];
        queueTotal = 0;
    }

    function loadImage(url) {
        console.log('[Labeler] loadImage called with:', url);
        setStatus('Loading image...');
        try {
            const targetOrigin = new URL(url, window.location.href).origin;
            if (targetOrigin === window.location.origin) {
                imageElement.removeAttribute('crossorigin');
            } else {
                imageElement.crossOrigin = 'anonymous';
            }
        } catch (e) {
            imageElement.removeAttribute('crossorigin');
        }
        imageElement.src = url;
    }

    function onImageLoad() {
        console.log('[Labeler] onImageLoad fired');
        //Resize canvas to match container
        const imgW = imageElement.naturalWidth;
        const imgH = imageElement.naturalHeight;
        console.log('[Labeler] Image dimensions:', imgW, 'x', imgH);

        if (!imgW || !imgH) {
            console.error('[Labeler] Image has no dimensions!');
            setStatus('Image loaded but has no dimensions');
            return;
        }

        resizeCanvasToContainer();
        resetView();

        if (labelerMode === 'detect' && currentBoxes.length === 0) {
            console.log('[Labeler] Auto-detect mode, calling runDetection');
            //Draw image first so there's no blank delay, then run detection
            drawCanvas();
            const cached = detectPrefetch.get(String(currentSerial));
            if (cached && cached.refined) {
                currentBoxes = parseYoloBoxes(cached.refined);
                drawCanvas();
                setStatus(`Found ${currentBoxes.length} box(es)`);
                updateInfo();
            } else if (cached && cached.raw) {
                currentBoxes = parseYoloBoxes(cached.raw);
                drawCanvas();
                setStatus(`Found ${currentBoxes.length} box(es)`);
                updateInfo();
                autoRefineCurrent(true);
            } else {
                runDetection();
            }
        } else {
            console.log('[Labeler] Drawing canvas directly');
            drawCanvas();
            setStatus(`Ready - sn${currentSerial}`);
            if (labelerMode === 'classify') {
                loadPredictions();
            }
        }
    }

    function onImageError(e) {
        console.error('[Labeler] Image load failed:', currentImageUrl, e);
        setStatus('Image load failed - skipping...');
        //Skip to next
        advanceQueue();
    }

    async function runDetection() {
        setStatus('Running YOLO+SAM...');
        try {
            //Send serial so backend reads from cache; no URL needed
            const data = await apiPost('/api/labeler/detect', {
                serial: currentSerial,
                url: currentItem?.url || null,
            });
            currentBoxes = parseYoloBoxes(data.boxes_yolo || '');
            selectedBoxIdx = 0;
            detectPrefetch.set(String(currentSerial), {
                raw: data.boxes_yolo || '',
                refined: data.boxes_yolo || '',
            });
            drawCanvas();
            setStatus(`Found ${currentBoxes.length} box(es)`);
            updateInfo();
        } catch (e) {
            setStatus(`Detection failed: ${e.message}`);
        }
    }

    async function runSamRefine() {
        if (!currentBoxes.length) {
            runDetection();
            return;
        }
        setStatus('Refining boxes...');
        try {
            const data = await apiPost('/api/labeler/refine', {
                serial: currentSerial,
                url: currentItem?.url || null,
                boxes: currentBoxes.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                passes: 2,
            });
            currentBoxes = parseYoloBoxes(data.boxes_yolo || '');
            selectedBoxIdx = Math.min(selectedBoxIdx, Math.max(0, currentBoxes.length - 1));
            detectPrefetch.set(String(currentSerial), {
                raw: data.boxes_yolo || '',
                refined: data.boxes_yolo || '',
            });
            drawCanvas();
            setStatus(`Refined ${currentBoxes.length} box(es)`);
            updateInfo();
        } catch (e) {
            setStatus(`Refine failed: ${e.message}`);
        }
    }

    async function autoRefineCurrent(runSecondPass = false) {
        if (boxesTouched || labelerMode !== 'detect') return;
        if (!currentBoxes.length) return;
        const serial = currentSerial;
        try {
            const data = await apiPost('/api/labeler/refine', {
                serial,
                url: currentItem?.url || null,
                boxes: currentBoxes.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                passes: runSecondPass ? 2 : 1,
            });
            if (serial !== currentSerial || boxesTouched) return;
            const refined = data.boxes_yolo || '';
            if (refined) {
                currentBoxes = parseYoloBoxes(refined);
                detectPrefetch.set(String(serial), { raw: refined, refined });
                drawCanvas();
                setStatus(`Found ${currentBoxes.length} box(es)`);
                updateInfo();
            }
        } catch (e) {
            //silent auto refine failure
        }
    }

    //---------- Canvas Drawing ----------

    function resizeCanvasToContainer() {
        if (!canvasAreaEl || !canvasEl) return;
        const rect = canvasAreaEl.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) return;
        canvasEl.width = Math.floor(rect.width);
        canvasEl.height = Math.floor(rect.height);
    }

    function resetView() {
        zoomLevel = 1.0;
        panX = 0;
        panY = 0;
    }

    function clampPan(imgW, imgH, scale) {
        const maxX = Math.max(0, (imgW * scale - canvasEl.width) / 2);
        const maxY = Math.max(0, (imgH * scale - canvasEl.height) / 2);
        panX = Math.max(-maxX, Math.min(maxX, panX));
        panY = Math.max(-maxY, Math.min(maxY, panY));
    }

    function getDrawParams() {
        const imgW = imageElement.naturalWidth || 1;
        const imgH = imageElement.naturalHeight || 1;
        baseScale = Math.min(canvasEl.width / imgW, canvasEl.height / imgH);
        const scale = baseScale * zoomLevel;
        clampPan(imgW, imgH, scale);
        const left = (canvasEl.width - imgW * scale) / 2 + panX;
        const top = (canvasEl.height - imgH * scale) / 2 + panY;
        return { imgW, imgH, scale, left, top };
    }

    function clearCanvas() {
        ctxCanvas.clearRect(0, 0, canvasEl.width, canvasEl.height);
        ctxCanvas.fillStyle = '#151515';
        ctxCanvas.fillRect(0, 0, canvasEl.width, canvasEl.height);
    }

    function drawCanvas() {
        if (!imageElement || !imageElement.complete) {
            clearCanvas();
            return;
        }
        if (labelerMode === 'classify') {
            drawClassifierCrop();
        } else {
            drawDetectorView();
        }
        updateInfo();
    }

    function drawDetectorView() {
        clearCanvas();
        const { imgW, imgH, scale, left, top } = getDrawParams();
        ctxCanvas.drawImage(
            imageElement,
            0, 0, imgW, imgH,
            left, top, imgW * scale, imgH * scale
        );

        currentBoxes.forEach((box, idx) => {
            const bx = box.cx * imgW;
            const by = box.cy * imgH;
            const bw = box.w * imgW;
            const bh = box.h * imgH;
            const x1 = left + (bx - bw / 2) * scale;
            const y1 = top + (by - bh / 2) * scale;
            const w = bw * scale;
            const h = bh * scale;

            const isSelected = idx === selectedBoxIdx;
            ctxCanvas.strokeStyle = isSelected ? '#00ff88' : '#008a4b';
            ctxCanvas.lineWidth = isSelected ? 3 : 2;
            ctxCanvas.strokeRect(x1, y1, w, h);

            const label = (currentLabels[idx] || '').trim();
            if (label) {
                ctxCanvas.fillStyle = isSelected ? '#00ff88' : '#008a4b';
                ctxCanvas.font = 'bold 14px sans-serif';
                ctxCanvas.fillText(label, x1 + 4, y1 - 6);
            }
        });
    }

    function drawClassifierCrop() {
        clearCanvas();
        if (!currentBoxes.length) return;
        const box = currentBoxes[currentCropIdx] || currentBoxes[0];
        if (!box) return;
        const imgW = imageElement.naturalWidth || 1;
        const imgH = imageElement.naturalHeight || 1;
        let x1 = (box.cx - box.w / 2) * imgW;
        let y1 = (box.cy - box.h / 2) * imgH;
        let x2 = (box.cx + box.w / 2) * imgW;
        let y2 = (box.cy + box.h / 2) * imgH;
        const padW = (x2 - x1) * CROP_PAD_PCT;
        const padH = (y2 - y1) * CROP_PAD_PCT;
        x1 = Math.max(0, x1 - padW);
        y1 = Math.max(0, y1 - padH);
        x2 = Math.min(imgW, x2 + padW);
        y2 = Math.min(imgH, y2 + padH);

        const cropW = Math.max(1, x2 - x1);
        const cropH = Math.max(1, y2 - y1);
        const scale = Math.min(canvasEl.width / cropW, canvasEl.height / cropH);
        const left = (canvasEl.width - cropW * scale) / 2;
        const top = (canvasEl.height - cropH * scale) / 2;

        ctxCanvas.drawImage(
            imageElement,
            x1, y1, cropW, cropH,
            left, top, cropW * scale, cropH * scale
        );
    }

    function updateInfo() {
        document.getElementById('info-serial').textContent = currentSerial || '-';
        const total = queueTotal || queue.length;
        document.getElementById('info-queue').textContent = total ? `${Math.min(queueIndex + 1, total)} / ${total}` : '-';
        document.getElementById('info-boxes').textContent = currentBoxes.length;
        const cropEl = document.getElementById('info-crop');
        if (cropEl) {
            cropEl.textContent = labelerMode === 'classify' && currentBoxes.length
                ? `${currentCropIdx + 1} / ${currentBoxes.length}`
                : '-';
        }
        const zoomEl = document.getElementById('info-zoom');
        if (zoomEl) {
            zoomEl.textContent = `${Math.round(zoomLevel * 100)}%`;
        }
        document.getElementById('pending-count').textContent = `${pendingUpdates.length} pending`;
    }

    function setStatus(msg) {
        if (statusEl) statusEl.textContent = msg;
    }

    //---------- Box Manipulation ----------

    function nudgeBox(corner, dx, dy) {
        if (currentBoxes.length === 0) return;
        const box = currentBoxes[selectedBoxIdx];
        if (!box) return;
        boxesTouched = true;

        //Convert px nudge to normalized coords
        const { imgW, imgH, scale } = getDrawParams();
        if (!imgW || !imgH || !scale) return;
        let ndx = dx / (imgW * scale);
        let ndy = dy / (imgH * scale);
        const minSize = 0.01;

        const x1 = box.cx - box.w / 2;
        const y1 = box.cy - box.h / 2;
        const x2 = box.cx + box.w / 2;
        const y2 = box.cy + box.h / 2;

        if (corner === 'tl') {
            const minDx = -x1;
            const maxDx = (x2 - minSize) - x1;
            const minDy = -y1;
            const maxDy = (y2 - minSize) - y1;
            ndx = Math.max(minDx, Math.min(maxDx, ndx));
            ndy = Math.max(minDy, Math.min(maxDy, ndy));
        } else {
            const minDx = (x1 + minSize) - x2;
            const maxDx = 1 - x2;
            const minDy = (y1 + minSize) - y2;
            const maxDy = 1 - y2;
            ndx = Math.max(minDx, Math.min(maxDx, ndx));
            ndy = Math.max(minDy, Math.min(maxDy, ndy));
        }

        if (corner === 'tl') {
            //Top-left: adjust position and shrink
            const newX1 = (box.cx - box.w / 2) + ndx;
            const newY1 = (box.cy - box.h / 2) + ndy;
            box.w = box.w - ndx;
            box.h = box.h - ndy;
            box.cx = newX1 + box.w / 2;
            box.cy = newY1 + box.h / 2;
        } else {
            //Bottom-right: adjust size with top-left anchored
            const newX2 = x1 + box.w + ndx;
            const newY2 = y1 + box.h + ndy;
            box.w = newX2 - x1;
            box.h = newY2 - y1;
            box.cx = x1 + box.w / 2;
            box.cy = y1 + box.h / 2;
        }

        //Clamp
        box.w = Math.max(minSize, Math.min(1, box.w));
        box.h = Math.max(minSize, Math.min(1, box.h));
        box.cx = Math.max(box.w / 2, Math.min(1 - box.w / 2, box.cx));
        box.cy = Math.max(box.h / 2, Math.min(1 - box.h / 2, box.cy));

        drawCanvas();
    }

    function addBox() {
        //Add a small box in center
        currentBoxes.push({ cx: 0.5, cy: 0.5, w: 0.2, h: 0.2 });
        selectedBoxIdx = currentBoxes.length - 1;
        boxesTouched = true;
        drawCanvas();
        setStatus('Added new box');
    }

    function deleteSelectedBox() {
        if (currentBoxes.length === 0) return;
        currentBoxes.splice(selectedBoxIdx, 1);
        currentLabels.splice(selectedBoxIdx, 1);
        selectedBoxIdx = Math.min(selectedBoxIdx, currentBoxes.length - 1);
        if (selectedBoxIdx < 0) selectedBoxIdx = 0;
        boxesTouched = true;
        drawCanvas();
        setStatus('Deleted box');
    }

    function selectNextBox() {
        if (currentBoxes.length === 0) return;
        selectedBoxIdx = (selectedBoxIdx + 1) % currentBoxes.length;
        drawCanvas();
    }

    //---------- Actions ----------

    function saveAndAdvance() {
        if (labelerMode === 'detect') {
            //Save boxes
            if (currentBoxes.length === 0) {
                rejectImage();
                return;
            }

            pendingUpdates.push({
                serial: currentSerial,
                box_coords: formatYoloBoxes(currentBoxes),
            });

            history.push({ type: 'detect', serial: currentSerial, boxes: [...currentBoxes] });
        } else {
            //Classifier mode - save labels
            pendingUpdates.push({
                serial: currentSerial,
                box_cat_ids: currentLabels.join('|'),
            });

            history.push({ type: 'classify', serial: currentSerial, labels: [...currentLabels] });
        }

        updateInfo();
        advanceQueue();
    }

    function rejectImage() {
        pendingUpdates.push({
            serial: currentSerial,
            box_coords: 'Rejected',
        });

        history.push({ type: 'reject', serial: currentSerial });
        updateInfo();
        advanceQueue();
    }

    function advanceQueue() {
        void releaseCurrentClaim();
        queueIndex++;
        modePositions[labelerMode] = queueIndex;
        if (queueIndex < queue.length) {
            void loadCurrentItem();
        } else {
            setStatus('Queue complete! Save pending changes.');
            clearCanvas();
        }
    }

    function undoLast() {
        if (history.length === 0) {
            setStatus('Nothing to undo');
            return;
        }

        const last = history.pop();
        //Remove from pending
        const idx = pendingUpdates.findIndex(u => u.serial === last.serial);
        if (idx >= 0) pendingUpdates.splice(idx, 1);

        //Go back
        void releaseCurrentClaim();
        queueIndex = Math.max(0, queueIndex - 1);
        modePositions[labelerMode] = queueIndex;
        void loadCurrentItem();
        setStatus('Undone');
        updateInfo();
    }

    async function saveAllPending() {
        if (pendingUpdates.length === 0) {
            setStatus('Nothing to save');
            return;
        }

        setStatus(`Saving ${pendingUpdates.length} updates...`);
        try {
            await apiPost('/api/labeler/save', { updates: pendingUpdates });
            setStatus(`Saved ${pendingUpdates.length} updates!`);
            pendingUpdates = [];
            updateInfo();
        } catch (e) {
            setStatus(`Save failed: ${e.message}`);
        }
    }

    //---------- Classifier Mode ----------

    async function loadPredictions(force = false) {
        if (currentBoxes.length === 0) return;
        if (!force) {
            if (currentPredictions.length) {
                renderPredictions();
                return;
            }
            const cached = predCache.get(getPredCacheKey(currentItem));
            if (cached) {
                currentPredictions = cached;
                renderPredictions();
                return;
            }
        }

        setStatus('Running classifier...');
        try {
            const requestSerial = currentSerial;
            const requestKey = getPredCacheKey(currentItem);
            const data = await apiPost('/api/labeler/identify', {
                serial: currentSerial,
                url: currentImageUrl,
                boxes: currentBoxes.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
            });

            if (requestSerial !== currentSerial || requestKey !== getPredCacheKey(currentItem)) {
                return;
            }
            currentPredictions = data.results || [];
            const key = getPredCacheKey(currentItem);
            if (key) {
                predCache.set(key, currentPredictions);
            }
            renderPredictions();
            setStatus(`Ready - sn${currentSerial}`);
        } catch (e) {
            setStatus(`Classify failed: ${e.message}`);
        }
    }

    function renderPredictions() {
        const listEl = document.getElementById('predictions-list');
        if (!listEl) return;

        const crop = currentPredictions[currentCropIdx];
        if (!crop) {
            listEl.innerHTML = '<div class="no-predictions">No predictions</div>';
            return;
        }

        listEl.innerHTML = (crop.candidates || []).slice(0, 9).map((c, i) => {
            const safeName = escapeHtml(c.name);
            const safeDesc = escapeHtml((c.desc || '').trim());
            const confPct = Math.max(0, Math.min(100, (c.conf || 0) * 100));
            let confLabel = 'Low Confidence';
            let confClass = 'conf-low';
            if (confPct >= 75) {
                confLabel = 'High Confidence';
                confClass = 'conf-high';
            } else if (confPct >= 50) {
                confLabel = 'Medium Confidence';
                confClass = 'conf-med';
            }
            const refs = (c.refs || []).map((ref, refIdx) => {
                const info = typeof ref === 'string'
                    ? { img: ref, serial: null, crop: null }
                    : (ref || {});
                const refImg = info.img || info.thumb || '';
                if (!refImg) return '';
                const sn = info.serial != null ? `sn${info.serial}` : '';
                const cropNum = Number(info.crop) || null;
                const cropText = cropNum ? ` crop ${cropNum}` : '';
                const caption = sn || cropNum ? `${sn}${cropText}`.trim() : '';
                return `
                    <div class="ref-frame">
                        <img src="data:image/jpeg;base64,${refImg}" alt="${safeName} ref ${refIdx + 1}">
                        ${caption ? `<div class="ref-overlay">${escapeHtml(caption)}</div>` : ''}
                    </div>
                `;
            }).join('');
            return `
                <div class="prediction-item" data-idx="${i + 1}">
                    <div class="prediction-head">
                        <span class="pred-key">${i + 1}</span>
                        <span class="pred-name">${safeName}</span>
                        <span class="pred-conf ${confClass}">${confLabel} (${confPct.toFixed(1)}%)</span>
                    </div>
                    ${safeDesc ? `<div class="pred-desc">${safeDesc}</div>` : ''}
                    <div class="prediction-refs">${refs}</div>
                </div>
            `;
        }).join('');
        applyRefAspectClamp(listEl);
    }

    function escapeHtml(value) {
        return String(value || '').replace(/[&<>"]/g, (ch) => {
            switch (ch) {
                case '&': return '&amp;';
                case '<': return '&lt;';
                case '>': return '&gt;';
                case '"': return '&quot;';
                default: return ch;
            }
        });
    }

    function applyRefAspectClamp(rootEl) {
        if (!rootEl) return;
        const maxWide = 16 / 9;
        const maxTall = 9 / 16;
        const imgs = rootEl.querySelectorAll('.prediction-refs img');
        imgs.forEach(img => {
            const frame = img.closest('.ref-frame');
            const apply = () => {
                if (!frame) return;
                const w = img.naturalWidth || 0;
                const h = img.naturalHeight || 0;
                if (!w || !h) return;
                const ratio = w / h;
                frame.classList.remove('clamp-wide', 'clamp-tall');
                if (ratio > maxWide) {
                    frame.classList.add('clamp-wide');
                } else if (ratio < maxTall) {
                    frame.classList.add('clamp-tall');
                }
            };
            if (img.complete) {
                apply();
            } else {
                img.addEventListener('load', apply, { once: true });
            }
        });
    }

    function selectPrediction(num) {
        const listEl = document.getElementById('predictions-list');
        const items = listEl.querySelectorAll('.prediction-item');
        if (num < 1 || num > items.length) return;

        const item = items[num - 1];
        const name = item.querySelector('.pred-name').textContent;
        currentLabels[currentCropIdx] = name;

        advanceCrop();
    }

    function markNeedsReview() {
        currentLabels[currentCropIdx] = 'NeedsReview';
        advanceCrop();
    }

    function rejectCrop() {
        currentLabels[currentCropIdx] = 'Rejected';
        advanceCrop();
    }

    function advanceCrop() {
        currentCropIdx++;
        if (currentCropIdx >= currentBoxes.length) {
            //All crops labeled
            saveAndAdvance();
        } else {
            loadPredictions();
            drawCanvas();
        }
    }

    function prefetchPredictions() {
        if (labelerMode !== 'classify') return;
        if (prefetchRunning) {
            prefetchRequested = true;
            return;
        }
        const epoch = predCacheEpoch;
        const start = queueIndex + 1;
        const end = Math.min(queue.length, start + PREFETCH_AHEAD);
        const targets = [];
        for (let i = start; i < end; i++) {
            const item = queue[i];
            if (!item || !item.boxes) continue;
            const key = getPredCacheKey(item);
            if (!key || predCache.has(key) || prefetchInFlight.has(key) || classifyWarmInFlight.has(key)) continue;
            targets.push({ item, key });
        }
        if (!targets.length) return;

        prefetchRunning = true;
        let idx = 0;
        let active = 0;

        const runNext = async () => {
            if (idx >= targets.length) {
                if (active === 0) {
                    prefetchRunning = false;
                    if (prefetchRequested) {
                        prefetchRequested = false;
                        prefetchPredictions();
                    }
                }
                return;
            }
            const target = targets[idx++];
            active++;
            prefetchInFlight.add(target.key);
            classifyWarmInFlight.add(target.key);
            try {
                const parsed = parseYoloBoxes(target.item.boxes);
                if (!parsed.length) {
                    return;
                }
                const data = await apiPost('/api/labeler/identify', {
                    serial: target.item.serial,
                    url: target.item.url || null,
                    boxes: parsed.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                    prefetch: true,
                }, { timeoutMs: API_PREFETCH_TIMEOUT_MS });
                if (epoch === predCacheEpoch) {
                    predCache.set(target.key, data.results || []);
                }
            } catch (e) {
                //Ignore prefetch failures
            } finally {
                prefetchInFlight.delete(target.key);
                classifyWarmInFlight.delete(target.key);
                active--;
                runNext();
                if (idx >= targets.length && active === 0) {
                    prefetchRunning = false;
                    if (prefetchRequested) {
                        prefetchRequested = false;
                        prefetchPredictions();
                    }
                }
            }
        };

        const slots = Math.min(PREFETCH_CONCURRENCY, targets.length);
        for (let i = 0; i < slots; i++) {
            runNext();
        }
    }

    function prefetchImages() {
        const start = queueIndex + 1;
        const end = Math.min(queue.length, start + IMAGE_PREFETCH_AHEAD);
        for (let i = start; i < end; i++) {
            const item = queue[i];
            if (!item || !item.serial) continue;
            prefetchImageSerial(item.serial);
        }
    }

    function prefetchDetection() {
        if (labelerMode !== 'detect') return;
        const now = Date.now();
        if (now - lastDetectPrefetch < DETECT_PREFETCH_COOLDOWN_MS) return;
        lastDetectPrefetch = now;
        if (detectPrefetchRunning) {
            detectPrefetchRequested = true;
            return;
        }
        const epoch = detectPrefetchEpoch;
        const start = queueIndex + 1;
        const end = Math.min(queue.length, start + DETECT_PREFETCH_AHEAD);
        const targets = [];
        for (let i = start; i < end; i++) {
            const item = queue[i];
            if (!item || !item.serial) continue;
            const key = String(item.serial);
            if (detectPrefetch.has(key) || detectPrefetchInFlight.has(key) || detectWarmInFlight.has(key)) continue;
            targets.push({ item, key });
        }
        if (!targets.length) return;

        detectPrefetchRunning = true;
        let idx = 0;
        let active = 0;

        const runNext = async () => {
            if (idx >= targets.length) {
                if (active === 0) {
                    detectPrefetchRunning = false;
                    if (detectPrefetchRequested) {
                        detectPrefetchRequested = false;
                        prefetchDetection();
                    }
                }
                return;
            }
            const target = targets[idx++];
            active++;
            detectPrefetchInFlight.add(target.key);
            detectWarmInFlight.add(target.key);
            try {
                const data = await apiPost('/api/labeler/detect', {
                    serial: target.item.serial,
                    url: target.item.url || null,
                    fast: false,
                    prefetch: true,
                });
                if (epoch === detectPrefetchEpoch && data && data.boxes_yolo) {
                    detectPrefetch.set(target.key, { raw: data.boxes_yolo, refined: null });
                }
            } catch (e) {
                //Ignore prefetch failures
            } finally {
                detectPrefetchInFlight.delete(target.key);
                detectWarmInFlight.delete(target.key);
                active--;
                runNext();
                if (idx >= targets.length && active === 0) {
                    detectPrefetchRunning = false;
                    if (detectPrefetchRequested) {
                        detectPrefetchRequested = false;
                        prefetchDetection();
                    }
                }
            }
        };

        const slots = Math.min(DETECT_PREFETCH_CONCURRENCY, targets.length);
        for (let i = 0; i < slots; i++) {
            runNext();
        }
    }

    function prefetchDetectionRefine() {
        if (labelerMode !== 'detect') return;
        if (DETECT_REFINE_PREFETCH_AHEAD <= 0) return;
        const now = Date.now();
        if (now - lastDetectRefinePrefetch < DETECT_REFINE_COOLDOWN_MS) return;
        lastDetectRefinePrefetch = now;
        const start = queueIndex + 1;
        const end = Math.min(queue.length, start + DETECT_REFINE_PREFETCH_AHEAD);
        for (let i = start; i < end; i++) {
            const item = queue[i];
            if (!item || !item.serial) continue;
            const key = String(item.serial);
            const entry = detectPrefetch.get(key);
            if (!entry || !entry.raw || entry.refined || detectRefineInFlight.has(key)) continue;
            detectRefineInFlight.add(key);
            apiPost('/api/labeler/refine', {
                serial: item.serial,
                url: item.url || null,
                boxes: parseYoloBoxes(entry.raw).map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                passes: DETECTOR_PREFETCH_REFINE_PASSES,
                prefetch: true,
            }).then((data) => {
                if (data && data.boxes_yolo) {
                    detectPrefetch.set(key, { raw: entry.raw, refined: data.boxes_yolo });
                }
            }).catch(() => {
                //ignore
            }).finally(() => {
                detectRefineInFlight.delete(key);
            });
        }
    }

    //---------- Events ----------

    function onCanvasClick(e) {
        if (labelerMode !== 'detect') return;
        if (suppressClick) {
            suppressClick = false;
            return;
        }
        if (currentBoxes.length === 0) return;

        const rect = canvasEl.getBoundingClientRect();
        const { imgW, imgH, scale, left, top } = getDrawParams();
        if (!imgW || !imgH || !scale) return;
        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        const imgX = (cx - left) / scale;
        const imgY = (cy - top) / scale;
        const x = imgX / imgW;
        const y = imgY / imgH;

        //Find clicked box
        for (let i = 0; i < currentBoxes.length; i++) {
            const box = currentBoxes[i];
            const x1 = box.cx - box.w / 2;
            const y1 = box.cy - box.h / 2;
            const x2 = box.cx + box.w / 2;
            const y2 = box.cy + box.h / 2;

            if (x >= x1 && x <= x2 && y >= y1 && y <= y2) {
                selectedBoxIdx = i;
                drawCanvas();
                return;
            }
        }
    }

    function onCanvasWheel(e) {
        if (labelerMode !== 'detect') return;
        if (!imageElement || !imageElement.complete) return;
        e.preventDefault();

        const imgW = imageElement.naturalWidth || 1;
        const imgH = imageElement.naturalHeight || 1;
        baseScale = Math.min(canvasEl.width / imgW, canvasEl.height / imgH);
        const rect = canvasEl.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;

        const scale = baseScale * zoomLevel;
        const left = (canvasEl.width - imgW * scale) / 2 + panX;
        const top = (canvasEl.height - imgH * scale) / 2 + panY;
        const imgX = (mouseX - left) / scale;
        const imgY = (mouseY - top) / scale;

        const dir = e.deltaY < 0 ? 1 : -1;
        const factor = 1 + (ZOOM_STEP * dir);
        const nextZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, zoomLevel * factor));
        if (nextZoom === zoomLevel) return;

        zoomLevel = nextZoom;
        const newScale = baseScale * zoomLevel;
        const newLeft = (canvasEl.width - imgW * newScale) / 2 + panX;
        const newTop = (canvasEl.height - imgH * newScale) / 2 + panY;
        const desiredLeft = mouseX - imgX * newScale;
        const desiredTop = mouseY - imgY * newScale;
        panX += desiredLeft - newLeft;
        panY += desiredTop - newTop;
        clampPan(imgW, imgH, newScale);
        drawCanvas();
    }

    function onCanvasMouseDown(e) {
        if (labelerMode !== 'detect') return;
        if (e.button !== 0) return;
        isPanning = true;
        panMoved = false;
        lastPan = { x: e.clientX, y: e.clientY };
    }

    function onCanvasMouseMove(e) {
        if (!isPanning) return;
        const dx = e.clientX - lastPan.x;
        const dy = e.clientY - lastPan.y;
        lastPan = { x: e.clientX, y: e.clientY };
        if (Math.abs(dx) + Math.abs(dy) > 1) {
            panMoved = true;
        }
        panX += dx;
        panY += dy;
        const imgW = imageElement.naturalWidth || 1;
        const imgH = imageElement.naturalHeight || 1;
        baseScale = Math.min(canvasEl.width / imgW, canvasEl.height / imgH);
        const scale = baseScale * zoomLevel;
        clampPan(imgW, imgH, scale);
        drawCanvas();
    }

    function onCanvasMouseUp(e) {
        if (e.button !== 0) return;
        isPanning = false;
        if (panMoved) {
            suppressClick = true;
        }
    }

    function onKeyDown(e) {
        //Only handle when labeler is visible
        if (!containerEl || containerEl.style.display === 'none') return;

        let key = e.key.toLowerCase();
        if (e.code === 'Space' || key === ' ') {
            key = 'space';
        }

        const movementKeys = new Set(['w', 'a', 's', 'd', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright']);
        if (labelerMode === 'detect' && movementKeys.has(key)) {
            if (key.startsWith('arrow')) e.preventDefault();
            pressedKeys.add(key);
            if (moveIntervalId === null) {
                moveIntervalId = setInterval(() => {
                    applyMovement();
                }, 50);
            }
            applyMovement();
            return;
        }

        const actionKeys = new Set(['backspace', 'y', 'enter', 'n', 'x', 'tab', 'space', '0', '1','2','3','4','5','6','7','8','9']);
        if (e.repeat && actionKeys.has(key)) return;

        //Common
        if (key === 'backspace') {
            e.preventDefault();
            if (throttleAction('undo')) {
                undoLast();
            }
            return;
        }

        if (labelerMode === 'detect') {
            handleDetectorKey(e, key);
        } else {
            handleClassifierKey(e, key);
        }
    }

    function onKeyUp(e) {
        let key = e.key.toLowerCase();
        if (e.code === 'Space' || key === ' ') {
            key = 'space';
        }
        const movementKeys = new Set(['w', 'a', 's', 'd', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright']);
        if (labelerMode === 'detect' && movementKeys.has(key)) {
            pressedKeys.delete(key);
            if (pressedKeys.size === 0 && moveIntervalId !== null) {
                clearInterval(moveIntervalId);
                moveIntervalId = null;
            }
        }
    }

    function applyMovement() {
        if (pressedKeys.size === 0) return;
        let dxTL = 0;
        let dyTL = 0;
        let dxBR = 0;
        let dyBR = 0;
        if (pressedKeys.has('w')) dyTL -= NUDGE_PX;
        if (pressedKeys.has('s')) dyTL += NUDGE_PX;
        if (pressedKeys.has('a')) dxTL -= NUDGE_PX;
        if (pressedKeys.has('d')) dxTL += NUDGE_PX;
        if (pressedKeys.has('arrowup')) dyBR -= NUDGE_PX;
        if (pressedKeys.has('arrowdown')) dyBR += NUDGE_PX;
        if (pressedKeys.has('arrowleft')) dxBR -= NUDGE_PX;
        if (pressedKeys.has('arrowright')) dxBR += NUDGE_PX;
        if (dxTL || dyTL) nudgeBox('tl', dxTL, dyTL);
        if (dxBR || dyBR) nudgeBox('br', dxBR, dyBR);
    }

    function handleDetectorKey(e, key) {
        switch (key) {
            case '2': if (throttleAction('addBox')) addBox(); break;
            case 'x': if (throttleAction('deleteBox')) deleteSelectedBox(); break;
            case 'e': if (throttleAction('refine')) runSamRefine(); break;
            case 'y':
            case 'enter':
                e.preventDefault();
                if (throttleAction('saveAdvance')) saveAndAdvance();
                break;
            case 'n':
                if (throttleAction('reject')) rejectImage();
                break;
            case 'tab':
            case 'space':
                e.preventDefault();
                if (throttleAction('nextBox')) selectNextBox();
                break;
        }
    }

    function handleClassifierKey(e, key) {
        if (key >= '1' && key <= '9') {
            if (throttleAction(`pred-${key}`)) {
                selectPrediction(parseInt(key));
            }
            return;
        }

        switch (key) {
            case '0':
                if (throttleAction('needsReview')) markNeedsReview();
                break;
            case 'x':
                if (throttleAction('rejectCrop')) rejectCrop();
                break;
            case 'enter':
                e.preventDefault();
                if (throttleAction('advanceCrop')) advanceCrop();
                break;
        }
    }

    function throttleAction(actionKey) {
        const now = Date.now();
        const last = actionCooldowns.get(actionKey) || 0;
        if (now - last < ACTION_COOLDOWN_MS) {
            return false;
        }
        actionCooldowns.set(actionKey, now);
        return true;
    }

    //---------- Expose to global ----------

    window.initLabeler = initLabeler;
    window.teardownLabeler = teardownLabeler;
    window.labelerSwitchMode = switchMode;
    window.addEventListener('beforeunload', teardownLabeler);

    //Auto-init when labeler view is shown
    document.addEventListener('DOMContentLoaded', () => {
        //Will be called by setView when labeler tab is activated
    });

})();
