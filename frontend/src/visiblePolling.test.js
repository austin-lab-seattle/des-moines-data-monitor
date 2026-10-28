import { test } from 'node:test';
import assert from 'node:assert/strict';
import { startVisiblePolling, DASHBOARD_REFRESH_MS } from './visiblePolling.js';

test('polling waits five minutes, pauses hidden tabs, resumes once and cleans up', async () => {
  let timer, listener, calls = 0;
  const doc = { hidden: false, addEventListener: (_, fn) => { listener = fn; }, removeEventListener: () => { listener = null; } };
  const stop = startVisiblePolling(() => { calls++; }, DASHBOARD_REFRESH_MS, {
    document: doc, setTimeout: (fn, delay) => { timer = fn; assert.equal(delay, 300000); return 1; }, clearTimeout: () => { timer = null; },
  });
  assert.equal(calls, 0);
  await timer(); assert.equal(calls, 1);
  doc.hidden = true; listener(); assert.equal(timer, null);
  doc.hidden = false; listener(); await Promise.resolve(); assert.equal(calls, 2);
  stop(); assert.equal(listener, null); assert.equal(timer, null);
});

test('hidden initial tabs do not poll and an in-flight poll cannot overlap', async () => {
  let listener, timer, done, calls = 0;
  const doc = { hidden: true, addEventListener: (_, fn) => { listener = fn; }, removeEventListener: () => {} };
  const stop = startVisiblePolling(() => { calls++; return new Promise(resolve => { done = resolve; }); }, 300000, {
    document: doc, setTimeout: fn => { timer = fn; return 1; }, clearTimeout: () => { timer = null; },
  });
  assert.equal(timer, null);
  doc.hidden = false; listener(); listener(); assert.equal(calls, 1);
  stop(); done(); await Promise.resolve(); await Promise.resolve(); assert.equal(timer, null);
});
