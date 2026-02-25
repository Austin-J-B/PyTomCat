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
 *
 *   Manual Review Mode:
 *     Click      - Select cat card
 *     X          - Reject crop
 *     Enter      - Defer and move to next photo
 *     Backspace  - Undo
 */
(function () {
    'use strict';

    const NUDGE_PX = 2; //Pixels per WASD/arrow press
    const CROP_PAD_PCT = 0.03;
    const ZOOM_MIN = 0.5;
    const ZOOM_MAX = 6.0;
    const ZOOM_STEP = 0.12;
    const PREFETCH_AHEAD = 12;
    const PREFETCH_CONCURRENCY = 1;
    const IMAGE_PREFETCH_AHEAD = 12;
    const IMAGE_PREFETCH_MAX = 80;
    const DETECT_PREFETCH_AHEAD = 12;
    const DETECT_PREFETCH_CONCURRENCY = 1;
    const DETECT_PREFETCH_COOLDOWN_MS = 8000;
    const DETECT_REFINE_PREFETCH_AHEAD = 12;
    const DETECT_REFINE_COOLDOWN_MS = 12000;
    const ACTION_COOLDOWN_MS = 250;
    const API_POST_TIMEOUT_MS = 45000;
    const API_PREFETCH_TIMEOUT_MS = 15000;
    const CLAIM_HEARTBEAT_MS = 15000;
    const SESSION_WARM_TARGET = 25;
    const SESSION_WARM_TICK_MS = 1500;
    const SESSION_REFRESH_QUEUES_MS = 30000;
    const DETECTOR_PREFETCH_REFINE_PASSES = 2;
    const INITIAL_DETECT_WARM_WINDOW = 25;
    const INITIAL_DETECT_WARM_MIN = 10;
    const INITIAL_DETECT_WARM_TIMEOUT_MS = 25000;
    const INITIAL_CLASSIFY_WARM_WINDOW = 25;
    const INITIAL_CLASSIFY_WARM_MIN = 10;
    const INITIAL_CLASSIFY_WARM_TIMEOUT_MS = 20000;
    const ITEM_READY_WAIT_TIMEOUT_MS = 90000;
    const API_RETRY_MAX_ATTEMPTS = 3;
    const API_RETRY_BASE_MS = 280;
    const CLASSIFY_LOAD_TICK_MS = 250;
    const CLASSIFY_REFS_PER_CAT_TARGET = 5;
    const CLASSIFY_WARM_READY_MIN_CANDIDATES = 5;
    const CLASSIFY_WARM_READY_MIN_REFS_PER_CAT = 3;
    const CLASSIFY_REF_MIN_CANDIDATES_WITH_REFS = 5;
    const CLASSIFY_REF_MIN_COVERAGE = 0.4;
    const CLASSIFY_REF_RETRY_ATTEMPTS = 4;
    const CLASSIFY_REF_REFRESH_COOLDOWN_MS = 1200;
    const CLASSIFY_PREFETCH_FAIL_BASE_MS = 5000;
    const CLASSIFY_PREFETCH_FAIL_MAX_MS = 120000;
    const CLASSIFY_ITEM_DISPLAY_READY_TIMEOUT_MS = 20000;
    const MANUAL_REF_CACHE_VERSION = 'manual_refs_v1';
    const FLAGGED_REF_SERIALS_STORAGE_KEY = 'labelerFlaggedRefSerials_v1';
    const MANUAL_PREFETCH_AHEAD = 4;
    const MANUAL_PREFETCH_CONCURRENCY = 1;
    const MANUAL_CANDIDATE_CACHE_MAX = 240;
    const UI_DIAG_MIN_INTERVAL_MS = 1500;
    const REF_IMAGE_PREFETCH_MAX = 12000;
    const REF_IMAGE_RETRY_BASE_MS = 1500;
    const REF_IMAGE_RETRY_MAX_MS = 60000;
    const REF_IMAGE_RETRY_MAX_ATTEMPTS = 6;

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
    let labelerMode = 'detect'; //'detect' or 'classify' or 'manual'
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
    let imageReadyForCurrentItem = false;
    let imageLoadToken = 0;
    let activeImageLoadToken = 0;
    let imageLoadRetryCount = 0;
    let zoomLevel = 1.0;
    let panX = 0;
    let panY = 0;
    let baseScale = 1.0;
    let isPanning = false;
    let panMoved = false;
    let suppressClick = false;
    let lastPan = { x: 0, y: 0 };
    let classifierWarmDisplayPct = 0;
    let classifierWarmItemKey = '';
    let loadPredictionsBusy = false;
    let loadPredictionsQueuedForce = false;
    let predCache = new Map();
    let prefetchInFlight = new Set();
    let prefetchRunning = false;
    let prefetchRequested = false;
    let predCacheEpoch = 0;
    let classifyPrefetchBackoffUntil = 0;
    let prefetchTimer = null;
    let imagePrefetch = new Map();
    let refImagePrefetch = new Map();
    let refImagePrefetchState = new Map();
    let refImagePrefetchRetry = new Map();
    let refImageRetryTimers = new Map();
    let refRenderRefreshTimer = null;
    let detectPrefetch = new Map();
    let detectWarmFailures = new Map();
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
    let prefetchedClaims = new Map();
    let retrainPollId = null;
    let lastQueueRefreshTs = 0;
    let queueRefreshPromise = null;
    let queueBackgroundRefreshPromise = null;
    let queueRefreshPromisesByMode = { detect: null, classify: null, manual: null };
    let lastQueueRefreshByMode = { detect: 0, classify: 0, manual: 0 };
    let detectQueue = [];
    let detectQueueTotal = 0;
    let classifyQueue = [];
    let classifyQueueTotal = 0;
    let manualQueue = [];
    let manualQueueTotal = 0;
    let modePositions = { detect: 0, classify: 0, manual: 0 };
    let detectWarmInFlight = new Set();
    let classifyWarmInFlight = new Set();
    let classifyForegroundInFlight = new Map();
    let classifyRefRefreshTs = new Map();
    let classifyPrefetchFailures = new Map();
    let manualCandidates = [];
    let manualReviewIndices = [];
    let manualReviewCursor = 0;
    let manualRefPollId = null;
    let manualSearchDebounceTimer = null;
    let manualCandidateCache = new Map();
    let manualCandidatesKey = '';
    let manualPrefetchInFlight = new Set();
    let manualPrefetchRunning = false;
    let manualPrefetchRequested = false;
    let incorrectFlagMode = false;
    let flagRequestsInFlight = new Set();
    let flaggedRefSerials = new Set();
    let initialDetectWarmDone = false;
    let initialClassifyWarmDone = false;
    let loadCurrentItemInFlight = false;
    let loadCurrentItemQueued = false;
    let warmOverlayEl = null;
    let warmBarEl = null;
    let warmLabelEl = null;
    let warmSubEl = null;
    let classifyItemLoadOverlayToken = 0;
    let classifyItemLoadOverlayActive = false;
    let pendingUndoRestore = null;

    //DOM references (set after init)
    let containerEl = null;
    let statusEl = null;
    let infoEl = null;
    let cropDisplayEl = null;
    let predictionsEl = null;
    let manualSearchInputEl = null;
    let manualSearchBtnEl = null;
    let manualSearchStatusEl = null;
    let predictionsTitleEl = null;
    let manualSidebarRestoreScrollTop = null;
    let flagBtnEl = null;
    let flagModeHintEl = null;

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
        loadFlaggedRefSerials();
        containerEl.classList.remove('labeler-mode-detect', 'labeler-mode-classify', 'labeler-mode-manual');
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
                    </div>
                    <div class="labeler-tabs">
                        <button class="labeler-tab active" data-mode="detect">Detector</button>
                        <button class="labeler-tab" data-mode="classify">Classifier</button>
                        <button class="labeler-tab" data-mode="manual">Manual Review</button>
                    </div>
                    <div class="labeler-status" id="labeler-status">Loading...</div>
                    <div id="manual-search-wrap" class="manual-search-wrap" style="display:none">
                        <input id="manual-search-input" class="manual-search-input" type="text" placeholder="Search Cat">
                        <button id="manual-search-btn" class="labeler-btn labeler-btn-secondary" type="button">Search Cat</button>
                        <div id="manual-search-status" class="manual-search-status"></div>
                    </div>
                    <div class="labeler-actions">
                        <button class="labeler-btn labeler-btn-secondary labeler-btn-quiet" id="btn-retrain-4am" title="Queue full gallery retrain for next 4 AM">Schedule Gallery Retrain</button>
                        <span class="retrain-status" id="retrain-status">Retrain: not scheduled</span>
                        <span class="pending-count" id="pending-count">0 pending</span>
                        <button class="labeler-btn" id="btn-save-all" title="Save pending annotations">Save All</button>
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
                        <div class="labeler-help-shortcuts">
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
                            <div id="shortcuts-manual" class="shortcuts-row" style="display:none">
                                <span class="shortcut-item"><kbd>Click</kbd> Select cat</span>
                                <span class="shortcut-item"><kbd>X</kbd> Reject crop</span>
                                <span class="shortcut-item"><kbd>Enter</kbd> Skip photo</span>
                                <span class="shortcut-item"><kbd>Backspace</kbd> Undo</span>
                            </div>
                        </div>
                        <div class="labeler-banner-tools">
                            <span class="labeler-flag-hint hidden" id="labeler-flag-hint">Flag mode: click reference photos</span>
                            <button
                                class="labeler-btn labeler-btn-secondary labeler-btn-danger labeler-flag-icon-btn"
                                id="btn-flag-incorrect"
                                type="button"
                                title="Flag incorrect label"
                                aria-label="Flag incorrect label"
                                aria-pressed="false"
                            >&#x1F6A9;</button>
                        </div>
                    </div>
                </div>
                
                <div class="labeler-main">
                    <div class="labeler-canvas-area">
                        <canvas id="labeler-canvas" width="800" height="600"></canvas>
                        <img id="labeler-image" style="display:none">
                        <div class="labeler-warm-overlay hidden" id="labeler-warm-overlay" aria-live="polite">
                            <div class="labeler-warm-card">
                                <div class="labeler-warm-title" id="labeler-warm-label">Preparing queue...</div>
                                <div class="labeler-warm-sub" id="labeler-warm-sub">0 / 10 ready</div>
                                <div class="labeler-warm-track">
                                    <div class="labeler-warm-bar" id="labeler-warm-bar"></div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="labeler-sidebar">
                        <div class="labeler-predictions" id="labeler-predictions" style="display:none">
                            <h4 id="predictions-title">Predictions</h4>
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
        predictionsTitleEl = document.getElementById('predictions-title');
        cropDisplayEl = document.getElementById('labeler-crop-display');
        manualSearchInputEl = document.getElementById('manual-search-input');
        manualSearchBtnEl = document.getElementById('manual-search-btn');
        manualSearchStatusEl = document.getElementById('manual-search-status');
        flagBtnEl = document.getElementById('btn-flag-incorrect');
        flagModeHintEl = document.getElementById('labeler-flag-hint');
        canvasAreaEl = containerEl.querySelector('.labeler-canvas-area');
        warmOverlayEl = document.getElementById('labeler-warm-overlay');
        warmBarEl = document.getElementById('labeler-warm-bar');
        warmLabelEl = document.getElementById('labeler-warm-label');
        warmSubEl = document.getElementById('labeler-warm-sub');
        setWarmOverlay(false);
        if (canvasEl) {
            canvasEl.tabIndex = 0;
            canvasEl.style.outline = 'none';
            canvasEl.addEventListener('mouseenter', () => canvasEl.focus());
        }
        const predictionsList = document.getElementById('predictions-list');
        predictionsList?.addEventListener('click', (e) => {
            const refFrame = e.target.closest('.ref-frame');
            if (incorrectFlagMode) {
                e.preventDefault();
                e.stopPropagation();
                if (refFrame) {
                    void flagReferenceFromFrame(refFrame);
                } else {
                    setStatus('Flag mode active: click reference photos to flag them.');
                }
                return;
            }
            if (labelerMode === 'manual') {
                const card = e.target.closest('.manual-cat-card');
                if (!card) return;
                const catName = String(card.dataset.name || '').trim();
                if (catName) selectManualCandidate(catName);
                return;
            }
            const item = e.target.closest('.prediction-item');
            if (!item) return;
            const idx = parseInt(item.dataset.idx || '', 10);
            if (idx) selectPrediction(idx);
        });
        manualSearchBtnEl?.addEventListener('click', runManualSearch);
        manualSearchInputEl?.addEventListener('keydown', (e) => {
            e.stopPropagation();
            if (e.key === 'Enter') {
                e.preventDefault();
                runManualSearch({ smooth: true, live: false });
            }
        });
        manualSearchInputEl?.addEventListener('input', () => {
            if (manualSearchDebounceTimer) {
                clearTimeout(manualSearchDebounceTimer);
            }
            manualSearchDebounceTimer = setTimeout(() => {
                runManualSearch({ smooth: false, live: true });
            }, 80);
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
        flagBtnEl?.addEventListener('click', toggleIncorrectFlagMode);
        document.getElementById('btn-retrain-4am').addEventListener('click', scheduleRetrain4am);
        const retrainBtn = document.getElementById('btn-retrain-4am');
        const retrainStatus = document.getElementById('retrain-status');
        if (flagBtnEl) flagBtnEl.style.display = 'none';
        updateFlagButtonState();
        if (retrainBtn) retrainBtn.style.display = 'none';
        if (retrainStatus) retrainStatus.style.display = 'none';

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
        startRetrainStatusPoll();
        togglePrefetchTimer(true);
        startWarmLoop();

        //Load initial queue
        loadQueue({ forceRefresh: true });
    }

    //---------- Mode Switching ----------

    function switchMode(mode) {
        void releaseCurrentClaim();
        imageLoadToken += 1;
        activeImageLoadToken = imageLoadToken;
        imageReadyForCurrentItem = false;
        labelerMode = mode;
        _cancelClassifyItemLoadOverlayWait();
        setWarmOverlay(false);
        setStatus(`Switching to ${mode}...`);
        const listEl = document.getElementById('predictions-list');
        if (listEl) listEl.innerHTML = '';
        currentPredictions = [];
        manualCandidates = [];
        clearCanvas();
        containerEl.querySelectorAll('.labeler-tab').forEach(tab => {
            tab.classList.toggle('active', tab.dataset.mode === mode);
        });

        containerEl.classList.remove('labeler-mode-detect', 'labeler-mode-classify', 'labeler-mode-manual');
        containerEl.classList.add(`labeler-mode-${mode}`);

        document.getElementById('shortcuts-detect').style.display = mode === 'detect' ? 'block' : 'none';
        document.getElementById('shortcuts-classify').style.display = mode === 'classify' ? 'block' : 'none';
        document.getElementById('shortcuts-manual').style.display = mode === 'manual' ? 'block' : 'none';
        predictionsEl.style.display = (mode === 'classify' || mode === 'manual') ? 'block' : 'none';
        if (predictionsTitleEl) {
            predictionsTitleEl.textContent = mode === 'classify' ? 'Predictions' : '';
            predictionsTitleEl.style.display = mode === 'classify' ? '' : 'none';
        }
        const retrainBtn = document.getElementById('btn-retrain-4am');
        const retrainStatus = document.getElementById('retrain-status');
        const showRetrain = mode === 'classify';
        const showFlag = mode === 'classify' || mode === 'manual';
        if (retrainBtn) retrainBtn.style.display = showRetrain ? '' : 'none';
        if (retrainStatus) retrainStatus.style.display = showRetrain ? '' : 'none';
        if (flagBtnEl) flagBtnEl.style.display = showFlag ? '' : 'none';
        if (!showFlag && incorrectFlagMode) {
            setIncorrectFlagMode(false);
        } else {
            updateFlagButtonState();
        }
        if (cropDisplayEl) cropDisplayEl.style.display = 'none';
        const manualWrap = document.getElementById('manual-search-wrap');
        if (manualWrap) manualWrap.style.display = mode === 'manual' ? '' : 'none';

        pressedKeys.clear();
        if (moveIntervalId !== null) {
            clearInterval(moveIntervalId);
            moveIntervalId = null;
        }
        actionCooldowns.clear();
        if (mode !== 'manual' && manualRefPollId) {
            clearInterval(manualRefPollId);
            manualRefPollId = null;
        }

        togglePrefetchTimer(true);
        startWarmLoop();
        if (mode === 'classify') {
            // Do not trigger heavy global ref warm here; classify must stay foreground-first.
        } else if (mode === 'manual') {
            warmManualRefCache();
        }
        loadQueue({ forceRefresh: false });
    }

    async function warmManualRefCache() {
        try {
            const needsForce = (() => {
                try {
                    return localStorage.getItem('labelerManualRefCacheVersion') !== MANUAL_REF_CACHE_VERSION;
                } catch (e) {
                    return true;
                }
            })();
            const status = await apiPost('/api/labeler/manual_refs/warm', { force: needsForce });
            try {
                localStorage.setItem('labelerManualRefCacheVersion', MANUAL_REF_CACHE_VERSION);
            } catch (e) {
                //ignore storage errors
            }
            if (status && !status.ready) {
                startManualRefPoll();
            }
        } catch (e) {
            console.warn('[Labeler] Manual ref cache warm failed:', e);
        }
    }

    function startManualRefPoll() {
        if (manualRefPollId) return;
        manualRefPollId = setInterval(async () => {
            try {
                const status = await apiGet('/api/labeler/manual_refs/status');
                if (labelerMode === 'manual' && status && status.building) {
                    const total = Number(status.total || allCats.length || 0);
                    const built = Number(status.built || status.cats || 0);
                    const pct = total > 0 ? Math.min(0.95, Math.max(0.05, built / total)) : 0.1;
                    setWarmOverlay(true, 'Preparing manual review cache...', `${built}/${total || '?'} cats ready`, pct);
                }
                if (status && status.ready) {
                    clearInterval(manualRefPollId);
                    manualRefPollId = null;
                    if (labelerMode === 'manual') {
                        setWarmOverlay(false);
                        loadManualCandidates(true);
                    }
                }
            } catch (e) {
                //Ignore transient errors
            }
        }, 2500);
    }

    //---------- API Calls ----------

    function isTransientStatus(status) {
        const code = Number(status || 0);
        return code === 408 || code === 421 || code === 425 || code === 429 || code === 502 || code === 503 || code === 504;
    }

    function waitMs(ms) {
        return new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms || 0))));
    }

    async function fetchJsonWithRetry(endpoint, init = {}, opts = {}) {
        const timeoutMs = Number(opts.timeoutMs || API_POST_TIMEOUT_MS);
        const maxAttempts = Math.max(1, Number(opts.maxAttempts || API_RETRY_MAX_ATTEMPTS));
        let lastError = null;

        for (let attempt = 1; attempt <= maxAttempts; attempt++) {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const resp = await fetch(buildApiUrl(endpoint), {
                    ...init,
                    credentials: 'include',
                    signal: controller.signal,
                });
                if (resp.ok) {
                    return resp.json();
                }
                if (attempt < maxAttempts && isTransientStatus(resp.status)) {
                    const retryAfterRaw = resp.headers.get('Retry-After');
                    const retryAfterSec = Number(retryAfterRaw);
                    const retryAfterMs = Number.isFinite(retryAfterSec) && retryAfterSec > 0
                        ? retryAfterSec * 1000
                        : 0;
                    const jitter = Math.floor(Math.random() * 120);
                    const backoff = retryAfterMs || (API_RETRY_BASE_MS * Math.pow(2, attempt - 1) + jitter);
                    await waitMs(backoff);
                    continue;
                }
                let detail = '';
                try {
                    const txt = await resp.text();
                    detail = String(txt || '').trim();
                } catch (e) {
                    detail = '';
                }
                throw new Error(detail ? `API error: ${resp.status} - ${detail}` : `API error: ${resp.status}`);
            } catch (e) {
                const isAbort = e && e.name === 'AbortError';
                const isNet = String(e && e.message || '').toLowerCase().includes('failed to fetch');
                const retryable = isAbort || isNet;
                lastError = isAbort ? new Error(`API timeout: ${timeoutMs}ms`) : e;
                if (attempt < maxAttempts && retryable) {
                    const jitter = Math.floor(Math.random() * 120);
                    const backoff = API_RETRY_BASE_MS * Math.pow(2, attempt - 1) + jitter;
                    await waitMs(backoff);
                    continue;
                }
                throw lastError;
            } finally {
                clearTimeout(timer);
            }
        }
        throw lastError || new Error('API request failed');
    }

    async function apiGet(endpoint, opts = {}) {
        return fetchJsonWithRetry(endpoint, {}, opts);
    }

    async function apiPost(endpoint, data, opts = {}) {
        const payload = data || {};
        const prefetch = !!payload.prefetch;
        const maxAttempts = prefetch ? 1 : Number(opts.maxAttempts || API_RETRY_MAX_ATTEMPTS);
        try {
            return await fetchJsonWithRetry(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            }, {
                ...opts,
                maxAttempts,
            });
        } catch (e) {
            throw e;
        }
    }

    async function warmCachedImage(serial) {
        if (!serial) return false;
        try {
            const resp = await fetch(buildApiUrl(`/api/labeler/cached_image/${serial}`), {
                credentials: 'include',
            });
            return !!resp.ok;
        } catch (e) {
            return false;
        }
    }

    async function postClassifyIdentify(item, boxStrings, opts = {}) {
        const boxes = Array.isArray(boxStrings) ? boxStrings : [];
        if (!item || !item.serial || !boxes.length) {
            throw new Error('Invalid classify identify request');
        }
        const prefetch = !!opts.prefetch;
        const focusCropIdxNum = Number(opts.focusCropIdx);
        const hasFocusCropIdx = Number.isInteger(focusCropIdxNum) && focusCropIdxNum >= 0;
        const key = prefetch ? '' : getPredCacheKey(item);
        if (key && classifyForegroundInFlight.has(key)) {
            return classifyForegroundInFlight.get(key);
        }
        const payload = {
            serial: item.serial,
            url: item.url || null,
            boxes,
            prefetch,
            rerank: false,
        };
        if (!prefetch && hasFocusCropIdx) {
            payload.focus_crop_idx = focusCropIdxNum;
        }
        const reqPromise = apiPost('/api/labeler/identify', payload, opts);
        if (!key) {
            return reqPromise;
        }
        classifyForegroundInFlight.set(key, reqPromise);
        try {
            return await reqPromise;
        } finally {
            const current = classifyForegroundInFlight.get(key);
            if (current === reqPromise) {
                classifyForegroundInFlight.delete(key);
            }
        }
    }

    async function postUiDiag(eventName, detail) {
        try {
            await apiPost('/api/labeler/ui_diag', {
                event: String(eventName || 'ui_diag'),
                mode: labelerMode,
                serial: currentSerial,
                detail: typeof detail === 'string' ? detail : JSON.stringify(detail || {}),
            }, { maxAttempts: 1, timeoutMs: 4000 });
        } catch (e) {
            // Diagnostics are best-effort.
        }
    }

    function stopClaimHeartbeat() {
        if (claimTimer) {
            clearInterval(claimTimer);
            claimTimer = null;
        }
    }

    function claimCacheKey(mode, serial) {
        const m = String(mode || '').trim();
        const s = String(serial || '').trim();
        if (!m || !s) return '';
        return `${m}:${s}`;
    }

    function prunePrefetchedClaims() {
        if (!prefetchedClaims.size) return;
        const now = Date.now();
        for (const [key, rec] of prefetchedClaims.entries()) {
            const expiresAt = Number(rec?.expiresAt || 0);
            if (!Number.isFinite(expiresAt) || expiresAt <= now) {
                prefetchedClaims.delete(key);
            }
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
        if (!item || !item.serial) return 'error';
        prunePrefetchedClaims();
        const key = claimCacheKey(mode, item.serial);
        if (key && currentClaim && currentClaim.mode === mode && String(currentClaim.serial) === String(item.serial)) {
            return 'granted';
        }
        const prefetched = key ? prefetchedClaims.get(key) : null;
        if (prefetched && !prefetched.inFlight) {
            prefetchedClaims.delete(key);
            currentClaim = { mode, serial: item.serial };
            startClaimHeartbeat();
            return 'granted';
        }
        try {
            const data = await apiPost('/api/labeler/claim', {
                action: 'acquire',
                mode,
                serial: item.serial,
            }, { timeoutMs: 4500, maxAttempts: 2 });
            if (data && data.granted) {
                currentClaim = { mode, serial: item.serial };
                startClaimHeartbeat();
                return 'granted';
            }
            return 'denied';
        } catch (e) {
            return 'error';
        }
    }

    function preclaimItem(item, mode) {
        if (!item || !item.serial || !mode) return;
        prunePrefetchedClaims();
        const key = claimCacheKey(mode, item.serial);
        if (!key) return;
        if (prefetchedClaims.has(key)) return;
        if (currentClaim && currentClaim.mode === mode && String(currentClaim.serial) === String(item.serial)) return;
        // Keep this short-lived: only the immediate next transition should use it.
        prefetchedClaims.set(key, { inFlight: true, expiresAt: Date.now() + 15000 });
        void apiPost('/api/labeler/claim', {
            action: 'acquire',
            mode,
            serial: item.serial,
        }, { timeoutMs: 3000, maxAttempts: 1 })
            .then((data) => {
                if (data && data.granted) {
                    prefetchedClaims.set(key, { inFlight: false, expiresAt: Date.now() + 25000 });
                } else {
                    prefetchedClaims.delete(key);
                }
            })
            .catch(() => {
                prefetchedClaims.delete(key);
            });
    }

    function preclaimAhead(mode, startIdx, count = 3) {
        const q = queueForMode(mode);
        const begin = Math.max(0, Number(startIdx || 0));
        const maxCount = Math.max(1, Number(count || 1));
        for (let i = 1; i <= maxCount; i++) {
            const item = q[begin + i];
            if (!item || !item.serial) break;
            preclaimItem(item, mode);
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

    function queueEndpointForMode(mode) {
        if (mode === 'detect') return '/api/labeler/queue/detect';
        if (mode === 'classify') return '/api/labeler/queue/classify';
        if (mode === 'manual') return '/api/labeler/queue/manual';
        throw new Error(`Unknown queue mode: ${mode}`);
    }

    function applyQueueResponseForMode(mode, data) {
        const payload = data || {};
        if (mode === 'detect') {
            detectQueue = payload.queue || [];
            detectQueueTotal = typeof payload.total === 'number' ? payload.total : detectQueue.length;
            return;
        }
        if (mode === 'classify') {
            classifyQueue = payload.queue || [];
            classifyQueueTotal = typeof payload.total === 'number' ? payload.total : classifyQueue.length;
            return;
        }
        if (mode === 'manual') {
            manualQueue = payload.queue || [];
            manualQueueTotal = typeof payload.total === 'number' ? payload.total : manualQueue.length;
            return;
        }
    }

    async function refreshQueueMode(mode, opts = {}) {
        const force = !!opts.force;
        const now = Date.now();
        const stale = (now - Number(lastQueueRefreshByMode[mode] || 0)) > SESSION_REFRESH_QUEUES_MS;
        if (!force && !stale && Number(lastQueueRefreshByMode[mode] || 0) > 0) {
            return;
        }
        if (queueRefreshPromisesByMode[mode]) {
            await queueRefreshPromisesByMode[mode];
            return;
        }
        const suffix = force ? '?force=1' : '';
        queueRefreshPromisesByMode[mode] = (async () => {
            const data = await apiGet(`${queueEndpointForMode(mode)}${suffix}`);
            applyQueueResponseForMode(mode, data);
            lastQueueRefreshByMode[mode] = Date.now();
            lastQueueRefreshTs = Date.now();
            syncActiveQueuePointersAfterRefresh();
        })();
        try {
            await queueRefreshPromisesByMode[mode];
        } finally {
            queueRefreshPromisesByMode[mode] = null;
        }
    }

    async function refreshNonActiveQueuesInBackground(activeMode, opts = {}) {
        const force = !!opts.force;
        if (queueBackgroundRefreshPromise) return queueBackgroundRefreshPromise;
        const modes = ['detect', 'classify', 'manual'].filter((m) => m !== activeMode);
        queueBackgroundRefreshPromise = (async () => {
            await Promise.allSettled(modes.map((m) => refreshQueueMode(m, { force })));
        })();
        try {
            await queueBackgroundRefreshPromise;
        } finally {
            queueBackgroundRefreshPromise = null;
        }
        return queueBackgroundRefreshPromise;
    }

    function queueForMode(mode) {
        if (mode === 'detect') return detectQueue;
        if (mode === 'classify') return classifyQueue;
        if (mode === 'manual') return manualQueue;
        return [];
    }

    function queueTotalForMode(mode) {
        if (mode === 'detect') return detectQueueTotal;
        if (mode === 'classify') return classifyQueueTotal;
        if (mode === 'manual') return manualQueueTotal;
        return 0;
    }

    function syncActiveQueuePointersAfterRefresh() {
        const activeQueue = queueForMode(labelerMode);
        if (Array.isArray(activeQueue)) {
            queue = activeQueue;
        }
        queueTotal = queueTotalForMode(labelerMode);
        const maxIdx = Math.max(0, queue.length - 1);
        if (queueIndex > maxIdx) {
            queueIndex = maxIdx;
        }
        if (queueIndex < 0) {
            queueIndex = 0;
        }
        if (modePositions[labelerMode] > maxIdx) {
            modePositions[labelerMode] = maxIdx;
        }
        if (modePositions[labelerMode] < 0) {
            modePositions[labelerMode] = 0;
        }
    }

    function removeQueueItemAtIndex(mode, idx) {
        const i = Number(idx);
        if (!Number.isInteger(i) || i < 0) return;
        if (mode === 'detect') {
            if (i >= detectQueue.length) return;
            detectQueue.splice(i, 1);
            detectQueueTotal = Math.max(0, Number(detectQueueTotal || 0) - 1);
            return;
        }
        if (mode === 'classify') {
            if (i >= classifyQueue.length) return;
            classifyQueue.splice(i, 1);
            classifyQueueTotal = Math.max(0, Number(classifyQueueTotal || 0) - 1);
            return;
        }
        if (mode === 'manual') {
            if (i >= manualQueue.length) return;
            manualQueue.splice(i, 1);
            manualQueueTotal = Math.max(0, Number(manualQueueTotal || 0) - 1);
        }
    }

    async function refreshQueues(force = false) {
        const now = Date.now();
        const stale = (now - lastQueueRefreshTs) > SESSION_REFRESH_QUEUES_MS;
        if (!force && !stale && lastQueueRefreshTs > 0) {
            return;
        }
        if (queueRefreshPromise) {
            await queueRefreshPromise;
            return;
        }
        queueRefreshPromise = (async () => {
            const [detectRes, classifyRes, manualRes] = await Promise.allSettled([
                refreshQueueMode('detect', { force }),
                refreshQueueMode('classify', { force }),
                refreshQueueMode('manual', { force }),
            ]);
            let okCount = 0;
            if (detectRes.status === 'fulfilled') {
                okCount += 1;
            } else {
                console.warn('[Labeler] queue/detect refresh failed:', detectRes.reason);
            }
            if (classifyRes.status === 'fulfilled') {
                okCount += 1;
            } else {
                console.warn('[Labeler] queue/classify refresh failed:', classifyRes.reason);
            }
            if (manualRes.status === 'fulfilled') {
                okCount += 1;
            } else {
                console.warn('[Labeler] queue/manual refresh failed:', manualRes.reason);
            }
            if (okCount <= 0) {
                throw new Error('All queue endpoints failed');
            }
            lastQueueRefreshTs = Date.now();
            syncActiveQueuePointersAfterRefresh();
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

    function setWarmOverlay(visible, title = '', subtitle = '', progress = null) {
        if (!warmOverlayEl) return;
        if (!visible) {
            warmOverlayEl.classList.add('hidden');
            return;
        }
        if (warmLabelEl && title) warmLabelEl.textContent = title;
        if (warmSubEl) warmSubEl.textContent = subtitle || '';
        if (warmBarEl) {
            let pct = Number(progress);
            if (!Number.isFinite(pct)) pct = 0;
            pct = Math.max(0, Math.min(1, pct));
            warmBarEl.style.width = `${Math.round(pct * 100)}%`;
            warmBarEl.dataset.pct = String(Math.round(pct * 100));
        }
        warmOverlayEl.classList.remove('hidden');
    }

    function _cancelClassifyItemLoadOverlayWait() {
        classifyItemLoadOverlayActive = false;
        classifyItemLoadOverlayToken += 1;
    }

    function _isClassifyItemLoadOverlayWaitActive() {
        return !!(
            classifyItemLoadOverlayActive
            && classifyItemLoadOverlayToken > 0
            && labelerMode === 'classify'
            && currentItem
        );
    }

    function _predictionRefsFullyLoadedForCrop(results, cropIdx) {
        const depth = _predictionLoadedRefDepthForCrop(results, cropIdx, CLASSIFY_REFS_PER_CAT_TARGET);
        if (depth.targetCount <= 0) return false;
        return depth.candidatesAtDepth >= depth.targetCount;
    }

    function _isCurrentClassifyItemDisplayReady() {
        if (labelerMode !== 'classify') return false;
        if (!currentItem || !currentBoxes.length) return false;
        if (!imageReadyForCurrentItem || !imageElement || !imageElement.complete) return false;
        if (!_predictionHasOptionsForCrop(currentPredictions, currentCropIdx)) return false;
        if (_predictionRefsAtTargetForCrop(currentPredictions, currentCropIdx)) {
            return _predictionRefsFullyLoadedForCrop(currentPredictions, currentCropIdx);
        }
        return _predictionRefsSufficientForCrop(currentPredictions, currentCropIdx);
    }

    function startCurrentClassifyItemLoadOverlayWait() {
        if (labelerMode !== 'classify' || !currentItem || !currentBoxes.length) return;
        const token = ++classifyItemLoadOverlayToken;
        classifyItemLoadOverlayActive = true;
        const requestSerial = currentSerial;
        const requestKey = getPredCacheKey(currentItem);
        const startedAt = Date.now();

        const tick = async () => {
            while (labelerActive && labelerMode === 'classify') {
                if (token !== classifyItemLoadOverlayToken) return;
                if (requestSerial !== currentSerial) return;
                if (requestKey !== getPredCacheKey(currentItem)) return;
                if (_isCurrentClassifyItemDisplayReady()) {
                    if (token === classifyItemLoadOverlayToken) {
                        classifyItemLoadOverlayActive = false;
                        setWarmOverlay(false);
                        if (!initialClassifyWarmDone) {
                            void ensureInitialClassifyWarmGate();
                        }
                    }
                    return;
                }
                const elapsed = Date.now() - startedAt;
                if (elapsed >= CLASSIFY_ITEM_DISPLAY_READY_TIMEOUT_MS) {
                    if (token === classifyItemLoadOverlayToken) {
                        classifyItemLoadOverlayActive = false;
                        setWarmOverlay(false);
                        if (!initialClassifyWarmDone) {
                            void ensureInitialClassifyWarmGate();
                        }
                    }
                    return;
                }

                const hasImage = !!(imageReadyForCurrentItem && imageElement && imageElement.complete);
                const hasPreds = _predictionHasOptionsForCrop(currentPredictions, currentCropIdx);
                if (!hasImage && !hasPreds) {
                    const pct = Math.max(0.03, Math.min(0.55, elapsed / CLASSIFY_ITEM_DISPLAY_READY_TIMEOUT_MS));
                    setWarmOverlay(true, 'Loading classifier item...', 'Loading image and classifier results', pct);
                } else if (!hasImage) {
                    const pct = Math.max(0.2, Math.min(0.7, elapsed / CLASSIFY_ITEM_DISPLAY_READY_TIMEOUT_MS));
                    setWarmOverlay(true, 'Loading classifier item...', 'Waiting for image to render', pct);
                } else if (!hasPreds) {
                    showClassifierWarmOverlay('Loading classifier options...');
                } else {
                    showClassifierWarmOverlay('Loading reference photos...');
                }
                await waitMs(80);
            }
        };
        void tick();
    }

    function isDetectEntryReady(entry) {
        if (!entry || typeof entry !== 'object') return false;
        if (entry.ready === true) return true;
        //Backwards-compat: legacy cache entries only tracked non-empty refined boxes.
        return typeof entry.refined === 'string' && entry.refined.length > 0;
    }

    function isDetectEntryUsable(entry) {
        if (!entry || typeof entry !== 'object') return false;
        if (isDetectEntryReady(entry)) return true;
        // Empty raw string is a valid "0 boxes detected" result and should
        // still count as usable to avoid infinite warm retries.
        return Object.prototype.hasOwnProperty.call(entry, 'raw');
    }

    function showClassifierWarmOverlay(title = 'Preparing classifier cache...') {
        const cov = _predictionLoadedRefCoverageForCrop(currentPredictions, currentCropIdx);
        const hasPredictions = Array.isArray(currentPredictions) && currentPredictions.length > 0;
        const refsPct = cov.targetCount > 0 ? Math.max(0, Math.min(1, cov.coverage)) : 0;
        let targetPct = hasPredictions
            ? (0.12 + refsPct * 0.72)
            : 0.08;
        if (_predictionRefsSufficientForCrop(currentPredictions, currentCropIdx)) {
            targetPct = Math.max(targetPct, 0.95);
        } else {
            targetPct = Math.max(0.03, Math.min(0.92, targetPct));
        }

        const itemKey = getPredCacheKey(currentItem) || String(currentSerial || '');
        if (classifierWarmItemKey !== itemKey) {
            classifierWarmItemKey = itemKey;
            classifierWarmDisplayPct = 0;
        }
        classifierWarmDisplayPct = targetPct;

        const refText = cov.targetCount > 0
            ? `${cov.candidatesWithRefs}/${cov.targetCount}`
            : 'loading';
        const covPctText = cov.targetCount > 0 ? ` (${Math.round(refsPct * 100)}% coverage)` : '';
        const subtitle = hasPredictions
            ? `loaded refs ${refText} cats${covPctText}`
            : 'Running classifier...';
        setWarmOverlay(true, title, subtitle, classifierWarmDisplayPct);
    }

    function startClassifierLoadProgressTicker(title = 'Loading classifier options...') {
        showClassifierWarmOverlay(title);
        const id = setInterval(() => {
            if (!labelerActive || labelerMode !== 'classify') {
                clearInterval(id);
                return;
            }
            showClassifierWarmOverlay(title);
        }, CLASSIFY_LOAD_TICK_MS);
        return () => clearInterval(id);
    }

    function isTransientApiError(err) {
        const msg = String(err && err.message || '');
        if (isNoImageApiError(err)) return true;
        return /\b(421|425|429|502|503|504)\b/.test(msg);
    }

    function getApiErrorStatus(err) {
        const msg = String(err && err.message || '');
        const m = msg.match(/\bAPI error:\s*(\d{3})\b/i);
        if (!m) return 0;
        const code = Number(m[1] || 0);
        return Number.isFinite(code) ? code : 0;
    }

    function isNoImageApiError(err) {
        const msg = String(err && err.message || '');
        if (!/\bAPI error:\s*400\b/i.test(msg)) return false;
        return /No image available/i.test(msg) || /Missing url or serial/i.test(msg);
    }

    function clearClassifyPrefetchFailure(key) {
        const k = String(key || '');
        if (!k) return;
        classifyPrefetchFailures.delete(k);
    }

    function markClassifyPrefetchFailure(key, err) {
        const k = String(key || '');
        if (!k) return;
        const now = Date.now();
        const status = getApiErrorStatus(err);
        const prev = classifyPrefetchFailures.get(k) || { count: 0, ts: 0, cooldown: 0 };
        const count = Math.max(1, Number(prev.count || 0) + 1);
        const boost = status === 429 ? 2 : 1;
        const cooldown = Math.min(
            CLASSIFY_PREFETCH_FAIL_MAX_MS,
            CLASSIFY_PREFETCH_FAIL_BASE_MS * boost * Math.pow(2, Math.min(5, count - 1)),
        );
        classifyPrefetchFailures.set(k, { count, ts: now, cooldown });
    }

    function isClassifyPrefetchBlocked(key) {
        const k = String(key || '');
        if (!k) return false;
        const rec = classifyPrefetchFailures.get(k);
        if (!rec) return false;
        const now = Date.now();
        const ts = Number(rec.ts || 0);
        const cooldown = Number(rec.cooldown || 0);
        if ((now - ts) >= Math.max(cooldown, CLASSIFY_PREFETCH_FAIL_MAX_MS)) {
            classifyPrefetchFailures.delete(k);
            return false;
        }
        return (now - ts) < cooldown;
    }

    function setDetectEntry(key, raw = '', refined = '', ready = false) {
        detectPrefetch.set(String(key), {
            raw: String(raw || ''),
            refined: String(refined || ''),
            ready: !!ready,
        });
        detectWarmFailures.delete(String(key));
    }

    function markDetectWarmFailure(key) {
        const k = String(key || '');
        if (!k) return;
        const cur = detectWarmFailures.get(k) || { count: 0, ts: 0 };
        detectWarmFailures.set(k, { count: Number(cur.count || 0) + 1, ts: Date.now() });
    }

    function isDetectWarmBlocked(key) {
        const rec = detectWarmFailures.get(String(key || ''));
        if (!rec) return false;
        const count = Math.max(1, Number(rec.count || 1));
        const ts = Number(rec.ts || 0);
        const cooldown = Math.min(60000, 4000 * count);
        return (Date.now() - ts) < cooldown;
    }

    function countWarmReadyDetect(windowItems) {
        let ready = 0;
        for (const item of windowItems) {
            const key = String(item?.serial || '');
            if (!key) continue;
            const cached = detectPrefetch.get(key);
            if (isDetectEntryUsable(cached)) ready++;
        }
        return ready;
    }

    function _classifyItemWarmReady(item) {
        const key = getPredCacheKey(item);
        if (!key) return false;
        const rows = predCache.get(key);
        if (!Array.isArray(rows) || !rows.length) return false;
        const idx = _targetCropIdxForItem(item);
        // Count ready only when ref thumbnails are actually decoded/ready,
        // not merely present as URLs in prediction payloads.
        const depth = _predictionLoadedRefDepthForCrop(rows, idx, CLASSIFY_REFS_PER_CAT_TARGET);
        if (depth.targetCount <= 0) return false;
        return depth.candidatesAtDepth >= depth.targetCount;
    }

    function countWarmReadyClassify(windowItems) {
        let ready = 0;
        for (const item of windowItems) {
            if (_classifyItemWarmReady(item)) ready += 1;
        }
        return ready;
    }

    async function ensureInitialDetectWarmGate() {
        if (labelerMode !== 'detect' || initialDetectWarmDone) return;
        const start = Math.max(0, Number(modePositions.detect || 0) + 1);
        const scanWindow = INITIAL_DETECT_WARM_WINDOW;
        const windowItems = detectQueue.slice(start, start + scanWindow);
        if (!windowItems.length) {
            initialDetectWarmDone = true;
            setWarmOverlay(false);
            return;
        }
        const targetReady = Math.min(INITIAL_DETECT_WARM_MIN, windowItems.length);
        const deadline = Date.now() + INITIAL_DETECT_WARM_TIMEOUT_MS;
        let lastDiagTs = 0;
        let lastReady = -1;
        let stagnantLoops = 0;
        while (labelerActive && labelerMode === 'detect') {
            const ready = countWarmReadyDetect(windowItems);
            if (ready === lastReady) {
                stagnantLoops += 1;
            } else {
                stagnantLoops = 0;
                lastReady = ready;
            }
            const blocked = windowItems.filter((item) => isDetectWarmBlocked(String(item?.serial || ''))).length;
            const inflight = windowItems.filter((item) => detectWarmInFlight.has(String(item?.serial || ''))).length;
            const usable = windowItems.filter((item) => isDetectEntryUsable(detectPrefetch.get(String(item?.serial || '')))).length;
            const pending = Math.max(0, windowItems.length - usable - blocked - inflight);
            const now = Date.now();
            if ((now - lastDiagTs) >= UI_DIAG_MIN_INTERVAL_MS || stagnantLoops >= 12) {
                void postUiDiag('detect_warm_gate', {
                    ready,
                    target: targetReady,
                    window: windowItems.length,
                    usable,
                    blocked,
                    inflight,
                    pending,
                    stagnant_loops: stagnantLoops,
                });
                lastDiagTs = now;
            }
            const pct = targetReady > 0 ? (ready / targetReady) : 1;
            setWarmOverlay(
                true,
                'Preparing detector queue...',
                `${ready}/${targetReady} ready • usable ${usable}/${windowItems.length} • in-flight ${inflight} • backoff ${blocked}`,
                pct,
            );
            if (ready >= targetReady) {
                initialDetectWarmDone = true;
                void postUiDiag('detect_warm_gate_done', {
                    ready,
                    target: targetReady,
                    window: windowItems.length,
                });
                setWarmOverlay(false);
                return;
            }
            if (Date.now() >= deadline) {
                void postUiDiag('detect_warm_gate_timeout', {
                    ready,
                    target: targetReady,
                    window: windowItems.length,
                    blocked,
                    inflight,
                    pending,
                });
                setStatus('Proceeding while detector cache continues warming...');
                setWarmOverlay(false);
                return;
            }
            const next = windowItems.find((item) => {
                const key = String(item.serial || '');
                return !isDetectEntryUsable(detectPrefetch.get(key)) && !isDetectWarmBlocked(key);
            });
            if (next) {
                await ensureDetectItemReady(next, true, true);
            } else {
                await runWarmTick();
            }
            await new Promise((resolve) => setTimeout(resolve, 250));
        }
    }

    async function ensureInitialClassifyWarmGate() {
        if (labelerMode !== 'classify' || initialClassifyWarmDone) return;
        const start = Math.max(0, Number(modePositions.classify || 0) + 1);
        const scanWindow = INITIAL_CLASSIFY_WARM_WINDOW;
        const windowItems = classifyQueue.slice(start, start + scanWindow);
        if (!windowItems.length) {
            initialClassifyWarmDone = true;
            setWarmOverlay(false);
            return;
        }
        const targetReady = Math.min(INITIAL_CLASSIFY_WARM_MIN, windowItems.length);
        const deadline = Date.now() + INITIAL_CLASSIFY_WARM_TIMEOUT_MS;
        while (labelerActive && labelerMode === 'classify') {
            const ready = countWarmReadyClassify(windowItems);
            const inflight = windowItems.filter((item) => classifyWarmInFlight.has(getPredCacheKey(item))).length;
            const blocked = windowItems.filter((item) => isClassifyPrefetchBlocked(getPredCacheKey(item))).length;
            const predReady = windowItems.filter((item) => predCache.has(getPredCacheKey(item))).length;
            const pct = targetReady > 0 ? (ready / targetReady) : 1;
            setWarmOverlay(
                true,
                'Preparing classifier queue...',
                `${ready}/${targetReady} ready • preds ${predReady}/${windowItems.length} • in-flight ${inflight} • backoff ${blocked}`,
                pct,
            );
            if (ready >= targetReady) {
                initialClassifyWarmDone = true;
                setWarmOverlay(false);
                return;
            }
            if (Date.now() >= deadline) {
                initialClassifyWarmDone = true;
                setStatus('Proceeding while classifier cache continues warming...');
                setWarmOverlay(false);
                return;
            }
            const next = windowItems.find((item) => !_classifyItemWarmReady(item));
            if (next) {
                await ensureClassifyItemReady(next, true, true);
            } else {
                await runWarmTick();
            }
            await new Promise((resolve) => setTimeout(resolve, 200));
        }
    }

    async function waitForCurrentItemReady(item) {
        if (!item) return;
        if (labelerMode === 'detect') {
            const key = String(item.serial || '');
            if (!key) return;
            const hit = detectPrefetch.get(key);
            if (isDetectEntryReady(hit)) return;
            const deadline = Date.now() + ITEM_READY_WAIT_TIMEOUT_MS;
            while (Date.now() < deadline && labelerActive && labelerMode === 'detect') {
                const elapsed = ITEM_READY_WAIT_TIMEOUT_MS - Math.max(0, deadline - Date.now());
                const pct = Math.min(0.95, Math.max(0.05, elapsed / ITEM_READY_WAIT_TIMEOUT_MS));
                setWarmOverlay(true, 'Preparing next detector image...', 'Running detector in background', pct);
                const ok = await ensureDetectItemReady(item, true, false);
                const cached = detectPrefetch.get(key);
                if (ok || isDetectEntryReady(cached)) {
                    setWarmOverlay(false);
                    return;
                }
                await runWarmTick();
                await new Promise((resolve) => setTimeout(resolve, 220));
            }
            setWarmOverlay(false);
            return;
        }
        if (labelerMode === 'classify') {
            const key = getPredCacheKey(item);
            if (!key) return;
            const deadline = Date.now() + Math.min(15000, ITEM_READY_WAIT_TIMEOUT_MS);
            while (Date.now() < deadline && labelerActive && labelerMode === 'classify') {
                const rows = predCache.get(key);
                if (_classifyItemWarmReady(item)) {
                    return;
                }
                if (Array.isArray(rows) && rows.length) {
                    prefetchRefsFromResults(rows);
                } else if (!classifyWarmInFlight.has(key) && !classifyForegroundInFlight.has(key)) {
                    void ensureClassifyItemReady(item, false, false);
                }
                await waitMs(120);
            }
            return;
        }
    }

    async function loadQueue(opts = {}) {
        const forceRefresh = !!opts.forceRefresh;
        setStatus('Loading queue...');
        try {
            setWarmOverlay(true, 'Loading queue...', `Fetching ${labelerMode} items`, 0.12);
            await refreshQueueMode(labelerMode, { force: forceRefresh });
            applyModeQueue();
            setStatus(`Queue: ${queueTotal} items`);
            if (queue.length > 0) {
                startWarmLoop();
                setWarmOverlay(true, 'Loading first item...', 'Claiming item and starting image load', 0.4);
                await loadCurrentItem();
                if (labelerMode !== 'classify') {
                    setWarmOverlay(false);
                }
                if (labelerMode === 'detect') {
                    // Warm detector queue in background after first item is already loading.
                    void ensureInitialDetectWarmGate();
                }
                void refreshNonActiveQueuesInBackground(labelerMode, { force: false });
            } else {
                await releaseCurrentClaim();
                setStatus('Queue empty - all done!');
                setWarmOverlay(false);
                clearCanvas();
            }
        } catch (e) {
            setStatus(`Error: ${e.message}`);
            setWarmOverlay(false);
            console.error('[Labeler] Queue load error:', e);
        }
    }

    async function loadCurrentItem() {
        if (loadCurrentItemInFlight) {
            loadCurrentItemQueued = true;
            return;
        }
        loadCurrentItemInFlight = true;
        console.log('[Labeler] loadCurrentItem called, queueIndex:', queueIndex, 'queue.length:', queue.length);
        prunePrefetchedClaims();
        let skippedClaims = 0;
        let claimRetryLoops = 0;
        let claimRetryStartedAt = 0;
        try {
            while (queueIndex < queue.length) {
                modePositions[labelerMode] = queueIndex;
                const item = queue[queueIndex];
                if (!item) break;
                const claimResult = await claimQueueItem(item, labelerMode);
                if (claimResult === 'error') {
                    claimRetryLoops += 1;
                    if (!claimRetryStartedAt) claimRetryStartedAt = Date.now();
                    const elapsed = Math.max(0, Date.now() - claimRetryStartedAt);
                    const pulse = ((claimRetryLoops - 1) % 12) / 12;
                    const pct = Math.max(0.08, Math.min(0.92, 0.1 + pulse * 0.8));
                    setWarmOverlay(
                        true,
                        'Waiting for queue lock...',
                        `Claim request retry ${claimRetryLoops} • ${Math.round(elapsed / 1000)}s`,
                        pct,
                    );
                    setStatus('Waiting for queue lock...');
                    await waitMs(220);
                    continue;
                }
                if (claimResult === 'denied') {
                    skippedClaims++;
                    // Remove races/locked items from the visible queue so the
                    // displayed position stays stable for this session.
                    removeQueueItemAtIndex(labelerMode, queueIndex);
                    applyModeQueue();
                    updateInfo();
                    continue;
                }
                if (claimRetryLoops > 0) {
                    setWarmOverlay(true, 'Claim acquired', 'Preparing image and predictions...', 0.18);
                }
                // Never block foreground navigation on warmup readiness.
                void waitForCurrentItemReady(item);
                console.log('[Labeler] Loading item:', item);
                currentItem = item;
                currentSerial = item.serial;
                //Use cached endpoint for fast loading (falls back to Google Drive if not cached)
                currentImageUrl = buildApiUrl(`/api/labeler/cached_image/${item.serial}`);
                console.log('[Labeler] Built cached URL:', currentImageUrl);
                imageReadyForCurrentItem = false;
                imageLoadRetryCount = 0;

                //Parse existing boxes if any (for classifier mode)
                if (item.boxes) {
                    currentBoxes = parseYoloBoxes(item.boxes);
                } else {
                    currentBoxes = [];
                    if (labelerMode === 'detect') {
                        const cachedDet = detectPrefetch.get(String(currentSerial));
                        if (isDetectEntryReady(cachedDet)) {
                            currentBoxes = parseYoloBoxes(cachedDet?.refined || cachedDet?.raw || '');
                        }
                    }
                }

                //Parse existing labels
                if (item.labels) {
                    currentLabels = item.labels.split('|');
                } else {
                    currentLabels = [];
                }
                if (currentLabels.length > currentBoxes.length) {
                    currentLabels = currentLabels.slice(0, currentBoxes.length);
                }
                while (currentLabels.length < currentBoxes.length) {
                    currentLabels.push('');
                }

                selectedBoxIdx = 0;
                currentCropIdx = 0;
                currentPredictions = [];
                manualCandidates = [];
                manualCandidatesKey = '';
                manualReviewIndices = [];
                manualReviewCursor = 0;
                boxesTouched = false;
                const listEl = document.getElementById('predictions-list');
                if (listEl) listEl.innerHTML = '';

                if (labelerMode === 'classify' && currentBoxes.length) {
                    const firstUnlabeled = currentLabels.findIndex(lbl => !lbl || !lbl.trim());
                    if (firstUnlabeled >= 0) currentCropIdx = firstUnlabeled;
                    const cached = predCache.get(getPredCacheKey(currentItem));
                    if (cached) {
                        currentPredictions = cached;
                        prefetchRefsFromResults(currentPredictions);
                        clampPredictionCropIdx(currentPredictions);
                    }
                } else if (labelerMode === 'manual' && currentBoxes.length) {
                    prepareManualReviewState();
                }

                if (
                    pendingUndoRestore
                    && pendingUndoRestore.mode === labelerMode
                    && Number(pendingUndoRestore.serial) === Number(currentSerial)
                ) {
                    if (Array.isArray(pendingUndoRestore.boxes) && labelerMode === 'detect') {
                        currentBoxes = pendingUndoRestore.boxes.map((b) => ({ ...b }));
                        selectedBoxIdx = 0;
                    }
                    if (Array.isArray(pendingUndoRestore.labels) && (labelerMode === 'classify' || labelerMode === 'manual')) {
                        currentLabels = pendingUndoRestore.labels.slice(0, currentBoxes.length);
                        while (currentLabels.length < currentBoxes.length) currentLabels.push('');
                        if (labelerMode === 'manual') {
                            prepareManualReviewState();
                        }
                    }
                    if (Number.isInteger(Number(pendingUndoRestore.cropIdx)) && currentBoxes.length > 0) {
                        currentCropIdx = Math.max(0, Math.min(Number(pendingUndoRestore.cropIdx), currentBoxes.length - 1));
                    }
                    pendingUndoRestore = null;
                }

                if (labelerMode === 'classify' && currentBoxes.length) {
                    startCurrentClassifyItemLoadOverlayWait();
                } else {
                    _cancelClassifyItemLoadOverlayWait();
                }
                updateInfo();
                if (labelerMode === 'classify' && currentBoxes.length) {
                    // Start classifier request immediately; do not wait for image paint.
                    void loadPredictions();
                }
                loadImage(currentImageUrl);
                prefetchPredictions();
                prefetchImages();
                if (labelerMode === 'detect') {
                    prefetchDetection();
                    prefetchDetectionRefine();
                }
                prefetchManualCandidates();
                const nextItem = queue[queueIndex + 1];
                if (nextItem && nextItem.serial) {
                    preclaimItem(nextItem, labelerMode);
                }
                preclaimAhead(labelerMode, queueIndex, 3);
                return;
            }
            await releaseCurrentClaim();
            if (skippedClaims > 0) {
                setStatus('No unlocked items right now. Try again shortly.');
            } else {
                setStatus('Queue complete!');
            }
            _cancelClassifyItemLoadOverlayWait();
            setWarmOverlay(false);
            clearCanvas();
        } finally {
            loadCurrentItemInFlight = false;
            if (loadCurrentItemQueued) {
                loadCurrentItemQueued = false;
                void loadCurrentItem();
            }
        }
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
                prefetchManualCandidates();
            }, 3000);
        }
    }

    function loadFlaggedRefSerials() {
        flaggedRefSerials = new Set();
        try {
            const raw = localStorage.getItem(FLAGGED_REF_SERIALS_STORAGE_KEY);
            if (!raw) return;
            const parsed = JSON.parse(raw);
            if (!Array.isArray(parsed)) return;
            for (const val of parsed) {
                const sn = Number.parseInt(String(val), 10);
                if (Number.isInteger(sn) && sn > 0) flaggedRefSerials.add(sn);
            }
        } catch (e) {
            flaggedRefSerials = new Set();
        }
    }

    function saveFlaggedRefSerials() {
        try {
            const out = Array.from(flaggedRefSerials)
                .filter((sn) => Number.isInteger(sn) && sn > 0)
                .slice(0, 5000);
            localStorage.setItem(FLAGGED_REF_SERIALS_STORAGE_KEY, JSON.stringify(out));
        } catch (e) {
            // Best-effort persistence only.
        }
    }

    function isRefSerialFlagged(serial) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return false;
        return flaggedRefSerials.has(sn);
    }

    function isRefSerialFlagging(serial) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return false;
        if (flaggedRefSerials.has(sn)) return false;
        return flagRequestsInFlight.has(sn);
    }

    function markRefSerialFlagged(serial) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return;
        if (!flaggedRefSerials.has(sn)) {
            flaggedRefSerials.add(sn);
            saveFlaggedRefSerials();
        }
    }

    function unmarkRefSerialFlagged(serial) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return;
        if (flaggedRefSerials.has(sn)) {
            flaggedRefSerials.delete(sn);
            saveFlaggedRefSerials();
        }
    }

    function forEachRefFrameBySerial(serial, visit) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0 || typeof visit !== 'function') return;
        const listEl = document.getElementById('predictions-list');
        if (!listEl) return;
        listEl.querySelectorAll(`.ref-frame[data-ref-serial="${sn}"]`).forEach((el) => visit(el));
    }

    function setRefSerialFlagging(serial, isFlagging) {
        forEachRefFrameBySerial(serial, (el) => {
            el.classList.toggle('ref-flagging', !!isFlagging);
        });
    }

    function resetPredCache() {
        predCacheEpoch += 1;
        predCache.clear();
        classifyForegroundInFlight.clear();
        classifyRefRefreshTs.clear();
        prefetchInFlight.clear();
        prefetchRunning = false;
        prefetchRequested = false;
        imagePrefetch.clear();
        refImagePrefetch.clear();
        refImagePrefetchState.clear();
        refImagePrefetchRetry.clear();
        if (refImageRetryTimers.size) {
            for (const timer of refImageRetryTimers.values()) {
                clearTimeout(timer);
            }
            refImageRetryTimers.clear();
        }
        if (refRenderRefreshTimer) {
            clearTimeout(refRenderRefreshTimer);
            refRenderRefreshTimer = null;
        }
        detectPrefetchEpoch += 1;
        detectPrefetch.clear();
        detectWarmFailures.clear();
        detectPrefetchInFlight.clear();
        detectPrefetchRunning = false;
        detectPrefetchRequested = false;
        detectRefineInFlight.clear();
        manualCandidates = [];
        manualReviewIndices = [];
        manualReviewCursor = 0;
        manualCandidatesKey = '';
        manualCandidateCache.clear();
        manualPrefetchInFlight.clear();
        manualPrefetchRunning = false;
        manualPrefetchRequested = false;
        classifierWarmDisplayPct = 0;
        classifierWarmItemKey = '';
        _cancelClassifyItemLoadOverlayWait();
        pendingUndoRestore = null;
        initialClassifyWarmDone = false;
        prefetchedClaims.clear();
    }

    function getPredCacheKey(item) {
        if (!item) return '';
        return `${item.serial || ''}|${item.boxes || ''}`;
    }

    function _targetCropIdxForItem(item) {
        const boxes = parseYoloBoxes(String(item?.boxes || ''));
        if (!boxes.length) return 0;
        const labels = String(item?.labels || '').split('|');
        const firstUnlabeled = labels.findIndex((lbl) => !String(lbl || '').trim());
        return firstUnlabeled >= 0 ? firstUnlabeled : 0;
    }

    function _fmtManualBoxNum(value) {
        const num = Number(value);
        if (!Number.isFinite(num)) return '0.000000';
        return num.toFixed(6);
    }

    function _manualBoxSig(box) {
        if (!box) return '';
        return [
            _fmtManualBoxNum(box.cx),
            _fmtManualBoxNum(box.cy),
            _fmtManualBoxNum(box.w),
            _fmtManualBoxNum(box.h),
        ].join(' ');
    }

    function _manualCandidateKey(serial, url, boxSig) {
        const serialPart = serial == null ? '' : String(serial).trim();
        const urlPart = String(url || '').trim();
        const boxPart = String(boxSig || '').trim();
        if (!boxPart) return '';
        return `${serialPart}|${urlPart}|${boxPart}`;
    }

    function _manualReviewIndicesForItem(item, boxes) {
        if (!Array.isArray(boxes) || !boxes.length) return [];
        const out = [];
        const seen = new Set();
        const declared = Array.isArray(item?.review_indices) ? item.review_indices : [];
        declared.forEach((raw) => {
            const idx = parseInt(raw, 10);
            if (!Number.isInteger(idx) || idx < 0 || idx >= boxes.length || seen.has(idx)) return;
            seen.add(idx);
            out.push(idx);
        });
        if (out.length) return out;

        const labels = String(item?.labels || '').split('|');
        for (let i = 0; i < boxes.length; i++) {
            if (_isNeedsReview(labels[i])) out.push(i);
        }
        return out;
    }

    function _manualRequestForItem(item) {
        if (!item || !item.boxes) return null;
        const boxes = parseYoloBoxes(item.boxes || '');
        if (!boxes.length) return null;
        const reviewIdx = _manualReviewIndicesForItem(item, boxes)[0];
        if (!Number.isInteger(reviewIdx) || reviewIdx < 0 || reviewIdx >= boxes.length) return null;
        const boxSig = _manualBoxSig(boxes[reviewIdx]);
        const key = _manualCandidateKey(item.serial, item.url, boxSig);
        if (!key) return null;
        return {
            key,
            serial: item.serial,
            url: item.url || null,
            box: boxSig,
        };
    }

    function _setManualCandidateCache(key, payload) {
        if (!key || !payload) return;
        manualCandidateCache.delete(key);
        manualCandidateCache.set(key, payload);
        while (manualCandidateCache.size > MANUAL_CANDIDATE_CACHE_MAX) {
            const oldest = manualCandidateCache.keys().next().value;
            if (!oldest) break;
            manualCandidateCache.delete(oldest);
        }
    }

    function prefetchManualCandidates() {
        if (labelerMode !== 'manual') return;
        if (manualPrefetchRunning) {
            manualPrefetchRequested = true;
            return;
        }
        const start = Math.max(0, queueIndex + 1);
        const end = Math.min(queue.length, start + MANUAL_PREFETCH_AHEAD);
        const targets = [];
        for (let i = start; i < end; i++) {
            const req = _manualRequestForItem(queue[i]);
            if (!req) continue;
            if (manualCandidateCache.has(req.key)) continue;
            if (manualPrefetchInFlight.has(req.key)) continue;
            targets.push(req);
        }
        if (!targets.length) return;

        manualPrefetchRunning = true;
        let idx = 0;
        let active = 0;

        const runNext = async () => {
            if (idx >= targets.length) {
                if (active === 0) {
                    manualPrefetchRunning = false;
                    if (manualPrefetchRequested) {
                        manualPrefetchRequested = false;
                        prefetchManualCandidates();
                    }
                }
                return;
            }
            const target = targets[idx++];
            active += 1;
            manualPrefetchInFlight.add(target.key);
            try {
                const payload = await apiPost('/api/labeler/manual/candidates', {
                    serial: target.serial,
                    url: target.url,
                    box: target.box,
                }, { maxAttempts: 2, timeoutMs: 60000 });
                if (payload && payload.ready !== false) {
                    _setManualCandidateCache(target.key, {
                        candidates: Array.isArray(payload.candidates) ? payload.candidates : [],
                    });
                }
            } catch (e) {
                //Manual prefetch is opportunistic.
            } finally {
                manualPrefetchInFlight.delete(target.key);
                active -= 1;
                runNext();
                if (idx >= targets.length && active === 0) {
                    manualPrefetchRunning = false;
                    if (manualPrefetchRequested) {
                        manualPrefetchRequested = false;
                        prefetchManualCandidates();
                    }
                }
            }
        };

        const workers = Math.min(MANUAL_PREFETCH_CONCURRENCY, targets.length);
        for (let i = 0; i < workers; i++) {
            runNext();
        }
    }

    function parseYoloBoxes(boxStr) {
        //Format: "cx cy w h|cx cy w h|..."
        const raw = String(boxStr || '').trim();
        if (!raw) return [];
        return raw.split('|').map(b => {
            const norm = String(b || '').replace(/[,;]+/g, ' ').trim();
            const [cx, cy, w, h] = norm.split(/\s+/).map(parseFloat);
            return { cx, cy, w, h };
        }).filter((b) => (
            Number.isFinite(b.cx)
            && Number.isFinite(b.cy)
            && Number.isFinite(b.w)
            && Number.isFinite(b.h)
            && b.w > 0
            && b.h > 0
        ));
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

    function _extractRefSrc(ref) {
        const info = typeof ref === 'string'
            ? { img: ref, serial: null, crop: null }
            : (ref || {});
        const refImg = String(info.img || info.thumb || '').trim();
        const refUrl = String(info.url || info.src || '').trim();
        if (refImg) {
            return refImg.startsWith('data:image')
                ? refImg
                : `data:image/jpeg;base64,${refImg}`;
        }
        if (refUrl) {
            return refUrl.startsWith('/') ? buildApiUrl(refUrl) : refUrl;
        }
        return '';
    }

    function _clearRefRetryTimer(src) {
        const key = String(src || '').trim();
        if (!key) return;
        const timer = refImageRetryTimers.get(key);
        if (timer) {
            clearTimeout(timer);
            refImageRetryTimers.delete(key);
        }
    }

    function _recordRefImageError(src) {
        const key = String(src || '').trim();
        const prev = refImagePrefetchRetry.get(key) || { attempts: 0 };
        const attempts = Math.max(1, Number(prev.attempts || 0) + 1);
        const exp = Math.min(REF_IMAGE_RETRY_MAX_ATTEMPTS - 1, Math.max(0, attempts - 1));
        const delayMs = Math.min(
            REF_IMAGE_RETRY_MAX_MS,
            REF_IMAGE_RETRY_BASE_MS * Math.pow(2, exp),
        );
        const rec = {
            attempts,
            nextTs: Date.now() + delayMs,
            delayMs,
        };
        refImagePrefetchRetry.set(key, rec);
        return rec;
    }

    function _scheduleRefImageRetry(src, delayMs) {
        const key = String(src || '').trim();
        if (!key) return;
        if (refImageRetryTimers.has(key)) return;
        const delay = Math.max(250, Number(delayMs) || 250);
        const timer = setTimeout(() => {
            refImageRetryTimers.delete(key);
            if (!labelerActive) return;
            prefetchRefImageSrc(key);
            scheduleRefRenderRefresh();
        }, delay);
        refImageRetryTimers.set(key, timer);
    }

    function prefetchRefImageSrc(src) {
        const key = String(src || '').trim();
        if (!key) return;
        const now = Date.now();
        const state = String(refImagePrefetchState.get(key) || '');
        if (state === 'ready') return;
        if (state === 'loading' && refImagePrefetch.has(key)) return;
        if (state === 'error') {
            const retry = refImagePrefetchRetry.get(key) || {};
            const nextTs = Number(retry.nextTs || 0);
            if (nextTs > now) return;
            refImagePrefetch.delete(key);
            refImagePrefetchState.delete(key);
            _clearRefRetryTimer(key);
        } else if (refImagePrefetch.has(key)) {
            return;
        }
        refImagePrefetchState.set(key, 'loading');
        const img = new Image();
        img.decoding = 'async';
        img.loading = 'eager';
        img.addEventListener('load', () => {
            refImagePrefetchState.set(key, 'ready');
            refImagePrefetchRetry.delete(key);
            _clearRefRetryTimer(key);
            scheduleRefRenderRefresh();
        }, { once: true });
        img.addEventListener('error', () => {
            refImagePrefetchState.set(key, 'error');
            refImagePrefetch.delete(key);
            const retry = _recordRefImageError(key);
            _scheduleRefImageRetry(key, Number(retry.delayMs || REF_IMAGE_RETRY_BASE_MS));
            scheduleRefRenderRefresh();
        }, { once: true });
        refImagePrefetch.set(key, img);
        img.src = key;
        if (img.complete) {
            if (img.naturalWidth > 0) {
                refImagePrefetchState.set(key, 'ready');
                refImagePrefetchRetry.delete(key);
                _clearRefRetryTimer(key);
            } else {
                refImagePrefetchState.set(key, 'error');
                refImagePrefetch.delete(key);
                const retry = _recordRefImageError(key);
                _scheduleRefImageRetry(key, Number(retry.delayMs || REF_IMAGE_RETRY_BASE_MS));
            }
        }
        if (refImagePrefetch.size > REF_IMAGE_PREFETCH_MAX) {
            const overflow = refImagePrefetch.size - REF_IMAGE_PREFETCH_MAX;
            const keys = refImagePrefetch.keys();
            for (let i = 0; i < overflow; i++) {
                const oldKey = keys.next().value;
                if (!oldKey) break;
                refImagePrefetch.delete(oldKey);
                refImagePrefetchState.delete(oldKey);
                refImagePrefetchRetry.delete(oldKey);
                _clearRefRetryTimer(oldKey);
            }
        }
    }

    function isRefImageReady(src) {
        const key = String(src || '').trim();
        if (!key) return false;
        const state = String(refImagePrefetchState.get(key) || '');
        if (state === 'ready') return true;
        if (state === 'error') return false;
        const img = refImagePrefetch.get(key);
        return !!(img && img.complete && img.naturalWidth > 0);
    }

    function scheduleRefRenderRefresh() {
        if (refRenderRefreshTimer) return;
        refRenderRefreshTimer = setTimeout(() => {
            refRenderRefreshTimer = null;
            if (!labelerActive) return;
            if (labelerMode === 'classify') {
                renderPredictions();
            } else if (labelerMode === 'manual') {
                renderManualCandidates();
            }
        }, 80);
    }

    function prefetchRefsFromResults(results) {
        const rows = Array.isArray(results) ? results : [];
        for (const crop of rows) {
            const cands = Array.isArray(crop?.candidates) ? crop.candidates : [];
            for (const cand of cands.slice(0, 9)) {
                const refs = Array.isArray(cand?.refs) ? cand.refs : [];
                for (const ref of refs.slice(0, CLASSIFY_REFS_PER_CAT_TARGET)) {
                    const src = _extractRefSrc(ref);
                    if (src) prefetchRefImageSrc(src);
                }
            }
        }
    }

    function warmWindowForMode(mode) {
        const q = queueForMode(mode);
        const start = Math.max(0, Number(modePositions[mode] || 0));
        const target = (
            mode === 'detect' && !initialDetectWarmDone
                ? Math.max(SESSION_WARM_TARGET, INITIAL_DETECT_WARM_WINDOW)
                : SESSION_WARM_TARGET
        );
        return q.slice(start, start + target);
    }

    async function _waitFor(predicate, timeoutMs = 30000, stepMs = 120) {
        const end = Date.now() + timeoutMs;
        while (Date.now() < end) {
            if (predicate()) return true;
            await new Promise((resolve) => setTimeout(resolve, stepMs));
        }
        return false;
    }

    async function ensureDetectItemReady(item, waitForInFlight = false, usePrefetch = true) {
        if (!item || !item.serial) return false;
        const key = String(item.serial);
        const cached = detectPrefetch.get(key);
        const cachedReady = isDetectEntryReady(cached);
        if (cachedReady) return true;
        if (usePrefetch && isDetectEntryUsable(cached)) return true;
        if (usePrefetch && isDetectWarmBlocked(key)) return false;

        if (detectWarmInFlight.has(key)) {
            return waitForInFlight
                ? _waitFor(() => {
                    const hit = detectPrefetch.get(key);
                    return usePrefetch ? isDetectEntryUsable(hit) : isDetectEntryReady(hit);
                })
                : false;
        }

        detectWarmInFlight.add(key);
        try {
            prefetchImageSerial(item.serial);
            const fastDetect = !!usePrefetch;
            const hasRaw = !!(cached && Object.prototype.hasOwnProperty.call(cached, 'raw'));
            let raw = (cached && typeof cached.raw === 'string') ? cached.raw : '';
            let samRefined = false;
            if (!hasRaw || (!raw && !cachedReady) || (!fastDetect && !cachedReady)) {
                const det = await apiPost('/api/labeler/detect', {
                    serial: item.serial,
                    url: item.url || null,
                    // Foreground path keeps full YOLO+SAM; prefetch stays lightweight.
                    fast: fastDetect,
                    prefetch: !!usePrefetch,
                }, { timeoutMs: usePrefetch ? API_PREFETCH_TIMEOUT_MS : API_POST_TIMEOUT_MS });
                raw = det.boxes_yolo || '';
                samRefined = !!det.sam_refined;
            }

            const ready = fastDetect ? false : true;
            setDetectEntry(key, raw, raw, ready);
            if (!fastDetect && !samRefined && raw) {
                prefetchDetectionRefine();
            }
            return true;
        } catch (e) {
            markDetectWarmFailure(key);
            void postUiDiag('detect_warm_item_error', {
                serial: item.serial,
                message: String(e && e.message || e || ''),
                use_prefetch: !!usePrefetch,
            });
            return false;
        } finally {
            detectWarmInFlight.delete(key);
        }
    }

    async function ensureClassifyItemReady(item, waitForInFlight = false, usePrefetch = true) {
        if (!item || !item.boxes) return false;
        const key = getPredCacheKey(item);
        if (!key) return false;
        if (predCache.has(key)) return true;
        if (usePrefetch && isClassifyPrefetchBlocked(key)) return false;
        if (classifyWarmInFlight.has(key)) {
            return waitForInFlight ? _waitFor(() => predCache.has(key)) : false;
        }

        classifyWarmInFlight.add(key);
        try {
            prefetchImageSerial(item.serial);
            const parsed = parseYoloBoxes(item.boxes || '');
            if (!parsed.length) return false;
            let data = null;
            try {
                data = await postClassifyIdentify(
                    item,
                    parsed.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                    {
                        prefetch: !!usePrefetch,
                        timeoutMs: usePrefetch ? API_PREFETCH_TIMEOUT_MS : API_POST_TIMEOUT_MS,
                    },
                );
            } catch (e) {
                if (!(isNoImageApiError(e) && !usePrefetch)) throw e;
                let recovered = false;
                for (let retry = 1; retry <= 3; retry++) {
                    await warmCachedImage(item.serial);
                    await waitMs(140 * retry);
                    try {
                        data = await postClassifyIdentify(
                            item,
                            parsed.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                            {
                                prefetch: false,
                                timeoutMs: API_POST_TIMEOUT_MS,
                                maxAttempts: 2,
                            },
                        );
                        recovered = true;
                        break;
                    } catch (e2) {
                        if (!isNoImageApiError(e2)) throw e2;
                    }
                }
                if (!recovered || !data) return false;
            }
            prefetchRefsFromResults(data?.results || []);
            predCache.set(key, data.results || []);
            clearClassifyPrefetchFailure(key);
            return true;
        } catch (e) {
            if (isNoImageApiError(e)) {
                // Source/cache may still be warming; do not impose long prefetch penalties.
                return false;
            }
            if (usePrefetch && isTransientApiError(e)) {
                classifyPrefetchBackoffUntil = Date.now() + 2500;
            }
            if (usePrefetch) {
                markClassifyPrefetchFailure(key, e);
            }
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
            const detectTargets = labelerMode === 'detect' ? warmWindowForMode('detect') : [];
            detectTargets.forEach((item) => prefetchImageSerial(item.serial));
            const classifyTargets = labelerMode === 'classify' ? warmWindowForMode('classify') : [];
            classifyTargets.forEach((item) => prefetchImageSerial(item.serial));
            const manualTargets = labelerMode === 'manual' ? warmWindowForMode('manual') : [];
            manualTargets.forEach((item) => prefetchImageSerial(item.serial));

            const detectTodo = detectTargets.filter((item) => {
                const key = String(item.serial || '');
                const cached = detectPrefetch.get(key);
                return (
                    !!item.serial &&
                    !isDetectEntryUsable(cached) &&
                    !detectWarmInFlight.has(key) &&
                    !isDetectWarmBlocked(key)
                );
            }).slice(0, DETECT_PREFETCH_CONCURRENCY);
            const classifyTodo = classifyTargets.filter((item) => {
                const key = getPredCacheKey(item);
                return (
                    !!key
                    && !predCache.has(key)
                    && !classifyWarmInFlight.has(key)
                    && !isClassifyPrefetchBlocked(key)
                );
            }).slice(0, PREFETCH_CONCURRENCY);

            const tasks = [];
            detectTodo.forEach((item) => tasks.push(ensureDetectItemReady(item, false)));
            classifyTodo.forEach((item) => tasks.push(ensureClassifyItemReady(item, false, true)));
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
        classifierWarmDisplayPct = 0;
        classifierWarmItemKey = '';
        loadPredictionsBusy = false;
        loadPredictionsQueuedForce = false;
        if (manualRefPollId) {
            clearInterval(manualRefPollId);
            manualRefPollId = null;
        }
        resetPredCache();
        incorrectFlagMode = false;
        flagRequestsInFlight.clear();
        containerEl?.classList?.remove('labeler-flag-mode');
        updateFlagButtonState();
        detectWarmInFlight.clear();
        classifyWarmInFlight.clear();
        classifyForegroundInFlight.clear();
        classifyRefRefreshTs.clear();
        classifyPrefetchFailures.clear();
        detectQueue = [];
        classifyQueue = [];
        manualQueue = [];
        detectQueueTotal = 0;
        classifyQueueTotal = 0;
        manualQueueTotal = 0;
        queue = [];
        queueTotal = 0;
        initialDetectWarmDone = false;
        initialClassifyWarmDone = false;
        setWarmOverlay(false);
    }

    function loadImage(url, opts = {}) {
        console.log('[Labeler] loadImage called with:', url);
        const token = ++imageLoadToken;
        activeImageLoadToken = token;
        imageReadyForCurrentItem = false;
        if (imageElement) imageElement.dataset.loadToken = String(token);
        setStatus('Loading image...');
        if (!url) {
            clearCanvas();
            return;
        }
        const noCrossOrigin = !!opts.noCrossOrigin;
        try {
            if (noCrossOrigin) {
                imageElement.removeAttribute('crossorigin');
            } else {
                const targetOrigin = new URL(url, window.location.href).origin;
                if (targetOrigin === window.location.origin) {
                    imageElement.removeAttribute('crossorigin');
                } else {
                    imageElement.crossOrigin = 'anonymous';
                }
            }
        } catch (e) {
            imageElement.removeAttribute('crossorigin');
        }
        imageElement.src = url;
    }

    function onImageLoad() {
        const token = Number(imageElement?.dataset?.loadToken || 0);
        if (!token || token !== activeImageLoadToken) {
            return;
        }
        imageReadyForCurrentItem = true;
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
            if (isDetectEntryReady(cached)) {
                currentBoxes = parseYoloBoxes(cached?.refined || cached?.raw || '');
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
            } else if (labelerMode === 'manual') {
                loadManualCandidates();
            }
        }
    }

    function onImageError(e) {
        const token = Number(imageElement?.dataset?.loadToken || 0);
        if (!token || token !== activeImageLoadToken) {
            return;
        }
        imageReadyForCurrentItem = false;
        console.error('[Labeler] Image load failed:', currentImageUrl, e);
        const serial = currentSerial;
        const itemUrl = String(currentItem?.url || '').trim();
        const retryN = Number(imageLoadRetryCount || 0);

        if (retryN === 0 && serial) {
            imageLoadRetryCount = 1;
            //Do not block on cache warm if source URL is already available.
            void warmCachedImage(serial);
            if (itemUrl.startsWith('http')) {
                setStatus('Cached image failed - loading source URL...');
                const sep = itemUrl.includes('?') ? '&' : '?';
                loadImage(`${itemUrl}${sep}tc_retry=${Date.now()}`, { noCrossOrigin: true });
                return;
            }
            setStatus('Image load failed - retrying cache...');
            loadImage(buildApiUrl(`/api/labeler/cached_image/${serial}?retry=${Date.now()}`));
            return;
        }

        if (retryN === 1 && itemUrl.startsWith('http')) {
            imageLoadRetryCount = 2;
            setStatus('Retrying source URL...');
            const sep = itemUrl.includes('?') ? '&' : '?';
            loadImage(`${itemUrl}${sep}tc_retry2=${Date.now()}`, { noCrossOrigin: true });
            return;
        }

        setStatus('Image load failed after retries - skipping...');
        advanceQueue();
    }

    async function runDetection() {
        setStatus('Running detector...');
        try {
            //Send serial so backend reads from cache; no URL needed
            let data = null;
            try {
                data = await apiPost('/api/labeler/detect', {
                    serial: currentSerial,
                    url: currentItem?.url || null,
                    // Full detector path: YOLO + SAM refine.
                    fast: false,
                });
            } catch (e) {
                if (!isNoImageApiError(e)) throw e;
                let lastErr = e;
                for (let retry = 1; retry <= 4; retry++) {
                    await warmCachedImage(currentSerial);
                    await waitMs(180 * retry);
                    try {
                        data = await apiPost('/api/labeler/detect', {
                            serial: currentSerial,
                            url: currentItem?.url || null,
                            fast: false,
                        }, { maxAttempts: 2 });
                        lastErr = null;
                        break;
                    } catch (e2) {
                        lastErr = e2;
                        if (!isNoImageApiError(e2)) throw e2;
                    }
                }
                if (lastErr) throw lastErr;
            }
            currentBoxes = parseYoloBoxes(data.boxes_yolo || '');
            selectedBoxIdx = 0;
            const raw = data.boxes_yolo || '';
            setDetectEntry(String(currentSerial), raw, raw, true);
            drawCanvas();
            setStatus(`Found ${currentBoxes.length} box(es)`);
            updateInfo();
            if (data && data.sam_refined === false && currentBoxes.length) {
                // Rare fallback: inline SAM timed out; continue refining in background.
                void autoRefineCurrent(true);
            }
        } catch (e) {
            if (isNoImageApiError(e)) {
                setStatus('Detector image unavailable - skipping...');
                advanceQueue();
                return;
            }
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
            setDetectEntry(String(currentSerial), data.boxes_yolo || '', data.boxes_yolo || '', true);
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
                setDetectEntry(String(serial), refined, refined, true);
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
        if (!imageReadyForCurrentItem || !imageElement || !imageElement.complete) {
            clearCanvas();
            return;
        }
        if (labelerMode === 'classify' || labelerMode === 'manual') {
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
            if ((labelerMode === 'classify' || labelerMode === 'manual') && currentBoxes.length) {
                const idx = Math.max(0, Math.min(Number(currentCropIdx || 0), currentBoxes.length - 1));
                cropEl.textContent = `${idx + 1} / ${currentBoxes.length}`;
            } else {
                cropEl.textContent = '-';
            }
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

    function updateFlagButtonState() {
        if (!flagBtnEl) return;
        flagBtnEl.classList.toggle('active', !!incorrectFlagMode);
        flagBtnEl.setAttribute('aria-pressed', incorrectFlagMode ? 'true' : 'false');
        flagBtnEl.title = incorrectFlagMode
            ? 'Flag mode ON: click a reference photo to clear labels for that serial'
            : 'Flag incorrect label';
        if (flagModeHintEl) {
            flagModeHintEl.classList.toggle('hidden', !incorrectFlagMode);
        }
    }

    function setIncorrectFlagMode(enabled) {
        incorrectFlagMode = !!enabled;
        if (containerEl) {
            containerEl.classList.toggle('labeler-flag-mode', incorrectFlagMode);
        }
        updateFlagButtonState();
        if (incorrectFlagMode) {
            setStatus('Flag mode ON: click reference photos to clear labels for those serials.');
        }
    }

    function toggleIncorrectFlagMode() {
        if (labelerMode !== 'classify' && labelerMode !== 'manual') return;
        setIncorrectFlagMode(!incorrectFlagMode);
        if (!incorrectFlagMode && currentSerial != null) {
            setStatus(`Ready - sn${currentSerial}`);
        }
    }

    async function flagReferenceFromFrame(frameEl) {
        if (!frameEl) return;
        const serialRaw = String(frameEl.getAttribute('data-ref-serial') || '').trim();
        const serial = Number.parseInt(serialRaw, 10);
        if (!Number.isInteger(serial) || serial <= 0) {
            setStatus('Reference cannot be flagged (no serial metadata).');
            return;
        }
        if (flagRequestsInFlight.has(serial)) {
            setStatus(`Already flagging sn${serial}...`);
            return;
        }
        const cropRaw = String(frameEl.getAttribute('data-ref-crop') || '').trim();
        const cropNum = Number.parseInt(cropRaw, 10);
        const sourceCrop = Number.isInteger(cropNum) && cropNum > 0 ? cropNum : null;

        flagRequestsInFlight.add(serial);
        setRefSerialFlagging(serial, true);
        setStatus(`Flagging sn${serial} for relabeling...`);
        try {
            const payload = await apiPost('/api/labeler/flag_incorrect', {
                serial,
                source_mode: labelerMode,
                source_serial: currentSerial,
                source_crop: sourceCrop,
            }, { maxAttempts: 4, timeoutMs: 30000 });
            forEachRefFrameBySerial(serial, (el) => {
                el.classList.remove('ref-flagging');
                el.classList.add('ref-flagged');
            });
            markRefSerialFlagged(serial);
            if (payload && payload.changed === false) {
                setStatus(incorrectFlagMode
                    ? `sn${serial} is already unlabeled. Flag mode still ON.`
                    : `sn${serial} is already unlabeled.`);
            } else {
                setStatus(incorrectFlagMode
                    ? `Flagged sn${serial} for relabeling (sheet updated). Flag mode still ON.`
                    : `Flagged sn${serial} for relabeling (sheet updated).`);
            }
            try {
                await refreshQueues(true);
                applyModeQueue();
                updateInfo();
            } catch (e) {
                // Queue refresh is best-effort after flagging.
            }
        } catch (e) {
            setStatus(`Flag failed: ${e.message}`);
        } finally {
            flagRequestsInFlight.delete(serial);
            setRefSerialFlagging(serial, false);
        }
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

    function queuePendingUpdate(update) {
        if (!update || !update.serial) return;
        const serial = String(update.serial);
        const idx = pendingUpdates.findIndex((u) => String(u?.serial || '') === serial);
        if (idx < 0) {
            pendingUpdates.push({ ...update });
            return;
        }
        pendingUpdates[idx] = { ...pendingUpdates[idx], ...update };
    }

    function findQueueIndexBySerial(serial) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return -1;
        for (let i = 0; i < queue.length; i++) {
            if (Number(queue[i]?.serial) === sn) return i;
        }
        return -1;
    }

    function pushCropLabelUndo(nextLabel) {
        if (labelerMode !== 'classify' && labelerMode !== 'manual') return;
        if (currentSerial == null || !currentBoxes.length) return;
        const cropIdx = Math.max(0, Math.min(Number(currentCropIdx || 0), currentBoxes.length - 1));
        const prevLabel = String(currentLabels[cropIdx] || '');
        const next = String(nextLabel || '');
        if (prevLabel === next) return;
        history.push({
            type: 'crop_label',
            mode: labelerMode,
            serial: Number(currentSerial),
            queueIndex: Number(queueIndex),
            cropIdx,
            labels: [...currentLabels],
            prevLabel,
            nextLabel: next,
        });
    }

    //---------- Actions ----------

    function saveAndAdvance() {
        if (labelerMode === 'detect') {
            //Save boxes
            if (currentBoxes.length === 0) {
                rejectImage();
                return;
            }

            queuePendingUpdate({
                serial: currentSerial,
                box_coords: formatYoloBoxes(currentBoxes),
            });

            history.push({
                type: 'detect',
                mode: labelerMode,
                serial: currentSerial,
                queueIndex: Number(queueIndex),
                boxes: currentBoxes.map((b) => ({ ...b })),
            });
        } else {
            //Classifier mode - save labels
            queuePendingUpdate({
                serial: currentSerial,
                box_cat_ids: currentLabels.join('|'),
            });
            // Once the user starts grading, do not keep warm-gate overlay behavior active.
            initialClassifyWarmDone = true;
            setWarmOverlay(false);

            history.push({
                type: 'classify',
                mode: labelerMode,
                serial: currentSerial,
                queueIndex: Number(queueIndex),
                cropIdx: currentBoxes.length
                    ? Math.max(0, Math.min(Number(currentCropIdx || 0), currentBoxes.length - 1))
                    : 0,
                labels: [...currentLabels],
            });

            if (labelerMode === 'classify') {
                primeHotNextClassifyItem();
            }
        }

        updateInfo();
        advanceQueue();
    }

    function rejectImage() {
        queuePendingUpdate({
            serial: currentSerial,
            box_coords: 'Rejected',
        });

        history.push({
            type: 'reject',
            mode: labelerMode,
            serial: currentSerial,
            queueIndex: Number(queueIndex),
        });
        updateInfo();
        advanceQueue();
    }

    function advanceQueue() {
        void releaseCurrentClaim();
        queueIndex++;
        modePositions[labelerMode] = queueIndex;
        if (queueIndex < queue.length) {
            const nextItem = queue[queueIndex];
            if (nextItem && nextItem.serial) {
                prefetchImageSerial(nextItem.serial);
                preclaimItem(nextItem, labelerMode);
                preclaimAhead(labelerMode, queueIndex, 3);
                if (labelerMode === 'classify') {
                    void ensureClassifyItemReady(nextItem, false, true);
                }
            }
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
        if (last && last.type === 'crop_label') {
            const sameMode = String(last.mode || '') === String(labelerMode || '');
            const sameSerial = Number(last.serial) === Number(currentSerial);
            const sameQueuePos = Number(last.queueIndex) === Number(queueIndex);
            if (sameMode && sameSerial && sameQueuePos) {
                const snap = Array.isArray(last.labels) ? last.labels.slice() : [];
                currentLabels = snap.slice(0, currentBoxes.length);
                while (currentLabels.length < currentBoxes.length) currentLabels.push('');
                if (labelerMode === 'manual') {
                    prepareManualReviewState();
                    const idx = Math.max(0, Math.min(Number(last.cropIdx || 0), Math.max(0, currentBoxes.length - 1)));
                    currentCropIdx = idx;
                    const cursor = manualReviewIndices.findIndex((n) => Number(n) === idx);
                    if (cursor >= 0) manualReviewCursor = cursor;
                    loadManualCandidates();
                } else {
                    currentCropIdx = Math.max(0, Math.min(Number(last.cropIdx || 0), Math.max(0, currentBoxes.length - 1)));
                    loadPredictions();
                }
                drawCanvas();
                updateInfo();
                setStatus(`Undid crop ${Number(last.cropIdx || 0) + 1}`);
                return;
            }
            // Fall back to navigation restore if the user already advanced.
            pendingUndoRestore = {
                mode: String(last.mode || labelerMode || ''),
                serial: Number(last.serial || 0),
                queueIndex: Number(last.queueIndex || 0),
                cropIdx: Number(last.cropIdx || 0),
                labels: Array.isArray(last.labels) ? last.labels.slice() : [],
            };
        }

        if (last && last.mode && String(last.mode) !== String(labelerMode || '')) {
            // Keep current behavior simple/safe across modes.
            history.push(last);
            pendingUndoRestore = null;
            setStatus(`Switch to ${last.mode} mode to undo that action`);
            return;
        }

        //Remove from pending
        const idx = pendingUpdates.findIndex(u => String(u?.serial || '') === String(last?.serial || ''));
        if (idx >= 0) pendingUpdates.splice(idx, 1);

        if (!pendingUndoRestore && last) {
            if (last.type === 'classify' && Array.isArray(last.labels)) {
                pendingUndoRestore = {
                    mode: String(last.mode || labelerMode || ''),
                    serial: Number(last.serial || 0),
                    queueIndex: Number(last.queueIndex || 0),
                    cropIdx: Number(last.cropIdx || 0),
                    labels: last.labels.slice(),
                };
            } else if (last.type === 'detect' && Array.isArray(last.boxes)) {
                pendingUndoRestore = {
                    mode: String(last.mode || labelerMode || ''),
                    serial: Number(last.serial || 0),
                    queueIndex: Number(last.queueIndex || 0),
                    boxes: last.boxes.map((b) => ({ ...b })),
                };
            } else {
                pendingUndoRestore = null;
            }
        }

        //Go back
        void releaseCurrentClaim();
        let targetIndex = last ? findQueueIndexBySerial(last.serial) : -1;
        if (targetIndex < 0 && Number.isInteger(Number(last?.queueIndex))) {
            targetIndex = Math.max(0, Math.min(Number(last.queueIndex), Math.max(0, queue.length - 1)));
        }
        if (targetIndex < 0) {
            targetIndex = Math.max(0, queueIndex - 1);
        }
        queueIndex = targetIndex;
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
            const savePayload = pendingUpdates.map((u) => ({ ...u }));
            const resp = await apiPost('/api/labeler/save', { updates: savePayload });
            const cleared = Array.isArray(resp?.unblacklisted_ref_serials) ? resp.unblacklisted_ref_serials : [];
            for (const sn of cleared) {
                unmarkRefSerialFlagged(sn);
                const listEl = document.getElementById('predictions-list');
                if (listEl) {
                    listEl.querySelectorAll(`.ref-frame[data-ref-serial="${Number(sn)}"]`).forEach((el) => {
                        el.classList.remove('ref-flagged');
                    });
                }
            }
            setStatus(`Saved ${pendingUpdates.length} updates!`);
            pendingUpdates = [];
            updateInfo();
        } catch (e) {
            setStatus(`Save failed: ${e.message}`);
        }
    }

    //---------- Classifier Mode ----------

    function _predictionRefCountForCrop(results, cropIdx) {
        const coverage = _predictionRefCoverageForCrop(results, cropIdx);
        return coverage.refCount;
    }

    function clampPredictionCropIdx(results = currentPredictions) {
        const rows = Array.isArray(results) ? results : [];
        if (!rows.length) return 0;
        const clamped = Math.max(0, Math.min(Number(currentCropIdx || 0), rows.length - 1));
        if (clamped !== currentCropIdx) currentCropIdx = clamped;
        return clamped;
    }

    function _predictionRefCoverageForCrop(results, cropIdx) {
        const rows = Array.isArray(results) ? results : [];
        const idx = Math.max(0, Number(cropIdx || 0));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) {
            return {
                candidateCount: 0,
                targetCount: 0,
                candidatesWithRefs: 0,
                refCount: 0,
                coverage: 0,
            };
        }
        const candidates = (crop.candidates || []).slice(0, 9);
        let count = 0;
        let withRefs = 0;
        for (const cand of candidates) {
            let hasAny = false;
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            for (const ref of refs) {
                if (typeof ref === 'string' && ref.trim()) {
                    count += 1;
                    hasAny = true;
                    continue;
                }
                if (!ref || typeof ref !== 'object') continue;
                const img = String(ref.img || ref.thumb || '').trim();
                const url = String(ref.url || ref.src || '').trim();
                if (img || url) {
                    count += 1;
                    hasAny = true;
                }
            }
            if (hasAny) withRefs += 1;
        }
        const targetCount = candidates.length;
        return {
            candidateCount: candidates.length,
            targetCount,
            candidatesWithRefs: withRefs,
            refCount: count,
            coverage: targetCount > 0 ? (withRefs / targetCount) : 0,
        };
    }

    function _predictionLoadedRefCoverageForCrop(results, cropIdx) {
        const rows = Array.isArray(results) ? results : [];
        const idx = Math.max(0, Number(cropIdx || 0));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) {
            return {
                candidateCount: 0,
                targetCount: 0,
                candidatesWithRefs: 0,
                refCount: 0,
                coverage: 0,
            };
        }
        const candidates = (crop.candidates || []).slice(0, 9);
        let count = 0;
        let withRefs = 0;
        for (const cand of candidates) {
            let hasAny = false;
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            for (const ref of refs) {
                const src = _extractRefSrc(ref);
                if (!src) continue;
                if (isRefImageReady(src)) {
                    count += 1;
                    hasAny = true;
                }
            }
            if (hasAny) withRefs += 1;
        }
        const targetCount = candidates.length;
        return {
            candidateCount: candidates.length,
            targetCount,
            candidatesWithRefs: withRefs,
            refCount: count,
            coverage: targetCount > 0 ? (withRefs / targetCount) : 0,
        };
    }

    function _predictionRefDepthForCrop(results, cropIdx, minRefsPerCandidate = CLASSIFY_REFS_PER_CAT_TARGET) {
        const rows = Array.isArray(results) ? results : [];
        const idx = Math.max(0, Number(cropIdx || 0));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) {
            return {
                targetCount: 0,
                candidatesAtDepth: 0,
                minRefsPerCandidate: Math.max(1, Number(minRefsPerCandidate || 1)),
            };
        }
        const threshold = Math.max(1, Number(minRefsPerCandidate || 1));
        const candidates = (crop.candidates || []).slice(0, 9);
        let atDepth = 0;
        for (const cand of candidates) {
            let refCount = 0;
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            for (const ref of refs) {
                if (typeof ref === 'string' && ref.trim()) {
                    refCount += 1;
                    continue;
                }
                if (!ref || typeof ref !== 'object') continue;
                const img = String(ref.img || ref.thumb || '').trim();
                const url = String(ref.url || ref.src || '').trim();
                if (img || url) refCount += 1;
            }
            if (refCount >= threshold) atDepth += 1;
        }
        return {
            targetCount: candidates.length,
            candidatesAtDepth: atDepth,
            minRefsPerCandidate: threshold,
        };
    }

    function _predictionLoadedRefDepthForCrop(results, cropIdx, minRefsPerCandidate = CLASSIFY_WARM_READY_MIN_REFS_PER_CAT) {
        const rows = Array.isArray(results) ? results : [];
        const idx = Math.max(0, Number(cropIdx || 0));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) {
            return {
                targetCount: 0,
                candidatesAtDepth: 0,
                minRefsPerCandidate: Math.max(1, Number(minRefsPerCandidate || 1)),
            };
        }
        const threshold = Math.max(1, Number(minRefsPerCandidate || 1));
        const candidates = (crop.candidates || []).slice(0, 9);
        let atDepth = 0;
        for (const cand of candidates) {
            let refCount = 0;
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            for (const ref of refs) {
                const src = _extractRefSrc(ref);
                if (!src) continue;
                if (isRefImageReady(src)) refCount += 1;
            }
            if (refCount >= threshold) atDepth += 1;
        }
        return {
            targetCount: candidates.length,
            candidatesAtDepth: atDepth,
            minRefsPerCandidate: threshold,
        };
    }

    function _predictionRefsDeepEnoughForCrop(results, cropIdx) {
        const depth = _predictionRefDepthForCrop(results, cropIdx, CLASSIFY_REFS_PER_CAT_TARGET);
        if (depth.targetCount <= 0) return false;
        const target = Math.min(9, depth.targetCount);
        const required = Math.min(target, Math.max(1, CLASSIFY_REF_MIN_CANDIDATES_WITH_REFS));
        return depth.candidatesAtDepth >= required;
    }

    function _predictionRefsSufficientForCrop(results, cropIdx) {
        const cov = _predictionLoadedRefCoverageForCrop(results, cropIdx);
        if (cov.targetCount <= 0) return false;
        const minCandidates = Math.min(
            cov.targetCount,
            Math.max(1, Number(CLASSIFY_REF_MIN_CANDIDATES_WITH_REFS || 1)),
        );
        const minCoverage = Math.max(0.05, Math.min(1, Number(CLASSIFY_REF_MIN_COVERAGE || 0.55)));
        return cov.candidatesWithRefs >= minCandidates && cov.coverage >= minCoverage;
    }

    function _predictionHasOptionsForCrop(results, cropIdx) {
        const rows = Array.isArray(results) ? results : [];
        if (!rows.length) return false;
        const idx = Math.max(0, Math.min(Number(cropIdx || 0), rows.length - 1));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) return false;
        return crop.candidates.length > 0;
    }

    function _predictionRefsAtTargetForCrop(results, cropIdx) {
        const depth = _predictionRefDepthForCrop(results, cropIdx, CLASSIFY_REFS_PER_CAT_TARGET);
        if (depth.targetCount <= 0) return false;
        return depth.candidatesAtDepth >= depth.targetCount;
    }

    function _predictionRefSignatureForCrop(results, cropIdx) {
        const rows = Array.isArray(results) ? results : [];
        const idx = Math.max(0, Number(cropIdx || 0));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) return '';
        const parts = [];
        for (const cand of (crop.candidates || []).slice(0, 9)) {
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            const refParts = [];
            for (const ref of refs.slice(0, CLASSIFY_REFS_PER_CAT_TARGET)) {
                if (typeof ref === 'string') {
                    refParts.push(`s:${ref}`);
                    continue;
                }
                const serial = ref && ref.serial != null ? String(ref.serial) : '';
                const cropNum = ref && ref.crop != null ? String(ref.crop) : '';
                const src = _extractRefSrc(ref);
                refParts.push(`${serial}:${cropNum}:${src}`);
            }
            parts.push(`${String(cand?.name || '')}|${refParts.join(',')}`);
        }
        return parts.join('||');
    }

    async function _refreshCurrentPredictionRefs(requestSerial, requestKey, title = 'Loading reference photos...') {
        if (labelerMode !== 'classify') return;
        if (!requestKey) return;
        const parsedBoxes = currentBoxes.map((b) => `${b.cx} ${b.cy} ${b.w} ${b.h}`);
        if (!parsedBoxes.length) return;
        const lastRefresh = Number(classifyRefRefreshTs.get(requestKey) || 0);
        const now = Date.now();
        if ((now - lastRefresh) < CLASSIFY_REF_REFRESH_COOLDOWN_MS) {
            return;
        }
        classifyRefRefreshTs.set(requestKey, now);
        for (let attempt = 0; attempt < CLASSIFY_REF_RETRY_ATTEMPTS; attempt++) {
            if (requestSerial !== currentSerial || requestKey !== getPredCacheKey(currentItem)) return;
            clampPredictionCropIdx(currentPredictions);
            if (_predictionRefsAtTargetForCrop(currentPredictions, currentCropIdx)) return;
            // Keep ref refresh fully in background; do not block interaction with an overlay.
            try {
                const retry = await postClassifyIdentify(
                    currentItem,
                    parsedBoxes,
                    { maxAttempts: 2, focusCropIdx: currentCropIdx },
                );
                prefetchRefsFromResults(retry?.results || []);
                if (requestSerial !== currentSerial || requestKey !== getPredCacheKey(currentItem)) return;
                const prevIdx = clampPredictionCropIdx(currentPredictions);
                const prevCov = _predictionRefCoverageForCrop(currentPredictions, prevIdx);
                const prevDepth = _predictionRefDepthForCrop(currentPredictions, prevIdx, CLASSIFY_REFS_PER_CAT_TARGET);
                const prevSig = _predictionRefSignatureForCrop(currentPredictions, prevIdx);
                const nextPreds = retry?.results || [];
                const nextIdx = Array.isArray(nextPreds) && nextPreds.length
                    ? Math.max(0, Math.min(prevIdx, nextPreds.length - 1))
                    : 0;
                const nextCov = _predictionRefCoverageForCrop(nextPreds, nextIdx);
                const nextDepth = _predictionRefDepthForCrop(nextPreds, nextIdx, CLASSIFY_REFS_PER_CAT_TARGET);
                const nextSig = _predictionRefSignatureForCrop(nextPreds, nextIdx);
                const improved = (
                    nextCov.candidatesWithRefs > prevCov.candidatesWithRefs
                    || nextCov.refCount > prevCov.refCount
                    || nextDepth.candidatesAtDepth > prevDepth.candidatesAtDepth
                    || nextSig !== prevSig
                );
                if (improved) {
                    currentPredictions = nextPreds;
                    currentCropIdx = nextIdx;
                    if (requestKey) predCache.set(requestKey, currentPredictions);
                    renderPredictions();
                }
            } catch (e) {
                // Best-effort retries while refs load.
            }
            if (_predictionRefsAtTargetForCrop(currentPredictions, currentCropIdx)) return;
            await waitMs(220 * (attempt + 1));
        }
    }

    async function loadPredictions(force = false) {
        if (currentBoxes.length === 0) return;
        if (labelerMode !== 'classify') return;
        if (currentPredictions.length) {
            clampPredictionCropIdx(currentPredictions);
        }
        if (loadPredictionsBusy) {
            loadPredictionsQueuedForce = loadPredictionsQueuedForce || !!force;
            return;
        }
        loadPredictionsBusy = true;
        let rerunQueued = false;
        try {
            const activeKey = getPredCacheKey(currentItem);
            let stopTicker = null;
            if (!force) {
                if (currentPredictions.length) {
                    if (_predictionHasOptionsForCrop(currentPredictions, currentCropIdx)) {
                        renderPredictions();
                        primeHotNextClassifyItem();
                        const rs = currentSerial;
                        const rk = getPredCacheKey(currentItem);
                        void _refreshCurrentPredictionRefs(rs, rk, 'Loading additional reference photos...');
                        return;
                    }
                }
                const cached = predCache.get(activeKey);
                if (cached) {
                    currentPredictions = cached;
                    prefetchRefsFromResults(currentPredictions);
                    clampPredictionCropIdx(currentPredictions);
                    if (_predictionHasOptionsForCrop(currentPredictions, currentCropIdx)) {
                        renderPredictions();
                        primeHotNextClassifyItem();
                        const rs = currentSerial;
                        const rk = getPredCacheKey(currentItem);
                        void _refreshCurrentPredictionRefs(rs, rk, 'Loading additional reference photos...');
                        return;
                    }
                }
                // Do not block UI waiting on background prefetch; foreground should always proceed.
            }

            if (!force && activeKey && classifyWarmInFlight.has(activeKey) && !predCache.has(activeKey)) {
                const joined = await _waitFor(() => {
                    if (activeKey !== getPredCacheKey(currentItem)) return true;
                    return predCache.has(activeKey);
                }, 3200, 70);
                if (joined && activeKey === getPredCacheKey(currentItem)) {
                    const warmed = predCache.get(activeKey);
                    if (Array.isArray(warmed) && warmed.length) {
                        currentPredictions = warmed;
                        prefetchRefsFromResults(currentPredictions);
                        clampPredictionCropIdx(currentPredictions);
                        if (_predictionHasOptionsForCrop(currentPredictions, currentCropIdx)) {
                            renderPredictions();
                            primeHotNextClassifyItem();
                            const rs = currentSerial;
                            const rk = getPredCacheKey(currentItem);
                            void _refreshCurrentPredictionRefs(rs, rk, 'Loading additional reference photos...');
                            return;
                        }
                    }
                }
            }

            setStatus('Running classifier...');
            stopTicker = startClassifierLoadProgressTicker('Loading classifier options...');
            try {
                const requestSerial = currentSerial;
                const requestKey = getPredCacheKey(currentItem);
                let data = null;
                try {
                    data = await postClassifyIdentify(
                        currentItem,
                        currentBoxes.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                        { maxAttempts: 4, focusCropIdx: currentCropIdx },
                    );
                } catch (e) {
                    if (!isNoImageApiError(e)) throw e;
                    let lastErr = e;
                    for (let retry = 1; retry <= 5; retry++) {
                        await warmCachedImage(currentSerial);
                        await waitMs(180 * retry);
                        try {
                            data = await postClassifyIdentify(
                                currentItem,
                                currentBoxes.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                                { maxAttempts: 2, focusCropIdx: currentCropIdx },
                            );
                            lastErr = null;
                            break;
                        } catch (e2) {
                            lastErr = e2;
                            if (!isNoImageApiError(e2)) throw e2;
                        }
                    }
                    if (lastErr) throw lastErr;
                }

                if (requestSerial !== currentSerial || requestKey !== getPredCacheKey(currentItem)) {
                    return;
                }
                currentPredictions = data.results || [];
                if ((!Array.isArray(currentPredictions) || !currentPredictions.length) && currentBoxes.length) {
                    // Rare safety net: if identify returned empty despite valid boxes, retry once.
                    data = await postClassifyIdentify(
                        currentItem,
                        currentBoxes.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                        { maxAttempts: 2, focusCropIdx: currentCropIdx },
                    );
                    currentPredictions = data.results || [];
                }
                prefetchRefsFromResults(currentPredictions);
                clampPredictionCropIdx(currentPredictions);
                const key = getPredCacheKey(currentItem);
                if (key) {
                    predCache.set(key, currentPredictions);
                    clearClassifyPrefetchFailure(key);
                }
                const cov = _predictionLoadedRefCoverageForCrop(currentPredictions, currentCropIdx);
                renderPredictions();
                if (!_predictionRefsSufficientForCrop(currentPredictions, currentCropIdx)) {
                    void _refreshCurrentPredictionRefs(requestSerial, requestKey, 'Loading reference photos...');
                } else {
                    void _refreshCurrentPredictionRefs(requestSerial, requestKey, 'Loading additional reference photos...');
                }
                if (_predictionRefsSufficientForCrop(currentPredictions, currentCropIdx)) {
                    setStatus(`Ready - sn${currentSerial}`);
                } else {
                    setStatus(
                        `Ready - sn${currentSerial} `
                        + `(loaded refs ${cov.candidatesWithRefs}/${cov.targetCount || '?'} cats)`
                    );
                }
                primeHotNextClassifyItem();
            } catch (e) {
                setStatus(`Classify failed: ${e.message}`);
            } finally {
                if (stopTicker) {
                    stopTicker();
                }
                if (!(_isClassifyItemLoadOverlayWaitActive() && !_isCurrentClassifyItemDisplayReady())) {
                    setWarmOverlay(false);
                }
            }
        } finally {
            loadPredictionsBusy = false;
            if (labelerMode === 'classify' && loadPredictionsQueuedForce) {
                const queuedForce = loadPredictionsQueuedForce;
                loadPredictionsQueuedForce = false;
                rerunQueued = true;
                void loadPredictions(queuedForce);
            } else {
                loadPredictionsQueuedForce = false;
            }
            if (!rerunQueued) {
                // Keep warm animation tied to the current photo only.
                classifierWarmItemKey = '';
            }
        }
    }

    function renderPredictions() {
        const listEl = document.getElementById('predictions-list');
        if (!listEl) return;

        if (!Array.isArray(currentPredictions) || !currentPredictions.length) {
            listEl.innerHTML = '<div class="no-predictions">No predictions</div>';
            return;
        }
        clampPredictionCropIdx(currentPredictions);
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
            const refs = [];
            const allRefs = Array.isArray(c.refs) ? c.refs : [];
            for (let refIdx = 0; refIdx < allRefs.length; refIdx++) {
                if (refs.length >= CLASSIFY_REFS_PER_CAT_TARGET) break;
                const ref = allRefs[refIdx];
                const info = typeof ref === 'string'
                    ? { img: ref, serial: null, crop: null }
                    : (ref || {});
                const refImg = String(info.img || info.thumb || '').trim();
                const refUrl = String(info.url || info.src || '').trim();
                let src = '';
                if (refImg) {
                    src = refImg.startsWith('data:image')
                        ? refImg
                        : `data:image/jpeg;base64,${refImg}`;
                } else if (refUrl) {
                    src = refUrl.startsWith('/') ? buildApiUrl(refUrl) : refUrl;
                }
                prefetchRefImageSrc(src);
                if (!src) continue;
                const refState = String(refImagePrefetchState.get(src) || '');
                if (refState === 'error') continue;
                const ready = isRefImageReady(src);
                const sn = info.serial != null ? `sn${info.serial}` : '';
                const isFlagged = isRefSerialFlagged(info.serial);
                const isFlagging = isRefSerialFlagging(info.serial);
                const cropNum = Number(info.crop) || null;
                const cropText = cropNum ? ` crop ${cropNum}` : '';
                const caption = sn || cropNum ? `${sn}${cropText}`.trim() : '';
                const serialAttr = info.serial != null ? ` data-ref-serial="${escapeHtml(String(info.serial))}"` : '';
                const cropAttr = cropNum ? ` data-ref-crop="${escapeHtml(String(cropNum))}"` : '';
                refs.push(`
                    <div class="ref-frame${isFlagged ? ' ref-flagged' : ''}${isFlagging ? ' ref-flagging' : ''}"${serialAttr}${cropAttr}>
                        <img loading="eager" decoding="async" src="${escapeHtml(src)}" alt="${safeName} ref ${refIdx + 1}">
                        ${ready && caption ? `<div class="ref-overlay">${escapeHtml(caption)}</div>` : ''}
                    </div>
                `);
            }
            return `
                <div class="prediction-item" data-idx="${i + 1}">
                    <div class="prediction-head">
                        <span class="pred-key">${i + 1}</span>
                        <span class="pred-name">${safeName}</span>
                        <span class="pred-conf ${confClass}">${confLabel} (${confPct.toFixed(1)}%)</span>
                    </div>
                    ${safeDesc ? `<div class="pred-desc">${safeDesc}</div>` : ''}
                    <div class="prediction-refs">${refs.join('')}</div>
                </div>
            `;
        }).join('');
        applyRefAspectClamp(listEl);
    }

    async function loadManualCandidates(force = false) {
        if (labelerMode !== 'manual') return;
        if (!currentBoxes.length || currentCropIdx < 0 || currentCropIdx >= currentBoxes.length) {
            const listEl = document.getElementById('predictions-list');
            if (listEl) listEl.innerHTML = '<div class="no-predictions">No review crops in this image.</div>';
            return;
        }

        const box = currentBoxes[currentCropIdx];
        if (!box) return;
        const boxSig = _manualBoxSig(box);
        const requestKey = _manualCandidateKey(currentSerial, currentItem?.url || '', boxSig);
        if (!requestKey) return;

        if (!force && manualCandidates.length && manualCandidatesKey === requestKey) {
            renderManualCandidates();
            prefetchManualCandidates();
            return;
        }

        if (!force && manualCandidateCache.has(requestKey)) {
            const cached = manualCandidateCache.get(requestKey) || {};
            manualCandidates = Array.isArray(cached.candidates) ? cached.candidates : [];
            manualCandidatesKey = requestKey;
            renderManualCandidates();
            setStatus(`Ready - sn${currentSerial}`);
            prefetchManualCandidates();
            return;
        }
        setStatus('Loading manual review options...');
        setWarmOverlay(true, 'Loading manual review options...', 'Comparing against all cats', 0.25);
        let keepOverlay = false;

        try {
            const payload = await apiPost('/api/labeler/manual/candidates', {
                serial: currentSerial,
                url: currentItem?.url || null,
                box: boxSig,
            }, { maxAttempts: 4, timeoutMs: 60000 });

            if (labelerMode !== 'manual') return;
            const liveBox = currentBoxes[currentCropIdx];
            const liveKey = liveBox ? _manualCandidateKey(currentSerial, currentItem?.url || '', _manualBoxSig(liveBox)) : '';
            if (requestKey !== liveKey) return;
            if (payload && payload.ready === false) {
                const status = payload.cache_status || {};
                const built = Number(status.built || status.cats || 0);
                const total = Number(status.total || allCats.length || 0);
                const pct = total > 0 ? Math.min(0.95, Math.max(0.05, built / total)) : 0.1;
                setWarmOverlay(true, 'Preparing manual review cache...', `${built}/${total || '?'} cats ready`, pct);
                startManualRefPoll();
                keepOverlay = true;
                return;
            }

            manualCandidates = Array.isArray(payload?.candidates) ? payload.candidates : [];
            manualCandidatesKey = requestKey;
            _setManualCandidateCache(requestKey, { candidates: manualCandidates });
            renderManualCandidates();
            setStatus(`Ready - sn${currentSerial}`);
            prefetchManualCandidates();
        } catch (e) {
            setStatus(`Manual review load failed: ${e.message}`);
        } finally {
            if (labelerMode === 'manual' && !keepOverlay) {
                setWarmOverlay(false);
            }
        }
    }

    function renderManualCandidates() {
        const listEl = document.getElementById('predictions-list');
        if (!listEl) return;
        const sidebarEl = containerEl?.querySelector('.labeler-sidebar');
        const restoreTop = Number.isFinite(manualSidebarRestoreScrollTop)
            ? Number(manualSidebarRestoreScrollTop)
            : null;
        manualSidebarRestoreScrollTop = null;
        if (!manualCandidates.length) {
            listEl.innerHTML = '<div class="no-predictions">No candidates available.</div>';
            if (restoreTop != null && sidebarEl) sidebarEl.scrollTop = restoreTop;
            return;
        }

        listEl.innerHTML = manualCandidates.map((cand) => {
            const name = String(cand?.name || '').trim();
            const catId = cand && cand.cat_id != null ? String(cand.cat_id) : '';
            const rawDisplay = String(cand?.display_name || name || '').trim();
            const hasIdPrefix = !!catId && new RegExp(`^\\s*${catId}\\s*[.)\\-:]`).test(rawDisplay);
            const displayName = hasIdPrefix
                ? rawDisplay
                : (catId ? `${catId}. ${rawDisplay}` : rawDisplay);
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            const safeDisplayName = escapeHtml(displayName || name);
            const safeDesc = escapeHtml(String(cand?.desc || '').trim());
            const selectName = String(name || rawDisplay || '').trim();
            const safeSelectName = escapeHtml(selectName);
            const refsHtml = [];
            for (let refIdx = 0; refIdx < refs.length; refIdx++) {
                if (refsHtml.length >= 5) break;
                const ref = refs[refIdx];
                const info = typeof ref === 'string'
                    ? { img: ref, serial: null, crop: null }
                    : (ref || {});
                const refImg = String(info.img || info.thumb || '').trim();
                const refUrl = String(info.url || info.src || '').trim();
                let src = '';
                if (refImg) {
                    src = refImg.startsWith('data:image')
                        ? refImg
                        : `data:image/jpeg;base64,${refImg}`;
                } else if (refUrl) {
                    src = refUrl.startsWith('/') ? buildApiUrl(refUrl) : refUrl;
                }
                prefetchRefImageSrc(src);
                if (!src) continue;
                const refState = String(refImagePrefetchState.get(src) || '');
                if (refState === 'error') continue;
                const ready = isRefImageReady(src);
                const sn = info.serial != null ? `sn${info.serial}` : '';
                const isFlagged = isRefSerialFlagged(info.serial);
                const isFlagging = isRefSerialFlagging(info.serial);
                const cropNum = Number(info.crop) || null;
                const cropText = cropNum ? ` crop ${cropNum}` : '';
                const caption = sn || cropNum ? `${sn}${cropText}`.trim() : '';
                const serialAttr = info.serial != null ? ` data-ref-serial="${escapeHtml(String(info.serial))}"` : '';
                const cropAttr = cropNum ? ` data-ref-crop="${escapeHtml(String(cropNum))}"` : '';
                refsHtml.push(`
                    <div class="ref-frame${isFlagged ? ' ref-flagged' : ''}${isFlagging ? ' ref-flagging' : ''}"${serialAttr}${cropAttr}>
                        <img loading="eager" decoding="async" src="${escapeHtml(src)}" alt="${safeDisplayName} ref ${refIdx + 1}">
                        ${ready && caption ? `<div class="ref-overlay">${escapeHtml(caption)}</div>` : ''}
                    </div>
                `);
            }
            const searchText = `${catId} ${displayName} ${name}`
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, ' ')
                .trim();

            return `
                <div class="manual-cat-card" data-name="${safeSelectName}" data-search="${searchText}" data-cat-id="${escapeHtml(catId)}">
                    <div class="prediction-head">
                        <span class="pred-name">${safeDisplayName}</span>
                    </div>
                    ${safeDesc ? `<div class="pred-desc">${safeDesc}</div>` : ''}
                    <div class="prediction-refs">${refsHtml.join('') || '<div class="no-predictions">No references</div>'}</div>
                </div>
            `;
        }).join('');
        applyRefAspectClamp(listEl);
        if (restoreTop != null && sidebarEl) {
            sidebarEl.scrollTop = restoreTop;
        }
    }

    function selectManualCandidate(catName) {
        if (labelerMode !== 'manual') return;
        const selected = String(catName || '').trim();
        if (!selected) return;
        const sidebarEl = containerEl?.querySelector('.labeler-sidebar');
        if (sidebarEl) {
            manualSidebarRestoreScrollTop = sidebarEl.scrollTop;
        }
        pushCropLabelUndo(selected);
        currentLabels[currentCropIdx] = selected;
        advanceManualCursor();
    }

    function scrollManualMatchIntoView(card, smooth = true) {
        if (!card) return;
        const behavior = smooth ? 'smooth' : 'auto';
        const doScroll = (mode) => {
            try {
                card.scrollIntoView({ behavior: mode, block: 'center', inline: 'nearest' });
            } catch (e) {
                //ignore
            }
        };
        doScroll(behavior);
        // Correct for lazy-loading ref images that change card heights after initial scroll.
        requestAnimationFrame(() => doScroll('auto'));
        setTimeout(() => doScroll('auto'), 120);
        setTimeout(() => doScroll('auto'), 320);
    }

    function runManualSearch(opts = {}) {
        if (labelerMode !== 'manual') return;
        const live = !!opts.live;
        const smooth = opts.smooth !== false;
        const query = String(manualSearchInputEl?.value || '')
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, ' ')
            .trim();
        const listEl = document.getElementById('predictions-list');
        if (!listEl) return;
        const cards = Array.from(listEl.querySelectorAll('.manual-cat-card'));
        cards.forEach((card) => card.classList.remove('manual-cat-match'));
        if (!query) {
            if (manualSearchStatusEl) manualSearchStatusEl.textContent = '';
            return;
        }
        if (!cards.length) return;

        const isNumeric = /^\d+$/.test(query);
        let match = null;
        let bestScore = Number.POSITIVE_INFINITY;
        for (const card of cards) {
            const catId = String(card.getAttribute('data-cat-id') || '').trim().toLowerCase();
            const text = String(card.getAttribute('data-search') || '').trim().toLowerCase();
            if (isNumeric) {
                if (catId === query) {
                    match = card;
                    break;
                }
                continue;
            }
            let score = Number.POSITIVE_INFINITY;
            if (text.startsWith(query)) {
                score = 0;
            } else if (text.includes(` ${query}`)) {
                score = 1;
            } else if (text.includes(query)) {
                score = 2;
            }
            if (score < bestScore) {
                bestScore = score;
                match = card;
            }
        }
        if (!match) {
            if (manualSearchStatusEl) manualSearchStatusEl.textContent = live ? '' : 'No match found.';
            return;
        }
        match.classList.add('manual-cat-match');
        scrollManualMatchIntoView(match, smooth);
        if (manualSearchStatusEl) manualSearchStatusEl.textContent = 'Match highlighted.';
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
        pushCropLabelUndo(name);
        currentLabels[currentCropIdx] = name;

        advanceCrop();
    }

    function markNeedsReview() {
        pushCropLabelUndo('NeedsReview');
        currentLabels[currentCropIdx] = 'NeedsReview';
        advanceCrop();
    }

    function rejectCrop() {
        pushCropLabelUndo('Rejected');
        currentLabels[currentCropIdx] = 'Rejected';
        advanceCrop();
    }

    function getNeedsReviewIndices() {
        const out = [];
        for (let i = 0; i < currentBoxes.length; i++) {
            if (_isNeedsReview(currentLabels[i])) {
                out.push(i);
            }
        }
        return out;
    }

    function _isNeedsReview(label) {
        const key = String(label || '').trim().toLowerCase();
        return key === 'needsreview' || key === 'needs review';
    }

    function prepareManualReviewState() {
        if (!currentBoxes.length) {
            manualReviewIndices = [];
            manualReviewCursor = 0;
            currentCropIdx = 0;
            return;
        }
        const declared = Array.isArray(currentItem?.review_indices) ? currentItem.review_indices : [];
        const seeded = declared
            .map((n) => parseInt(n, 10))
            .filter((n) => Number.isInteger(n) && n >= 0 && n < currentBoxes.length && _isNeedsReview(currentLabels[n]));
        manualReviewIndices = seeded.length ? seeded : getNeedsReviewIndices();
        manualReviewCursor = 0;
        if (!manualReviewIndices.length) {
            currentCropIdx = 0;
            return;
        }
        const firstPending = manualReviewIndices.findIndex((idx) => _isNeedsReview(currentLabels[idx]));
        if (firstPending >= 0) {
            manualReviewCursor = firstPending;
        }
        currentCropIdx = manualReviewIndices[manualReviewCursor] || 0;
    }

    function advanceManualCursor() {
        if (!manualReviewIndices.length) {
            saveAndAdvance();
            return;
        }
        let nextCursor = manualReviewCursor + 1;
        while (nextCursor < manualReviewIndices.length) {
            const idx = manualReviewIndices[nextCursor];
            if (_isNeedsReview(currentLabels[idx])) {
                manualReviewCursor = nextCursor;
                currentCropIdx = idx;
                drawCanvas();
                loadManualCandidates();
                return;
            }
            nextCursor++;
        }
        saveAndAdvance();
    }

    function advanceCrop() {
        if (labelerMode === 'manual') {
            advanceManualCursor();
            return;
        }
        currentCropIdx++;
        if (currentCropIdx >= currentBoxes.length) {
            saveAndAdvance();
            return;
        }
        loadPredictions();
        drawCanvas();
    }

    function deferManualPhoto() {
        if (labelerMode !== 'manual') return;
        setStatus(`Deferred sn${currentSerial}`);
        advanceQueue();
    }

    function prefetchPredictions() {
        if (labelerMode !== 'classify') return;
        if (Date.now() < classifyPrefetchBackoffUntil) return;
        if (loadPredictionsBusy) {
            prefetchRequested = true;
            return;
        }
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
            if (
                !key
                || predCache.has(key)
                || prefetchInFlight.has(key)
                || classifyWarmInFlight.has(key)
                || isClassifyPrefetchBlocked(key)
            ) continue;
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
                    rerank: false,
                }, { timeoutMs: API_PREFETCH_TIMEOUT_MS });
                prefetchRefsFromResults(data?.results || []);
                if (epoch === predCacheEpoch) {
                    predCache.set(target.key, data.results || []);
                }
                clearClassifyPrefetchFailure(target.key);
            } catch (e) {
                if (isNoImageApiError(e)) {
                    // Keep trying quickly while image cache/source stabilizes.
                } else {
                    if (isTransientApiError(e)) {
                        classifyPrefetchBackoffUntil = Date.now() + 2500;
                    }
                    markClassifyPrefetchFailure(target.key, e);
                }
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

    function primeHotNextClassifyItem() {
        if (labelerMode !== 'classify') return;
        const nextItem = queue[queueIndex + 1];
        if (!nextItem || !nextItem.boxes) return;
        const key = getPredCacheKey(nextItem);
        if (key && predCache.has(key)) {
            const rows = predCache.get(key);
            if (Array.isArray(rows) && rows.length) {
                prefetchRefsFromResults(rows);
            }
            // If predictions exist but ref thumbnails are still decoding/loading, keep
            // nudging the ref prefetch path instead of treating the item as fully primed.
            if (_classifyItemWarmReady(nextItem)) {
                return;
            }
        }
        if (
            !key
            || classifyWarmInFlight.has(key)
            || classifyForegroundInFlight.has(key)
        ) return;
        // Use non-prefetch path for the immediate next item so transitions do not
        // fall back to a foreground "Loading classifier options..." state.
        void ensureClassifyItemReady(nextItem, false, false);
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
            if (
                detectPrefetch.has(key) ||
                detectPrefetchInFlight.has(key) ||
                detectWarmInFlight.has(key) ||
                isDetectWarmBlocked(key)
            ) continue;
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
                    fast: true,
                    prefetch: true,
                }, { timeoutMs: API_PREFETCH_TIMEOUT_MS });
                if (epoch === detectPrefetchEpoch && data) {
                    const raw = data.boxes_yolo || '';
                    setDetectEntry(target.key, raw, raw, false);
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
            if (!entry || !entry.raw || isDetectEntryReady(entry) || detectRefineInFlight.has(key)) continue;
            detectRefineInFlight.add(key);
            apiPost('/api/labeler/refine', {
                serial: item.serial,
                url: item.url || null,
                boxes: parseYoloBoxes(entry.raw).map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                passes: DETECTOR_PREFETCH_REFINE_PASSES,
                prefetch: true,
            }, { timeoutMs: API_PREFETCH_TIMEOUT_MS }).then((data) => {
                if (data && data.boxes_yolo) {
                    setDetectEntry(key, entry.raw, data.boxes_yolo, true);
                }
            }).catch(() => {
                //Fail soft: keep raw detector boxes so this serial does not block warm progress.
                setDetectEntry(key, entry.raw, entry.raw, true);
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

    function isEditableTarget(target) {
        const el = target instanceof Element ? target : null;
        if (!el) return false;
        if (el.isContentEditable) return true;
        const tag = (el.tagName || '').toLowerCase();
        if (tag === 'textarea') return true;
        if (tag !== 'input') return false;
        const type = String(el.getAttribute('type') || 'text').toLowerCase();
        return [
            'text', 'search', 'url', 'tel', 'email', 'password',
            'number', 'date', 'datetime-local', 'month', 'time', 'week',
        ].includes(type);
    }

    function onKeyDown(e) {
        //Only handle when labeler is visible
        if (!containerEl || containerEl.style.display === 'none') return;
        if (isEditableTarget(e.target)) return;

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
        } else if (labelerMode === 'classify') {
            handleClassifierKey(e, key);
        } else {
            handleManualKey(e, key);
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

    function handleManualKey(e, key) {
        //Number keys intentionally do nothing in manual mode (click selection only).
        if (key >= '0' && key <= '9') {
            return;
        }
        switch (key) {
            case 'x':
                if (throttleAction('manualReject')) rejectCrop();
                break;
            case 'enter':
                e.preventDefault();
                if (throttleAction('manualNextPhoto')) deferManualPhoto();
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
