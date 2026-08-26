/**
 * Regression tests for the labeler's sidebar render and ref-image loader.
 *
 * These cover the frontend counterparts to scripts/test_labeler_hot_paths.py --
 * the paths that made the labeler tab lock up or run out of memory:
 *
 *   1. Every ref image that finished loading re-rendered the whole sidebar
 *      through innerHTML. In manual review that is ~164 cards / ~820 <img>
 *      nodes rebuilt per completed load, so the list fought itself for the
 *      main thread and lost the scroll position and search highlight each time.
 *   2. The ref fetch queue was one array driven by shift()/indexOf()/unshift(),
 *      so the enqueue pass over a rendered list was quadratic in queue depth.
 *   3. Ref-image retries had no attempt ceiling, so a permanently missing crop
 *      retried forever and scheduled a re-render on every failure.
 *   4. The cat id was interpolated straight into a RegExp, so a metacharacter
 *      in it threw out of .map() and killed the render for the entire list.
 *
 * The tests pull the functions out of labeler.js by name and run them, so they
 * exercise the shipped source rather than a copy of it.
 *
 * Run:  node scripts/test_labeler_render.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const SRC_PATH = path.join(__dirname, '..', 'labeler.js');
const SRC = fs.readFileSync(SRC_PATH, 'utf8');

const FAILURES = [];
function check(name, ok, detail) {
    console.log('  %s %s%s', ok ? 'PASS' : 'FAIL', name, ok ? '' : '  -> ' + (detail || ''));
    if (!ok) FAILURES.push(name);
}

/** Slice out `function <name>(...) { ... }` by brace matching. */
function extractFunction(name) {
    const marker = '    function ' + name + '(';
    const start = SRC.indexOf(marker);
    if (start < 0) throw new Error('function not found in labeler.js: ' + name);
    let depth = 0;
    for (let j = SRC.indexOf('{', start); j < SRC.length; j++) {
        if (SRC[j] === '{') depth++;
        else if (SRC[j] === '}') {
            depth--;
            if (depth === 0) return SRC.slice(start, j + 1);
        }
    }
    throw new Error('unbalanced braces reading ' + name);
}

// ---------------------------------------------------------------- ref queue
function testRefFetchQueue() {
    console.log('ref fetch queue');
    const start = SRC.indexOf('    const refImageFetchQueueHigh = [];');
    const end = SRC.indexOf('    let refImagesLoadingCount = 0;');
    if (start < 0 || end < 0) throw new Error('ref queue block not found');
    const q = new Function(SRC.slice(start, end)
        + 'return { push: _refQueuePush, shift: _refQueueShift, pending: _refQueuePending,'
        + '         reset: _refQueueReset, normLen: () => refImageFetchQueueNormal.length };')();

    q.reset();
    ['a', 'b', 'c'].forEach((k) => q.push(k, false));
    check('normal lane is FIFO', [q.shift(), q.shift(), q.shift()].join('') === 'abc');

    q.reset();
    q.push('n1', false); q.push('h1', true); q.push('n2', false); q.push('h2', true);
    const order = [q.shift(), q.shift(), q.shift(), q.shift()].join(',');
    check('high priority drains before normal', order === 'h1,h2,n1,n2', order);

    q.reset();
    for (let i = 0; i < 50; i++) q.push('k' + i, i % 3 === 0);
    let seen = 0;
    while (q.pending() > 0 && seen <= 100) { q.shift(); seen++; }
    check('pending count drains to exactly zero', seen === 50 && q.pending() === 0, 'seen=' + seen);

    q.reset();
    check('shift on empty queue is safe', !q.shift() && q.pending() === 0);

    // The head pointer must not let the backing array grow without bound.
    q.reset();
    let maxLen = 0;
    for (let round = 0; round < 500; round++) {
        for (let i = 0; i < 40; i++) q.push('r' + round + 'k' + i, false);
        for (let i = 0; i < 40; i++) q.shift();
        maxLen = Math.max(maxLen, q.normLen());
    }
    check('backing array stays bounded across 20k enqueues', maxLen < 2000, 'max length ' + maxLen);
    check('queue is empty after churn', q.pending() === 0, String(q.pending()));
}

