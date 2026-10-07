'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(process.env.LABELER_TEST_SOURCE || path.join(__dirname, '..', 'labeler.js'), 'utf8');
function extract(name) {
    const start = source.search(new RegExp('    (?:async )?function ' + name + '\\('));
    assert(start >= 0, name);
    let depth = 0;
    const bodyStart = source.indexOf(') {', start) + 2;
    for (let i = bodyStart; i < source.length; i++) {
        if (source[i] === '{') depth++;
        if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
    }
    throw new Error('Unbalanced function: ' + name);
}

async function readiness({ image = true, predictions = true, refs = false, relax = true, budget = 2400 } = {}) {
    let now = 0;
    const events = [];
    const rows = predictions ? [{ candidates: [] }] : [];
    const context = {
        Date: { now: () => now }, labelerMode: 'classify', labelerActive: true,
        CLASSIFY_READY_WAIT_TIMEOUT_MS: budget, ITEM_READY_WAIT_TIMEOUT_MS: budget,
        CLASSIFY_REF_DISPLAY_WAIT_MS: 1500, CLASSIFY_REFS_PER_CAT_TARGET: 5,
        READY_WAIT_DIAG_INTERVAL_MS: 1000, FLAG_CLASSIFY_READY_RELAX: relax,
        predCache: new Map(predictions ? [['key', rows]] : []),
        classifyWarmInFlight: new Set(), classifyForegroundInFlight: new Set(),
        getPredCacheKey: () => 'key', prefetchImageSerial: () => {},
        prefetchDisplayRefsForItem: () => {}, ensureClassifyItemReady: async () => {},
        _classifyItemWarmReady: () => predictions, isPrefetchedImageReady: () => image,
        _targetCropIdxForItem: () => 0,
        _predictionLoadedRefDepthForCrop: () => ({ targetCount: 9 }),
        _predictionLoadedRefCoverageForCrop: () => ({ targetCount: 9 }),
        _predictionLoadedRefCountsForCrop: () => [0, 0, 0, 1, 3, 1, 0, 0, 2],
        _predictionRefsSufficientForCrop: () => refs,
        _predictionLoadedRefsAtTargetForCrop: () => refs,
        isPrefetchedImageTerminalError: () => false, isPrefetchedImageStalled: () => false,
        compactCachedImagePathDiag: () => ({}), compactRefImageQueueDiag: () => ({ queued: 360 }),
        getPrefetchedImageState: () => ({}), setWarmOverlay: () => {}, initialClassifyWarmDone: true,
        postUiDiag: async (event, data) => events.push({ event, ...data }),
        waitMs: async ms => { now += ms; },
    };
    vm.createContext(context);
    vm.runInContext(extract('waitForCurrentItemReady'), context);
    const ready = await context.waitForCurrentItemReady({ serial: 12741 });
    return { ready, now, events };
}

(async () => {
    const stalled = await readiness();
    assert.equal(stalled.ready, true);
    assert(stalled.now >= 1500 && stalled.now < 1700);
    assert.equal(stalled.events.at(-1).ready_reason, 'preds_ready+image_ready+refs_loading');
    assert.equal((await readiness({ image: false })).ready, false);
    assert.equal((await readiness({ predictions: false })).ready, false);
    assert.equal((await readiness({ relax: false })).ready, false);
    assert.equal((await readiness({ refs: true })).now, 0);
    assert.equal((await readiness({ budget: 1000 })).ready, true);

    const loads = [];
    const context = {
        CLASSIFY_REFS_PER_CAT_TARGET: 5,
        _getRefDisplaySources: ref => ({ fastSrc: ref, hqSrc: ref + '-hq' }),
        prefetchRefImageSrc: (src, opts) => loads.push({ src, priority: opts.priority }),
    };
    vm.createContext(context);
    vm.runInContext(extract('prefetchRefsFromResults'), context);
    context.prefetchRefsFromResults([{ candidates: [
        { refs: ['a1', 'a2', 'a3'] }, { refs: ['b1', 'b2', 'b3'] },
    ] }], { priority: 'high', maxRefsPerCandidate: 3 });
    assert.deepEqual(loads.filter(x => x.priority === 'high').map(x => x.src),
        ['a1', 'b1', 'a2', 'b2', 'a3', 'b3']);
    assert(loads.filter(x => x.src.endsWith('-hq')).every(x => x.priority === 'normal'));

    let warmOptions;
    Object.assign(context, {
        _targetCropIdxForItem: () => 0, prefetchDisplayRefsForCrop: () => {},
        CLASSIFY_WARM_PREFETCH_MAX_CANDIDATES: 5, CLASSIFY_WARM_PREFETCH_MAX_REFS: 3,
        prefetchRefsFromResults: (rows, opts) => { warmOptions = opts; },
    });
    vm.runInContext(extract('prefetchDisplayRefsForItem'), context);
    context.prefetchDisplayRefsForItem({}, Array.from({ length: 25 }, () => ({})));
    assert.equal(warmOptions.maxCrops, 2);
    assert.equal(warmOptions.priority, 'normal');
    console.log('All classifier readiness and reference scheduling tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
