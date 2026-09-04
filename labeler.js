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
    const MOVE_REPEAT_DELAY_MS = 180;
    const MOVE_BASE_SPEED_PX_PER_SEC = (NUDGE_PX / 50) * 1000;
    const MOVE_ACCEL_RAMP_MS = 1800;
    const MOVE_MAX_SPEED_MULTIPLIER = 4.5;
    const MOVE_ACCEL_EXPONENT = 1.6;
    const CROP_PAD_PCT = 0.03;
    const ZOOM_MIN = 0.5;
    const ZOOM_MAX = 6.0;
    const ZOOM_STEP = 0.12;
    const PREFETCH_AHEAD = 6;
    const PREFETCH_CONCURRENCY = 2;
    const IMAGE_PREFETCH_AHEAD = 3;
    const IMAGE_PREFETCH_AHEAD_CLASSIFY = 5;
    const IMAGE_PREFETCH_PARALLEL = 3;
    const IMAGE_PREFETCH_RESTART_MS = 60000;
    const IMAGE_PREFETCH_AHEAD_DETECT = 4;
    const DETECT_PREFETCH_CONCURRENCY = 1;
    const DETECT_PREFETCH_COOLDOWN_MS = 1000;
    const DETECT_REFINE_PREFETCH_AHEAD = 0;
    const DETECT_REFINE_COOLDOWN_MS = 12000;
    const ACTION_COOLDOWN_MS = 250;
    const API_POST_TIMEOUT_MS = 90000;
    const API_PREFETCH_TIMEOUT_MS = 15000;
    const DETECT_PREFETCH_TIMEOUT_MS = 90000;
    const CLAIM_HEARTBEAT_MS = 15000;
    const CLAIM_ACQUIRE_TIMEOUT_MS = 10000;
    const CLAIM_PRECLAIM_TIMEOUT_MS = 8000;
    const CLAIM_RETRY_REFRESH_COOLDOWN_MS = 15000;
    const CLAIM_PRECLAIM_AHEAD_COUNT = 1;
    const SESSION_WARM_TARGET = 15;
    const SESSION_WARM_TICK_MS = 333;
    const SESSION_REFRESH_QUEUES_MS = 30000;
    const DETECTOR_AUTO_REFINE_PASSES = 1;
    const DETECTOR_PREFETCH_REFINE_PASSES = 1;
    const DETECTOR_MANUAL_REFINE_PASSES = 2;
    const INITIAL_DETECT_WARM_WINDOW = 25;

    // Detect has smaller crops, classify has complex bounding boxes requiring caching
    const INITIAL_MANUAL_WARM_WINDOW = 25;

    const INITIAL_DETECT_WARM_MIN = 10;
    const INITIAL_DETECT_WARM_TIMEOUT_MS = 25000;
    const INITIAL_CLASSIFY_WARM_WINDOW = 6;
    const INITIAL_CLASSIFY_WARM_MIN = 5;
    const INITIAL_CLASSIFY_WARM_TIMEOUT_MS = 30000;
    const ITEM_READY_WAIT_TIMEOUT_MS = 90000;
    const DETECT_READY_WAIT_TIMEOUT_MS = 60000;
    const CLASSIFY_READY_WAIT_TIMEOUT_MS = 15000;
    const CLASSIFY_WARM_JOIN_TIMEOUT_MS = 12000;
    const CLASSIFY_PREFETCH_TIMEOUT_MS = 30000;
    const READY_WAIT_DIAG_INTERVAL_MS = 2000;
    const API_RETRY_MAX_ATTEMPTS = 3;
    const API_RETRY_BASE_MS = 280;
    const SAVE_BATCH_MAX = 500;
    const CLASSIFY_LOAD_TICK_MS = 250;
    const CLASSIFY_REFS_PER_CAT_TARGET = 5;
    const CLASSIFY_WARM_READY_MIN_CANDIDATES = 5;
    const CLASSIFY_WARM_READY_MIN_REFS_PER_CAT = 3;
    const CLASSIFY_REF_MIN_CANDIDATES_WITH_REFS = 5;
    const CLASSIFY_REF_MIN_COVERAGE = 0.4;
    const CLASSIFY_REF_RETRY_ATTEMPTS = 2;
    const CLASSIFY_REF_REFRESH_COOLDOWN_MS = 2800;
    const CLASSIFY_NEXT_CROP_PREFETCH_COOLDOWN_MS = 2200;
    const CLASSIFY_WARM_PREFETCH_MAX_CROPS = 1;
    const CLASSIFY_WARM_PREFETCH_MAX_CANDIDATES = 5;
    const CLASSIFY_WARM_PREFETCH_MAX_REFS = 3;
    const CLASSIFY_DISPLAY_PREFETCH_MAX_CANDIDATES = 9;
    const CLASSIFY_DISPLAY_PREFETCH_MAX_REFS = CLASSIFY_REFS_PER_CAT_TARGET;
    const CLASSIFY_REF_CACHE_VERSION = 'classify_refs_v2';
    const CLASSIFY_PREFETCH_FAIL_BASE_MS = 1500;
    const CLASSIFY_PREFETCH_FAIL_MAX_MS = 30000;
    const CLASSIFY_ITEM_DISPLAY_READY_TIMEOUT_MS = 30000;
    const MANUAL_REF_CACHE_VERSION = 'manual_refs_v2';
    const FLAGGED_REF_SERIALS_STORAGE_KEY = 'labelerFlaggedRefSerials_v1';
    const MANUAL_PREFETCH_AHEAD = 6;
    const MANUAL_PREFETCH_CONCURRENCY = 2;
    const MANUAL_CANDIDATE_CACHE_MAX = 240;
    const MANUAL_REFS_PER_CAT_TARGET = 5;
    const MANUAL_DISPLAY_PREFETCH_MAX_CANDIDATES = 18;
    const MANUAL_DISPLAY_PREFETCH_MAX_REFS = 3;
    const UI_DIAG_MIN_INTERVAL_MS = 1500;
    const REF_IMAGE_PREFETCH_MAX = 12000;
    const IMAGE_PREFETCH_MAX = 2000;
    const REF_IMAGE_RETRY_BASE_MS = 1500;
    const REF_IMAGE_RETRY_MAX_MS = 60000;
    const REF_IMAGE_RETRY_MAX_ATTEMPTS = 6;
    const REF_DISPLAY_HQ_SIZE = 480;
    const REF_DISPLAY_FAST_SIZE = 480;
    const CLASSIFY_AUTO_SKIP_STREAK_LIMIT = 3;
    const IMAGE_PREFETCH_RETRY_BASE_MS = 1200;
    const IMAGE_PREFETCH_RETRY_MAX_MS = 20000;
    const IMAGE_PREFETCH_RETRY_MAX_ATTEMPTS = 5;
    const IMAGE_PREFETCH_STALL_BYPASS_MS = 2800;
    const CACHED_IMAGE_PATH_PROBE_COOLDOWN_MS = 8000;

    function _flagEnabled(flagName, defaultOn = true) {
        try {
            const src = (typeof window !== 'undefined' && window.__LABELER_FEATURES && typeof window.__LABELER_FEATURES === 'object')
                ? window.__LABELER_FEATURES
                : {};
            if (!(flagName in src)) return !!defaultOn;
            return !!src[flagName];
        } catch (e) {
            return !!defaultOn;
        }
    }

    const FLAG_PREFETCH_RETRY_FIX = _flagEnabled('prefetchRetryFix', true);
    const FLAG_CLASSIFY_READY_RELAX = _flagEnabled('classifyReadyRelax', true);
    const FLAG_CACHED_IMAGE_PATH_PROBE = _flagEnabled('cachedImagePathProbe', false);

    const PendingUpdateQueue = window.LabelerSaveQueue?.PendingUpdateQueue;
    const createApiClient = window.LabelerApi?.createClient;
    if (!PendingUpdateQueue || !createApiClient) {
        throw new Error('Labeler support modules must be loaded before labeler.js');
    }

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

    function getCsrfToken() {
        if (typeof window === 'undefined') return '';
        return typeof window.__TC_CSRF_TOKEN === 'string' ? window.__TC_CSRF_TOKEN : '';
    }

    function buildApiUrl(path) {
        const base = getApiBase();
        if (!path) return base;
        if (/^https?:\/\//i.test(path)) return path;
        if (!path.startsWith('/')) path = `/${path}`;
        return `${base}${path}`;
    }

    let labelerRequestController = new AbortController();
    const apiClient = createApiClient({
        buildUrl: buildApiUrl,
        getCsrfToken,
        defaultTimeoutMs: API_POST_TIMEOUT_MS,
        defaultMaxAttempts: API_RETRY_MAX_ATTEMPTS,
        retryBaseMs: API_RETRY_BASE_MS,
    });

    function ensureLabelerRequestController() {
        if (labelerRequestController.signal.aborted) {
            labelerRequestController = new AbortController();
        }
        return labelerRequestController;
    }

    function labelerSessionStorage() {
        try {
            return window.sessionStorage;
        } catch (e) {
            return null;
        }
    }

    function pendingJournalKey() {
        const userId = String(window.__TC_LABELER_USER_ID || '').trim();
        return userId ? `tomcat.labeler.pending.v1:${userId}` : '';
    }

    function attachPendingJournal() {
        const restored = pendingUpdates.attachStorage(labelerSessionStorage(), pendingJournalKey());
        if (restored > 0) restoredPendingNoticeCount = restored;
        syncPendingUnloadWarning();
        if (infoEl) updateInfo();
        return restored;
    }

    function buildCachedImageUrl(serial, opts = {}) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return '';
        const query = new URLSearchParams();
        const intent = String(opts.intent || '').trim().toLowerCase();
        if (intent === 'prefetch' || intent === 'foreground') {
            query.set('intent', intent);
        }
        if (opts.proxy) {
            query.set('proxy', '1');
        }
        if (opts.cacheBust) {
            query.set('cb', String(opts.cacheBust));
        }
        const base = `/api/labeler/cached_image/${sn}`;
        const qs = query.toString();
        return buildApiUrl(qs ? `${base}?${qs}` : base);
    }

    function _cachedImageIntent(intent) {
        const s = String(intent || '').trim().toLowerCase();
        if (s === 'prefetch' || s === 'foreground') return s;
        return 'unknown';
    }

    function _cachedImageProbeKey(serial, intent) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return '';
        return `${sn}:${_cachedImageIntent(intent)}`;
    }

    function _recordCachedImagePathProbe(serial, intent, rec) {
        const key = _cachedImageProbeKey(serial, intent);
        if (!key) return;
        const sn = Number.parseInt(String(serial || ''), 10);
        const out = {
            serial: sn,
            intent: _cachedImageIntent(intent),
            path: String(rec?.path || ''),
            cache: String(rec?.cache || ''),
            status: Number(rec?.status || 0),
            redirected: !!rec?.redirected,
            at: Date.now(),
            source: String(rec?.source || ''),
        };
        cachedImagePathByIntent.set(key, out);
        cachedImagePathLatest.set(String(sn), out);
        if (cachedImagePathByIntent.size > 4000) {
            const overflow = cachedImagePathByIntent.size - 4000;
            const keys = cachedImagePathByIntent.keys();
            for (let i = 0; i < overflow; i++) {
                const evictKey = keys.next().value;
                if (!evictKey) continue;
                cachedImagePathByIntent.delete(evictKey);
            }
        }
        if (cachedImagePathLatest.size > 2000) {
            const overflow = cachedImagePathLatest.size - 2000;
            const keys = cachedImagePathLatest.keys();
            for (let i = 0; i < overflow; i++) {
                const evictKey = keys.next().value;
                if (!evictKey) continue;
                cachedImagePathLatest.delete(evictKey);
            }
        }
    }

    function getCachedImagePathDiag(serial) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return { latest: null, prefetch: null, foreground: null };
        const s = String(sn);
        return {
            latest: cachedImagePathLatest.get(s) || null,
            prefetch: cachedImagePathByIntent.get(`${s}:prefetch`) || null,
            foreground: cachedImagePathByIntent.get(`${s}:foreground`) || null,
        };
    }

    function _compactCachedPathRecord(rec) {
        if (!rec || typeof rec !== 'object') return null;
        return {
            intent: String(rec.intent || ''),
            path: String(rec.path || ''),
            cache: String(rec.cache || ''),
            status: Number(rec.status || 0),
            source: String(rec.source || ''),
        };
    }

    function compactCachedImagePathDiag(serial) {
        const raw = getCachedImagePathDiag(serial);
        return {
            latest: _compactCachedPathRecord(raw?.latest),
            prefetch: _compactCachedPathRecord(raw?.prefetch),
            foreground: _compactCachedPathRecord(raw?.foreground),
        };
    }

    async function probeCachedImagePath(serial, intent = 'foreground', opts = {}) {
        const key = _cachedImageProbeKey(serial, intent);
        if (!key) return null;
        if (cachedImagePathProbeInFlight.has(key)) return null;
        const now = Date.now();
        const force = !!opts.force;
        if (!FLAG_CACHED_IMAGE_PATH_PROBE && !force) return null;
        const lastTs = Number(cachedImagePathProbeTs.get(key) || 0);
        if (!force && (now - lastTs) < CACHED_IMAGE_PATH_PROBE_COOLDOWN_MS) return null;
        cachedImagePathProbeInFlight.add(key);
        cachedImagePathProbeTs.set(key, now);
        try {
            const url = buildCachedImageUrl(serial, { intent, cacheBust: `diag-${now}` });
            const resp = await fetch(url, {
                credentials: 'include',
                redirect: 'manual',
                cache: 'no-store',
                signal: labelerRequestController.signal,
            });
            _recordCachedImagePathProbe(serial, intent, {
                path: String(resp.headers.get('X-Labeler-Image-Path') || ''),
                cache: String(resp.headers.get('X-Labeler-Cache') || ''),
                status: Number(resp.status || 0),
                redirected: !!resp.redirected,
                source: 'probe',
            });
            try {
                if (resp.body && typeof resp.body.cancel === 'function') {
                    await resp.body.cancel();
                }
            } catch (e) {
                // Best-effort cancellation for diagnostics.
            }
            return getCachedImagePathDiag(serial);
        } catch (e) {
            return null;
        } finally {
            cachedImagePathProbeInFlight.delete(key);
        }
    }

    function isLikelyDriveImageUrl(url) {
        try {
            const parsed = new URL(String(url || '').trim(), window.location.origin);
            const host = String(parsed.hostname || '').replace(/\.+$/, '').toLowerCase();
            if (!host) return false;
            return (
                host === 'drive.google.com'
                || host === 'drive.usercontent.google.com'
                || host === 'googleusercontent.com'
                || host.endsWith('.googleusercontent.com')
            );
        } catch (e) {
            return false;
        }
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
    let currentSamMaskTiles = [];
    let selectedBoxIdx = 0;
    let currentLabels = []; //Array of cat names per box
    let currentCropIdx = 0;
    let allCats = []; //Dropdown options
    let history = []; //Undo stack
    const pendingUpdates = new PendingUpdateQueue({ maxStoredChars: 524288 });
    let saveInFlight = false;
    let imageElement = null;
    let canvasEl = null;
    let ctxCanvas = null;
    let canvasAreaEl = null;
    let currentPredictions = [];
    let imageReadyForCurrentItem = false;
    let imageLoadToken = 0;
    let activeImageLoadToken = 0;
    let imageLoadRetryCount = 0;
    let queueAdvanceStartedAt = 0;
    let queueAdvanceFromSerial = null;
    let queueAdvanceMeta = null;
    let autoSkipStreak = 0;
    let autoSkipHistory = [];
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
    let classifierWarmStartedAt = 0;
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
    let imagePrefetchState = new Map();
    let imagePrefetchRetryTimers = new Map();
    let imagePrefetchQueue = [];
    let imagePrefetchQueued = new Set();
    let imagePrefetchWorkerRunning = false;
    let cachedImagePathByIntent = new Map();
    let cachedImagePathLatest = new Map();
    let cachedImagePathProbeInFlight = new Set();
    let cachedImagePathProbeTs = new Map();
    let itemContextCache = new Map();
    let itemContextInFlight = new Map();
    let itemContextFetchAttempted = new Set();
    let refImagePrefetch = new Map();
    let refImagePrefetchState = new Map();
    let refImagePrefetchRetry = new Map();
    let refImageRetryTimers = new Map();
    let refRenderRefreshTimer = null;
    let detectPrefetch = new Map();
    let samMaskImageCache = new Map();
    let detectWarmFailures = new Map();
    let detectPrefetchInFlight = new Set();
    let detectPrefetchRunning = false;
    let detectPrefetchRequested = false;
    let detectPrefetchEpoch = 0;
    let lastDetectPrefetch = 0;
    let lastDetectRefinePrefetch = 0;
    let detectRefineInFlight = new Set();
    let detectAutoRefineInFlight = new Set();
    let detectPrimeInFlight = new Set();
    let detectExtraRefinedSerials = new Set();
    let boxesTouched = false;
    let pressedKeys = new Set();
    let movementHoldStartByKey = new Map();
    let movementFrameId = null;
    let lastMovementFrameTs = 0;
    let movementRemainderPx = {
        tlx: 0,
        tly: 0,
        brx: 0,
        bry: 0,
    };
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
    let queueLocalMissingByMode = {
        detect: { excluded: 0, sample: [] },
        classify: { excluded: 0, sample: [] },
        manual: { excluded: 0, sample: [] },
    };
    let modePositions = { detect: 0, classify: 0, manual: 0 };
    let detectWarmInFlight = new Set();
    let classifyWarmInFlight = new Set();
    let classifyPrimeInFlight = new Set();
    let classifyForegroundInFlight = new Map();
    let classifyRefRefreshTs = new Map();
    let classifyRefRefreshInFlight = new Map();
    let classifyNextCropPrefetchTs = new Map();
    let classifyPrefetchFailures = new Map();
    let classifyRefWarmStarted = false;
    let manualCandidates = [];
    let manualReviewIndices = [];
    let manualReviewCursor = 0;
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
    let loadCurrentItemModeToken = 0;
    let warmOverlayEl = null;
    let warmBarEl = null;
    let warmLabelEl = null;
    let warmSubEl = null;
    let classifyItemLoadOverlayToken = 0;
    let classifyItemLoadOverlayActive = false;
    let pendingUndoRestore = null;
    let lastClaimErrorKind = '';
    let lastClaimErrorMessage = '';
    let lastResumeWarmKickTs = 0;
    let pendingUnloadWarningAttached = false;
    let restoredPendingNoticeCount = 0;

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
        ensureLabelerRequestController();
        const restoredPending = attachPendingJournal();
        containerEl = document.getElementById('labeler-container');
        if (!containerEl) {
            console.warn('[Labeler] No labeler-container found');
            return;
        }
        if (labelerInitialized) {
            labelerActive = true;
            const restoredNotice = restoredPending || restoredPendingNoticeCount;
            if (restoredNotice > 0) {
                setStatus(`Restored ${restoredNotice} unsaved update${restoredNotice === 1 ? '' : 's'} from this tab.`);
                restoredPendingNoticeCount = 0;
            }
            updateInfo();
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
                        <div class="labeler-brand" aria-label="TomCat photo labeler">
                            <span class="labeler-brand-kicker">TomCat</span>
                            <span class="labeler-brand-title">Photo Labeler</span>
                        </div>
                    </div>
                    <div class="labeler-tabs" role="tablist" aria-label="Labeling mode">
                        <button class="labeler-tab active" data-mode="detect" role="tab" aria-selected="true">Detector</button>
                        <button class="labeler-tab" data-mode="classify" role="tab" aria-selected="false">Classifier</button>
                        <button class="labeler-tab" data-mode="manual" role="tab" aria-selected="false">Manual Review</button>
                    </div>
                    <div class="labeler-status" id="labeler-status" role="status" aria-live="polite">Loading...</div>
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
                        <div class="labeler-info-group labeler-info-left">
                            <div class="info-row"><span class="info-label">Serial:</span> <span id="info-serial">-</span></div>
                            <div class="info-row"><span class="info-label">Queue:</span> <span id="info-queue">-</span></div>
                            <div class="info-row"><span class="info-label">Boxes:</span> <span id="info-boxes">-</span></div>
                            <div class="info-row"><span class="info-label">Crop:</span> <span id="info-crop">-</span></div>
                            <div class="info-row"><span class="info-label">Zoom:</span> <span id="info-zoom">100%</span></div>
                        </div>
                        <div class="labeler-info-group labeler-info-right">
                            <div class="info-row"><span class="info-label">Date/Time:</span> <span id="info-datetime">-</span></div>
                            <div class="info-row"><span class="info-label">Author:</span> <span id="info-author">-</span></div>
                            <div class="info-row"><span class="info-label">Channel:</span> <span id="info-channel">-</span></div>
                        </div>
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
        const restoredNotice = restoredPending || restoredPendingNoticeCount;
        if (restoredNotice > 0) {
            setStatus(`Restored ${restoredNotice} unsaved update${restoredNotice === 1 ? '' : 's'} from this tab.`);
            restoredPendingNoticeCount = 0;
        }
        updateInfo();
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
        window.addEventListener('focus', () => {
            kickWarmOnResume('window-focus');
        });
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                kickWarmOnResume('tab-visible');
            }
        });

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
        loadCurrentItemModeToken += 1;
        void releaseCurrentClaim();
        labelerRequestController.abort();
        ensureLabelerRequestController();
        imageLoadToken += 1;
        activeImageLoadToken = imageLoadToken;
        imageReadyForCurrentItem = false;
        labelerMode = mode;
        _cancelClassifyItemLoadOverlayWait();
        setWarmOverlay(false);
        setStatus(`Switching to ${mode}...`);
        const listEl = document.getElementById('predictions-list');
        if (listEl) listEl.innerHTML = '';
        currentSerial = null;
        currentImageUrl = null;
        currentItem = null;
        currentBoxes = [];
        currentSamMaskTiles = [];
        currentLabels = [];
        currentCropIdx = 0;
        selectedBoxIdx = 0;
        currentPredictions = [];
        manualCandidates = [];
        clearCanvas();
        updateInfo();
        containerEl.querySelectorAll('.labeler-tab').forEach(tab => {
            const selected = tab.dataset.mode === mode;
            tab.classList.toggle('active', selected);
            tab.setAttribute('aria-selected', selected ? 'true' : 'false');
        });

        containerEl.classList.remove('labeler-mode-detect', 'labeler-mode-classify', 'labeler-mode-manual');
        containerEl.classList.add(`labeler-mode-${mode}`);

        document.getElementById('shortcuts-detect').style.display = mode === 'detect' ? 'block' : 'none';
        document.getElementById('shortcuts-classify').style.display = mode === 'classify' ? 'block' : 'none';
        document.getElementById('shortcuts-manual').style.display = mode === 'manual' ? 'block' : 'none';
        predictionsEl.style.display = (mode === 'classify' || mode === 'manual') ? 'block' : 'none';
        if (predictionsTitleEl) {
            predictionsTitleEl.textContent = mode === 'classify' ? 'Predictions' : (mode === 'manual' ? 'Candidates' : '');
            predictionsTitleEl.style.display = (mode === 'classify' || mode === 'manual') ? '' : 'none';
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

        stopMovementLoop();
        actionCooldowns.clear();

        togglePrefetchTimer(true);
        startWarmLoop();
        if (mode === 'manual') {
            void warmManualRefCache();
        }
        loadQueue({ forceRefresh: false });
    }

    async function warmClassifierRefCache() {
        if (classifyRefWarmStarted) return;
        classifyRefWarmStarted = true;
        try {
            const needsForce = (() => {
                try {
                    return localStorage.getItem('labelerClassifyRefCacheVersion') !== CLASSIFY_REF_CACHE_VERSION;
                } catch (e) {
                    return true;
                }
            })();
            await apiPost('/api/labeler/refs/warm', { force: needsForce });
            try {
                localStorage.setItem('labelerClassifyRefCacheVersion', CLASSIFY_REF_CACHE_VERSION);
            } catch (e) {
                // Ignore storage errors.
            }
        } catch (e) {
            classifyRefWarmStarted = false;
            console.warn('[Labeler] Classify ref cache warm failed:', e);
        }
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
            await apiPost('/api/labeler/manual_refs/warm', { force: needsForce });
            try {
                localStorage.setItem('labelerManualRefCacheVersion', MANUAL_REF_CACHE_VERSION);
            } catch (e) {
                //ignore storage errors
            }
        } catch (e) {
            console.warn('[Labeler] Manual ref cache warm failed:', e);
        }
    }

    function waitMs(ms) {
        return new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms || 0))));
    }

    async function apiGet(endpoint, opts = {}) {
        const signal = opts.sessionScoped === false ? opts.signal : (opts.signal || labelerRequestController.signal);
        return apiClient.get(endpoint, { ...opts, signal });
    }

    async function apiPost(endpoint, data, opts = {}) {
        const payload = data || {};
        const prefetch = !!payload.prefetch;
        const maxAttempts = prefetch ? 1 : Number(opts.maxAttempts || API_RETRY_MAX_ATTEMPTS);
        const signal = opts.sessionScoped === false ? opts.signal : (opts.signal || labelerRequestController.signal);
        return apiClient.post(endpoint, payload, { ...opts, maxAttempts, signal });
    }

    function normalizeItemContext(data, fallbackSerial = null) {
        const serial = Number.parseInt(String(data?.serial ?? fallbackSerial ?? ''), 10);
        if (!Number.isInteger(serial) || serial <= 0) return null;
        const ctx = {
            serial,
            author_id: String(data?.author_id || '').trim(),
            author_display_name: String(data?.author_display_name || '').trim(),
            channel_id: String(data?.channel_id || '').trim(),
            channel_name: String(data?.channel_name || '').trim(),
            guild_id: String(data?.guild_id || '').trim(),
            message_id: String(data?.message_id || '').trim(),
            timestamp: String(data?.timestamp || '').trim(),
            has_context: !!data?.has_context,
        };
        const hasAny =
            !!ctx.author_id
            || !!ctx.author_display_name
            || !!ctx.channel_id
            || !!ctx.channel_name
            || !!ctx.guild_id
            || !!ctx.message_id
            || !!ctx.timestamp
            || !!ctx.has_context;
        return hasAny ? ctx : null;
    }

    function mergeItemContext(target, ctx) {
        if (!target || !ctx) return target;
        [
            'author_id',
            'author_display_name',
            'channel_id',
            'channel_name',
            'guild_id',
            'message_id',
            'timestamp',
        ].forEach((field) => {
            const value = String(ctx?.[field] || '').trim();
            if (value) target[field] = value;
        });
        if (ctx?.has_context) target.has_context = true;
        return target;
    }

    function seedItemContextCacheFromItems(items) {
        if (!Array.isArray(items)) return;
        items.forEach((item) => {
            const ctx = normalizeItemContext(item, item?.serial);
            if (!ctx) return;
            const key = String(ctx.serial);
            const existing = itemContextCache.get(key) || {};
            itemContextCache.set(key, { ...existing, ...ctx });
            mergeItemContext(item, itemContextCache.get(key));
        });
    }

    function applyItemContextToKnownItems(serial, ctx) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0 || !ctx) return;
        const key = String(sn);
        const merged = { ...(itemContextCache.get(key) || {}), ...ctx, serial: sn };
        itemContextCache.set(key, merged);
        [detectQueue, classifyQueue, manualQueue].forEach((items) => {
            if (!Array.isArray(items)) return;
            items.forEach((item) => {
                if (Number(item?.serial) === sn) mergeItemContext(item, merged);
            });
        });
        if (Number(currentSerial) === sn && currentItem) {
            mergeItemContext(currentItem, merged);
        }
    }

    function itemAuthorText(item) {
        const display = String(item?.author_display_name || '').trim();
        if (display) return display;
        const authorId = String(item?.author_id || '').trim();
        return authorId || '-';
    }

    function itemChannelText(item) {
        const channelName = String(item?.channel_name || '').trim();
        if (channelName) return channelName;
        const channelId = String(item?.channel_id || '').trim();
        return channelId || '-';
    }

    function formatTimestampCentral(rawTs) {
        const text = String(rawTs || '').trim();
        if (!text) return '-';
        const dt = new Date(text);
        if (Number.isNaN(dt.getTime())) return '-';
        try {
            const parts = new Intl.DateTimeFormat('en-US', {
                timeZone: 'America/Chicago',
                month: 'numeric',
                day: 'numeric',
                year: '2-digit',
                hour: 'numeric',
                minute: '2-digit',
                hour12: true,
            }).formatToParts(dt);
            const partMap = {};
            parts.forEach((part) => {
                if (part && part.type && !(part.type in partMap)) {
                    partMap[part.type] = String(part.value || '');
                }
            });
            const month = String(partMap.month || '').trim();
            const day = String(partMap.day || '').trim();
            const year = String(partMap.year || '').trim();
            const hour = String(partMap.hour || '').trim();
            const minute = String(partMap.minute || '').trim();
            const dayPeriod = String(partMap.dayPeriod || '').trim().toLowerCase();
            if (!month || !day || !year || !hour || !minute || !dayPeriod) return '-';
            return `${month}/${day}/${year} ${hour}:${minute}${dayPeriod}`;
        } catch (e) {
            return '-';
        }
    }

    function itemTimestampText(item) {
        return formatTimestampCentral(item?.timestamp);
    }

    async function fetchItemContext(serial, opts = {}) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return null;
        const key = String(sn);
        if (itemContextInFlight.has(key)) {
            return itemContextInFlight.get(key);
        }
        const suffix = opts.force ? '?force=1' : '';
        const promise = (async () => {
            try {
                const data = await apiGet(`/api/labeler/context/${sn}${suffix}`);
                const ctx = normalizeItemContext(data, sn) || { serial: sn, has_context: !!data?.has_context };
                itemContextFetchAttempted.add(key);
                applyItemContextToKnownItems(sn, ctx);
                return itemContextCache.get(key) || ctx;
            } finally {
                itemContextInFlight.delete(key);
            }
        })();
        itemContextInFlight.set(key, promise);
        return promise;
    }

    function ensureCurrentItemContext(item = currentItem) {
        const sn = Number.parseInt(String(item?.serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return;
        const key = String(sn);
        const cached = itemContextCache.get(key);
        if (cached) {
            mergeItemContext(item, cached);
        }
        const hasAuthor = !!String(item?.author_display_name || '').trim();
        const hasChannel = !!String(item?.channel_name || '').trim();
        if (hasAuthor && hasChannel) return;
        if (itemContextFetchAttempted.has(key)) return;
        void fetchItemContext(sn).then((ctx) => {
            if (!ctx) return;
            if (Number(currentSerial) === sn) updateInfo();
        }).catch(() => {
            // Item context fetch is best-effort.
        });
    }

    async function warmCachedImage(serial, opts = {}) {
        if (!serial) return false;
        const intent = _cachedImageIntent(opts.intent || 'prefetch');
        try {
            const resp = await fetch(buildCachedImageUrl(serial, { intent, cacheBust: Date.now() }), {
                credentials: 'include',
                redirect: 'manual',
                cache: 'no-store',
                signal: labelerRequestController.signal,
            });
            _recordCachedImagePathProbe(serial, intent, {
                path: String(resp.headers.get('X-Labeler-Image-Path') || ''),
                cache: String(resp.headers.get('X-Labeler-Cache') || ''),
                status: Number(resp.status || 0),
                redirected: !!resp.redirected,
                source: 'warm',
            });
            try {
                if (resp.body && typeof resp.body.cancel === 'function') {
                    await resp.body.cancel();
                }
            } catch (e) {
                // Best-effort cancellation for diagnostics.
            }
            return resp.ok || (resp.status >= 300 && resp.status < 400);
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
        const key = getPredFocusCacheKey(item, hasFocusCropIdx ? focusCropIdxNum : null);
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
        if (hasFocusCropIdx) {
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

    function resetAutoSkipStreak() {
        autoSkipStreak = 0;
        autoSkipHistory = [];
    }

    function autoSkipCurrentItem(reason, extra = {}) {
        const rec = {
            reason: String(reason || 'unknown'),
            mode: String(labelerMode || ''),
            serial: Number(currentSerial || 0),
            queue_index: Number(queueIndex || 0),
            queue_pos: Number(queueIndex || 0) + 1,
            queue_total: Number(queueTotal || 0),
            ts: Date.now(),
            ...extra,
        };
        autoSkipStreak = Math.max(1, Number(autoSkipStreak || 0) + 1);
        autoSkipHistory.push(rec);
        if (autoSkipHistory.length > 8) autoSkipHistory = autoSkipHistory.slice(-8);
        void postUiDiag('auto_skip', {
            ...rec,
            streak: autoSkipStreak,
        });

        const shouldHalt = (
            labelerMode === 'classify'
            && autoSkipStreak >= CLASSIFY_AUTO_SKIP_STREAK_LIMIT
        );
        if (shouldHalt) {
            queueAdvanceStartedAt = 0;
            queueAdvanceFromSerial = null;
            const recentSerials = autoSkipHistory
                .map((r) => Number(r?.serial || 0))
                .filter((sn) => Number.isInteger(sn) && sn > 0)
                .map((sn) => `sn${sn}`)
                .join(', ');
            setWarmOverlay(false);
            setStatus(
                `Paused after ${autoSkipStreak} auto-skips. `
                + `Recent: ${recentSerials || 'unknown'}. Refresh or inspect this item.`
            );
            void postUiDiag('auto_skip_halt', {
                ...rec,
                streak: autoSkipStreak,
                recent: autoSkipHistory.slice(-CLASSIFY_AUTO_SKIP_STREAK_LIMIT),
            });
            return;
        }

        advanceQueue();
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
            }, { timeoutMs: 6000, maxAttempts: 1, sessionScoped: false });
        } catch (e) {
            //Ignore release failures; server TTL will expire stale claims.
        }
    }

    async function claimQueueItem(item, mode) {
        if (!item || !item.serial) return 'error';
        prunePrefetchedClaims();
        const key = claimCacheKey(mode, item.serial);
        const claimStartedAt = Date.now();
        if (key && currentClaim && currentClaim.mode === mode && String(currentClaim.serial) === String(item.serial)) {
            lastClaimErrorKind = '';
            lastClaimErrorMessage = '';
            return 'granted';
        }
        const prefetched = key ? prefetchedClaims.get(key) : null;
        if (prefetched && !prefetched.inFlight) {
            prefetchedClaims.delete(key);
            currentClaim = { mode, serial: item.serial };
            startClaimHeartbeat();
            lastClaimErrorKind = '';
            lastClaimErrorMessage = '';
            return 'granted';
        }
        try {
            const data = await apiPost('/api/labeler/claim', {
                action: 'acquire',
                mode,
                serial: item.serial,
            }, { timeoutMs: CLAIM_ACQUIRE_TIMEOUT_MS, maxAttempts: 1 });
            const claimMs = Math.max(0, Date.now() - claimStartedAt);
            if (claimMs >= 1200) {
                void postUiDiag('claim_acquire_slow', {
                    serial: Number(item?.serial || 0) || null,
                    mode: String(mode || ''),
                    ms: claimMs,
                    timeout_ms: CLAIM_ACQUIRE_TIMEOUT_MS,
                    prefetched: !!(prefetched && !prefetched.inFlight),
                });
            }
            if (data && data.granted) {
                currentClaim = { mode, serial: item.serial };
                startClaimHeartbeat();
                lastClaimErrorKind = '';
                lastClaimErrorMessage = '';
                return 'granted';
            }
            lastClaimErrorKind = '';
            lastClaimErrorMessage = '';
            return 'denied';
        } catch (e) {
            const msg = String(e && e.message || '').trim();
            const status = getApiErrorStatus(e);
            const claimMs = Math.max(0, Date.now() - claimStartedAt);
            lastClaimErrorMessage = msg || 'Claim request failed';
            void postUiDiag('claim_acquire_error', {
                serial: Number(item?.serial || 0) || null,
                mode: String(mode || ''),
                ms: claimMs,
                status: Number(status || 0) || null,
                timeout_ms: CLAIM_ACQUIRE_TIMEOUT_MS,
                error: String(msg || ''),
            });
            if (status === 401 || /missing session user/i.test(msg)) {
                lastClaimErrorKind = 'auth';
                return 'auth';
            }
            if (isTransientApiError(e) || /networkerror|network request failed|failed to fetch|load failed/i.test(msg)) {
                lastClaimErrorKind = 'network';
                return 'retry';
            }
            lastClaimErrorKind = 'error';
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
        }, { timeoutMs: CLAIM_PRECLAIM_TIMEOUT_MS, maxAttempts: 1 })
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

    function preclaimAhead(mode, startIdx, count = CLAIM_PRECLAIM_AHEAD_COUNT) {
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
        seedItemContextCacheFromItems(payload.queue || []);
        const localMissing = {
            excluded: Math.max(0, Number(payload.local_missing_excluded || 0) || 0),
            sample: Array.isArray(payload.local_missing_sample)
                ? payload.local_missing_sample.slice(0, 50).map((n) => Number(n)).filter((n) => Number.isInteger(n) && n > 0)
                : [],
        };
        if (mode === 'detect') {
            detectQueue = payload.queue || [];
            detectQueueTotal = typeof payload.total === 'number' ? payload.total : detectQueue.length;
            queueLocalMissingByMode.detect = localMissing;
            return;
        }
        if (mode === 'classify') {
            classifyQueue = payload.queue || [];
            classifyQueueTotal = typeof payload.total === 'number' ? payload.total : classifyQueue.length;
            queueLocalMissingByMode.classify = localMissing;
            return;
        }
        if (mode === 'manual') {
            manualQueue = payload.queue || [];
            manualQueueTotal = typeof payload.total === 'number' ? payload.total : manualQueue.length;
            queueLocalMissingByMode.manual = localMissing;
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

    function kickWarmOnResume(reason = '') {
        if (!labelerActive) return;
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
        const now = Date.now();
        if ((now - Number(lastResumeWarmKickTs || 0)) < 1200) return;
        lastResumeWarmKickTs = now;

        // Resume path: browser timers are often throttled in background tabs.
        // Kick warmers immediately when the tab/window becomes active again.
        void refreshQueueMode(labelerMode, { force: false })
            .then(() => {
                if (!labelerActive) return;
                if (labelerMode === 'classify') {
                    prefetchPredictions();
                    primeHotNextClassifyItem();
                    if (Array.isArray(currentPredictions) && currentPredictions.length) {
                        prefetchDisplayRefsForCrop(currentPredictions, currentCropIdx, 'high');
                        const rs = currentSerial;
                        const rk = getPredCacheKey(currentItem);
                        if (rs != null && rk) {
                            void _refreshCurrentPredictionRefs(rs, rk, 'Loading additional reference photos...');
                        }
                    }
                } else if (labelerMode === 'detect') {
                    prefetchDetection();
                    prefetchDetectionRefine();
                    primeHotNextDetectItem();
                } else if (labelerMode === 'manual') {
                    prefetchManualCandidates();
                }
                prefetchImages();
            })
            .catch(() => {
                // Resume warm kick is best-effort.
            });
        // Keep warm path focused on the active mode.
        void runWarmTick();
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

    function queueLocalMissingForMode(mode) {
        const rec = queueLocalMissingByMode && queueLocalMissingByMode[mode];
        if (!rec || typeof rec !== 'object') {
            return { excluded: 0, sample: [] };
        }
        return {
            excluded: Math.max(0, Number(rec.excluded || 0) || 0),
            sample: Array.isArray(rec.sample) ? rec.sample : [],
        };
    }

    function syncActiveQueuePointersAfterRefresh() {
        const activeQueue = queueForMode(labelerMode);
        if (Array.isArray(activeQueue)) {
            queue = activeQueue;
        }
        queueTotal = queueTotalForMode(labelerMode);

        // After a refresh the queue may have shrunk (e.g. saved items removed).
        // Re-locate the current serial in the new queue so the user continues
        // where they left off instead of jumping to the end.
        if (currentSerial && queue.length) {
            const serialNum = Number(currentSerial);
            const foundIdx = queue.findIndex((item) => Number(item?.serial) === serialNum);
            if (foundIdx >= 0) {
                queueIndex = foundIdx;
                modePositions[labelerMode] = foundIdx;
                return;
            }
        }

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
        // Refresh only active mode to avoid cross-mode queue churn.
        await refreshQueueMode(labelerMode, { force });
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
        return _predictionLoadedRefsAtTargetForCrop(currentPredictions, currentCropIdx);
    }

    function startCurrentClassifyItemLoadOverlayWait() {
        if (labelerMode !== 'classify' || !currentItem || !currentBoxes.length) return;
        const token = ++classifyItemLoadOverlayToken;
        classifyItemLoadOverlayActive = true;
        const requestSerial = currentSerial;
        const requestKey = getPredCacheKey(currentItem);
        const startedAt = Date.now();
        const expire = startedAt + CLASSIFY_ITEM_DISPLAY_READY_TIMEOUT_MS;

        const id = setInterval(() => {
            if (token !== classifyItemLoadOverlayToken) {
                clearInterval(id);
                return;
            }
            if (requestSerial !== currentSerial || requestKey !== getPredCacheKey(currentItem)) {
                clearInterval(id);
                return;
            }
            if (_isCurrentClassifyItemDisplayReady()) {
                if (token === classifyItemLoadOverlayToken) {
                    classifyItemLoadOverlayActive = false;
                    setWarmOverlay(false);
                }
                clearInterval(id);
                return;
            }
            if (Date.now() > expire) {
                clearInterval(id);
                setWarmOverlay(false);
                return;
            }
            const elapsed = Math.max(0, Date.now() - startedAt);
            const hasImage = isClassifyImageReady(currentItem);
            const hasPreds = _predictionHasOptionsForCrop(currentPredictions, currentCropIdx);
            const cov = _predictionLoadedRefCoverageForCrop(currentPredictions, currentCropIdx);

            let title = initialClassifyWarmDone ? 'Loading classifier item...' : 'Preparing classifier queue...';
            let subtitle = '';
            let pct = 0;

            if (!hasImage && !hasPreds) {
                subtitle = 'Fetching image and passing to DINOv3 API...';
                pct = 0.2;
            } else if (!hasImage) {
                subtitle = 'Waiting for image to render...';
                pct = Math.max(0.2, Math.min(0.7, elapsed / CLASSIFY_ITEM_DISPLAY_READY_TIMEOUT_MS));
            } else if (!hasPreds) {
                subtitle = 'Running DINOv3 API analysis...';
                pct = 0.5;
            } else {
                const refsPct = cov.targetCount > 0 ? Math.max(0, Math.min(1, cov.coverage)) : 0;
                const refText = cov.targetCount > 0 ? `${cov.candidatesWithRefs}/${cov.targetCount}` : 'loading';
                const covPctText = cov.targetCount > 0 ? ` (${Math.round(refsPct * 100)}% coverage)` : '';
                pct = 0.7 + (refsPct * 0.3);

                if (cov.coverage >= 1.0) {
                    subtitle = `All reference photos loaded`;
                } else {
                    subtitle = `Loading reference photos: ${refText} cats${covPctText}`;
                }
            }

            setWarmOverlay(true, title, subtitle, pct);
        }, 50);
    }

    function isDetectEntryReady(entry) {
        if (!entry || typeof entry !== 'object') return false;
        if (entry.ready === true) return true;
        //Backwards-compat: legacy cache entries only tracked non-empty refined boxes.
        return typeof entry.refined === 'string' && entry.refined.length > 0;
    }

    function isDetectEntryUsable(entry) {
        if (!entry || typeof entry !== 'object') return false;
        return isDetectEntryReady(entry);
    }

    function compactBoxDeltaDiag(delta) {
        if (!delta || typeof delta !== 'object') return null;
        return {
            pairs: Number(delta.pairs || 0),
            shifted: Number(delta.shifted || 0),
            max_center_shift_px: Number(Number(delta.max_center_shift_px || 0).toFixed(2)),
            max_edge_shift_px: Number(Number(delta.max_edge_shift_px || 0).toFixed(2)),
            max_area_ratio: Number(Number(delta.max_area_ratio || 1).toFixed(3)),
        };
    }

    function compactSamSummary(summary) {
        if (!summary || typeof summary !== 'object') return null;
        const selected = (summary.selected && typeof summary.selected === 'object') ? summary.selected : {};
        const samples = Array.isArray(summary.samples) ? summary.samples.slice(0, 2) : [];
        return {
            passes: Number(summary.passes || 0),
            boxes: Number(summary.boxes || 0),
            accepted_boxes: Number(summary.accepted_boxes || 0),
            fallback_boxes: Number(summary.fallback_boxes || 0),
            clipped_boxes: Number(summary.clipped_boxes || 0),
            guard_reject_boxes: Number(summary.guard_reject_boxes || 0),
            candidate_masks: Number(summary.candidate_masks || 0),
            accepted_masks: Number(summary.accepted_masks || 0),
            selected_tight: Number(selected.tight || 0),
            selected_iou: Number(selected.iou || 0),
            selected_fallback: Number(selected.fallback || 0),
            max_outside_guard_ratio: Number(Number(summary.max_outside_guard_ratio || 0).toFixed(3)),
            max_detector_coverage: Number(Number(summary.max_detector_coverage || 0).toFixed(3)),
            max_area_ratio: Number(Number(summary.max_area_ratio || 1).toFixed(3)),
            samples,
        };
    }

    function samSummaryAcceptedBoxes(summary) {
        if (!summary || typeof summary !== 'object') return 0;
        return Number(summary.accepted_boxes || 0);
    }

    function detectRefineSucceeded(refineDiag, boxCount = 0) {
        if (!refineDiag || typeof refineDiag !== 'object') {
            return Number(boxCount || 0) === 0;
        }
        const samSummary = (refineDiag.sam_summary && typeof refineDiag.sam_summary === 'object')
            ? refineDiag.sam_summary
            : null;
        if (Number(boxCount || 0) === 0) return true;
        return samSummaryAcceptedBoxes(samSummary) > 0;
    }

    function buildDetectEntryDiag(serial) {
        const key = String(serial || '');
        if (!key) return null;
        const entry = detectPrefetch.get(key);
        const meta = (entry && entry.meta && typeof entry.meta === 'object') ? entry.meta : {};
        const detectDiag = (meta.detect && typeof meta.detect === 'object') ? meta.detect : null;
        const refineDiag = (meta.refine && typeof meta.refine === 'object') ? meta.refine : null;
        const rawBoxes = parseYoloBoxes(String(entry?.raw || ''));
        const refinedBoxes = parseYoloBoxes(String(entry?.refined || entry?.raw || ''));
        return {
            present: !!entry,
            ready: !!isDetectEntryReady(entry),
            usable: !!isDetectEntryUsable(entry),
            extra_refined: detectExtraRefinedSerials.has(key),
            sam_overlay_polygons: Array.isArray(entry?.samOverlay) ? entry.samOverlay.filter((poly) => Array.isArray(poly) && poly.length >= 3).length : 0,
            sam_mask_tiles: Array.isArray(entry?.samMaskTiles) ? entry.samMaskTiles.filter((tile) => tile && typeof tile === 'object' && String(tile.png_b64 || '').trim()).length : 0,
            warm_blocked: isDetectWarmBlocked(key),
            raw_box_count: rawBoxes.length,
            refined_box_count: refinedBoxes.length,
            inflight: {
                warm: detectWarmInFlight.has(key),
                prefetch: detectPrefetchInFlight.has(key),
                refine_prefetch: detectRefineInFlight.has(key),
                auto_refine: detectAutoRefineInFlight.has(key),
                prime: detectPrimeInFlight.has(key),
            },
            detect: detectDiag ? {
                compute_rid: String(detectDiag.compute_rid || ''),
                served_rid: String(detectDiag.served_rid || ''),
                cache_hit: !!detectDiag.cache_hit,
                image_source: String(detectDiag.image_source || ''),
                sam_refined: !!detectDiag.sam_refined,
                raw_box_count: Number(detectDiag.raw_box_count || 0),
                out_box_count: Number(detectDiag.out_box_count || 0),
                box_delta: compactBoxDeltaDiag(detectDiag.box_delta),
                sam_summary: compactSamSummary(detectDiag.sam_summary),
            } : null,
            refine: refineDiag ? {
                compute_rid: String(refineDiag.compute_rid || ''),
                served_rid: String(refineDiag.served_rid || ''),
                cache_hit: !!refineDiag.cache_hit,
                input_box_count: Number(refineDiag.input_box_count || 0),
                out_box_count: Number(refineDiag.out_box_count || 0),
                box_delta: compactBoxDeltaDiag(refineDiag.box_delta),
                sam_summary: compactSamSummary(refineDiag.sam_summary),
            } : null,
        };
    }

    function buildDetectPipelineDiag(serial) {
        return {
            target: buildDetectEntryDiag(serial),
            cache_entries: detectPrefetch.size,
            extra_refined_entries: detectExtraRefinedSerials.size,
            warm_failures: detectWarmFailures.size,
            inflight: {
                warm: detectWarmInFlight.size,
                prefetch: detectPrefetchInFlight.size,
                refine_prefetch: detectRefineInFlight.size,
                auto_refine: detectAutoRefineInFlight.size,
                prime: detectPrimeInFlight.size,
            },
        };
    }

    function buildClassifyPipelineDiag(item) {
        const key = getPredCacheKey(item);
        const rows = key ? predCache.get(key) : null;
        const idx = item ? _targetCropIdxForItem(item) : 0;
        const coverage = _predictionLoadedRefCoverageForCrop(rows, idx);
        return {
            target: {
                key: String(key || ''),
                pred_cache_hit: !!(key && predCache.has(key)),
                warm_ready: !!(item && _classifyItemWarmReady(item)),
                focus_crop_idx: Number(idx || 0),
                pred_rows: Array.isArray(rows) ? rows.length : 0,
                ref_coverage: {
                    candidates_with_refs: Number(coverage.candidatesWithRefs || 0),
                    target_count: Number(coverage.targetCount || 0),
                    coverage: Number(Number(coverage.coverage || 0).toFixed(3)),
                },
            },
            inflight: {
                warm: key ? classifyWarmInFlight.has(key) : false,
                prime: key ? classifyPrimeInFlight.has(key) : false,
                foreground: key ? classifyForegroundInFlight.has(key) : false,
                prefetch: key ? prefetchInFlight.has(key) : false,
                ref_refresh: key ? classifyRefRefreshInFlight.has(key) : false,
                ref_images_loading: Number(refImagesLoadingCount || 0),
                ref_queue: Number(refImageFetchQueue.length || 0),
                ref_queued: Number(refImageFetchQueued.size || 0),
            },
        };
    }

    // This function is no longer needed. The interval inside ensureClassifyItemReady handles it now.
    function showClassifierWarmOverlay(title, subtitle, pct) {
        setWarmOverlay(true, title, subtitle, pct);
    }

    // Hallucinated progress ticker removed.
    // The overlay is now updated synchronously by ensureClassifyItemReady as state changes.

    function isTransientApiError(err) {
        if (err && typeof err.retryable === 'boolean') return err.retryable;
        const msg = String(err && err.message || '');
        if (isNoImageApiError(err)) return true;
        return /\b(421|425|429|502|503|504)\b/.test(msg);
    }

    function getApiErrorStatus(err) {
        const structuredStatus = Number(err?.status || 0);
        if (Number.isFinite(structuredStatus) && structuredStatus > 0) return structuredStatus;
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

    function setDetectEntry(key, raw = '', refined = '', ready = false, meta = null, samOverlay = null, samMaskTiles = null) {
        const k = String(key || '');
        const nextRaw = String(raw || '');
        const nextRefined = String(refined || '');
        const prev = detectPrefetch.get(k);
        const prevMeta = (prev && prev.meta && typeof prev.meta === 'object') ? prev.meta : null;
        const prevOverlay = Array.isArray(prev?.samOverlay) ? prev.samOverlay : [];
        const prevMaskTiles = Array.isArray(prev?.samMaskTiles) ? prev.samMaskTiles : [];
        const nextMeta = (meta && typeof meta === 'object')
            ? { ...(prevMeta || {}), ...meta }
            : prevMeta;
        const nextOverlay = (samOverlay === null)
            ? prevOverlay
            : normalizeSamPolygons(samOverlay);
        const nextMaskTiles = (samMaskTiles === null)
            ? prevMaskTiles
            : normalizeSamMaskTiles(samMaskTiles);
        detectPrefetch.set(k, {
            raw: nextRaw,
            refined: nextRefined,
            ready: !!ready,
            meta: nextMeta,
            samOverlay: nextOverlay,
            samMaskTiles: nextMaskTiles,
        });
        if (
            !prev
            || String(prev.raw || '') !== nextRaw
            || String(prev.refined || '') !== nextRefined
        ) {
            detectExtraRefinedSerials.delete(k);
        }
        detectWarmFailures.delete(k);
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
            if (isDetectItemWarmReady(item)) ready++;
        }
        return ready;
    }

    function isDetectItemWarmReady(item) {
        const key = String(item?.serial || '');
        if (!key) return false;
        const entry = detectPrefetch.get(key);
        if (!isDetectEntryReady(entry)) return false;
        const boxes = parseYoloBoxes(String(entry?.refined || entry?.raw || ''));
        return boxes.length === 0 || detectExtraRefinedSerials.has(key);
    }

    function _classifyItemWarmReady(item) {
        const key = getPredCacheKey(item);
        if (!key) return false;
        const rows = predCache.get(key);
        if (!Array.isArray(rows) || !rows.length) return false;
        const idx = _targetCropIdxForItem(item);
        return _predictionHasOptionsForCrop(rows, idx);
    }

    function _classifyItemRefsReady(item) {
        const key = getPredCacheKey(item);
        if (!key) return false;
        const rows = predCache.get(key);
        if (!Array.isArray(rows) || !rows.length) return false;
        const idx = _targetCropIdxForItem(item);
        return _predictionLoadedRefsAtTargetForCrop(rows, idx);
    }

    function _classifyItemFullyReady(item) {
        if (!item || !item.serial) return false;
        return (
            isPrefetchedImageReady(item.serial)
            && _classifyItemWarmReady(item)
            && _classifyItemRefsReady(item)
        );
    }

    function compactRefImageQueueDiag() {
        let ready = 0;
        let loading = 0;
        let queued = 0;
        let error = 0;
        refImagePrefetchState.forEach((state) => {
            const value = String(state || '');
            if (value === 'ready') ready += 1;
            else if (value === 'loading') loading += 1;
            else if (value === 'queued') queued += 1;
            else if (value === 'error') error += 1;
        });
        return {
            ready,
            loading,
            queued,
            error,
            active_loads: Number(refImagesLoadingCount || 0),
            queue_depth: Number(refImageFetchQueue.length || 0),
            concurrency: Number(REF_IMAGE_CONCURRENCY || 0),
        };
    }

    function prefetchWarmRefsForItem(item, results, priority = 'normal') {
        const idx = _targetCropIdxForItem(item);
        const rows = Array.isArray(results) ? results : [];
        // Warm refs for ALL crops in the item, not just the focus crop.
        // The API already returns refs for every crop; only the focus crop
        // gets 'high' priority while the rest use 'normal' so the current
        // crop's refs still land first.
        const totalCrops = rows.length || 1;
        prefetchRefsFromResults(results, {
            priority,
            focusCropIdx: idx,
            maxCrops: totalCrops,
            maxCandidates: CLASSIFY_WARM_PREFETCH_MAX_CANDIDATES,
            maxRefsPerCandidate: CLASSIFY_WARM_PREFETCH_MAX_REFS,
        });
    }

    function prefetchDisplayRefsForCrop(results, cropIdx, priority = 'high') {
        const rows = Array.isArray(results) ? results : [];
        if (!rows.length) return;
        const idx = Math.max(0, Math.min(Number(cropIdx || 0), rows.length - 1));
        prefetchRefsFromResults(rows, {
            priority,
            focusCropIdx: idx,
            maxCrops: 1,
            maxCandidates: CLASSIFY_DISPLAY_PREFETCH_MAX_CANDIDATES,
            maxRefsPerCandidate: CLASSIFY_DISPLAY_PREFETCH_MAX_REFS,
        });
    }

    function prefetchDisplayRefsForItem(item, results, priority = 'high') {
        const idx = _targetCropIdxForItem(item);
        const rows = Array.isArray(results) ? results : [];
        // Prefetch display-quality refs for the focus crop at requested
        // priority, then warm-level refs for remaining crops at 'normal'
        // so every crop in a multi-crop image starts loading early.
        prefetchDisplayRefsForCrop(results, idx, priority);
        if (rows.length > 1) {
            prefetchRefsFromResults(results, {
                priority: 'normal',
                focusCropIdx: idx,
                maxCrops: rows.length,
                maxCandidates: CLASSIFY_WARM_PREFETCH_MAX_CANDIDATES,
                maxRefsPerCandidate: CLASSIFY_WARM_PREFETCH_MAX_REFS,
            });
        }
    }

    function _nextPendingClassifyCropIdx(startIdx = currentCropIdx) {
        if (!Array.isArray(currentBoxes) || !currentBoxes.length) return -1;
        const start = Math.max(-1, Math.min(Number(startIdx || 0), currentBoxes.length - 1));
        for (let i = start + 1; i < currentBoxes.length; i++) {
            if (String(currentLabels[i] || '').trim()) continue;
            return i;
        }
        return -1;
    }

    async function prefetchUpcomingClassifyCrop(fromCropIdx = currentCropIdx) {
        if (labelerMode !== 'classify' || !currentItem || !currentBoxes.length) return;

        const requestSerial = Number(currentSerial || 0);
        const requestKey = getPredCacheKey(currentItem);
        if (!requestKey) return;

        // Fan out to ALL remaining pending crops simultaneously. Previously this
        // chained one crop at a time (crop N+1 → await API → crop N+2 → await API...),
        // meaning the last crop in a multi-crop photo never started warming until every
        // preceding crop's API call had resolved. Now each crop's warm runs in parallel:
        // first remaining crop gets 'high' ref priority, subsequent get 'normal'.
        let scanIdx = fromCropIdx;
        let refPriority = 'high';
        while (true) {
            const nextIdx = _nextPendingClassifyCropIdx(scanIdx);
            if (nextIdx < 0 || nextIdx >= currentBoxes.length) break;

            void (async (cropIdx, priority) => {
                const focusKey = getPredFocusCacheKey(currentItem, cropIdx);
                if (!focusKey) return;

                const existingRows = predCache.get(requestKey) || currentPredictions || [];
                if (_predictionRefsAtTargetForCrop(existingRows, cropIdx)) {
                    prefetchDisplayRefsForCrop(existingRows, cropIdx, priority);
                    return;
                }

                const now = Date.now();
                const lastTs = Number(classifyNextCropPrefetchTs.get(focusKey) || 0);
                if ((now - lastTs) < CLASSIFY_NEXT_CROP_PREFETCH_COOLDOWN_MS) return;
                classifyNextCropPrefetchTs.set(focusKey, now);

                try {
                    const retry = await postClassifyIdentify(
                        currentItem,
                        currentBoxes.map((b) => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                        {
                            prefetch: true,
                            maxAttempts: 1,
                            timeoutMs: Math.min(API_PREFETCH_TIMEOUT_MS, 12000),
                            focusCropIdx: cropIdx,
                        },
                    );
                    const latestRows = predCache.get(requestKey) || existingRows;
                    const mergedPreds = mergePredictionResults(latestRows, retry?.results || [], cropIdx);
                    predCache.set(requestKey, mergedPreds);
                    prefetchDisplayRefsForCrop(mergedPreds, cropIdx, priority);
                    if (
                        requestSerial === Number(currentSerial || 0)
                        && requestKey === getPredCacheKey(currentItem)
                    ) {
                        currentPredictions = mergedPreds;
                    }
                } catch (e) {
                    // Best-effort; foreground path remains authoritative.
                }
            })(nextIdx, refPriority);

            refPriority = 'normal';
            scanIdx = nextIdx;
        }
    }

    function countWarmReadyClassify(windowItems) {
        let ready = 0;
        for (const item of windowItems) {
            if (_classifyItemFullyReady(item)) ready += 1;
        }
        return ready;
    }

    async function ensureInitialDetectWarmGate() {
        if (initialDetectWarmDone || labelerMode !== 'detect') return;

        let windowItems = detectQueue.slice(0, Math.min(detectQueue.length, Math.max(2, SESSION_WARM_TARGET)));
        if (!windowItems.length) {
            initialDetectWarmDone = true;
            setWarmOverlay(false);
            return;
        }

        // Force the first item to load synchronously so the UI is ready
        const first = windowItems[0];
        if (!isDetectItemWarmReady(first)) {
            setWarmOverlay(true, 'Preparing first image...', 'Running detector', 0);
            await ensureDetectItemReady(first, true, true);
        }

        initialDetectWarmDone = true;
        setWarmOverlay(false);
        // Let the background primeHotNextDetectItem handle the rest of the queue
    }

    async function ensureInitialClassifyWarmGate() {
        if (initialClassifyWarmDone || labelerMode !== 'classify') return;

        const targetWindow = Math.max(INITIAL_CLASSIFY_WARM_WINDOW, INITIAL_CLASSIFY_WARM_MIN);
        let windowItems = classifyQueue.slice(0, Math.min(classifyQueue.length, targetWindow));
        if (!windowItems.length) {
            initialClassifyWarmDone = true;
            setWarmOverlay(false);
            return;
        }
        const targetReady = Math.max(1, Math.min(windowItems.length, INITIAL_CLASSIFY_WARM_MIN));
        const activeItems = windowItems.slice(0, targetReady);
        const deadline = Date.now() + INITIAL_CLASSIFY_WARM_TIMEOUT_MS;
        let lastDiagAt = 0;

        while (Date.now() < deadline && labelerActive && labelerMode === 'classify') {
            activeItems.forEach((item, idx) => {
                if (!item || !item.serial) return;
                queueImagePrefetchSerial(item.serial, { priority: idx === 0 ? 'high' : 'normal' });
                const key = getPredCacheKey(item);
                const rows = key ? predCache.get(key) : null;
                if (Array.isArray(rows) && rows.length) {
                    prefetchDisplayRefsForItem(item, rows, idx === 0 ? 'high' : 'normal');
                }
                if (
                    key
                    && !_classifyItemWarmReady(item)
                    && !classifyWarmInFlight.has(key)
                    && !classifyForegroundInFlight.has(key)
                    && !classifyPrimeInFlight.has(key)
                ) {
                    void ensureClassifyItemReady(item, false, true);
                }
            });

            const readyCount = countWarmReadyClassify(activeItems);
            const elapsed = Math.max(0, INITIAL_CLASSIFY_WARM_TIMEOUT_MS - Math.max(0, deadline - Date.now()));
            const pct = Math.max(
                Math.min(0.95, readyCount / Math.max(1, targetReady)),
                Math.min(0.15, elapsed / INITIAL_CLASSIFY_WARM_TIMEOUT_MS),
            );
            setWarmOverlay(
                true,
                'Preparing classifier queue...',
                `${readyCount}/${targetReady} items ready for grading`,
                pct,
            );
            if (readyCount >= targetReady) {
                initialClassifyWarmDone = true;
                setWarmOverlay(false);
                void postUiDiag('classify_initial_warm_done', {
                    target_ready: targetReady,
                    ready_items: readyCount,
                    queue_window: activeItems.length,
                    ref_queue: compactRefImageQueueDiag(),
                });
                return;
            }

            const now = Date.now();
            if ((now - lastDiagAt) >= READY_WAIT_DIAG_INTERVAL_MS) {
                lastDiagAt = now;
                void postUiDiag('classify_initial_warm_wait', {
                    target_ready: targetReady,
                    ready_items: readyCount,
                    queue_window: activeItems.length,
                    ref_queue: compactRefImageQueueDiag(),
                    items: activeItems.map((item) => ({
                        serial: Number(item?.serial || 0) || null,
                        image_ready: !!isPrefetchedImageReady(item?.serial),
                        preds_ready: !!_classifyItemWarmReady(item),
                        refs_ready: !!_classifyItemRefsReady(item),
                    })),
                });
            }
            await waitMs(120);
        }

        initialClassifyWarmDone = true;
        setWarmOverlay(false);
        void postUiDiag('classify_initial_warm_timeout', {
            target_ready: targetReady,
            ready_items: countWarmReadyClassify(activeItems),
            queue_window: activeItems.length,
            ref_queue: compactRefImageQueueDiag(),
        });
    }

    async function waitForCurrentItemReady(item) {
        if (!item) return false;
        if (labelerMode === 'detect') {
            const key = String(item.serial || '');
            if (!key) return false;
            const waitBudgetMs = Math.min(DETECT_READY_WAIT_TIMEOUT_MS, ITEM_READY_WAIT_TIMEOUT_MS);
            const startedAt = Date.now();
            const deadline = startedAt + waitBudgetMs;
            let lastDiagAt = 0;
            let ensureKicked = false;

            //Check if already fully refined in cache
            if (isDetectItemWarmReady(item)) {
                setWarmOverlay(false);
                void postUiDiag('detect_item_ready_done', {
                    serial: Number(item?.serial || 0) || null,
                    wait_ms: 0,
                    ready: true,
                    extra_refined: true,
                    source: 'cache_hit',
                });
                return true;
            }

            //Kick off full detect+refine pipeline in background
            if (!detectPrimeInFlight.has(key) && !detectAutoRefineInFlight.has(key)) {
                void ensureDetectItemDisplayReady(item, false);
                ensureKicked = true;
            }

            //Poll until fully refined or timeout
            while (Date.now() < deadline && labelerActive && labelerMode === 'detect') {
                const current = detectPrefetch.get(key);
                if (isDetectItemWarmReady(item)) {
                    setWarmOverlay(false);
                    void postUiDiag('detect_item_ready_done', {
                        serial: Number(item?.serial || 0) || null,
                        wait_ms: Math.max(0, Date.now() - startedAt),
                        ready: true,
                        extra_refined: true,
                        source: 'wait_loop',
                    });
                    return true;
                }

                const elapsed = Math.max(0, Date.now() - startedAt);
                const isUsable = isDetectEntryUsable(current);
                const isReady = isDetectEntryReady(current);
                const pct = Math.min(0.95, Math.max(0.05, elapsed / waitBudgetMs));

                let subtitle = 'Running detector and refining boxes...';
                if (!current && !isUsable) {
                    subtitle = 'Running YOLO detection...';
                } else if (isReady && !detectExtraRefinedSerials.has(key)) {
                    subtitle = 'Finalizing refined boxes...';
                }
                setWarmOverlay(true, 'Preparing next image...', subtitle, pct);

                //Re-kick if the pipeline stalled
                if (!ensureKicked && !detectAutoRefineInFlight.has(key) && !detectPrimeInFlight.has(key)) {
                    void ensureDetectItemDisplayReady(item, false);
                    ensureKicked = true;
                }

                const now = Date.now();
                if ((now - lastDiagAt) >= READY_WAIT_DIAG_INTERVAL_MS) {
                    lastDiagAt = now;
                    void postUiDiag('detect_item_ready_wait', {
                        serial: Number(item?.serial || 0) || null,
                        ready: !!isReady,
                        usable: !!isUsable,
                        extra_refined: detectExtraRefinedSerials.has(key),
                        detect_inflight: detectWarmInFlight.has(key),
                        refine_inflight: detectAutoRefineInFlight.has(key),
                        prefetch_inflight: detectPrefetchInFlight.has(key),
                        prime_inflight: detectPrimeInFlight.has(key),
                        ensure_kicked: ensureKicked,
                    });
                }
                await waitMs(150);
            }
            setWarmOverlay(false);
            void postUiDiag('detect_item_ready_timeout', {
                serial: Number(item?.serial || 0) || null,
                wait_ms: waitBudgetMs,
                ready: !!isDetectEntryReady(detectPrefetch.get(key)),
                usable: !!isDetectEntryUsable(detectPrefetch.get(key)),
                extra_refined: detectExtraRefinedSerials.has(key),
            });
            return false;
        }
        if (labelerMode === 'classify') {
            const key = getPredCacheKey(item);
            if (!key) return false;
            prefetchImageSerial(item.serial);
            const waitBudgetMs = Math.min(CLASSIFY_READY_WAIT_TIMEOUT_MS, ITEM_READY_WAIT_TIMEOUT_MS);
            const deadline = Date.now() + waitBudgetMs;
            let lastDiagAt = 0;
            while (Date.now() < deadline && labelerActive && labelerMode === 'classify') {
                const rows = predCache.get(key);
                if (Array.isArray(rows) && rows.length) {
                    prefetchDisplayRefsForItem(item, rows, 'high');
                } else if (!classifyWarmInFlight.has(key) && !classifyForegroundInFlight.has(key)) {
                    void ensureClassifyItemReady(item, false, false);
                }
                const predsReady = _classifyItemWarmReady(item);
                const imageReady = isPrefetchedImageReady(item.serial);
                const idx = _targetCropIdxForItem(item);
                const depth = _predictionLoadedRefDepthForCrop(rows, idx, CLASSIFY_REFS_PER_CAT_TARGET);
                const cov = _predictionLoadedRefCoverageForCrop(rows, idx);
                const loadedCounts = _predictionLoadedRefCountsForCrop(rows, idx);
                const refsSufficient = _predictionRefsSufficientForCrop(rows, idx);
                const refsAtTarget = _predictionLoadedRefsAtTargetForCrop(rows, idx);
                const refsReadyForDisplay = refsAtTarget;
                const predsCached = predCache.has(key);
                const prefetchTerminalError = isPrefetchedImageTerminalError(item.serial);
                const prefetchStalled = isPrefetchedImageStalled(item.serial);
                const elapsed = Math.max(0, waitBudgetMs - Math.max(0, deadline - Date.now()));
                const hasPreds = Array.isArray(rows) && rows.length > 0;
                let readyReason = '';
                if (refsReadyForDisplay && imageReady) readyReason = 'refs_ready+image_ready';
                else if (predsCached && prefetchTerminalError && elapsed >= Math.floor(waitBudgetMs * 0.75)) {
                    readyReason = 'preds_cached+prefetch_terminal_error';
                } else if (predsCached && prefetchStalled && elapsed >= Math.floor(waitBudgetMs * 0.75)) {
                    readyReason = 'preds_cached+prefetch_stalled';
                } else if (refsSufficient && imageReady && predsCached && elapsed >= Math.min(500, Math.floor(waitBudgetMs * 0.04))) {
                    readyReason = 'refs_sufficient+image_ready';
                }
                if (readyReason) {
                    setWarmOverlay(false);
                    void postUiDiag('classify_item_ready_done', {
                        serial: Number(item?.serial || 0) || null,
                        wait_ms: elapsed,
                        wait_budget_ms: waitBudgetMs,
                        refs_ready: refsReadyForDisplay,
                        preds_ready: predsReady,
                        image_prefetch_ready: imageReady,
                        image_prefetch_terminal_error: prefetchTerminalError,
                        image_prefetch_stalled: prefetchStalled,
                        ready_reason: readyReason,
                        classify_ready_relax_flag: !!FLAG_CLASSIFY_READY_RELAX,
                        cached_image_path: compactCachedImagePathDiag(item.serial),
                        refs_sufficient: refsSufficient,
                        refs_at_target: refsAtTarget,
                        refs_depth_ready_candidates: Number(depth.candidatesAtDepth || 0),
                        refs_depth_target_candidates: Number(depth.targetCount || 0),
                        refs_depth_min_per_candidate: Number(depth.minRefsPerCandidate || 0),
                        refs_loaded_per_candidate: loadedCounts.join(','),
                        refs_with_loaded_images: Number(cov.candidatesWithRefs || 0),
                        refs_target_candidates: Number(cov.targetCount || 0),
                        ref_queue: compactRefImageQueueDiag(),
                    });
                    return true;
                }
                const pct = Math.min(0.95, Math.max(0.05, elapsed / waitBudgetMs));
                const subtitle = hasPreds
                    ? 'Loading reference photos...'
                    : 'Waiting for image and classifier results...';
                const title = initialClassifyWarmDone ? 'Loading classifier item...' : 'Preparing classifier queue...';
                setWarmOverlay(true, title, subtitle, pct);
                const now = Date.now();
                if ((now - lastDiagAt) >= READY_WAIT_DIAG_INTERVAL_MS) {
                    lastDiagAt = now;
                    void postUiDiag('classify_item_ready_wait', {
                        serial: Number(item?.serial || 0) || null,
                        wait_budget_ms: waitBudgetMs,
                        refs_ready: refsReadyForDisplay,
                        preds_ready: predsReady,
                        refs_sufficient: refsSufficient,
                        refs_at_target: refsAtTarget,
                        image_prefetch_ready: imageReady,
                        image_prefetch_terminal_error: prefetchTerminalError,
                        image_prefetch_stalled: prefetchStalled,
                        classify_ready_relax_flag: !!FLAG_CLASSIFY_READY_RELAX,
                        cached_image_path: compactCachedImagePathDiag(item.serial),
                        pred_cache_hit: !!(rows && Array.isArray(rows) && rows.length),
                        refs_with_loaded_images: Number(cov.candidatesWithRefs || 0),
                        refs_target_candidates: Number(cov.targetCount || 0),
                        refs_depth_ready_candidates: Number(depth.candidatesAtDepth || 0),
                        refs_depth_target_candidates: Number(depth.targetCount || 0),
                        refs_depth_min_per_candidate: Number(depth.minRefsPerCandidate || 0),
                        refs_loaded_per_candidate: loadedCounts.join(','),
                        image_prefetch_state: getPrefetchedImageState(item.serial),
                        ref_queue: compactRefImageQueueDiag(),
                    });
                }
                if (!imageReady) {
                    prefetchImageSerial(item.serial);
                }
                await waitMs(120);
            }
            setWarmOverlay(false);
            const finalRows = predCache.get(key);
            const finalIdx = _targetCropIdxForItem(item);
            const finalCov = _predictionLoadedRefCoverageForCrop(finalRows, finalIdx);
            const finalDepth = _predictionLoadedRefDepthForCrop(finalRows, finalIdx, CLASSIFY_REFS_PER_CAT_TARGET);
            const finalLoadedCounts = _predictionLoadedRefCountsForCrop(finalRows, finalIdx);
            const finalPredsReady = _classifyItemWarmReady(item);
            const finalRefsSufficient = _predictionRefsSufficientForCrop(finalRows, finalIdx);
            const finalRefsAtTarget = _predictionLoadedRefsAtTargetForCrop(finalRows, finalIdx);
            const finalRefsReadyForDisplay = finalRefsAtTarget;
            const finalPredsCached = predCache.has(key);
            const finalImageReady = isPrefetchedImageReady(item.serial);
            const finalPrefetchTerminalError = isPrefetchedImageTerminalError(item.serial);
            const finalPrefetchStalled = isPrefetchedImageStalled(item.serial);
            let finalReadyReason = '';
            if (finalRefsReadyForDisplay && finalImageReady) finalReadyReason = 'deadline_refs_ready+image_ready';
            else if (finalPredsCached && finalPrefetchTerminalError) finalReadyReason = 'deadline_preds_cached+prefetch_terminal_error';
            else if (finalPredsCached && finalPrefetchStalled) finalReadyReason = 'deadline_preds_cached+prefetch_stalled';
            else if (finalRefsSufficient && finalImageReady && finalPredsCached) finalReadyReason = 'deadline_refs_sufficient+image_ready';
            if (finalReadyReason) {
                void postUiDiag('classify_item_ready_done', {
                    serial: Number(item?.serial || 0) || null,
                    wait_ms: waitBudgetMs,
                    wait_budget_ms: waitBudgetMs,
                    refs_ready: finalRefsReadyForDisplay,
                    preds_ready: finalPredsReady,
                    refs_sufficient: finalRefsSufficient,
                    refs_at_target: finalRefsAtTarget,
                    image_prefetch_ready: finalImageReady,
                    image_prefetch_terminal_error: finalPrefetchTerminalError,
                    image_prefetch_stalled: finalPrefetchStalled,
                    ready_reason: finalReadyReason,
                    late_after_deadline: true,
                    classify_ready_relax_flag: !!FLAG_CLASSIFY_READY_RELAX,
                    cached_image_path: compactCachedImagePathDiag(item.serial),
                    refs_depth_ready_candidates: Number(finalDepth.candidatesAtDepth || 0),
                    refs_depth_target_candidates: Number(finalDepth.targetCount || 0),
                    refs_depth_min_per_candidate: Number(finalDepth.minRefsPerCandidate || 0),
                    refs_loaded_per_candidate: finalLoadedCounts.join(','),
                    refs_with_loaded_images: Number(finalCov.candidatesWithRefs || 0),
                    refs_target_candidates: Number(finalCov.targetCount || 0),
                    image_prefetch_state: getPrefetchedImageState(item.serial),
                    ref_queue: compactRefImageQueueDiag(),
                });
                return true;
            }
            void postUiDiag('classify_item_ready_timeout', {
                serial: Number(item?.serial || 0) || null,
                wait_ms: waitBudgetMs,
                wait_budget_ms: waitBudgetMs,
                image_prefetch_ready: finalImageReady,
                image_prefetch_terminal_error: finalPrefetchTerminalError,
                image_prefetch_stalled: finalPrefetchStalled,
                preds_ready: finalPredsReady,
                classify_ready_relax_flag: !!FLAG_CLASSIFY_READY_RELAX,
                cached_image_path: compactCachedImagePathDiag(item.serial),
                image_prefetch_state: getPrefetchedImageState(item.serial),
                refs_with_loaded_images: Number(finalCov.candidatesWithRefs || 0),
                refs_target_candidates: Number(finalCov.targetCount || 0),
                refs_depth_ready_candidates: Number(finalDepth.candidatesAtDepth || 0),
                refs_depth_target_candidates: Number(finalDepth.targetCount || 0),
                refs_depth_min_per_candidate: Number(finalDepth.minRefsPerCandidate || 0),
                refs_loaded_per_candidate: finalLoadedCounts.join(','),
                refs_sufficient: finalRefsSufficient,
                refs_at_target: finalRefsAtTarget,
                pred_cache_hit: !!(Array.isArray(finalRows) && finalRows.length),
                ref_queue: compactRefImageQueueDiag(),
            });
            return false;
        }
        return true;
    }

    async function loadQueue(opts = {}) {
        const forceRefresh = !!opts.forceRefresh;
        setStatus('Loading queue...');
        try {
            setWarmOverlay(true, 'Loading queue...', `Fetching ${labelerMode} items`, 0.12);
            await refreshQueueMode(labelerMode, { force: forceRefresh });
            applyModeQueue();
            const miss = queueLocalMissingForMode(labelerMode);
            if ((Number(miss.excluded || 0) || 0) > 0) {
                setStatus(`Queue: ${queueTotal} items (${miss.excluded} local-missing excluded)`);
            } else {
                setStatus(`Queue: ${queueTotal} items`);
            }
            if (queue.length > 0) {
                startWarmLoop();
                if (labelerMode === 'classify') {
                    await ensureInitialClassifyWarmGate();
                    setWarmOverlay(true, 'Preparing classifier queue...', 'Claiming first ready item', 0.82);
                } else {
                    setWarmOverlay(true, 'Loading first item...', 'Claiming item and starting image load', 0.4);
                }
                await loadCurrentItem();
                if (labelerMode === 'classify') {
                    void warmClassifierRefCache();
                }
                if (labelerMode !== 'classify') {
                    setWarmOverlay(false);
                }
                if (labelerMode === 'detect') {
                    // Warm detector queue in background after first item is already loading.
                    void ensureInitialDetectWarmGate();
                }
                // Keep non-active queue refresh lazy to reduce backend pressure.
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
        const modeTokenAtStart = loadCurrentItemModeToken;
        const modeAtStart = labelerMode;
        const isStaleLoad = () => (
            !labelerActive
            || modeTokenAtStart !== loadCurrentItemModeToken
            || modeAtStart !== labelerMode
        );
        loadCurrentItemInFlight = true;
        prunePrefetchedClaims();
        let skippedClaims = 0;
        let claimRetryLoops = 0;
        let lastClaimRetryQueueRefreshAt = 0;
        let claimRetryStartedAt = 0;
        try {
            while (queueIndex < queue.length) {
                if (isStaleLoad()) return;
                modePositions[labelerMode] = queueIndex;
                const item = queue[queueIndex];
                if (!item) break;
                if (queueAdvanceMeta) {
                    queueAdvanceMeta.to_serial = Number(item?.serial || 0) || null;
                    if (!queueAdvanceMeta.claim_started_at) {
                        queueAdvanceMeta.claim_started_at = Date.now();
                    }
                }
                const claimResult = await claimQueueItem(item, labelerMode);
                if (isStaleLoad()) return;
                if (claimResult === 'error' || claimResult === 'retry' || claimResult === 'auth') {
                    claimRetryLoops += 1;
                    if (!claimRetryStartedAt) claimRetryStartedAt = Date.now();
                    const elapsed = Math.max(0, Date.now() - claimRetryStartedAt);
                    if (queueAdvanceMeta) {
                        queueAdvanceMeta.claim_retry_loops = claimRetryLoops;
                        queueAdvanceMeta.claim_error_kind = String(lastClaimErrorKind || '');
                        queueAdvanceMeta.claim_last_result = String(claimResult || '');
                        queueAdvanceMeta.claim_last_error = String(lastClaimErrorMessage || '').slice(0, 180);
                    }
                    const pulse = ((claimRetryLoops - 1) % 12) / 12;
                    const pct = Math.max(0.08, Math.min(0.92, 0.1 + pulse * 0.8));
                    let overlayTitle = 'Retrying claim...';
                    let overlaySub = `Claim request retry ${claimRetryLoops} - ${Math.round(elapsed / 1000)}s`;
                    let statusMsg = 'Retrying claim...';
                    if (claimResult === 'retry' || lastClaimErrorKind === 'network') {
                        overlayTitle = 'Reconnecting...';
                        statusMsg = 'Claim request retrying (network/transient error)...';
                    } else if (claimResult === 'auth' || lastClaimErrorKind === 'auth') {
                        overlayTitle = 'Session expired';
                        overlaySub = 'Claim failed (missing/expired session). Refresh the page.';
                        statusMsg = 'Claim failed: session expired. Refresh the page.';
                    } else if (lastClaimErrorMessage) {
                        const compact = String(lastClaimErrorMessage).slice(0, 180);
                        overlaySub = `Claim request retry ${claimRetryLoops}: ${compact}`;
                        statusMsg = 'Claim request error - retrying...';
                    }
                    if (claimRetryLoops === 1 || (claimRetryLoops % 10) === 0) {
                        void postUiDiag('claim_retry', {
                            serial: Number(item?.serial || 0) || null,
                            result: claimResult,
                            error_kind: String(lastClaimErrorKind || ''),
                            error: String(lastClaimErrorMessage || ''),
                            loops: claimRetryLoops,
                            elapsed_ms: elapsed,
                        });
                    }
                    setWarmOverlay(
                        true,
                        overlayTitle,
                        overlaySub,
                        pct,
                    );
                    setStatus(statusMsg);
                    const now = Date.now();
                    const shouldRefreshQueue = (
                        claimRetryLoops >= 20
                        && (claimRetryLoops % 20) === 0
                        && (now - Number(lastClaimRetryQueueRefreshAt || 0)) >= CLAIM_RETRY_REFRESH_COOLDOWN_MS
                    );
                    if (shouldRefreshQueue) {
                        lastClaimRetryQueueRefreshAt = now;
                        void refreshQueueMode(labelerMode, { force: false });
                    }
                    const retryDelayMs = Math.min(4000, 500 * Math.pow(2, Math.min(claimRetryLoops - 1, 3)));
                    await waitMs(retryDelayMs);
                    if (isStaleLoad()) return;
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
                if (queueAdvanceMeta) {
                    queueAdvanceMeta.claim_granted_at = Date.now();
                    queueAdvanceMeta.claim_retry_loops = claimRetryLoops;
                    if (!queueAdvanceMeta.claim_error_kind) {
                        queueAdvanceMeta.claim_error_kind = String(lastClaimErrorKind || '');
                    }
                }
                const readyWaitStartedAt = Date.now();
                let readyForDisplay = await waitForCurrentItemReady(item);
                if (isStaleLoad()) return;
                if (labelerMode === 'classify' && !readyForDisplay) {
                    const hardDeadline = Date.now() + ITEM_READY_WAIT_TIMEOUT_MS;
                    let hardGatePasses = 0;
                    while (
                        !readyForDisplay
                        && labelerActive
                        && labelerMode === 'classify'
                        && Date.now() < hardDeadline
                    ) {
                        hardGatePasses += 1;
                        const key = getPredCacheKey(item);
                        queueImagePrefetchSerial(item.serial, {
                            force: true,
                            cacheBust: Date.now(),
                            priority: 'high',
                        });
                        try {
                            await ensureClassifyItemReady(item, true, false);
                        } catch (e) {
                            // The detailed diagnostics live in the identify/ref logs.
                        }
                        if (isStaleLoad()) return;
                        const warmed = key ? predCache.get(key) : null;
                        if (Array.isArray(warmed) && warmed.length) {
                            prefetchDisplayRefsForItem(item, warmed, 'high');
                        }
                        void postUiDiag('classify_item_reveal_blocked', {
                            serial: Number(item?.serial || 0) || null,
                            hard_gate_passes: hardGatePasses,
                            pred_cache_hit: !!(key && predCache.has(key)),
                            refs_ready: !!_classifyItemRefsReady(item),
                            image_ready: !!isPrefetchedImageReady(item?.serial),
                            ref_queue: compactRefImageQueueDiag(),
                        });
                        readyForDisplay = await waitForCurrentItemReady(item);
                        if (isStaleLoad()) return;
                    }
                }
                if (queueAdvanceMeta) {
                    queueAdvanceMeta.item_ready_wait_ms = Math.max(0, Date.now() - readyWaitStartedAt);
                    queueAdvanceMeta.item_ready_wait_ready = !!readyForDisplay;
                }
                if (labelerMode === 'classify' && !readyForDisplay) {
                    const stuckSerial = Number(item?.serial || 0) || null;
                    setWarmOverlay(true, 'Still preparing classifier item', `sn${stuckSerial || '?'} is still preparing`, 0.98);
                    setStatus(`Classifier is still preparing sn${stuckSerial || '?'}`);
                    void postUiDiag('classify_item_reveal_stalled', {
                        serial: stuckSerial,
                        pred_cache_hit: !!predCache.has(getPredCacheKey(item)),
                        refs_ready: !!_classifyItemRefsReady(item),
                        image_ready: !!isPrefetchedImageReady(item?.serial),
                        ref_queue: compactRefImageQueueDiag(),
                    });
                    return;
                }
                currentItem = item;
                currentSerial = item.serial;
                if (labelerMode === 'classify') {
                    void postUiDiag('classify_item_activated', {
                        serial: Number(item?.serial || 0) || null,
                        key: String(getPredCacheKey(item) || ''),
                        num_boxes: Number(parseYoloBoxes(String(item?.boxes || '')).length || 0),
                        pred_cache_hit: !!predCache.has(getPredCacheKey(item)),
                        queue_index: Number(queueIndex || 0),
                    });
                }
                const imageLoadChoice = resolveDisplayImageUrl(item.serial);
                // Prefer reusing the already-prefetched URL to avoid a second network fetch.
                currentImageUrl = imageLoadChoice.url || buildCachedImageUrl(item.serial, { intent: 'foreground' });
                imageReadyForCurrentItem = false;
                imageLoadRetryCount = 0;
                if (queueAdvanceMeta) {
                    queueAdvanceMeta.image_source = String(imageLoadChoice.source || '');
                    queueAdvanceMeta.image_intent = String(imageLoadChoice.intent || '');
                    queueAdvanceMeta.image_prefetch_ready = !!imageLoadChoice.prefetch_ready;
                }
                if (queueAdvanceMeta && labelerMode === 'classify') {
                    const key = getPredCacheKey(item);
                    queueAdvanceMeta.pred_cache_hit = !!(key && predCache.has(key));
                    queueAdvanceMeta.classify_warm_ready = !!_classifyItemWarmReady(item);
                    queueAdvanceMeta.drive_like = !!isLikelyDriveImageUrl(item?.url || '');
                }

                //Parse existing boxes if any (for classifier mode)
                if (item.boxes) {
                    currentBoxes = parseYoloBoxes(item.boxes);
                    currentSamMaskTiles = new Array(currentBoxes.length).fill(null);
                } else {
                    currentBoxes = [];
                    currentSamMaskTiles = [];
                    if (labelerMode === 'detect') {
                        const cachedDet = detectPrefetch.get(String(currentSerial));
                        if (isDetectItemWarmReady({ serial: currentSerial })) {
                            currentBoxes = parseYoloBoxes(cachedDet?.refined || cachedDet?.raw || '');
                            currentSamMaskTiles = cloneSamMaskTiles(cachedDet?.samMaskTiles || []);
                        }
                    }
                }
                if (currentSamMaskTiles.length < currentBoxes.length) {
                    currentSamMaskTiles = currentSamMaskTiles.concat(new Array(currentBoxes.length - currentSamMaskTiles.length).fill(null));
                } else if (currentSamMaskTiles.length > currentBoxes.length) {
                    currentSamMaskTiles = currentSamMaskTiles.slice(0, currentBoxes.length);
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
                // Auto-fix pre-existing duplicate cat labels so they don't
                // softlock the grading flow.  Later occurrences are cleared so
                // the user can re-assign them.
                {
                    const _seen = new Set();
                    for (let _i = 0; _i < currentLabels.length; _i++) {
                        const _k = _normalizeAssignedCatKey(currentLabels[_i]);
                        if (!_k) continue;
                        if (_seen.has(_k)) {
                            currentLabels[_i] = '';
                        } else {
                            _seen.add(_k);
                        }
                    }
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
                        prefetchDisplayRefsForCrop(currentPredictions, currentCropIdx, 'high');
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
                        currentSamMaskTiles = cloneSamMaskTiles(pendingUndoRestore.samMaskTiles || []);
                        if (currentSamMaskTiles.length < currentBoxes.length) {
                            currentSamMaskTiles = currentSamMaskTiles.concat(new Array(currentBoxes.length - currentSamMaskTiles.length).fill(null));
                        }
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
                    primeHotNextDetectItem();
                }
                prefetchManualCandidates();
                preclaimAhead(labelerMode, queueIndex, CLAIM_PRECLAIM_AHEAD_COUNT);
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
                if (labelerMode === 'detect') {
                    prefetchDetection();
                    prefetchDetectionRefine();
                    primeHotNextDetectItem();
                }
                if (labelerMode === 'classify') {
                    primeHotNextClassifyItem();
                }
                if (labelerMode === 'manual') {
                    prefetchManualCandidates();
                }
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
        classifyPrimeInFlight.clear();
        classifyForegroundInFlight.clear();
        classifyRefRefreshTs.clear();
        classifyRefRefreshInFlight.clear();
        classifyNextCropPrefetchTs.clear();
        prefetchInFlight.clear();
        prefetchRunning = false;
        prefetchRequested = false;
        for (const img of imagePrefetch.values()) {
            if (!img) continue;
            img.onload = null;
            img.onerror = null;
            try { img.src = ''; } catch (e) { /* best effort */ }
        }
        imagePrefetch.clear();
        imagePrefetchState.clear();
        imagePrefetchQueue = [];
        imagePrefetchQueued.clear();
        imagePrefetchWorkerRunning = false;
        cachedImagePathByIntent.clear();
        cachedImagePathLatest.clear();
        cachedImagePathProbeInFlight.clear();
        cachedImagePathProbeTs.clear();
        if (imagePrefetchRetryTimers.size) {
            for (const timer of imagePrefetchRetryTimers.values()) {
                clearTimeout(timer);
            }
            imagePrefetchRetryTimers.clear();
        }
        for (const img of refImagePrefetch.values()) {
            if (!img) continue;
            img.onload = null;
            img.onerror = null;
            try { img.src = ''; } catch (e) { /* best effort */ }
        }
        refImagePrefetch.clear();
        refImagePrefetchState.clear();
        refImagePrefetchRetry.clear();
        refImageFetchQueue.length = 0;
        refImageFetchQueued.clear();
        refImagesLoadingCount = 0;
        refImageLoadingOrder.length = 0;
        refImageLoadingPriority.clear();
        refImagePreempted.clear();
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
        detectAutoRefineInFlight.clear();
        detectPrimeInFlight.clear();
        detectExtraRefinedSerials.clear();
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
        classifierWarmStartedAt = 0;
        _cancelClassifyItemLoadOverlayWait();
        pendingUndoRestore = null;
        initialClassifyWarmDone = false;
        prefetchedClaims.clear();
    }

    function getPredCacheKey(item) {
        if (!item) return '';
        return `${item.serial || ''}|${item.boxes || ''}`;
    }

    function getPredFocusCacheKey(item, cropIdx = null) {
        const base = getPredCacheKey(item);
        if (!base) return '';
        const idx = Number(cropIdx);
        const hasIdx = Number.isInteger(idx) && idx >= 0;
        return `${base}|focus=${hasIdx ? idx : 'all'}`;
    }

    function _mergePredictionCandidateRefs(prevCand, nextCand) {
        const prevRefs = Array.isArray(prevCand?.refs) ? prevCand.refs : [];
        const nextRefs = Array.isArray(nextCand?.refs) ? nextCand.refs : [];
        return {
            ...(prevCand || {}),
            ...(nextCand || {}),
            refs: nextRefs.length ? nextRefs : prevRefs,
        };
    }

    function _mergePredictionCropRow(prevRow, nextRow) {
        const prevCands = Array.isArray(prevRow?.candidates) ? prevRow.candidates : [];
        const nextCands = Array.isArray(nextRow?.candidates) ? nextRow.candidates : [];
        if (!prevRow) return nextRow || null;
        if (!nextRow) return prevRow || null;
        if (!prevCands.length) return nextRow;
        if (!nextCands.length) return prevRow;

        const prevByName = new Map();
        for (const cand of prevCands) {
            const key = String(cand?.name || '').trim();
            if (!key || prevByName.has(key)) continue;
            prevByName.set(key, cand);
        }

        const mergedCandidates = nextCands.map((cand) => {
            const key = String(cand?.name || '').trim();
            const prevCand = key ? prevByName.get(key) : null;
            return prevCand ? _mergePredictionCandidateRefs(prevCand, cand) : cand;
        });

        return {
            ...prevRow,
            ...nextRow,
            candidates: mergedCandidates,
        };
    }

    function mergePredictionResults(prevResults, nextResults, focusCropIdx = null) {
        const prevRows = Array.isArray(prevResults) ? prevResults : [];
        const nextRows = Array.isArray(nextResults) ? nextResults : [];
        if (!prevRows.length) return nextRows;
        if (!nextRows.length) return prevRows;

        const maxLen = Math.max(prevRows.length, nextRows.length);
        const focusIdx = Number(focusCropIdx);
        const hasFocus = Number.isInteger(focusIdx) && focusIdx >= 0;
        const merged = [];

        for (let i = 0; i < maxLen; i++) {
            const prevRow = prevRows[i] || null;
            const nextRow = nextRows[i] || null;
            if (!prevRow) {
                merged.push(nextRow);
                continue;
            }
            if (!nextRow) {
                merged.push(prevRow);
                continue;
            }
            if (!hasFocus || i === focusIdx) {
                merged.push(_mergePredictionCropRow(prevRow, nextRow));
                continue;
            }
            const nextHasRefs = (Array.isArray(nextRow?.candidates) ? nextRow.candidates : []).some((cand) => {
                const refs = Array.isArray(cand?.refs) ? cand.refs : [];
                return refs.length > 0;
            });
            merged.push(nextHasRefs ? _mergePredictionCropRow(prevRow, nextRow) : prevRow);
        }

        return merged.filter(Boolean);
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

    function prefetchDisplayManualRefs(candidates, priority = 'high') {
        const rows = Array.isArray(candidates) ? candidates : [];
        if (!rows.length) return;
        const visible = _visibleManualCandidatesForCrop(rows, currentCropIdx);
        visible.slice(0, MANUAL_DISPLAY_PREFETCH_MAX_CANDIDATES).forEach((cand) => {
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            refs.slice(0, MANUAL_DISPLAY_PREFETCH_MAX_REFS).forEach((ref) => {
                const srcs = _getRefDisplaySources(ref);
                const baseSrc = srcs.baseSrc;
                const inlineSrc = srcs.inlineSrc;
                const fastSrc = srcs.fastSrc;
                const hqSrc = srcs.hqSrc;
                const fallbackUrlSrc = srcs.fallbackUrlSrc;
                if (inlineSrc && inlineSrc !== baseSrc) {
                    prefetchRefImageSrc(inlineSrc, { priority });
                    return;
                }
                if (fastSrc) prefetchRefImageSrc(fastSrc, { priority });
                if (hqSrc) prefetchRefImageSrc(hqSrc, { priority });
                if (baseSrc) prefetchRefImageSrc(baseSrc, { priority });
                if (fallbackUrlSrc) prefetchRefImageSrc(fallbackUrlSrc, { priority });
            });
        });
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
                    const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
                    _setManualCandidateCache(target.key, { candidates });
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

    function normalizeSamPolygons(value) {
        const rows = Array.isArray(value) ? value : [];
        return rows.map((poly) => {
            const pts = Array.isArray(poly) ? poly : [];
            return pts.map((pt) => {
                const pair = Array.isArray(pt) ? pt : [];
                const x = Number(pair[0]);
                const y = Number(pair[1]);
                if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
                return {
                    x: Math.max(0, Math.min(1, x)),
                    y: Math.max(0, Math.min(1, y)),
                };
            }).filter((pt) => !!pt);
        }).map((poly) => (poly.length >= 3 ? poly : []));
    }

    function normalizeSamMaskTiles(value) {
        const rows = Array.isArray(value) ? value : [];
        return rows.map((tile) => {
            const row = (tile && typeof tile === 'object') ? tile : {};
            const png = String(row.png_b64 || '').trim();
            if (!png) return null;
            const x1 = Number(row.x1);
            const y1 = Number(row.y1);
            const x2 = Number(row.x2);
            const y2 = Number(row.y2);
            if (![x1, y1, x2, y2].every((v) => Number.isFinite(v))) return null;
            return {
                x1: Math.max(0, Math.min(1, x1)),
                y1: Math.max(0, Math.min(1, y1)),
                x2: Math.max(0, Math.min(1, x2)),
                y2: Math.max(0, Math.min(1, y2)),
                png_b64: png,
            };
        });
    }

    function cloneSamMaskTiles(value) {
        const rows = Array.isArray(value) ? value : [];
        return rows.map((tile) => {
            if (!tile || typeof tile !== 'object') return null;
            return {
                x1: Number(tile.x1 || 0),
                y1: Number(tile.y1 || 0),
                x2: Number(tile.x2 || 0),
                y2: Number(tile.y2 || 0),
                png_b64: String(tile.png_b64 || ''),
            };
        });
    }

    function getSamMaskImage(tile) {
        if (!tile || typeof tile !== 'object') return null;
        const png = String(tile.png_b64 || '').trim();
        if (!png) return null;
        const key = png;
        const cached = samMaskImageCache.get(key);
        if (cached) return cached;
        const img = new Image();
        img.decoding = 'async';
        img.onload = () => {
            if (labelerActive && labelerMode === 'detect') {
                drawCanvas();
            }
        };
        img.src = `data:image/png;base64,${png}`;
        samMaskImageCache.set(key, img);
        if (samMaskImageCache.size > 1200) {
            const overflow = samMaskImageCache.size - 1200;
            const keys = samMaskImageCache.keys();
            for (let i = 0; i < overflow; i++) {
                const oldKey = keys.next().value;
                if (!oldKey) break;
                samMaskImageCache.delete(oldKey);
            }
        }
        return img;
    }

    function formatYoloBoxes(boxes) {
        return boxes.map(b => `${b.cx.toFixed(6)} ${b.cy.toFixed(6)} ${b.w.toFixed(6)} ${b.h.toFixed(6)}`).join('|');
    }

    function _imagePrefetchKey(serial) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) return '';
        return String(sn);
    }

    function _clearImagePrefetchRetryTimer(key) {
        const timer = imagePrefetchRetryTimers.get(key);
        if (timer) {
            clearTimeout(timer);
            imagePrefetchRetryTimers.delete(key);
        }
    }

    function _scheduleImagePrefetchRetry(serial, key, attempts) {
        if (!FLAG_PREFETCH_RETRY_FIX) return;
        if (!key || imagePrefetchRetryTimers.has(key)) return;
        if (attempts >= IMAGE_PREFETCH_RETRY_MAX_ATTEMPTS) return;
        const exp = Math.max(0, Math.min(8, Number(attempts || 0)));
        const delayMs = Math.min(
            IMAGE_PREFETCH_RETRY_MAX_MS,
            IMAGE_PREFETCH_RETRY_BASE_MS * Math.pow(2, exp),
        );
        const timer = setTimeout(() => {
            imagePrefetchRetryTimers.delete(key);
            queueImagePrefetchSerial(serial, { force: true, cacheBust: Date.now(), priority: 'high' });
        }, delayMs);
        imagePrefetchRetryTimers.set(key, timer);
    }

    function queueImagePrefetchSerial(serial, opts = {}) {
        const key = _imagePrefetchKey(serial);
        if (!key) return;
        const force = !!opts.force;
        const priority = String(opts.priority || '').trim().toLowerCase();
        const highPriority = priority === 'high';
        const now = Date.now();
        const rec = imagePrefetchState.get(key) || { state: 'idle', nextRetryTs: 0 };
        if (!force) {
            if (rec.state === 'loading' || rec.state === 'ready') return;
            if (rec.state === 'error' && Number(rec.nextRetryTs || 0) > now) return;
            if (imagePrefetchQueued.has(key)) return;
        }
        const work = {
            serial: Number.parseInt(key, 10),
            force,
            cacheBust: opts.cacheBust || null,
        };
        if (highPriority) {
            imagePrefetchQueue.unshift(work);
        } else {
            imagePrefetchQueue.push(work);
        }
        imagePrefetchQueued.add(key);
        processImagePrefetchQueue();
    }

    function processImagePrefetchQueue() {
        if (imagePrefetchWorkerRunning) return;
        if (!imagePrefetchQueue.length) return;
        imagePrefetchWorkerRunning = true;
        void (async () => {
            try {
                while (labelerActive && imagePrefetchQueue.length > 0) {
                    //Launch up to IMAGE_PREFETCH_PARALLEL loads concurrently
                    const batch = [];
                    while (batch.length < IMAGE_PREFETCH_PARALLEL && imagePrefetchQueue.length > 0) {
                        const work = imagePrefetchQueue.shift();
                        if (!work || !work.serial) continue;
                        const key = _imagePrefetchKey(work.serial);
                        if (!key) continue;
                        imagePrefetchQueued.delete(key);
                        prefetchImageSerial(work.serial, {
                            force: !!work.force,
                            cacheBust: work.cacheBust || null,
                        });
                        batch.push(key);
                    }
                    if (!batch.length) continue;
                    await _waitFor(() => {
                        return batch.every(k => {
                            const st = imagePrefetchState.get(k);
                            const state = String(st?.state || 'idle');
                            return state !== 'loading';
                        });
                    }, API_PREFETCH_TIMEOUT_MS + 3000, 70);
                }
            } finally {
                imagePrefetchWorkerRunning = false;
                if (labelerActive && imagePrefetchQueue.length > 0) {
                    processImagePrefetchQueue();
                }
            }
        })();
    }

    function prefetchImageSerial(serial, opts = {}) {
        const key = _imagePrefetchKey(serial);
        if (!key) return;
        const force = !!opts.force;
        const existing = imagePrefetch.get(key);
        const rec = imagePrefetchState.get(key) || { state: 'idle', attempts: 0, nextRetryTs: 0 };
        const now = Date.now();
        if (!force) {
            if (rec.state === 'loading' && existing && existing.img) return;
            if (rec.state === 'ready' && existing && existing.img) return;
            if (rec.state === 'error' && Number(rec.nextRetryTs || 0) > now) return;
        }

        const cacheBust = opts.cacheBust || (force ? Date.now() : null);
        const url = buildCachedImageUrl(serial, { intent: 'prefetch', cacheBust });
        if (!url) return;
        _clearImagePrefetchRetryTimer(key);
        const img = new Image();
        img.decoding = 'async';
        img.loading = 'eager';
        imagePrefetch.set(key, { img, url, startedAt: now });
        imagePrefetchState.set(key, {
            state: 'loading',
            attempts: Number(force ? (rec.attempts || 0) : 0),
            nextRetryTs: 0,
            error: '',
            updatedAt: now,
        });

        img.onload = () => {
            const current = imagePrefetch.get(key);
            if (!current || current.img !== img) return;
            const naturalW = Number(img.naturalWidth || 0);
            const naturalH = Number(img.naturalHeight || 0);
            if (naturalW > 0 && naturalH > 0) {
                imagePrefetchState.set(key, {
                    state: 'ready',
                    attempts: 0,
                    nextRetryTs: 0,
                    error: '',
                    updatedAt: Date.now(),
                });
                return;
            }
            const attempts = Number(rec.attempts || 0) + 1;
            const exp = Math.max(0, Math.min(8, attempts - 1));
            const delayMs = Math.min(
                IMAGE_PREFETCH_RETRY_MAX_MS,
                IMAGE_PREFETCH_RETRY_BASE_MS * Math.pow(2, exp),
            );
            imagePrefetchState.set(key, {
                state: 'error',
                attempts,
                nextRetryTs: Date.now() + delayMs,
                error: 'load_complete_zero_dimensions',
                updatedAt: Date.now(),
            });
            _scheduleImagePrefetchRetry(serial, key, attempts);
        };

        img.onerror = () => {
            const current = imagePrefetch.get(key);
            if (!current || current.img !== img) return;
            const attempts = Number(rec.attempts || 0) + 1;
            const exp = Math.max(0, Math.min(8, attempts - 1));
            const delayMs = Math.min(
                IMAGE_PREFETCH_RETRY_MAX_MS,
                IMAGE_PREFETCH_RETRY_BASE_MS * Math.pow(2, exp),
            );
            imagePrefetchState.set(key, {
                state: 'error',
                attempts,
                nextRetryTs: Date.now() + delayMs,
                error: 'load_error',
                updatedAt: Date.now(),
            });
            _scheduleImagePrefetchRetry(serial, key, attempts);
        };

        img.src = url;
        if (imagePrefetch.size > IMAGE_PREFETCH_MAX) {
            const overflow = imagePrefetch.size - IMAGE_PREFETCH_MAX;
            const keys = imagePrefetch.keys();
            for (let i = 0; i < overflow; i++) {
                const evictKey = keys.next().value;
                if (!evictKey) continue;
                imagePrefetch.delete(evictKey);
                imagePrefetchState.delete(evictKey);
                _clearImagePrefetchRetryTimer(evictKey);
            }
        }
    }

    function isPrefetchedImageReady(serial) {
        const key = _imagePrefetchKey(serial);
        if (!key) return false;
        const entry = imagePrefetch.get(key);
        const img = entry && entry.img ? entry.img : null;
        return !!(img && img.complete && img.naturalWidth > 0 && img.naturalHeight > 0);
    }

    function isPrefetchedImageTerminalError(serial) {
        const key = _imagePrefetchKey(serial);
        if (!key) return false;
        const st = imagePrefetchState.get(key);
        if (!st || String(st.state || '') !== 'error') return false;
        return Number(st.attempts || 0) >= IMAGE_PREFETCH_RETRY_MAX_ATTEMPTS;
    }

    function isPrefetchedImageStalled(serial, minMs = IMAGE_PREFETCH_STALL_BYPASS_MS) {
        const key = _imagePrefetchKey(serial);
        if (!key) return false;
        const st = imagePrefetchState.get(key);
        if (!st || String(st.state || '') !== 'loading') return false;
        const updatedAt = Number(st.updatedAt || 0);
        if (!updatedAt) return false;
        return (Date.now() - updatedAt) >= Math.max(400, Number(minMs || IMAGE_PREFETCH_STALL_BYPASS_MS));
    }

    function getPrefetchedImageState(serial) {
        const key = _imagePrefetchKey(serial);
        if (!key) return { present: false, complete: false, natural_width: 0, natural_height: 0, state: 'idle' };
        const entry = imagePrefetch.get(key);
        const img = entry && entry.img ? entry.img : null;
        const st = imagePrefetchState.get(key) || { state: 'idle', attempts: 0, nextRetryTs: 0, error: '' };
        const loadingForMs = (
            String(st.state || '') === 'loading' && Number(st.updatedAt || 0) > 0
        ) ? Math.max(0, Date.now() - Number(st.updatedAt || 0)) : 0;
        if (!img) {
            return {
                present: false,
                complete: false,
                natural_width: 0,
                natural_height: 0,
                state: String(st.state || 'idle'),
                attempts: Number(st.attempts || 0),
                next_retry_ms: Math.max(0, Number(st.nextRetryTs || 0) - Date.now()),
                loading_for_ms: loadingForMs,
                error: String(st.error || ''),
            };
        }
        return {
            present: true,
            complete: !!img.complete,
            natural_width: Number(img.naturalWidth || 0),
            natural_height: Number(img.naturalHeight || 0),
            current_src: String(img.currentSrc || img.src || ''),
            state: String(st.state || 'idle'),
            attempts: Number(st.attempts || 0),
            next_retry_ms: Math.max(0, Number(st.nextRetryTs || 0) - Date.now()),
            loading_for_ms: loadingForMs,
            error: String(st.error || ''),
        };
    }

    function resolveDisplayImageUrl(serial) {
        const sn = Number.parseInt(String(serial || ''), 10);
        if (!Number.isInteger(sn) || sn <= 0) {
            return { url: '', source: 'none', intent: 'foreground', prefetch_ready: false };
        }
        const key = _imagePrefetchKey(sn);
        const entry = key ? imagePrefetch.get(key) : null;
        const prefetchReady = isPrefetchedImageReady(sn);
        const prefetchedUrl = String(entry?.img?.currentSrc || entry?.url || '').trim();
        if (prefetchReady && prefetchedUrl) {
            return {
                url: prefetchedUrl,
                source: 'prefetch_ready_reuse',
                intent: 'prefetch',
                prefetch_ready: true,
            };
        }
        return {
            url: buildCachedImageUrl(sn, { intent: 'foreground' }),
            source: 'foreground_fetch',
            intent: 'foreground',
            prefetch_ready: !!prefetchReady,
        };
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
        const hasSheetCropMeta = Number.isInteger(Number(info?.serial)) && Number(info?.serial) > 0
            && Number.isInteger(Number(info?.crop)) && Number(info?.crop) > 0;
        if (hasSheetCropMeta && refUrl) {
            return refUrl.startsWith('/') ? buildApiUrl(refUrl) : refUrl;
        }
        if (refUrl) {
            return refUrl.startsWith('/') ? buildApiUrl(refUrl) : refUrl;
        }
        return '';
    }

    function _extractRefCropUrl(ref, size = REF_DISPLAY_HQ_SIZE) {
        const info = typeof ref === 'string'
            ? { img: ref, serial: null, crop: null }
            : (ref || {});
        const refUrl = String(info.url || info.src || '').trim();
        const hasSheetCropMeta = Number.isInteger(Number(info?.serial)) && Number(info?.serial) > 0
            && Number.isInteger(Number(info?.crop)) && Number(info?.crop) > 0;
        if (!hasSheetCropMeta || !refUrl) return '';
        const base = refUrl.startsWith('/') ? buildApiUrl(refUrl) : refUrl;
        const sep = base.includes('?') ? '&' : '?';
        const px = Math.max(96, Math.min(512, Number(size || REF_DISPLAY_HQ_SIZE) || REF_DISPLAY_HQ_SIZE));
        return `${base}${sep}size=${Math.round(px)}`;
    }

    function _getRefDisplaySources(ref) {
        const info = typeof ref === 'string'
            ? { img: ref, serial: null, crop: null }
            : (ref || {});
        const refImg = String(info.img || info.thumb || '').trim();
        const refUrl = String(info.url || info.src || '').trim();
        const inlineSrc = refImg
            ? (refImg.startsWith('data:image')
                ? refImg
                : `data:image/jpeg;base64,${refImg}`)
            : '';
        const hasSheetCropMeta = Number.isInteger(Number(info?.serial)) && Number(info?.serial) > 0
            && Number.isInteger(Number(info?.crop)) && Number(info?.crop) > 0;
        // For metadata-backed refs, use one stable crop URL only. Fetching both the
        // default ref URL and a second HQ variant doubles server work for the same card.
        const baseSrc = hasSheetCropMeta ? '' : _extractRefSrc(info);
        // Fast-tier path is intentionally disabled in local-only mode.
        const fastSrc = '';
        const hqSrc = hasSheetCropMeta ? _extractRefCropUrl(info, REF_DISPLAY_HQ_SIZE) : '';
        const fallbackUrlSrc = (!hqSrc && refUrl)
            ? (refUrl.startsWith('/') ? buildApiUrl(refUrl) : refUrl)
            : '';
        return {
            baseSrc,
            inlineSrc,
            fastSrc,
            hqSrc,
            fallbackUrlSrc,
        };
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

    const refImageFetchQueue = [];
    const refImageFetchQueued = new Set();
    let refImagesLoadingCount = 0;
    const REF_IMAGE_CONCURRENCY = 12;
    // Keys currently being loaded, in start order, with their priority.
    // Used to find a preemption candidate when a high-priority item arrives
    // and all slots are occupied by normal-priority loads.
    const refImageLoadingOrder = [];           // [key, ...]  oldest-first
    const refImageLoadingPriority = new Map(); // key → 'high' | 'normal'
    const refImagePreempted = new Set();       // keys cancelled intentionally (no backoff on re-queue)

    function _preemptOneNormalLoad() {
        // Find the oldest normal-priority in-flight load and abort it so the
        // slot can be used immediately by the waiting high-priority item.
        for (let i = 0; i < refImageLoadingOrder.length; i++) {
            const candidate = refImageLoadingOrder[i];
            if (refImageLoadingPriority.get(candidate) !== 'normal') continue;
            const img = refImagePrefetch.get(candidate);
            if (!img || typeof img.src !== 'string') continue;
            // Mark as preempted so onerror requeues cleanly without backoff.
            refImagePreempted.add(candidate);
            img.src = '';
            return true;
        }
        return false;
    }

    function processRefImageQueue() {
        if (!labelerActive) return;
        while (refImagesLoadingCount < REF_IMAGE_CONCURRENCY && refImageFetchQueue.length > 0) {
            const key = refImageFetchQueue.shift();
            if (!key) continue;
            refImageFetchQueued.delete(key);

            const state = String(refImagePrefetchState.get(key) || '');
            if (state !== 'queued') continue;

            refImagesLoadingCount++;
            refImagePrefetchState.set(key, 'loading');

            const loadPriority = refImageLoadingPriority.get(key) || 'normal';
            refImageLoadingOrder.push(key);

            const img = new Image();
            img.decoding = 'async';
            img.loading = 'eager';

            let finished = false;
            const _onFinish = () => {
                if (finished) return;
                finished = true;
                refImagesLoadingCount--;
                const idx = refImageLoadingOrder.indexOf(key);
                if (idx !== -1) refImageLoadingOrder.splice(idx, 1);
                refImageLoadingPriority.delete(key);
                setTimeout(processRefImageQueue, 10);
            };

            img.onload = () => {
                refImagePreempted.delete(key);
                refImagePrefetchState.set(key, 'ready');
                refImagePrefetchRetry.delete(key);
                _clearRefRetryTimer(key);
                scheduleRefRenderRefresh();
                _onFinish();
            };
            img.onerror = () => {
                if (refImagePreempted.has(key)) {
                    // Intentionally cancelled to free a slot for a high-priority load.
                    // Re-queue immediately without backoff using the original priority.
                    refImagePreempted.delete(key);
                    refImagePrefetch.delete(key);
                    refImagePrefetchState.set(key, 'queued');
                    if (loadPriority === 'high') {
                        refImageFetchQueue.unshift(key);
                    } else {
                        refImageFetchQueue.push(key);
                    }
                    refImageFetchQueued.add(key);
                    scheduleRefRenderRefresh();
                    _onFinish();
                    return;
                }
                refImagePrefetchState.set(key, 'error');
                refImagePrefetch.delete(key);
                const retry = _recordRefImageError(key);
                _scheduleRefImageRetry(key, Number(retry.delayMs || REF_IMAGE_RETRY_BASE_MS));
                scheduleRefRenderRefresh();
                _onFinish();
            };

            refImagePrefetch.set(key, img);
            img.src = key;
        }
    }

    function prefetchRefImageSrc(src, opts = {}) {
        const key = String(src || '').trim();
        if (!key) return;
        const priority = String(opts?.priority || '').trim().toLowerCase();
        const isHighPriority = priority === 'high';

        //Data URIs are already embedded; mark ready immediately without queuing.
        if (key.startsWith('data:')) {
            if (refImagePrefetchState.get(key) === 'ready') return;
            const img = new Image();
            img.src = key;
            refImagePrefetch.set(key, img);
            refImagePrefetchState.set(key, 'ready');
            return;
        }

        const now = Date.now();
        const state = String(refImagePrefetchState.get(key) || '');
        if (state === 'ready') return;
        if (state === 'queued' && isHighPriority && refImageFetchQueued.has(key)) {
            const idx = refImageFetchQueue.indexOf(key);
            if (idx > 0) {
                refImageFetchQueue.splice(idx, 1);
                refImageFetchQueue.unshift(key);
            }
        }
        if ((state === 'loading' || state === 'queued') && refImagePrefetch.has(key)) return;
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

        refImagePrefetchState.set(key, 'queued');
        refImagePrefetch.set(key, { complete: false, naturalWidth: 0 });
        const loadPri = isHighPriority ? 'high' : 'normal';
        refImageLoadingPriority.set(key, loadPri);
        if (!refImageFetchQueued.has(key)) {
            if (isHighPriority) {
                refImageFetchQueue.unshift(key);
            } else {
                refImageFetchQueue.push(key);
            }
            refImageFetchQueued.add(key);
        }
        // If all slots are occupied by normal-priority loads, preempt one so this
        // high-priority item can start immediately rather than waiting for a slot to
        // free up on its own (which could take hundreds of milliseconds per load).
        if (isHighPriority && refImagesLoadingCount >= REF_IMAGE_CONCURRENCY) {
            _preemptOneNormalLoad();
        }
        processRefImageQueue();

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
                refImageFetchQueued.delete(oldKey);
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

    function prefetchRefsFromResults(results, opts = {}) {
        const rows = Array.isArray(results) ? results : [];
        if (!rows.length) return;
        const priority = String(opts?.priority || '').trim().toLowerCase() === 'high' ? 'high' : 'normal';
        const maxCandidatesRaw = Number(opts?.maxCandidates);
        const maxCandidates = Number.isFinite(maxCandidatesRaw)
            ? Math.max(1, Math.min(12, Math.floor(maxCandidatesRaw)))
            : 9;
        const maxRefsRaw = Number(opts?.maxRefsPerCandidate);
        const maxRefsPerCandidate = Number.isFinite(maxRefsRaw)
            ? Math.max(1, Math.min(CLASSIFY_REFS_PER_CAT_TARGET, Math.floor(maxRefsRaw)))
            : CLASSIFY_REFS_PER_CAT_TARGET;
        const maxCropsRaw = Number(opts?.maxCrops);
        const maxCrops = Number.isFinite(maxCropsRaw)
            ? Math.max(1, Math.min(rows.length, Math.floor(maxCropsRaw)))
            : rows.length;

        const focusRaw = Number(opts?.focusCropIdx);
        const hasFocus = Number.isInteger(focusRaw) && focusRaw >= 0 && focusRaw < rows.length;
        const ordered = [];
        if (hasFocus) ordered.push(rows[focusRaw]);
        for (let i = 0; i < rows.length; i++) {
            if (hasFocus && i === focusRaw) continue;
            ordered.push(rows[i]);
        }

        let cropsSeen = 0;
        for (const crop of ordered) {
            if (cropsSeen >= maxCrops) break;
            cropsSeen += 1;
            const cands = Array.isArray(crop?.candidates) ? crop.candidates : [];
            const activeCands = cands.slice(0, maxCandidates);
            // Round-robin by depth: enqueue 1 ref per candidate per pass so
            // every candidate gets at least one ref loading before any gets a
            // second.  Since unshift puts the last-added item at the front of
            // the queue, iterate depths in REVERSE so depth-0 (the first ref
            // per candidate) ends up at the very front.
            for (let depth = maxRefsPerCandidate - 1; depth >= 0; depth--) {
                for (const cand of activeCands) {
                    const refs = Array.isArray(cand?.refs) ? cand.refs : [];
                    if (depth >= refs.length) continue;
                    const ref = refs[depth];
                    const srcs = _getRefDisplaySources(ref);
                    // Always prefetch display-path sources for next-up readiness.
                    if (srcs.inlineSrc && srcs.inlineSrc !== srcs.baseSrc) {
                        prefetchRefImageSrc(srcs.inlineSrc, { priority });
                    }
                    if (srcs.fastSrc) {
                        prefetchRefImageSrc(srcs.fastSrc, { priority });
                    } else if (srcs.baseSrc) {
                        prefetchRefImageSrc(srcs.baseSrc, { priority });
                    } else if (srcs.fallbackUrlSrc) {
                        prefetchRefImageSrc(srcs.fallbackUrlSrc, { priority });
                    }
                    if (srcs.hqSrc) {
                        prefetchRefImageSrc(srcs.hqSrc, { priority: priority === 'high' ? 'high' : 'normal' });
                    }
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
        if (isDetectItemWarmReady(item)) return true;
        if (usePrefetch && isDetectWarmBlocked(key)) return false;

        if (detectWarmInFlight.has(key)) {
            return waitForInFlight
                ? _waitFor(() => {
                    const hit = detectPrefetch.get(key);
                    if (!isDetectEntryReady(hit)) return false;
                    const boxes = parseYoloBoxes(String(hit?.refined || hit?.raw || ''));
                    return boxes.length === 0 || detectExtraRefinedSerials.has(key);
                })
                : false;
        }

        detectWarmInFlight.add(key);
        try {
            prefetchImageSerial(item.serial);
            const det = await apiPost('/api/labeler/detect', {
                serial: item.serial,
                url: item.url || null,
                fast: false,
                prefetch: !!usePrefetch,
            }, { timeoutMs: usePrefetch ? DETECT_PREFETCH_TIMEOUT_MS : API_POST_TIMEOUT_MS });
            const raw = String(det?.raw_boxes_yolo || det?.boxes_yolo || '').trim();
            const refined = String(det?.boxes_yolo || '').trim();
            const finalBoxes = refined || raw;
            const boxCount = parseYoloBoxes(finalBoxes).length;
            const ready = !!det?.sam_refined || boxCount === 0;
            setDetectEntry(
                key,
                raw,
                ready ? finalBoxes : '',
                ready,
                { detect: det?.detect_diag || null },
                det?.sam_polygons || [],
                det?.sam_mask_tiles || [],
            );
            if (ready) {
                detectExtraRefinedSerials.add(key);
                return true;
            }
            detectExtraRefinedSerials.delete(key);
            markDetectWarmFailure(key);
            return false;
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
        if (predCache.has(key)) {
            const rows = predCache.get(key) || [];
            if (Array.isArray(rows) && rows.length) {
                if (usePrefetch) {
                    if (waitForInFlight) {
                        prefetchDisplayRefsForItem(item, rows, 'high');
                    } else {
                        prefetchWarmRefsForItem(item, rows, 'normal');
                    }
                } else {
                    prefetchDisplayRefsForItem(item, rows, 'high');
                }
                return true;
            }
            if (!usePrefetch) {
                predCache.delete(key);
            }
        }
        if (usePrefetch && isClassifyPrefetchBlocked(key)) return false;
        if (classifyWarmInFlight.has(key)) {
            return waitForInFlight ? _waitFor(() => predCache.has(key)) : false;
        }

        classifyWarmInFlight.add(key);
        try {
            prefetchImageSerial(item.serial);
            const parsed = parseYoloBoxes(item.boxes || '');
            if (!parsed.length) return false;
            const focusCropIdx = _targetCropIdxForItem(item);
            let data = null;
            try {
                data = await postClassifyIdentify(
                    item,
                    parsed.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                    {
                        prefetch: !!usePrefetch,
                        timeoutMs: usePrefetch ? CLASSIFY_PREFETCH_TIMEOUT_MS : API_POST_TIMEOUT_MS,
                        focusCropIdx,
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
                                focusCropIdx,
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
            const nextResults = data?.results || [];
            const mergedResults = mergePredictionResults(predCache.get(key), nextResults, focusCropIdx);
            predCache.set(key, mergedResults);
            if (usePrefetch) {
                prefetchWarmRefsForItem(item, mergedResults, 'normal');
            } else {
                prefetchDisplayRefsForItem(item, mergedResults, 'high');
            }
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

    async function ensureDetectItemDisplayReady(item, waitForInFlight = true) {
        if (!item || !item.serial) return false;
        if (labelerMode !== 'detect') return false;
        prefetchImageSerial(item.serial);
        await ensureDetectItemReady(item, !!waitForInFlight, true);
        if (labelerMode !== 'detect') return false;
        return isDetectItemWarmReady(item);
    }

    function primeHotNextDetectItem() {
        if (labelerMode !== 'detect') return;
        for (let offset = 1; offset <= Math.max(1, DETECT_REFINE_PREFETCH_AHEAD); offset++) {
            _primeDetectItemAtOffset(offset);
        }
    }

    function _primeDetectItemAtOffset(offset) {
        if (labelerMode !== 'detect') return;
        const item = queue[queueIndex + offset];
        if (!item || !item.serial) return;
        const key = String(item.serial);
        const cached = detectPrefetch.get(key);
        if (isDetectItemWarmReady(item)) return;
        if (detectPrimeInFlight.has(key)) return;
        detectPrimeInFlight.add(key);
        void (async () => {
            const t0 = Date.now();
            let ok = false;
            try {
                //Abort if user switched away from detect mode
                if (labelerMode !== 'detect') return;
                ok = await ensureDetectItemDisplayReady(item, offset === 1);
            } catch (e) {
                ok = false;
            } finally {
                detectPrimeInFlight.delete(key);
            }
            const dt = Math.max(0, Date.now() - t0);
            if (dt >= 500 || !ok) {
                void postUiDiag('detect_next_prime', {
                    serial: Number(item?.serial || 0) || null,
                    ok: !!ok,
                    ms: dt,
                    offset: offset,
                    ready: !!isDetectEntryReady(detectPrefetch.get(key)),
                    extra_refined: detectExtraRefinedSerials.has(key),
                    mode_at_finish: labelerMode,
                });
            }
        })();
    }

    async function runWarmTick() {
        if (!labelerActive || warmLoopRunning) return;
        warmLoopRunning = true;
        try {
            const _warmRefreshCooldownMs = 10000;
            if ((Date.now() - lastQueueRefreshTs) >= _warmRefreshCooldownMs) {
                await refreshQueues(false);
            }
            const detectTargets = labelerMode === 'detect' ? warmWindowForMode('detect') : [];
            detectTargets.forEach((item, idx) => queueImagePrefetchSerial(item.serial, { priority: idx === 0 ? 'high' : 'normal' }));
            const classifyTargets = labelerMode === 'classify'
                ? warmWindowForMode('classify').slice(1)
                : [];
            classifyTargets
                .slice(0, IMAGE_PREFETCH_AHEAD_CLASSIFY)
                .forEach((item, idx) => queueImagePrefetchSerial(item.serial, { priority: idx === 0 ? 'high' : 'normal' }));
            if (labelerMode === 'classify') {
                classifyTargets.forEach((item, idx) => {
                    const key = getPredCacheKey(item);
                    const rows = key ? predCache.get(key) : null;
                    if (Array.isArray(rows) && rows.length && !_classifyItemRefsReady(item)) {
                        if (idx === 0) {
                            prefetchDisplayRefsForItem(item, rows, 'high');
                        } else {
                            prefetchWarmRefsForItem(item, rows, 'normal');
                        }
                    }
                });
            }
            const manualTargets = labelerMode === 'manual' ? warmWindowForMode('manual') : [];
            manualTargets.forEach((item, idx) => queueImagePrefetchSerial(item.serial, { priority: idx === 0 ? 'high' : 'normal' }));

            const detectTodo = detectTargets.filter((item) => {
                const key = String(item.serial || '');
                return (
                    !!item.serial &&
                    !isDetectItemWarmReady(item) &&
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
                    && !classifyPrimeInFlight.has(key)
                    && !isClassifyPrefetchBlocked(key)
                );
            }).slice(0, PREFETCH_CONCURRENCY);

            const tasks = [];
            //Skip detect inference during classify mode to free GPU for identify
            if (labelerMode !== 'classify') {
                detectTodo.forEach((item) => tasks.push(ensureDetectItemReady(item, false)));
            }
            classifyTodo.forEach((item) => tasks.push(ensureClassifyItemReady(item, false, true)));
            if (tasks.length) {
                await Promise.all(tasks);
            }

            // Keep background classify warm working on both predictions and ref images
            // so the next item is fully ready before reveal.
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
        labelerRequestController.abort();
        loadCurrentItemModeToken += 1;
        imageLoadToken += 1;
        activeImageLoadToken = imageLoadToken;
        stopMovementLoop();
        isPanning = false;
        panMoved = false;
        suppressClick = false;
        if (manualSearchDebounceTimer) {
            clearTimeout(manualSearchDebounceTimer);
            manualSearchDebounceTimer = null;
        }
        stopRetrainStatusPoll();
        stopWarmLoop();
        togglePrefetchTimer(false);
        classifierWarmDisplayPct = 0;
        classifierWarmItemKey = '';
        classifierWarmStartedAt = 0;
        queueAdvanceStartedAt = 0;
        queueAdvanceFromSerial = null;
        resetAutoSkipStreak();
        loadPredictionsBusy = false;
        loadPredictionsQueuedForce = false;
        resetPredCache();
        incorrectFlagMode = false;
        flagRequestsInFlight.clear();
        containerEl?.classList?.remove('labeler-flag-mode');
        updateFlagButtonState();
        detectWarmInFlight.clear();
        classifyWarmInFlight.clear();
        classifyPrimeInFlight.clear();
        classifyForegroundInFlight.clear();
        classifyRefRefreshTs.clear();
        classifyRefRefreshInFlight.clear();
        classifyNextCropPrefetchTs.clear();
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
        const token = ++imageLoadToken;
        activeImageLoadToken = token;
        imageReadyForCurrentItem = false;
        if (queueAdvanceMeta && !queueAdvanceMeta.load_image_at) {
            queueAdvanceMeta.load_image_at = Date.now();
        }
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
        resetAutoSkipStreak();
        //Resize canvas to match container
        const imgW = imageElement.naturalWidth;
        const imgH = imageElement.naturalHeight;

        if (!imgW || !imgH) {
            console.error('[Labeler] Image has no dimensions!');
            setStatus('Image loaded but has no dimensions');
            return;
        }

        if (queueAdvanceStartedAt > 0) {
            const transitionMs = Math.max(0, Date.now() - Number(queueAdvanceStartedAt || 0));
            const fromSerial = Number(queueAdvanceFromSerial || 0) || null;
            const meta = queueAdvanceMeta && typeof queueAdvanceMeta === 'object'
                ? { ...queueAdvanceMeta, image_loaded_at: Date.now() }
                : null;
            queueAdvanceStartedAt = 0;
            queueAdvanceFromSerial = null;
            queueAdvanceMeta = null;
            if (transitionMs >= 1500) {
                const claimWaitMs = meta && meta.claim_started_at && meta.claim_granted_at
                    ? Math.max(0, Number(meta.claim_granted_at) - Number(meta.claim_started_at))
                    : 0;
                const preImageMs = meta && meta.claim_granted_at && meta.load_image_at
                    ? Math.max(0, Number(meta.load_image_at) - Number(meta.claim_granted_at))
                    : 0;
                const imageLoadMs = meta && meta.load_image_at && meta.image_loaded_at
                    ? Math.max(0, Number(meta.image_loaded_at) - Number(meta.load_image_at))
                    : 0;
                const readyWaitMs = Math.max(0, Number(meta?.item_ready_wait_ms || 0));
                const preImageOtherMs = Math.max(0, preImageMs - readyWaitMs);
                void postUiDiag('transition_slow', {
                    from_serial: fromSerial,
                    to_serial: Number(currentSerial || 0) || null,
                    ms: transitionMs,
                    mode: labelerMode,
                    queue_index: Number(queueIndex || 0),
                    queue_total: Number(queueTotal || 0),
                    claim_wait_ms: claimWaitMs,
                    pre_image_ms: preImageMs,
                    ready_wait_ms: readyWaitMs,
                    ready_wait_ready: !!meta?.item_ready_wait_ready,
                    pre_image_other_ms: preImageOtherMs,
                    image_load_ms: imageLoadMs,
                    claim_retry_loops: Number(meta?.claim_retry_loops || 0),
                    claim_error_kind: String(meta?.claim_error_kind || ''),
                    claim_last_result: String(meta?.claim_last_result || ''),
                    claim_last_error: String(meta?.claim_last_error || ''),
                    pred_cache_hit: !!meta?.pred_cache_hit,
                    classify_warm_ready: !!meta?.classify_warm_ready,
                    drive_like: !!meta?.drive_like,
                    image_source: String(meta?.image_source || ''),
                    image_intent: String(meta?.image_intent || ''),
                    image_prefetch_ready: !!meta?.image_prefetch_ready,
                    image_prefetch_state: getPrefetchedImageState(currentSerial),
                    cached_image_path: compactCachedImagePathDiag(currentSerial),
                    detect_pipeline: labelerMode === 'detect' ? buildDetectPipelineDiag(currentSerial) : null,
                    classify_pipeline: labelerMode === 'classify' ? buildClassifyPipelineDiag(currentItem) : null,
                });
            }
        }

        resizeCanvasToContainer();
        resetView();

        if (labelerMode === 'detect' && currentBoxes.length === 0) {
            //Draw image first so there's no blank delay, then run detection
            drawCanvas();
            const cached = detectPrefetch.get(String(currentSerial));
            if (isDetectItemWarmReady({ serial: currentSerial })) {
                currentBoxes = parseYoloBoxes(cached?.refined || cached?.raw || '');
                drawCanvas();
                setStatus(`Found ${currentBoxes.length} box(es)`);
                updateInfo();
            } else {
                runDetection();
            }
        } else {
            drawCanvas();
            setStatus(`Ready - sn${currentSerial}`);
            if (labelerMode === 'detect' && currentBoxes.length) {
                const key = String(currentSerial || '');
                if (!detectExtraRefinedSerials.has(key)) {
                    void autoRefineCurrent();
                }
            } else if (labelerMode === 'classify') {
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
        const isDriveSource = isLikelyDriveImageUrl(itemUrl);
        const retryN = Number(imageLoadRetryCount || 0);
        void postUiDiag('image_error', {
            serial: Number(serial || 0) || null,
            retry_count: retryN,
            item_url: itemUrl,
            current_image_url: String(currentImageUrl || ''),
            image_source: String(queueAdvanceMeta?.image_source || ''),
            image_intent: String(queueAdvanceMeta?.image_intent || ''),
            drive_like: !!isDriveSource,
            cached_image_path: compactCachedImagePathDiag(serial),
            image_prefetch_state: getPrefetchedImageState(serial),
        });
        if (serial) {
            void probeCachedImagePath(serial, 'foreground', { force: true });
        }

        if (retryN === 0 && serial) {
            imageLoadRetryCount = 1;
            //Do not block on cache warm if source URL is already available.
            void warmCachedImage(serial);
            if (isDriveSource) {
                setStatus('Cached image failed - retrying via server proxy...');
                loadImage(buildCachedImageUrl(serial, { intent: 'foreground', proxy: true, cacheBust: `retry-${Date.now()}` }));
                return;
            }
            if (itemUrl.startsWith('http')) {
                setStatus('Cached image failed - loading source URL...');
                const sep = itemUrl.includes('?') ? '&' : '?';
                loadImage(`${itemUrl}${sep}tc_retry=${Date.now()}`, { noCrossOrigin: true });
                return;
            }
            setStatus('Image load failed - retrying cache...');
            loadImage(buildCachedImageUrl(serial, { intent: 'foreground', cacheBust: `retry-${Date.now()}` }));
            return;
        }

        if (retryN === 1 && serial && isDriveSource) {
            imageLoadRetryCount = 2;
            setStatus('Retrying server proxy...');
            loadImage(buildCachedImageUrl(serial, { intent: 'foreground', proxy: true, cacheBust: `retry2-${Date.now()}` }));
            return;
        }

        if (retryN === 1 && itemUrl.startsWith('http')) {
            imageLoadRetryCount = 2;
            setStatus('Retrying source URL...');
            const sep = itemUrl.includes('?') ? '&' : '?';
            loadImage(`${itemUrl}${sep}tc_retry2=${Date.now()}`, { noCrossOrigin: true });
            return;
        }

        const missingMsg = serial ? `Missing local image: sn${serial} - skipping...` : 'Image load failed after retries - skipping...';
        setStatus(missingMsg);
        autoSkipCurrentItem(serial ? 'local_image_missing' : 'image_load_failed_after_retries', {
            retry_count: retryN,
            current_image_url: String(currentImageUrl || ''),
            item_url: itemUrl,
        });
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
            currentSamMaskTiles = normalizeSamMaskTiles(data?.sam_mask_tiles || []);
            if (currentSamMaskTiles.length < currentBoxes.length) {
                currentSamMaskTiles = currentSamMaskTiles.concat(new Array(currentBoxes.length - currentSamMaskTiles.length).fill(null));
            }
            selectedBoxIdx = 0;
            const raw = data.raw_boxes_yolo || data.boxes_yolo || '';
            const refined = data.boxes_yolo || raw;
            const samRefined = !!data?.sam_refined;
            const ready = samRefined || currentBoxes.length === 0;
            if (!ready) {
                throw new Error('Detector completed without finalized SAM boxes');
            }
            setDetectEntry(String(currentSerial), raw, refined, true, { detect: data.detect_diag || null }, data?.sam_polygons || [], data?.sam_mask_tiles || []);
            const key = String(currentSerial || '');
            detectExtraRefinedSerials.add(key);
            drawCanvas();
            setStatus(`Found ${currentBoxes.length} box(es)`);
            updateInfo();
        } catch (e) {
            if (isNoImageApiError(e)) {
                setStatus('Detector image unavailable - skipping...');
                autoSkipCurrentItem('detector_image_unavailable', {
                    api: 'detect',
                    message: String(e && e.message || ''),
                });
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
        const key = String(currentSerial || '');
        if (key && detectAutoRefineInFlight.has(key)) {
            setStatus('Refining boxes...');
            return;
        }
        setStatus('Refining boxes...');
        try {
            const data = await apiPost('/api/labeler/refine', {
                serial: currentSerial,
                url: currentItem?.url || null,
                boxes: currentBoxes.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                passes: DETECTOR_MANUAL_REFINE_PASSES,
            });
            currentBoxes = parseYoloBoxes(data.boxes_yolo || '');
            currentSamMaskTiles = normalizeSamMaskTiles(data?.sam_mask_tiles || []);
            if (currentSamMaskTiles.length < currentBoxes.length) {
                currentSamMaskTiles = currentSamMaskTiles.concat(new Array(currentBoxes.length - currentSamMaskTiles.length).fill(null));
            }
            selectedBoxIdx = Math.min(selectedBoxIdx, Math.max(0, currentBoxes.length - 1));
            const prevRaw = String(detectPrefetch.get(String(currentSerial))?.raw || data.boxes_yolo || '');
            const refineOk = detectRefineSucceeded(data?.refine_diag || null, currentBoxes.length);
            setDetectEntry(
                String(currentSerial),
                prevRaw,
                data.boxes_yolo || prevRaw,
                true,
                { refine: data?.refine_diag || null },
                data?.sam_polygons || [],
                data?.sam_mask_tiles || [],
            );
            if (key && refineOk) detectExtraRefinedSerials.add(key);
            drawCanvas();
            setStatus(refineOk ? `Refined ${currentBoxes.length} box(es)` : 'SAM kept detector box');
            updateInfo();
        } catch (e) {
            setStatus(`Refine failed: ${e.message}`);
        }
    }

    async function autoRefineCurrent(runSecondPass = false) {
        if (boxesTouched || labelerMode !== 'detect') return;
        if (!currentBoxes.length) return;
        const serial = currentSerial;
        const key = String(serial || '');
        if (!key || detectAutoRefineInFlight.has(key)) return;
        detectAutoRefineInFlight.add(key);
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
                currentSamMaskTiles = normalizeSamMaskTiles(data?.sam_mask_tiles || []);
                if (currentSamMaskTiles.length < currentBoxes.length) {
                    currentSamMaskTiles = currentSamMaskTiles.concat(new Array(currentBoxes.length - currentSamMaskTiles.length).fill(null));
                }
                const prevRaw = String(detectPrefetch.get(String(serial))?.raw || refined || '');
                const refineOk = detectRefineSucceeded(data?.refine_diag || null, currentBoxes.length);
                setDetectEntry(
                    String(serial),
                    prevRaw,
                    refined,
                    true,
                    { refine: data?.refine_diag || null },
                    data?.sam_polygons || [],
                    data?.sam_mask_tiles || [],
                );
                if (key && refineOk) detectExtraRefinedSerials.add(key);
                drawCanvas();
                setStatus(`Found ${currentBoxes.length} box(es)`);
                updateInfo();
            }
        } catch (e) {
            //silent auto refine failure
        } finally {
            detectAutoRefineInFlight.delete(key);
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
        currentSamMaskTiles.forEach((tile) => {
            if (!tile || typeof tile !== 'object') return;
            const img = getSamMaskImage(tile);
            if (!img || !img.complete || !img.naturalWidth) return;
            const x1 = left + (Number(tile.x1 || 0) * imgW * scale);
            const y1 = top + (Number(tile.y1 || 0) * imgH * scale);
            const x2 = left + (Number(tile.x2 || 0) * imgW * scale);
            const y2 = top + (Number(tile.y2 || 0) * imgH * scale);
            const w = Math.max(0, x2 - x1);
            const h = Math.max(0, y2 - y1);
            if (w <= 0 || h <= 0) return;
            ctxCanvas.drawImage(img, x1, y1, w, h);
        });

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

    function onPendingBeforeUnload(event) {
        if (pendingUpdates.length === 0) return;
        event.preventDefault();
        event.returnValue = true;
    }

    function syncPendingUnloadWarning() {
        const shouldAttach = pendingUpdates.length > 0;
        if (shouldAttach === pendingUnloadWarningAttached) return;
        pendingUnloadWarningAttached = shouldAttach;
        if (shouldAttach) {
            window.addEventListener('beforeunload', onPendingBeforeUnload);
        } else {
            window.removeEventListener('beforeunload', onPendingBeforeUnload);
        }
    }

    function onLabelerPageHide(event) {
        if (!event.persisted) teardownLabeler();
    }

    function updateInfo() {
        document.getElementById('info-serial').textContent = currentSerial || '-';
        const total = queueTotal || queue.length;
        document.getElementById('info-queue').textContent = total ? `${Math.min(queueIndex + 1, total)} / ${total}` : '-';
        const datetimeEl = document.getElementById('info-datetime');
        if (datetimeEl) {
            datetimeEl.textContent = currentItem ? itemTimestampText(currentItem) : '-';
            datetimeEl.title = String(currentItem?.timestamp || '').trim()
                ? `${String(currentItem.timestamp).trim()} (shown in Central time)`
                : '';
        }
        const authorEl = document.getElementById('info-author');
        if (authorEl) {
            authorEl.textContent = currentItem ? itemAuthorText(currentItem) : '-';
            authorEl.title = String(currentItem?.author_id || '').trim() ? `User ID: ${String(currentItem.author_id).trim()}` : '';
        }
        const channelEl = document.getElementById('info-channel');
        if (channelEl) {
            channelEl.textContent = currentItem ? itemChannelText(currentItem) : '-';
            channelEl.title = String(currentItem?.channel_id || '').trim() ? `Channel ID: ${String(currentItem.channel_id).trim()}` : '';
        }
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
        const pendingCountEl = document.getElementById('pending-count');
        pendingCountEl.textContent = `${pendingUpdates.length} pending`;
        pendingCountEl.title = pendingUpdates.lastPersistenceError
            ? 'Pending work is in memory, but browser session recovery is unavailable.'
            : 'Unsaved annotations in this browser tab';
        pendingCountEl.classList.toggle('has-pending', pendingUpdates.length > 0);
        pendingCountEl.classList.toggle('storage-error', !!pendingUpdates.lastPersistenceError);
        syncPendingUnloadWarning();
        const saveButton = document.getElementById('btn-save-all');
        if (saveButton && !saveInFlight) {
            saveButton.disabled = pendingUpdates.length === 0;
        }
        if (currentItem) ensureCurrentItemContext(currentItem);
    }

    function setStatus(msg) {
        if (!statusEl) return;
        const text = String(msg || '');
        const lower = text.toLowerCase();
        statusEl.textContent = text;
        statusEl.dataset.tone = /fail|error|invalid|unavailable|blocked/.test(lower)
            ? 'error'
            : /load|sav|prepar|warm|switch|fetch|process|schedul/.test(lower)
                ? 'busy'
                : /ready|saved|done|undone|restored/.test(lower)
                    ? 'success'
                    : 'neutral';
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
            const queuedSheetUpdate = !!(payload && payload.queued && payload.applied !== true);
            if (queuedSheetUpdate) {
                const pendingCount = Number(payload?.queue_pending || 0);
                const deduped = !!payload?.queue_deduped;
                const suffix = pendingCount > 1 ? ` (${pendingCount} queued)` : '';
                setStatus(
                    deduped
                        ? `sn${serial} already queued for background flag sync${suffix}.`
                        : `Flagged sn${serial} (sheet sync queued in background)${suffix}.`
                );
            } else if (payload && payload.changed === false) {
                setStatus(incorrectFlagMode
                    ? `sn${serial} is already unlabeled. Flag mode still ON.`
                    : `sn${serial} is already unlabeled.`);
            } else {
                setStatus(incorrectFlagMode
                    ? `Flagged sn${serial} for relabeling (sheet updated). Flag mode still ON.`
                    : `Flagged sn${serial} for relabeling (sheet updated).`);
            }
            if (!queuedSheetUpdate) {
                try {
                    await refreshQueues(true);
                    applyModeQueue();
                    updateInfo();
                } catch (e) {
                    // Queue refresh is best-effort after flagging.
                }
            } else {
                updateInfo();
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
        currentSamMaskTiles.push(null);
        selectedBoxIdx = currentBoxes.length - 1;
        boxesTouched = true;
        drawCanvas();
        setStatus('Added new box');
    }

    function deleteSelectedBox() {
        if (currentBoxes.length === 0) return;
        currentBoxes.splice(selectedBoxIdx, 1);
        currentSamMaskTiles.splice(selectedBoxIdx, 1);
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
        pendingUpdates.upsert(update);
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

            // Prime the immediate next detect item with pre-display SAM refine.
            primeHotNextDetectItem();

            history.push({
                type: 'detect',
                mode: labelerMode,
                serial: currentSerial,
                queueIndex: Number(queueIndex),
                boxes: currentBoxes.map((b) => ({ ...b })),
                samMaskTiles: cloneSamMaskTiles(currentSamMaskTiles),
            });
        } else {
            const duplicateCats = _duplicateAssignedCats(currentLabels);
            if (duplicateCats.length) {
                setStatus(`Duplicate cat labels are not allowed in one image: ${duplicateCats.join(', ')}`);
                return;
            }
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
        queueAdvanceStartedAt = Date.now();
        queueAdvanceFromSerial = Number(currentSerial || 0) || null;
        queueAdvanceMeta = {
            mode: String(labelerMode || ''),
            from_serial: Number(currentSerial || 0) || null,
            to_serial: null,
            started_at: queueAdvanceStartedAt,
            claim_started_at: 0,
            claim_granted_at: 0,
            load_image_at: 0,
            image_loaded_at: 0,
            claim_retry_loops: 0,
            claim_error_kind: '',
            pred_cache_hit: false,
            classify_warm_ready: false,
            drive_like: false,
            image_source: '',
            image_intent: '',
            image_prefetch_ready: false,
            item_ready_wait_ms: 0,
            item_ready_wait_ready: false,
        };
        setStatus(labelerMode === 'classify' ? 'Loading next classifier photo...' : 'Loading next item...');
        if (labelerMode === 'classify') {
            setWarmOverlay(true, 'Loading next classifier photo...', 'Claiming item and starting image load', 0.08);
        }
        void releaseCurrentClaim();
        queueIndex++;
        modePositions[labelerMode] = queueIndex;
        if (queueIndex < queue.length) {
            const nextItem = queue[queueIndex];
            if (nextItem && nextItem.serial) {
                prefetchImageSerial(nextItem.serial);
                preclaimItem(nextItem, labelerMode);
                preclaimAhead(labelerMode, queueIndex, CLAIM_PRECLAIM_AHEAD_COUNT);
                if (labelerMode === 'classify') {
                    void ensureClassifyItemReady(nextItem, true, true);
                } else if (labelerMode === 'detect') {
                    primeHotNextDetectItem();
                }
            }
            void loadCurrentItem();
        } else {
            queueAdvanceStartedAt = 0;
            queueAdvanceFromSerial = null;
            queueAdvanceMeta = null;
            setStatus('Queue complete! Save pending changes.');
            clearCanvas();
        }
    }

    function undoLast() {
        if (saveInFlight) {
            setStatus('Wait for the current save to finish before undoing.');
            return;
        }
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

        const mutatesPending = last && ['detect', 'classify', 'reject'].includes(String(last.type || ''));
        if (mutatesPending) {
            pendingUpdates.remove(last?.serial);
        }

        if (!pendingUndoRestore && last) {
            if ((last.type === 'classify' || last.type === 'manual_defer') && Array.isArray(last.labels)) {
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
                    samMaskTiles: cloneSamMaskTiles(last.samMaskTiles || []),
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
        if (saveInFlight) {
            setStatus('A save is already in progress.');
            return;
        }
        if (pendingUpdates.length === 0) {
            setStatus('Nothing to save');
            return;
        }

        const savePayload = pendingUpdates.snapshot(SAVE_BATCH_MAX);
        const saveButton = document.getElementById('btn-save-all');
        saveInFlight = true;
        if (saveButton) {
            saveButton.disabled = true;
            saveButton.setAttribute('aria-busy', 'true');
            saveButton.textContent = 'Saving…';
        }
        setStatus(`Saving ${savePayload.length} updates...`);
        try {
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
            const hasSavedSerials = Array.isArray(resp?.saved_serials);
            const savedSerials = hasSavedSerials ? resp.saved_serials : null;
            pendingUpdates.acknowledge(savePayload, savedSerials);
            const savedCount = Number.isFinite(Number(resp?.saved))
                ? Number(resp.saved)
                : (savedSerials ? savedSerials.length : savePayload.length);
            const missingCount = Array.isArray(resp?.missing_serials) ? resp.missing_serials.length : 0;
            if (missingCount > 0) {
                setStatus(`Saved ${savedCount}; ${missingCount} missing item${missingCount === 1 ? '' : 's'} remain pending.`);
            } else if (pendingUpdates.length > 0) {
                setStatus(`Saved ${savedCount}; ${pendingUpdates.length} update${pendingUpdates.length === 1 ? '' : 's'} still pending.`);
            } else {
                setStatus(`Saved ${savedCount} update${savedCount === 1 ? '' : 's'}.`);
            }
            updateInfo();
        } catch (e) {
            setStatus(`Save failed: ${e.message}`);
        } finally {
            saveInFlight = false;
            if (saveButton) {
                saveButton.disabled = pendingUpdates.length === 0;
                saveButton.removeAttribute('aria-busy');
                saveButton.textContent = 'Save All';
            }
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

    function _readyPredictionCandidatesForCrop(results, cropIdx) {
        return _visiblePredictionCandidatesForCrop(results, cropIdx).slice(0, 9);
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
        const candidates = _readyPredictionCandidatesForCrop(rows, idx);
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
        const candidates = _readyPredictionCandidatesForCrop(rows, idx);
        let count = 0;
        let withRefs = 0;
        for (const cand of candidates) {
            let hasAny = false;
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            for (const ref of refs) {
                const srcs = _getRefDisplaySources(ref);
                const baseSrc = srcs.baseSrc;
                const fastSrc = srcs.fastSrc;
                const hqSrc = srcs.hqSrc;
                if (hqSrc && isRefImageReady(hqSrc)) {
                    count += 1;
                    hasAny = true;
                } else if (fastSrc && isRefImageReady(fastSrc)) {
                    count += 1;
                    hasAny = true;
                } else if (baseSrc && isRefImageReady(baseSrc)) {
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
        const candidates = _readyPredictionCandidatesForCrop(rows, idx);
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
            if (refs.length <= 0) continue;
            const expectedRefs = Math.min(threshold, refs.length);
            if (refCount >= expectedRefs) atDepth += 1;
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
        const candidates = _readyPredictionCandidatesForCrop(rows, idx);
        let atDepth = 0;
        for (const cand of candidates) {
            let refCount = 0;
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            for (const ref of refs) {
                const srcs = _getRefDisplaySources(ref);
                const baseSrc = srcs.baseSrc;
                const fastSrc = srcs.fastSrc;
                const hqSrc = srcs.hqSrc;
                if (hqSrc && isRefImageReady(hqSrc)) {
                    refCount += 1;
                } else if (fastSrc && isRefImageReady(fastSrc)) {
                    refCount += 1;
                } else if (baseSrc && isRefImageReady(baseSrc)) {
                    refCount += 1;
                }
            }
            if (refs.length <= 0) continue;
            const expectedRefs = Math.min(threshold, refs.length);
            if (refCount >= expectedRefs) atDepth += 1;
        }
        return {
            targetCount: candidates.length,
            candidatesAtDepth: atDepth,
            minRefsPerCandidate: threshold,
        };
    }

    function _predictionLoadedRefCountsForCrop(results, cropIdx) {
        const rows = Array.isArray(results) ? results : [];
        const idx = Math.max(0, Number(cropIdx || 0));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) {
            return [];
        }
        const counts = [];
        for (const cand of _readyPredictionCandidatesForCrop(rows, idx)) {
            let refCount = 0;
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            for (const ref of refs) {
                const srcs = _getRefDisplaySources(ref);
                const baseSrc = srcs.baseSrc;
                const fastSrc = srcs.fastSrc;
                const hqSrc = srcs.hqSrc;
                if (hqSrc && isRefImageReady(hqSrc)) {
                    refCount += 1;
                } else if (fastSrc && isRefImageReady(fastSrc)) {
                    refCount += 1;
                } else if (baseSrc && isRefImageReady(baseSrc)) {
                    refCount += 1;
                }
            }
            counts.push(refCount);
        }
        return counts;
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
        return _readyPredictionCandidatesForCrop(rows, idx).length > 0;
    }

    function _predictionHasInlineRefsForCrop(results, cropIdx, minInline = 1) {
        const rows = Array.isArray(results) ? results : [];
        if (!rows.length) return false;
        const idx = Math.max(0, Math.min(Number(cropIdx || 0), rows.length - 1));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) return false;
        let inlineCount = 0;
        for (const cand of _readyPredictionCandidatesForCrop(rows, idx)) {
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            for (const ref of refs.slice(0, CLASSIFY_REFS_PER_CAT_TARGET)) {
                if (typeof ref === 'string') {
                    if (String(ref || '').startsWith('data:image')) {
                        inlineCount += 1;
                    }
                    continue;
                }
                const img = String(ref?.img || ref?.thumb || '').trim();
                if (img) inlineCount += 1;
                if (inlineCount >= Math.max(1, Number(minInline || 1))) return true;
            }
        }
        return inlineCount >= Math.max(1, Number(minInline || 1));
    }

    function _predictionRefsAtTargetForCrop(results, cropIdx) {
        const depth = _predictionRefDepthForCrop(results, cropIdx, CLASSIFY_REFS_PER_CAT_TARGET);
        if (depth.targetCount <= 0) return false;
        return depth.candidatesAtDepth >= depth.targetCount;
    }

    function _predictionLoadedRefsAtTargetForCrop(results, cropIdx) {
        const depth = _predictionLoadedRefDepthForCrop(results, cropIdx, CLASSIFY_REFS_PER_CAT_TARGET);
        if (depth.targetCount <= 0) return false;
        return depth.candidatesAtDepth >= depth.targetCount;
    }

    function _predictionRefSignatureForCrop(results, cropIdx) {
        const rows = Array.isArray(results) ? results : [];
        const idx = Math.max(0, Number(cropIdx || 0));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) return '';
        const parts = [];
        for (const cand of _readyPredictionCandidatesForCrop(rows, idx)) {
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
        const focusCropIdx = Math.max(0, Math.min(Number(currentCropIdx || 0), parsedBoxes.length - 1));
        const refreshKey = getPredFocusCacheKey(currentItem, focusCropIdx);
        if (!refreshKey) return;
        const inFlight = classifyRefRefreshInFlight.get(refreshKey);
        if (inFlight) {
            return inFlight;
        }
        const lastRefresh = Number(classifyRefRefreshTs.get(refreshKey) || 0);
        const now = Date.now();
        if ((now - lastRefresh) < CLASSIFY_REF_REFRESH_COOLDOWN_MS) {
            return;
        }
        const refreshPromise = (async () => {
            classifyRefRefreshTs.set(refreshKey, now);
            for (let attempt = 0; attempt < CLASSIFY_REF_RETRY_ATTEMPTS; attempt++) {
                if (requestSerial !== currentSerial || requestKey !== getPredCacheKey(currentItem)) return;
                if (_predictionRefsAtTargetForCrop(currentPredictions, focusCropIdx)) return;
                // Keep ref refresh fully in background; do not block interaction with an overlay.
                try {
                    const retry = await postClassifyIdentify(
                        currentItem,
                        parsedBoxes,
                        {
                            maxAttempts: 1,
                            timeoutMs: Math.min(API_PREFETCH_TIMEOUT_MS, 9000),
                            focusCropIdx,
                        },
                    );
                    const nextPreds = retry?.results || [];
                    const mergedPreds = mergePredictionResults(currentPredictions, nextPreds, focusCropIdx);
                    prefetchDisplayRefsForCrop(mergedPreds, focusCropIdx, 'high');
                    if (requestSerial !== currentSerial || requestKey !== getPredCacheKey(currentItem)) return;
                    const prevCov = _predictionRefCoverageForCrop(currentPredictions, focusCropIdx);
                    const prevDepth = _predictionRefDepthForCrop(currentPredictions, focusCropIdx, CLASSIFY_REFS_PER_CAT_TARGET);
                    const prevSig = _predictionRefSignatureForCrop(currentPredictions, focusCropIdx);
                    const nextCov = _predictionRefCoverageForCrop(mergedPreds, focusCropIdx);
                    const nextDepth = _predictionRefDepthForCrop(mergedPreds, focusCropIdx, CLASSIFY_REFS_PER_CAT_TARGET);
                    const nextSig = _predictionRefSignatureForCrop(mergedPreds, focusCropIdx);
                    const improved = (
                        nextCov.candidatesWithRefs > prevCov.candidatesWithRefs
                        || nextCov.refCount > prevCov.refCount
                        || nextDepth.candidatesAtDepth > prevDepth.candidatesAtDepth
                        || nextSig !== prevSig
                    );
                    if (improved) {
                        currentPredictions = mergedPreds;
                        clampPredictionCropIdx(currentPredictions);
                        if (requestKey) predCache.set(requestKey, currentPredictions);
                        renderPredictions();
                    }
                } catch (e) {
                    // Best-effort retries while refs load.
                }
                if (_predictionRefsAtTargetForCrop(currentPredictions, focusCropIdx)) return;
                await waitMs(220 * (attempt + 1));
            }
        })();
        classifyRefRefreshInFlight.set(refreshKey, refreshPromise);
        try {
            await refreshPromise;
        } finally {
            if (classifyRefRefreshInFlight.get(refreshKey) === refreshPromise) {
                classifyRefRefreshInFlight.delete(refreshKey);
            }
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
            const diagRenderWithoutTargetRefs = (source) => {
                if (labelerMode !== 'classify') return;
                const depth = _predictionLoadedRefDepthForCrop(currentPredictions, currentCropIdx, CLASSIFY_REFS_PER_CAT_TARGET);
                const cov = _predictionLoadedRefCoverageForCrop(currentPredictions, currentCropIdx);
                if (depth.targetCount <= 0) return;
                if (depth.candidatesAtDepth >= depth.targetCount) return;
                void postUiDiag('classify_predictions_rendered_before_full_refs', {
                    serial: Number(currentSerial || 0) || null,
                    key: String(activeKey || ''),
                    source: String(source || ''),
                    crop_idx: Number(currentCropIdx || 0),
                    target_candidates: Number(depth.targetCount || 0),
                    candidates_at_target_depth: Number(depth.candidatesAtDepth || 0),
                    refs_with_loaded_images: Number(cov.candidatesWithRefs || 0),
                    refs_target_candidates: Number(cov.targetCount || 0),
                    ref_queue: compactRefImageQueueDiag(),
                });
            };
            if (!force) {
                if (currentPredictions.length) {
                    if (_predictionHasOptionsForCrop(currentPredictions, currentCropIdx)) {
                        prefetchDisplayRefsForCrop(currentPredictions, currentCropIdx, 'high');
                        renderPredictions();
                        diagRenderWithoutTargetRefs('current_predictions');
                        void prefetchUpcomingClassifyCrop(currentCropIdx);
                        primeHotNextClassifyItem();
                        const rs = currentSerial;
                        const rk = getPredCacheKey(currentItem);
                        if (!_predictionRefsAtTargetForCrop(currentPredictions, currentCropIdx)) {
                            void _refreshCurrentPredictionRefs(rs, rk, 'Loading additional reference photos...');
                        }
                        return;
                    }
                }
                const cached = predCache.get(activeKey);
                if (cached) {
                    currentPredictions = cached;
                    prefetchDisplayRefsForCrop(currentPredictions, currentCropIdx, 'high');
                    clampPredictionCropIdx(currentPredictions);
                    if (_predictionHasOptionsForCrop(currentPredictions, currentCropIdx)) {
                        renderPredictions();
                        diagRenderWithoutTargetRefs('pred_cache');
                        void prefetchUpcomingClassifyCrop(currentCropIdx);
                        primeHotNextClassifyItem();
                        const rs = currentSerial;
                        const rk = getPredCacheKey(currentItem);
                        if (!_predictionRefsAtTargetForCrop(currentPredictions, currentCropIdx)) {
                            void _refreshCurrentPredictionRefs(rs, rk, 'Loading additional reference photos...');
                        }
                        return;
                    }
                }
                // Do not block UI waiting on background prefetch; foreground should always proceed.
            }

            if (!force && activeKey && classifyWarmInFlight.has(activeKey) && !predCache.has(activeKey)) {
                const joined = await _waitFor(() => {
                    if (activeKey !== getPredCacheKey(currentItem)) return true;
                    return predCache.has(activeKey);
                }, CLASSIFY_WARM_JOIN_TIMEOUT_MS, 70);
                if (joined && activeKey === getPredCacheKey(currentItem)) {
                    const warmed = predCache.get(activeKey);
                    if (Array.isArray(warmed) && warmed.length) {
                        currentPredictions = warmed;
                        prefetchDisplayRefsForCrop(currentPredictions, currentCropIdx, 'high');
                        clampPredictionCropIdx(currentPredictions);
                        if (_predictionHasOptionsForCrop(currentPredictions, currentCropIdx)) {
                            renderPredictions();
                            diagRenderWithoutTargetRefs('warm_join');
                            void prefetchUpcomingClassifyCrop(currentCropIdx);
                            primeHotNextClassifyItem();
                            const rs = currentSerial;
                            const rk = getPredCacheKey(currentItem);
                            if (!_predictionRefsAtTargetForCrop(currentPredictions, currentCropIdx)) {
                                void _refreshCurrentPredictionRefs(rs, rk, 'Loading additional reference photos...');
                            }
                            return;
                        }
                    }
                }
            }

            setStatus('Running classifier...');
            try {
                const requestSerial = currentSerial;
                const requestKey = getPredCacheKey(currentItem);
                const requestFocusCropIdx = Math.max(0, Math.min(
                    Number(currentCropIdx || 0),
                    Math.max(0, currentBoxes.length - 1),
                ));
                let data = null;
                try {
                    data = await postClassifyIdentify(
                        currentItem,
                        currentBoxes.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                        { maxAttempts: 4, focusCropIdx: requestFocusCropIdx },
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
                                { maxAttempts: 2, focusCropIdx: requestFocusCropIdx },
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
                const incomingPredictions = data.results || [];
                const cachedPredictions = requestKey ? predCache.get(requestKey) : [];
                currentPredictions = mergePredictionResults(
                    cachedPredictions,
                    incomingPredictions,
                    requestFocusCropIdx,
                );
                if ((!Array.isArray(currentPredictions) || !currentPredictions.length) && currentBoxes.length) {
                    // Rare safety net: if identify returned empty despite valid boxes, retry once.
                    data = await postClassifyIdentify(
                        currentItem,
                        currentBoxes.map(b => `${b.cx} ${b.cy} ${b.w} ${b.h}`),
                        { maxAttempts: 2, focusCropIdx: requestFocusCropIdx },
                    );
                    currentPredictions = mergePredictionResults(
                        cachedPredictions,
                        data.results || [],
                        requestFocusCropIdx,
                    );
                }
                prefetchDisplayRefsForCrop(currentPredictions, currentCropIdx, 'high');
                clampPredictionCropIdx(currentPredictions);
                const key = getPredCacheKey(currentItem);
                if (key) {
                    predCache.set(key, currentPredictions);
                    clearClassifyPrefetchFailure(key);
                }
                const cov = _predictionLoadedRefCoverageForCrop(currentPredictions, currentCropIdx);
                renderPredictions();
                diagRenderWithoutTargetRefs('foreground_identify');
                void prefetchUpcomingClassifyCrop(requestFocusCropIdx);
                if (!_predictionRefsAtTargetForCrop(currentPredictions, currentCropIdx)) {
                    void _refreshCurrentPredictionRefs(requestSerial, requestKey, 'Loading reference photos...');
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
        const visibleCandidates = _visiblePredictionCandidatesForCrop(currentPredictions, currentCropIdx);
        if (!visibleCandidates.length) {
            listEl.innerHTML = '<div class="no-predictions">No available predictions</div>';
            return;
        }

        listEl.innerHTML = visibleCandidates.slice(0, 9).map((c, i) => {
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
                const srcs = _getRefDisplaySources(info);
                const baseSrc = srcs.baseSrc;
                const inlineSrc = srcs.inlineSrc;
                const fastSrc = srcs.fastSrc;
                const hqSrc = srcs.hqSrc;
                const fallbackUrlSrc = srcs.fallbackUrlSrc;

                if (inlineSrc && inlineSrc !== baseSrc) {
                    prefetchRefImageSrc(inlineSrc, { priority: 'high' });
                } else {
                    if (fastSrc) prefetchRefImageSrc(fastSrc, { priority: 'high' });
                    if (baseSrc) prefetchRefImageSrc(baseSrc, { priority: 'high' });
                    if (hqSrc) prefetchRefImageSrc(hqSrc);
                    if (fallbackUrlSrc) prefetchRefImageSrc(fallbackUrlSrc);
                }

                let src = '';
                if (hqSrc && isRefImageReady(hqSrc)) {
                    src = hqSrc;
                } else if (fastSrc && isRefImageReady(fastSrc)) {
                    src = fastSrc;
                } else if (inlineSrc) {
                    src = inlineSrc;
                } else if (fastSrc) {
                    src = fastSrc;
                } else if (hqSrc) {
                    src = hqSrc;
                } else if (baseSrc && isRefImageReady(baseSrc)) {
                    src = baseSrc;
                } else if (baseSrc) {
                    src = baseSrc;
                } else if (fallbackUrlSrc) {
                    src = fallbackUrlSrc;
                }
                if (!src) continue;

                const srcState = String(refImagePrefetchState.get(src) || '');
                if (srcState === 'error') {
                    const backups = [
                        (src === hqSrc ? fastSrc : ''),
                        (src === hqSrc || src === fastSrc ? baseSrc : ''),
                        inlineSrc,
                        fallbackUrlSrc,
                    ].filter(Boolean);
                    const backup = backups.find((candidateSrc) => (
                        String(refImagePrefetchState.get(candidateSrc) || '') !== 'error'
                    )) || '';
                    if (!backup) {
                        continue;
                    }
                    src = backup;
                }

                const sn = info.serial != null ? `sn${info.serial}` : '';
                const isFlagged = isRefSerialFlagged(info.serial);
                const isFlagging = isRefSerialFlagging(info.serial);
                if (isFlagged) continue;
                const cropNum = Number(info.crop) || null;
                const cropText = cropNum ? ` crop ${cropNum}` : '';
                const caption = sn || cropNum ? `${sn}${cropText}`.trim() : '';
                const serialAttr = info.serial != null ? ` data-ref-serial="${escapeHtml(String(info.serial))}"` : '';
                const cropAttr = cropNum ? ` data-ref-crop="${escapeHtml(String(cropNum))}"` : '';
                refs.push(`
                    <div class="ref-frame${isFlagged ? ' ref-flagged' : ''}${isFlagging ? ' ref-flagging' : ''}"${serialAttr}${cropAttr}>
                        <img loading="eager" decoding="async" src="${escapeHtml(src)}" alt="${safeName} ref ${refIdx + 1}">
                        ${caption ? `<div class="ref-overlay">${escapeHtml(caption)}</div>` : ''}
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
            prefetchDisplayManualRefs(manualCandidates, 'high');
            renderManualCandidates();
            prefetchManualCandidates();
            return;
        }

        if (!force && manualCandidateCache.has(requestKey)) {
            const cached = manualCandidateCache.get(requestKey) || {};
            manualCandidates = Array.isArray(cached.candidates) ? cached.candidates : [];
            manualCandidatesKey = requestKey;
            prefetchDisplayManualRefs(manualCandidates, 'high');
            renderManualCandidates();
            setStatus(`Ready - sn${currentSerial}`);
            prefetchManualCandidates();
            return;
        }
        setStatus('Loading manual review options...');
        setWarmOverlay(true, 'Loading manual review options...', 'Comparing against all cats', 0.25);

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

            manualCandidates = Array.isArray(payload?.candidates) ? payload.candidates : [];
            manualCandidatesKey = requestKey;
            _setManualCandidateCache(requestKey, { candidates: manualCandidates });
            prefetchDisplayManualRefs(manualCandidates, 'high');
            renderManualCandidates();
            setStatus(`Ready - sn${currentSerial}`);
            prefetchManualCandidates();
        } catch (e) {
            setStatus(`Manual review load failed: ${e.message}`);
        } finally {
            if (labelerMode === 'manual') {
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
        const visibleCandidates = _visibleManualCandidatesForCrop(manualCandidates, currentCropIdx);
        if (!visibleCandidates.length) {
            listEl.innerHTML = '<div class="no-predictions">No candidates available.</div>';
            if (restoreTop != null && sidebarEl) sidebarEl.scrollTop = restoreTop;
            return;
        }

        listEl.innerHTML = visibleCandidates.map((cand, candIdx) => {
            const name = String(cand?.name || '').trim();
            const catId = cand && cand.cat_id != null ? String(cand.cat_id) : '';
            const rawDisplay = String(cand?.display_name || name || '').trim();
            const displayName = (
                catId
                    ? rawDisplay.replace(new RegExp(`^\\s*${catId}\\s*[.)\\-:]\\s*`), '').trim()
                    : rawDisplay
            ) || name;
            const refs = Array.isArray(cand?.refs) ? cand.refs : [];
            const safeDisplayName = escapeHtml(displayName || name);
            const safeDesc = escapeHtml(String(cand?.desc || '').trim());
            const selectName = String(name || rawDisplay || '').trim();
            const safeSelectName = escapeHtml(selectName);
            const confRaw = Number(cand?.conf);
            const hasConf = Number.isFinite(confRaw) && confRaw > 0;
            const confPct = hasConf ? Math.max(0, Math.min(100, confRaw * 100)) : 0;
            let confLabel = '';
            let confClass = '';
            if (hasConf) {
                confLabel = 'Low Confidence';
                confClass = 'conf-low';
                if (confPct >= 75) {
                    confLabel = 'High Confidence';
                    confClass = 'conf-high';
                } else if (confPct >= 50) {
                    confLabel = 'Medium Confidence';
                    confClass = 'conf-med';
                }
            }
            const refsHtml = [];
            for (let refIdx = 0; refIdx < refs.length; refIdx++) {
                if (refsHtml.length >= MANUAL_REFS_PER_CAT_TARGET) break;
                const ref = refs[refIdx];
                const info = typeof ref === 'string'
                    ? { img: ref, serial: null, crop: null }
                    : (ref || {});
                const srcs = _getRefDisplaySources(info);
                const baseSrc = srcs.baseSrc;
                const inlineSrc = srcs.inlineSrc;
                const fastSrc = srcs.fastSrc;
                const hqSrc = srcs.hqSrc;
                const fallbackUrlSrc = srcs.fallbackUrlSrc;

                let src = '';
                if (hqSrc && isRefImageReady(hqSrc)) {
                    src = hqSrc;
                } else if (fastSrc && isRefImageReady(fastSrc)) {
                    src = fastSrc;
                } else if (inlineSrc) {
                    src = inlineSrc;
                } else if (fastSrc) {
                    src = fastSrc;
                } else if (hqSrc) {
                    src = hqSrc;
                } else if (baseSrc && isRefImageReady(baseSrc)) {
                    src = baseSrc;
                } else if (baseSrc) {
                    src = baseSrc;
                } else if (fallbackUrlSrc) {
                    src = fallbackUrlSrc;
                }
                if (!src) continue;

                const srcState = String(refImagePrefetchState.get(src) || '');
                if (srcState === 'error') {
                    const backups = [
                        (src === hqSrc ? fastSrc : ''),
                        (src === hqSrc || src === fastSrc ? baseSrc : ''),
                        inlineSrc,
                        fallbackUrlSrc,
                    ].filter(Boolean);
                    const backup = backups.find((candidateSrc) => (
                        String(refImagePrefetchState.get(candidateSrc) || '') !== 'error'
                    )) || '';
                    if (!backup) {
                        continue;
                    }
                    src = backup;
                }

                const sn = info.serial != null ? `sn${info.serial}` : '';
                const isFlagged = isRefSerialFlagged(info.serial);
                const isFlagging = isRefSerialFlagging(info.serial);
                if (isFlagged) continue;
                const cropNum = Number(info.crop) || null;
                const cropText = cropNum ? ` crop ${cropNum}` : '';
                const caption = sn || cropNum ? `${sn}${cropText}`.trim() : '';
                const serialAttr = info.serial != null ? ` data-ref-serial="${escapeHtml(String(info.serial))}"` : '';
                const cropAttr = cropNum ? ` data-ref-crop="${escapeHtml(String(cropNum))}"` : '';
                refsHtml.push(`
                    <div class="ref-frame${isFlagged ? ' ref-flagged' : ''}${isFlagging ? ' ref-flagging' : ''}"${serialAttr}${cropAttr}>
                        <img loading="lazy" decoding="async" src="${escapeHtml(src)}" alt="${safeDisplayName} ref ${refIdx + 1}">
                        ${caption ? `<div class="ref-overlay">${escapeHtml(caption)}</div>` : ''}
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
                        <span class="pred-key">#${candIdx + 1}</span>
                        <span class="pred-name">${safeDisplayName}</span>
                        ${hasConf ? `<span class="pred-conf ${confClass}">${confLabel} (${confPct.toFixed(1)}%)</span>` : ''}
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
        if (labelerMode === 'classify') {
            currentPredictions = [];
            renderPredictions();
        }
        advanceCrop();
    }

    function markNeedsReview() {
        pushCropLabelUndo('NeedsReview');
        currentLabels[currentCropIdx] = 'NeedsReview';
        if (labelerMode === 'classify') {
            currentPredictions = [];
            renderPredictions();
        }
        advanceCrop();
    }

    function rejectCrop() {
        pushCropLabelUndo('Rejected');
        currentLabels[currentCropIdx] = 'Rejected';
        if (labelerMode === 'classify') {
            currentPredictions = [];
            renderPredictions();
        }
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
        history.push({
            type: 'manual_defer',
            mode: labelerMode,
            serial: Number(currentSerial),
            queueIndex: Number(queueIndex),
            cropIdx: currentBoxes.length
                ? Math.max(0, Math.min(Number(currentCropIdx || 0), currentBoxes.length - 1))
                : 0,
            labels: [...currentLabels],
        });
        setStatus(`Deferred sn${currentSerial}`);
        advanceQueue();
    }

    // Hallucinated progress functions removed

    function _normalizeAssignedCatLabel(label) {
        const raw = String(label || '').trim();
        if (!raw) return '';
        const key = raw.toLowerCase();
        if (key === 'needsreview' || key === 'needs review' || key === 'rejected') {
            return '';
        }
        return raw;
    }

    function _normalizeAssignedCatKey(label) {
        const normalized = _normalizeAssignedCatLabel(label);
        return normalized
            ? normalized
                .replace(/^\s*\d+\s*[.)\-:]?\s*/, '')
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, '')
            : '';
    }

    function _assignedCatKeysExcludingCrop(cropIdx) {
        const targetIdx = Math.max(0, Number(cropIdx || 0));
        const used = new Set();
        for (let i = 0; i < currentLabels.length; i++) {
            if (i === targetIdx) continue;
            const key = _normalizeAssignedCatKey(currentLabels[i]);
            if (key) used.add(key);
        }
        return used;
    }

    function _filterCandidatesByAssignedCats(candidates, cropIdx) {
        const rows = Array.isArray(candidates) ? candidates : [];
        if (!rows.length) return [];
        const blocked = _assignedCatKeysExcludingCrop(cropIdx);
        if (!blocked.size) return rows.slice();
        return rows.filter((cand) => {
            const key = _normalizeAssignedCatKey(cand?.name || '');
            return !key || !blocked.has(key);
        });
    }

    function _visiblePredictionCandidatesForCrop(results, cropIdx) {
        const rows = Array.isArray(results) ? results : [];
        if (!rows.length) return [];
        const idx = Math.max(0, Math.min(Number(cropIdx || 0), rows.length - 1));
        const crop = rows[idx];
        if (!crop || !Array.isArray(crop.candidates)) return [];
        return _filterCandidatesByAssignedCats(crop.candidates, idx);
    }

    function _visibleManualCandidatesForCrop(candidates, cropIdx) {
        const idx = Math.max(0, Number(cropIdx || 0));
        return _filterCandidatesByAssignedCats(candidates, idx);
    }

    function _duplicateAssignedCats(labels = currentLabels) {
        const rows = Array.isArray(labels) ? labels : [];
        const seen = new Set();
        const duplicates = [];
        for (const label of rows) {
            const normalized = _normalizeAssignedCatLabel(label);
            if (!normalized) continue;
            const key = normalized.toLowerCase();
            if (seen.has(key)) {
                if (!duplicates.includes(normalized)) duplicates.push(normalized);
                continue;
            }
            seen.add(key);
        }
        return duplicates;
    }

    function prefetchPredictions() {
        if (labelerMode !== 'classify') return;
        primeHotNextClassifyItem();
    }

    function primeHotNextClassifyItem() {
        if (labelerMode !== 'classify') return;
        const primeAheadMax = 1;
        for (let offset = 1; offset <= primeAheadMax; offset++) {
            _primeClassifyItemAtOffset(offset);
        }
    }

    function _primeClassifyItemAtOffset(offset) {
        const item = queue[queueIndex + offset];
        if (!item || !item.boxes) return;
        const key = getPredCacheKey(item);
        if (key && predCache.has(key)) {
            const rows = predCache.get(key) || [];
            if (_classifyItemFullyReady(item)) {
                return;
            }
            if (Array.isArray(rows) && rows.length) {
                prefetchDisplayRefsForItem(item, rows, offset === 1 ? 'high' : 'normal');
            }
        }
        if (
            !key
            || classifyWarmInFlight.has(key)
            || classifyForegroundInFlight.has(key)
            || classifyPrimeInFlight.has(key)
        ) return;
        classifyPrimeInFlight.add(key);
        void (async () => {
            const t0 = Date.now();
            let ok = false;
            try {
                ok = await ensureClassifyItemReady(item, true, true);
            } catch (e) {
                ok = false;
            } finally {
                classifyPrimeInFlight.delete(key);
            }
            const dt = Math.max(0, Date.now() - t0);
            if (dt >= 500 || !ok) {
                void postUiDiag('classify_next_prime', {
                    serial: Number(item?.serial || 0) || null,
                    ok: !!ok,
                    ms: dt,
                    offset,
                    preds_cached: !!predCache.has(key),
                    warm_ready: !!_classifyItemWarmReady(item),
                    refs_ready: !!_classifyItemRefsReady(item),
                    image_ready: !!isPrefetchedImageReady(item?.serial),
                    ref_queue: compactRefImageQueueDiag(),
                });
            }
        })();
    }

    function prefetchImages() {
        const start = queueIndex + 1;
        const ahead = labelerMode === 'classify' ? IMAGE_PREFETCH_AHEAD_CLASSIFY : IMAGE_PREFETCH_AHEAD;
        const end = Math.min(queue.length, start + ahead);
        for (let i = start; i < end; i++) {
            const item = queue[i];
            if (!item || !item.serial) continue;
            queueImagePrefetchSerial(item.serial, { priority: i === start ? 'high' : 'normal' });
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
                isDetectItemWarmReady(item) ||
                detectPrefetchInFlight.has(key) ||
                detectWarmInFlight.has(key) ||
                isDetectWarmBlocked(key)
            ) continue;
            targets.push({ item, key });
        }
        if (!targets.length) return;

        detectPrefetchRunning = true;
        void (async () => {
            try {
                for (const target of targets.slice(0, DETECT_PREFETCH_CONCURRENCY || 1)) {
                    if (epoch !== detectPrefetchEpoch) break;
                    detectPrefetchInFlight.add(target.key);
                    try {
                        await ensureDetectItemReady(target.item, true, true);
                    } finally {
                        detectPrefetchInFlight.delete(target.key);
                    }
                }
            } finally {
                detectPrefetchRunning = false;
                if (detectPrefetchRequested) {
                    detectPrefetchRequested = false;
                    prefetchDetection();
                }
            }
        })();
    }

    function prefetchDetectionRefine() {
        return;
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
            if (!pressedKeys.has(key)) {
                pressedKeys.add(key);
                movementHoldStartByKey.set(key, performance.now());
                applyMovementStep(NUDGE_PX);
                ensureMovementLoop();
            }
            return;
        }

        const actionKeys = new Set(['backspace', 'y', 'enter', 'n', 'x', 'tab', 'space', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9']);
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
            movementHoldStartByKey.delete(key);
            if (pressedKeys.size === 0) {
                stopMovementLoop();
            }
        }
    }

    function ensureMovementLoop() {
        if (movementFrameId !== null) return;
        lastMovementFrameTs = 0;
        movementFrameId = requestAnimationFrame(runMovementFrame);
    }

    function stopMovementLoop() {
        pressedKeys.clear();
        movementHoldStartByKey.clear();
        if (movementFrameId !== null) {
            cancelAnimationFrame(movementFrameId);
            movementFrameId = null;
        }
        lastMovementFrameTs = 0;
        movementRemainderPx.tlx = 0;
        movementRemainderPx.tly = 0;
        movementRemainderPx.brx = 0;
        movementRemainderPx.bry = 0;
    }

    function runMovementFrame(ts) {
        movementFrameId = null;
        if (pressedKeys.size === 0) {
            stopMovementLoop();
            return;
        }
        if (!lastMovementFrameTs) {
            lastMovementFrameTs = ts;
        } else {
            const dtMs = Math.max(0, Math.min(80, ts - lastMovementFrameTs));
            lastMovementFrameTs = ts;
            applyHeldMovement(dtMs, ts);
        }
        if (pressedKeys.size > 0) {
            movementFrameId = requestAnimationFrame(runMovementFrame);
        }
    }

    function _movementSpeedMultiplier(heldMs) {
        const activeMs = Math.max(0, heldMs - MOVE_REPEAT_DELAY_MS);
        const t = Math.max(0, Math.min(1, activeMs / MOVE_ACCEL_RAMP_MS));
        return 1 + ((MOVE_MAX_SPEED_MULTIPLIER - 1) * Math.pow(t, MOVE_ACCEL_EXPONENT));
    }

    function _flushMovementChannel(deltaPx, remainderKey) {
        const total = movementRemainderPx[remainderKey] + deltaPx;
        const wholePx = total >= 0 ? Math.floor(total) : Math.ceil(total);
        movementRemainderPx[remainderKey] = total - wholePx;
        return wholePx;
    }

    function _movementVectorsForStep(stepPx) {
        let dxTL = 0;
        let dyTL = 0;
        let dxBR = 0;
        let dyBR = 0;
        if (pressedKeys.has('w')) dyTL -= stepPx;
        if (pressedKeys.has('s')) dyTL += stepPx;
        if (pressedKeys.has('a')) dxTL -= stepPx;
        if (pressedKeys.has('d')) dxTL += stepPx;
        if (pressedKeys.has('arrowup')) dyBR -= stepPx;
        if (pressedKeys.has('arrowdown')) dyBR += stepPx;
        if (pressedKeys.has('arrowleft')) dxBR -= stepPx;
        if (pressedKeys.has('arrowright')) dxBR += stepPx;
        return { dxTL, dyTL, dxBR, dyBR };
    }

    function applyMovementStep(stepPx) {
        if (pressedKeys.size === 0) return;
        const { dxTL, dyTL, dxBR, dyBR } = _movementVectorsForStep(stepPx);
        if (dxTL || dyTL) nudgeBox('tl', dxTL, dyTL);
        if (dxBR || dyBR) nudgeBox('br', dxBR, dyBR);
    }

    function applyHeldMovement(dtMs, nowTs) {
        if (pressedKeys.size === 0 || dtMs <= 0) return;
        const dtSec = dtMs / 1000;
        let rawDxTL = 0;
        let rawDyTL = 0;
        let rawDxBR = 0;
        let rawDyBR = 0;

        for (const key of pressedKeys) {
            const startedAt = movementHoldStartByKey.get(key) ?? nowTs;
            const heldMs = Math.max(0, nowTs - startedAt);
            if (heldMs < MOVE_REPEAT_DELAY_MS) continue;
            const stepPx = MOVE_BASE_SPEED_PX_PER_SEC * _movementSpeedMultiplier(heldMs) * dtSec;
            switch (key) {
                case 'w': rawDyTL -= stepPx; break;
                case 's': rawDyTL += stepPx; break;
                case 'a': rawDxTL -= stepPx; break;
                case 'd': rawDxTL += stepPx; break;
                case 'arrowup': rawDyBR -= stepPx; break;
                case 'arrowdown': rawDyBR += stepPx; break;
                case 'arrowleft': rawDxBR -= stepPx; break;
                case 'arrowright': rawDxBR += stepPx; break;
            }
        }

        const dxTL = _flushMovementChannel(rawDxTL, 'tlx');
        const dyTL = _flushMovementChannel(rawDyTL, 'tly');
        const dxBR = _flushMovementChannel(rawDxBR, 'brx');
        const dyBR = _flushMovementChannel(rawDyBR, 'bry');
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
    window.labelerAttachPendingJournal = attachPendingJournal;
    window.addEventListener('pagehide', onLabelerPageHide);

    //Auto-init when labeler view is shown
    document.addEventListener('DOMContentLoaded', () => {
        //Will be called by setView when labeler tab is activated
    });

})();
