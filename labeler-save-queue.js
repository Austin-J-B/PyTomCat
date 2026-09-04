/**
 * Pending annotation updates for the image labeler.
 *
 * Saving uses a snapshot. An acknowledgement only removes an item when the
 * queued value still matches that snapshot, so edits made while the request is
 * in flight remain pending for the next save.
 */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.LabelerSaveQueue = api;
    }
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const UPDATE_FIELDS = ['box_coords', 'box_cat_ids', 'comments'];

    function normalizeSerial(value) {
        const serial = Number.parseInt(String(value ?? ''), 10);
        return Number.isInteger(serial) && serial > 0 ? serial : null;
    }

    function cloneUpdate(update) {
        const serial = normalizeSerial(update?.serial);
        if (serial === null) return null;
        const copy = { serial };
        for (const field of UPDATE_FIELDS) {
            if (Object.prototype.hasOwnProperty.call(update, field)) {
                copy[field] = String(update[field] ?? '');
            }
        }
        return copy;
    }

    function updatesEqual(left, right) {
        if (normalizeSerial(left?.serial) !== normalizeSerial(right?.serial)) return false;
        return UPDATE_FIELDS.every((field) => {
            const leftHas = Object.prototype.hasOwnProperty.call(left || {}, field);
            const rightHas = Object.prototype.hasOwnProperty.call(right || {}, field);
            return leftHas === rightHas && (!leftHas || String(left[field] ?? '') === String(right[field] ?? ''));
        });
    }

    class PendingUpdateQueue {
        constructor(options = {}) {
            this._updates = new Map();
            this._storage = null;
            this._storageKey = '';
            this._maxStoredChars = Math.max(1024, Number(options.maxStoredChars || 524288));
            this.lastPersistenceError = '';
            if (options.storage && options.storageKey) {
                this.attachStorage(options.storage, options.storageKey);
            }
        }

        get length() {
            return this._updates.size;
        }

        upsert(update) {
            const copy = cloneUpdate(update);
            if (!copy) return false;
            const previous = this._updates.get(copy.serial) || { serial: copy.serial };
            this._updates.set(copy.serial, { ...previous, ...copy });
            this._persist();
            return true;
        }

        remove(serial) {
            const normalized = normalizeSerial(serial);
            const removed = normalized !== null && this._updates.delete(normalized);
            if (removed) this._persist();
            return removed;
        }

        snapshot(limit = null) {
            const updates = Array.from(this._updates.values(), (update) => ({ ...update }));
            const max = Number(limit);
            return Number.isInteger(max) && max > 0 ? updates.slice(0, max) : updates;
        }

        /**
         * Remove successfully saved snapshot entries without dropping newer edits.
         * Returns the number of entries removed from the live queue.
         */
        acknowledge(snapshot, savedSerials = null) {
            const saved = savedSerials == null
                ? null
                : new Set(savedSerials.map(normalizeSerial).filter((serial) => serial !== null));
            let removed = 0;
            for (const sent of Array.isArray(snapshot) ? snapshot : []) {
                const serial = normalizeSerial(sent?.serial);
                if (serial === null || (saved && !saved.has(serial))) continue;
                const current = this._updates.get(serial);
                if (current && updatesEqual(current, sent)) {
                    this._updates.delete(serial);
                    removed += 1;
                }
            }
            if (removed) this._persist();
            return removed;
        }

        /** Attach a per-session browser store and restore any pending entries. */
        attachStorage(storage, storageKey) {
            const key = String(storageKey || '').trim();
            if (!storage || !key) return 0;
            if (this._storage === storage && this._storageKey === key) return 0;

            this._persist();
            this._updates.clear();
            this._storage = storage;
            this._storageKey = key;
            this.lastPersistenceError = '';
            try {
                const raw = storage.getItem(key);
                if (!raw) return 0;
                if (raw.length > this._maxStoredChars) {
                    this.lastPersistenceError = 'journal_too_large';
                    try { storage.removeItem(key); } catch (_) { /* best effort */ }
                    return 0;
                }
                const parsed = JSON.parse(raw);
                const updates = Array.isArray(parsed) ? parsed : parsed?.updates;
                if (!Array.isArray(updates)) throw new Error('invalid journal');
                for (const update of updates) {
                    const copy = cloneUpdate(update);
                    if (!copy) continue;
                    const previous = this._updates.get(copy.serial) || { serial: copy.serial };
                    this._updates.set(copy.serial, { ...previous, ...copy });
                }
                return this.length;
            } catch (error) {
                this.lastPersistenceError = 'restore_failed';
                try { storage.removeItem(key); } catch (_) { /* best effort */ }
                return 0;
            }
        }

        _persist() {
            if (!this._storage || !this._storageKey) return false;
            try {
                if (this.length === 0) {
                    this._storage.removeItem(this._storageKey);
                    this.lastPersistenceError = '';
                    return true;
                }
                const payload = JSON.stringify({ version: 1, updates: this.snapshot() });
                if (payload.length > this._maxStoredChars) {
                    this.lastPersistenceError = 'journal_too_large';
                    return false;
                }
                this._storage.setItem(this._storageKey, payload);
                this.lastPersistenceError = '';
                return true;
            } catch (error) {
                this.lastPersistenceError = 'storage_unavailable';
                return false;
            }
        }
    }

    return { PendingUpdateQueue, normalizeSerial, updatesEqual };
}));
