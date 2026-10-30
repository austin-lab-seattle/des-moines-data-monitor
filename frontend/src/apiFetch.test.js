import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fetchApiRead } from './apiFetch.js';

test('one transient read failure is retried without altering the returned data', async () => {
  let calls = 0;
  const response = await fetchApiRead('/air-quality/v1/observations?instrument=NO2-CAPS', {}, {
    fetch: async () => ++calls === 1 ? new Response('unavailable', { status: 503 }) : new Response('{"value":1.23456789}'), delay: async () => {},
  });
  assert.equal(calls, 2); assert.equal(await response.text(), '{"value":1.23456789}');
});

test('exports, submissions and authentication or quota failures are never automatically retried', async () => {
  for (const [path, method, status] of [['/observations/export','GET',503], ['/access-requests','POST',503], ['/observations','GET',401], ['/observations','GET',403], ['/observations','GET',404], ['/observations','GET',429]]) {
    let calls = 0;
    const result = await fetchApiRead(path, { method }, { fetch: async () => { calls++; return new Response('', { status }); }, delay: async () => {} });
    assert.equal(result.status, status); assert.equal(calls, 1);
  }
});

test('caller cancellation prevents stale retries, and persistent failures stop after two attempts', async () => {
  const caller = new AbortController(); let calls = 0;
  await assert.rejects(fetchApiRead('/observations', { signal: caller.signal }, { fetch: async () => {
    calls++; caller.abort(); throw new DOMException('cancelled','AbortError');
  }, delay: async () => {} }), { name: 'AbortError' });
  assert.equal(calls, 1); calls = 0;
  const result = await fetchApiRead('/observations', {}, { fetch: async () => { calls++; return new Response('', { status: 503 }); }, delay: async () => {} });
  assert.equal(calls, 2); assert.equal(result.status, 503);
});
