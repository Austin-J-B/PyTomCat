'use strict';

const assert = require('node:assert/strict');
const { ApiError, createClient, retryAfterMs } = require('../labeler-api.js');

async function testJsonAndEmptyResponses() {
    const responses = [
        new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
        new Response(null, { status: 204 }),
    ];
    const client = createClient({ fetchImpl: async () => responses.shift(), retryBaseMs: 0 });
    assert.deepEqual(await client.get('/ok'), { ok: true });
    assert.equal(await client.get('/empty'), null);
}

async function testStructuredHttpError() {
    const client = createClient({
        fetchImpl: async () => new Response('Bad update', { status: 400 }),
        retryBaseMs: 0,
    });
    await assert.rejects(
        () => client.post('/save', {}),
        (error) => error instanceof ApiError && error.status === 400 && error.retryable === false,
    );
}

async function testTransientResponseIsReadBeforeRetry() {
    let calls = 0;
    let firstBodyRead = false;
    const first = {
        ok: false,
        status: 503,
        headers: new Headers(),
        async text() {
            firstBodyRead = true;
            return 'try later';
        },
    };
    const client = createClient({
        retryBaseMs: 0,
        fetchImpl: async () => {
            calls += 1;
            return calls === 1
                ? first
                : new Response('{"ok":true}', { status: 200, headers: { 'Content-Type': 'application/json' } });
        },
    });
    assert.deepEqual(await client.get('/retry'), { ok: true });
    assert.equal(firstBodyRead, true);
    assert.equal(calls, 2);
}

async function testSessionCancellation() {
    const controller = new AbortController();
    const client = createClient({
        fetchImpl: async (_url, init) => new Promise((_resolve, reject) => {
            init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
        }),
    });
    const request = client.get('/slow', { signal: controller.signal });
    controller.abort();
    await assert.rejects(request, (error) => error instanceof ApiError && error.code === 'aborted');
}

async function main() {
    await testJsonAndEmptyResponses();
    await testStructuredHttpError();
    await testTransientResponseIsReadBeforeRetry();
    await testSessionCancellation();
    assert.equal(retryAfterMs('120', Date.now(), 30000), 30000);
    console.log('labeler API client: all checks passed');
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
