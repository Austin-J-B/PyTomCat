/** Small, cancelable HTTP client for the image labeler. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.LabelerApi = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const TRANSIENT_STATUSES = new Set([408, 421, 425, 429, 502, 503, 504]);

    class ApiError extends Error {
        constructor(message, options = {}) {
            super(message);
            this.name = 'ApiError';
            this.status = Number(options.status || 0);
            this.code = String(options.code || 'request_failed');
            this.retryable = !!options.retryable;
            if (options.cause) this.cause = options.cause;
        }
    }

    function retryAfterMs(value, now = Date.now(), maximum = 30000) {
        const raw = String(value || '').trim();
        if (!raw) return 0;
        const seconds = Number(raw);
        let delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(raw) - now;
        if (!Number.isFinite(delay) || delay <= 0) return 0;
        return Math.min(Math.max(0, delay), Math.max(0, Number(maximum || 0)));
    }

    function wait(ms, signal) {
        return new Promise((resolve, reject) => {
            if (signal?.aborted) {
                reject(new ApiError('API request canceled', { code: 'aborted' }));
                return;
            }
            const timer = setTimeout(done, Math.max(0, Number(ms || 0)));
            function done() {
                signal?.removeEventListener?.('abort', aborted);
                resolve();
            }
            function aborted() {
                clearTimeout(timer);
                signal?.removeEventListener?.('abort', aborted);
                reject(new ApiError('API request canceled', { code: 'aborted' }));
            }
            signal?.addEventListener?.('abort', aborted, { once: true });
        });
    }

    async function readResponse(response) {
        if (response.status === 204 || response.status === 205) return null;
        const text = await response.text();
        if (!text) return null;
        const contentType = String(response.headers?.get?.('Content-Type') || '').toLowerCase();
        if (contentType.includes('json')) return JSON.parse(text);
        try {
            return JSON.parse(text);
        } catch (_) {
            return text;
        }
    }

    function createClient(options = {}) {
        const fetchImpl = options.fetchImpl || (typeof fetch === 'function' ? fetch.bind(globalThis) : null);
        if (!fetchImpl) throw new Error('fetch is unavailable');
        const buildUrl = typeof options.buildUrl === 'function' ? options.buildUrl : (path) => String(path || '');
        const getCsrfToken = typeof options.getCsrfToken === 'function' ? options.getCsrfToken : () => '';
        const defaultTimeoutMs = Math.max(1, Number(options.defaultTimeoutMs ?? 90000));
        const defaultMaxAttempts = Math.max(1, Number(options.defaultMaxAttempts ?? 3));
        const retryBaseMs = Math.max(0, Number(options.retryBaseMs ?? 280));
        const maxRetryAfterMs = Math.max(0, Number(options.maxRetryAfterMs ?? 30000));

        async function request(endpoint, init = {}, requestOptions = {}) {
            const timeoutMs = Math.max(1, Number(requestOptions.timeoutMs ?? defaultTimeoutMs));
            const maxAttempts = Math.max(1, Number(requestOptions.maxAttempts ?? defaultMaxAttempts));
            const upstreamSignal = requestOptions.signal || null;
            let lastError = null;

            for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
                if (upstreamSignal?.aborted) {
                    throw new ApiError('API request canceled', { code: 'aborted' });
                }
                const controller = new AbortController();
                let timedOut = false;
                const abortFromUpstream = () => controller.abort(upstreamSignal?.reason);
                upstreamSignal?.addEventListener?.('abort', abortFromUpstream, { once: true });
                const timer = setTimeout(() => {
                    timedOut = true;
                    controller.abort();
                }, timeoutMs);

                try {
                    const method = String(init.method || 'GET').toUpperCase();
                    const headers = new Headers(init.headers || {});
                    const csrfToken = getCsrfToken();
                    if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
                        headers.set('X-CSRF-Token', csrfToken);
                    }
                    const response = await fetchImpl(buildUrl(endpoint), {
                        ...init,
                        method,
                        headers,
                        credentials: 'include',
                        signal: controller.signal,
                    });
                    const body = await readResponse(response);
                    if (response.ok) return body;

                    const detail = typeof body === 'string'
                        ? body.trim().slice(0, 1000)
                        : String(body?.error || body?.message || '').trim().slice(0, 1000);
                    const retryable = TRANSIENT_STATUSES.has(Number(response.status));
                    const error = new ApiError(
                        detail ? `API error: ${response.status} - ${detail}` : `API error: ${response.status}`,
                        { status: response.status, code: 'http_error', retryable },
                    );
                    lastError = error;
                    if (!retryable || attempt >= maxAttempts) throw error;

                    const retryDelay = retryAfterMs(
                        response.headers?.get?.('Retry-After'),
                        Date.now(),
                        maxRetryAfterMs,
                    );
                    const jitter = Math.floor(Math.random() * 120);
                    await wait(retryDelay || (retryBaseMs * Math.pow(2, attempt - 1) + jitter), upstreamSignal);
                } catch (error) {
                    if (error instanceof ApiError) {
                        if (error.code === 'aborted' || error.status || attempt >= maxAttempts) throw error;
                        lastError = error;
                        continue;
                    }
                    if (upstreamSignal?.aborted) {
                        throw new ApiError('API request canceled', { code: 'aborted', cause: error });
                    }
                    const message = String(error?.message || '').toLowerCase();
                    const networkFailure = error instanceof TypeError
                        || message.includes('failed to fetch')
                        || message.includes('networkerror')
                        || message.includes('network request failed')
                        || message.includes('load failed');
                    const retryable = timedOut || networkFailure;
                    lastError = new ApiError(
                        timedOut ? `API timeout: ${timeoutMs}ms` : String(error?.message || 'API request failed'),
                        { code: timedOut ? 'timeout' : 'network_error', retryable, cause: error },
                    );
                    if (!retryable || attempt >= maxAttempts) throw lastError;
                    const jitter = Math.floor(Math.random() * 120);
                    await wait(retryBaseMs * Math.pow(2, attempt - 1) + jitter, upstreamSignal);
                } finally {
                    clearTimeout(timer);
                    upstreamSignal?.removeEventListener?.('abort', abortFromUpstream);
                }
            }
            throw lastError || new ApiError('API request failed');
        }

        return {
            request,
            get(endpoint, options = {}) {
                return request(endpoint, { method: 'GET' }, options);
            },
            post(endpoint, data, options = {}) {
                return request(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data || {}),
                }, options);
            },
        };
    }

    return { ApiError, TRANSIENT_STATUSES, createClient, retryAfterMs };
}));