// ------------------------------------------------------- ref source choice
function makeChooser(states) {
    const isRefImageReady = (src) => String(states.get(String(src || '').trim()) || '') === 'ready';
    return new Function('refImagePrefetchState', 'isRefImageReady',
        extractFunction('_chooseRefDisplaySrc') + '\n'
        + extractFunction('_refHasAnySource') + '\n'
        + 'return { choose: _chooseRefDisplaySrc, hasAny: _refHasAnySource };'
    )(states, isRefImageReady);
}

const SRCS = (o) => Object.assign(
    { baseSrc: '', inlineSrc: '', fastSrc: '', hqSrc: '', fallbackUrlSrc: '' }, o);

function testRefSourceChoice() {
    console.log('ref source selection');
    const states = new Map();
    const { choose, hasAny } = makeChooser(states);

    states.clear();
    check('inline shows while hq is still loading',
        choose(SRCS({ hqSrc: 'HQ', inlineSrc: 'IN' })) === 'IN');

    states.set('HQ', 'ready');
    check('hq wins once ready', choose(SRCS({ hqSrc: 'HQ', inlineSrc: 'IN' })) === 'HQ');

    states.clear();
    states.set('HQ', 'error');
    check('errored hq falls back to inline',
        choose(SRCS({ hqSrc: 'HQ', inlineSrc: 'IN' })) === 'IN');

    states.clear();
    states.set('HQ', 'error');
    check('all-errored ref yields no src', choose(SRCS({ hqSrc: 'HQ' })) === '');
    check('  ...but still reports a source, so it renders hidden and can recover',
        hasAny(SRCS({ hqSrc: 'HQ' })) === true);
    check('a ref with no variants at all reports none', hasAny(SRCS({})) === false);

    states.clear();
    check('falls through to fallback url when nothing else exists',
        choose(SRCS({ fallbackUrlSrc: 'FB' })) === 'FB');
    check('null input is tolerated', choose(null) === '' && hasAny(null) === false);
}

// ------------------------------------------------------ incremental patcher
function makeFrame(key, initialSrc) {
    const classes = new Set(initialSrc ? [] : ['ref-unavailable']);
    const img = {
        _src: initialSrc || null,
        getAttribute: (n) => (n === 'src' ? img._src : null),
        setAttribute: (n, v) => { if (n === 'src') img._src = v; },
        complete: true,
        naturalWidth: 100,
        naturalHeight: 100,
        addEventListener() {},
    };
    return {
        img,
        style: { display: initialSrc ? '' : 'none' },
        classList: {
            contains: (c) => classes.has(c),
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
        },
        getAttribute: () => key,
        querySelector: () => img,
    };
}

function runPatcher(frames, sources, states, mode, kind) {
    const list = {
        querySelectorAll: () => ({ length: frames.length, forEach: (f) => frames.forEach(f) }),
    };
    const doc = { getElementById: (id) => (id === 'predictions-list' ? list : null) };
    const { choose } = makeChooser(states);
    // The last two are the temporary render-cost instrumentation; stubbed so
    // the patcher can be exercised on its own, and so removing the
    // instrumentation later does not break this test.
    return new Function(
        'document', 'renderedRefSources', 'renderedRefListKind', 'labelerMode',
        '_chooseRefDisplaySrc', '_applyRefAspectClampToImg', '_perfNow', '_perfRecordMs',
        'renderPerf',
        extractFunction('refreshRenderedRefImages') + '\nreturn refreshRenderedRefImages();'
    )(doc, sources, kind, mode, choose, () => {}, () => 0, () => {}, { patch: {} });
}

