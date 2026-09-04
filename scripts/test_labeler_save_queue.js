'use strict';

const assert = require('node:assert/strict');
const { PendingUpdateQueue } = require('../labeler-save-queue.js');

function fakeStorage() {
    const values = new Map();
    return {
        getItem: (key) => values.has(key) ? values.get(key) : null,
        setItem: (key, value) => values.set(key, String(value)),
        removeItem: (key) => values.delete(key),
    };
}

function testMergeAndSnapshot() {
    const queue = new PendingUpdateQueue();
    assert.equal(queue.upsert({ serial: 12, box_coords: 'box-a' }), true);
    assert.equal(queue.upsert({ serial: '12', box_cat_ids: 'Twix' }), true);
    assert.deepEqual(queue.snapshot(), [{ serial: 12, box_coords: 'box-a', box_cat_ids: 'Twix' }]);
}

function testAcknowledgePreservesNewerEdits() {
    const queue = new PendingUpdateQueue();
    queue.upsert({ serial: 12, box_cat_ids: 'Twix' });
    queue.upsert({ serial: 13, box_cat_ids: 'Mango' });
    const sent = queue.snapshot();

    queue.upsert({ serial: 12, box_cat_ids: 'Rocket' });
    queue.upsert({ serial: 14, box_cat_ids: 'Nori' });
    assert.equal(queue.acknowledge(sent, [12, 13]), 1);
    assert.deepEqual(queue.snapshot(), [
        { serial: 12, box_cat_ids: 'Rocket' },
        { serial: 14, box_cat_ids: 'Nori' },
    ]);
}

function testMissingRowsRemainPending() {
    const queue = new PendingUpdateQueue();
    queue.upsert({ serial: 21, box_coords: 'box' });
    const sent = queue.snapshot();
    assert.equal(queue.acknowledge(sent, []), 0);
    assert.equal(queue.length, 1);
    assert.equal(queue.remove(21), true);
    assert.equal(queue.length, 0);
}

function testSessionJournalRestoresAndClears() {
    const storage = fakeStorage();
    const first = new PendingUpdateQueue({ storage, storageKey: 'volunteer-1' });
    first.upsert({ serial: 31, box_cat_ids: 'Twix' });

    const restored = new PendingUpdateQueue({ storage, storageKey: 'volunteer-1' });
    assert.deepEqual(restored.snapshot(), [{ serial: 31, box_cat_ids: 'Twix' }]);
    restored.acknowledge(restored.snapshot(), [31]);

    const empty = new PendingUpdateQueue({ storage, storageKey: 'volunteer-1' });
    assert.equal(empty.length, 0);
}

function testStorageFailureDoesNotBreakInMemoryQueue() {
    const storage = {
        getItem: () => null,
        removeItem: () => {},
        setItem: () => { throw new Error('quota exceeded'); },
    };
    const queue = new PendingUpdateQueue({ storage, storageKey: 'full-store' });
    assert.equal(queue.upsert({ serial: 41, box_coords: 'box' }), true);
    assert.equal(queue.length, 1);
    assert.equal(queue.lastPersistenceError, 'storage_unavailable');
}

function testOversizedJournalIsDiscarded() {
    const storage = fakeStorage();
    storage.setItem('pending:user-1', 'x'.repeat(2048));
    const queue = new PendingUpdateQueue({ maxStoredChars: 1024 });

    assert.equal(queue.attachStorage(storage, 'pending:user-1'), 0);
    assert.equal(queue.length, 0);
    assert.equal(queue.lastPersistenceError, 'journal_too_large');
    assert.equal(storage.getItem('pending:user-1'), null);
}

testMergeAndSnapshot();
testAcknowledgePreservesNewerEdits();
testMissingRowsRemainPending();
testSessionJournalRestoresAndClears();
testStorageFailureDoesNotBreakInMemoryQueue();
testOversizedJournalIsDiscarded();
console.log('labeler save queue: all checks passed');
