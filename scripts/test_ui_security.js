'use strict';

const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'index.html'), 'utf8');

function check(condition, message) {
    if (!condition) throw new Error(message);
}

check(source.includes('const TRUSTED_API_ORIGINS = new Set(['), 'trusted API allowlist is missing');
check(source.includes('apiBase = normalizeApiBase(currentApi)'), 'API base is not validated');
check(!source.includes("apiBase = (urlParams.get('api')"), 'init must not trust the api query parameter');
check(!source.includes('localStorage.setItem(STATE_STORAGE_KEY'), 'OAuth nonce must not persist in localStorage');
check(!source.includes('localStorage.getItem(STATE_STORAGE_KEY'), 'OAuth nonce must not be read from localStorage');

console.log('UI security checks: all checks passed');