function testIncrementalPatcher() {
    console.log('incremental ref patcher');

    let states = new Map([['HQ', 'loading']]);
    let sources = new Map([['classify:0:0', SRCS({ hqSrc: 'HQ', inlineSrc: 'IN' })]]);
    let f = makeFrame('classify:0:0', 'IN');
    check('leaves the img alone while hq is loading',
        runPatcher([f], sources, states, 'classify', 'classify') === true && f.img._src === 'IN',
        f.img._src);
    states.set('HQ', 'ready');
    runPatcher([f], sources, states, 'classify', 'classify');
    check('upgrades the img in place once hq is ready', f.img._src === 'HQ', f.img._src);

    states = new Map([['HQ0', 'ready'], ['HQ1', 'loading']]);
    sources = new Map([
        ['classify:0:0', SRCS({ hqSrc: 'HQ0', inlineSrc: 'I0' })],
        ['classify:0:1', SRCS({ hqSrc: 'HQ1', inlineSrc: 'I1' })],
    ]);
    const a = makeFrame('classify:0:0', 'I0');
    const b = makeFrame('classify:0:1', 'I1');
    runPatcher([a, b], sources, states, 'classify', 'classify');
    check('touches only the frame that changed',
        a.img._src === 'HQ0' && b.img._src === 'I1', a.img._src + ' / ' + b.img._src);

    states = new Map([['HQ', 'error']]);
    sources = new Map([['classify:0:0', SRCS({ hqSrc: 'HQ' })]]);
    f = makeFrame('classify:0:0', 'HQ');
    runPatcher([f], sources, states, 'classify', 'classify');
    check('hides a frame whose every source failed',
        f.style.display === 'none' && f.classList.contains('ref-unavailable'));
    states.set('HQ', 'ready');
    runPatcher([f], sources, states, 'classify', 'classify');
    check('restores that frame when a retry succeeds',
        f.style.display === '' && !f.classList.contains('ref-unavailable') && f.img._src === 'HQ',
        f.img._src);

    // Anything that does not match what we last rendered must defer to a full render.
    sources = new Map([['classify:0:0', SRCS({ hqSrc: 'HQ', inlineSrc: 'IN' })]]);
    check('defers to a full render when the index is empty',
        runPatcher([], new Map(), new Map(), 'classify', 'classify') === false);
    check('defers to a full render when the rendered list is for another mode',
        runPatcher([makeFrame('classify:0:0', 'IN')], sources, new Map(), 'manual', 'classify') === false);
    check('defers to a full render when nothing is rendered yet',
        runPatcher([], sources, new Map(), 'classify', 'classify') === false);

    const stale = makeFrame('classify:9:9', 'OLD');
    check('skips a frame key it does not track',
        runPatcher([stale], sources, new Map(), 'classify', 'classify') === true
        && stale.img._src === 'OLD', stale.img._src);
}

// ------------------------------------------------------------ cat id prefix
function testCatIdPrefix() {
    console.log('cat id prefix stripping');
    const strip = new Function(extractFunction('_stripCatIdPrefix') + '\nreturn _stripCatIdPrefix;')();

    const cases = [
        ['12', '12. Whiskers', 'Whiskers'],
        ['12', '12) Whiskers', 'Whiskers'],
        ['12', '12- Whiskers', 'Whiskers'],
        ['12', '12: Whiskers', 'Whiskers'],
        ['12', '  12 .  Whiskers', 'Whiskers'],
        ['12', '123. Whiskers', '123. Whiskers'],
        ['12', '12Whiskers', '12Whiskers'],
        ['12', 'Whiskers', 'Whiskers'],
        ['7', ' 7:Tom', 'Tom'],
        ['7', '7 - Tom', 'Tom'],
        ['104', '104 . Mr. Bigglesworth', 'Mr. Bigglesworth'],
        ['3', '3.3. Tri', '3. Tri'],
        ['12', '12..Double', '.Double'],
        ['', 'Whiskers', 'Whiskers'],
    ];
    let bad = 0;
    let firstBad = '';
    for (const [id, input, want] of cases) {
        const got = strip(input, id);
        if (got !== want) {
            bad++;
            if (!firstBad) firstBad = JSON.stringify({ id, input, want, got });
        }
    }
    check('matches the documented prefix forms', bad === 0, firstBad);

    // The whole point: a cat id with a regex metacharacter must not throw.
    let threw = false;
    let got = '';
    try { got = strip('(9. Cat', '(9'); } catch (e) { threw = true; }
    check('a regex-metacharacter cat id does not throw', !threw && got === 'Cat', got);
}

// ------------------------------------------------- render-cost accounting
// TEMPORARY, paired with the render-cost instrumentation in labeler.js.
// Remove this test when that instrumentation comes out.
function testRenderPerfAccounting() {
    console.log('render cost accounting');
    const start = SRC.indexOf('    const RENDER_PERF_REPORT_MS = 60000;');
    const end = SRC.indexOf('    //Upgrade the already-rendered ref frames in place.');
    if (start < 0 || end < 0) {
        check('instrumentation block present', false, 'block markers not found');
        return;
    }
    const perf = new Function('FLAG_RENDER_PERF', 'document', 'labelerMode', 'postUiDiag', 'console',
        SRC.slice(start, end)
        + 'return { renderPerf, summary: renderPerfSummary, record: _perfRecordMs,'
        + '         reset: _resetRenderPerf, bucket: _perfBucket };'
    )(true, { getElementById: () => null }, 'manual', () => {}, { log() {} });

    perf.reset();
    // Three full rebuilds averaging 80ms over 800 ref frames each.
    perf.record(perf.renderPerf.full, 70, 800);
    perf.record(perf.renderPerf.full, 80, 800);
    perf.record(perf.renderPerf.full, 90, 800);
    // A thousand incremental patches at 0.5ms.
    for (let i = 0; i < 1000; i++) perf.record(perf.renderPerf.patch, 0.5, 1);
    // Each of those patches came from one coalesced ref-load event.
    perf.renderPerf.refreshRequests = 1000;

    const s = perf.summary();
    check('averages full-render cost', s.full_avg_ms === 80, String(s.full_avg_ms));
    check('reports worst full render', s.full_max_ms === 90, String(s.full_max_ms));
    check('reports ref frames per render', s.full_avg_ref_frames === 800, String(s.full_avg_ref_frames));
    check('averages patch cost', s.patch_avg_ms === 0.5, String(s.patch_avg_ms));
    // now = 240ms of rebuilds + 500ms of patches
    check('current main-thread cost is rebuilds plus patches',
        s.main_thread_ms_now === 740, String(s.main_thread_ms_now));
    // before = 1000 coalesced events x 80ms, plus the 240ms of real rebuilds
    check('prior main-thread cost prices each event as a full rebuild',
        s.main_thread_ms_before_est === 80240, String(s.main_thread_ms_before_est));
    check('speedup is the ratio of the two', s.speedup_x === Number((80240 / 740).toFixed(1)),
        String(s.speedup_x));

    // With no data the summary must not divide by zero.
    perf.reset();
    const empty = perf.summary();
    check('empty summary is all zeroes, no NaN',
        empty.full_avg_ms === 0 && empty.patch_avg_ms === 0
        && empty.speedup_x === 0 && empty.main_thread_ms_now === 0,
        JSON.stringify(empty));
}

// ----------------------------------------------------------------- ceilings
function testLoaderCeilings() {
    console.log('loader ceilings');
    const num = (name) => {
        const m = SRC.match(new RegExp('const ' + name + ' = (\\d+);'));
        return m ? Number(m[1]) : NaN;
    };
    // These bound resident decoded-bitmap memory. If someone raises them past
    // what a browser tab can hold, eviction stops running and the tab OOMs.
    check('full-photo cache cap stays small', num('IMAGE_PREFETCH_MAX') <= 256,
        String(num('IMAGE_PREFETCH_MAX')));
    check('ref-crop cache cap stays bounded', num('REF_IMAGE_PREFETCH_MAX') <= 4000,
        String(num('REF_IMAGE_PREFETCH_MAX')));
    check('in-flight ref loads have a timeout', num('REF_IMAGE_LOAD_TIMEOUT_MS') > 0,
        String(num('REF_IMAGE_LOAD_TIMEOUT_MS')));

    // A dead ref must stop retrying; every retry schedules a sidebar refresh.
    const retry = extractFunction('_scheduleRefImageRetry');
    check('ref retries honour an attempt ceiling',
        retry.includes('REF_IMAGE_RETRY_MAX_ATTEMPTS') && /attempts/.test(retry),
        'no attempt ceiling in _scheduleRefImageRetry');
}

console.log('labeler render/loader regression tests\n');
testRefFetchQueue();
testRefSourceChoice();
testIncrementalPatcher();
testCatIdPrefix();
testRenderPerfAccounting();
testLoaderCeilings();

if (FAILURES.length) {
    console.log('\n%d FAILED: %s', FAILURES.length, FAILURES.join(', '));
    process.exit(1);
}
console.log('\nall labeler render tests passed');
