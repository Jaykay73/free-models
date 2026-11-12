import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {resolve, call_with_fallback} from '../clients/js/nimstatus-client.mjs';

test('cache, expiry, stale-while-error, defaults, retry classification and bound', async () => {
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/healthy/responses.json', import.meta.url)));
  const mids = Object.keys(fixture.models);
  const originalNow = Date.now, originalFetch = globalThis.fetch, originalWarn = console.warn;
  let now = originalNow(), count = 0, warnings = 0;
  Date.now = () => now;
  console.warn = () => warnings++;
  const data = {schema_version: 1, valid_until: new Date(now + 3600000).toISOString(),
    aliases: {fast: {primary: mids[0], fallbacks: mids.slice(1), stale: false}}};
  const success = async () => {count++; return {ok: true, text: async () => JSON.stringify(data)};};
  globalThis.fetch = success;
  const opts = {status_url: 'https://example.test/js/status.json', defaults: {fast: mids}};
  try {
    const concurrent = await Promise.all([resolve('fast', opts), resolve('fast', opts)]);
    assert.deepEqual(concurrent, [mids, mids]);
    assert.equal(count, 1);
    now += 299000;
    assert.deepEqual(await resolve('fast', opts), mids);
    assert.equal(count, 1);
    now += 1000;
    await resolve('fast', opts);
    assert.equal(count, 2);
    globalThis.fetch = async () => {throw new Error('offline');};
    now += 300000;
    assert.deepEqual(await resolve('fast', opts), mids);
    assert.ok(warnings > 0);
    assert.deepEqual(await resolve('fast', {...opts, status_url: opts.status_url + '/first-fail'}), mids);
    globalThis.fetch = success;
    data.valid_until = '2000-01-01T00:00:00Z';
    now += 300000;
    const before = warnings;
    await resolve('fast', opts);
    assert.ok(warnings > before);
    const attempts = [];
    const invoke = async model => {attempts.push(model); if (attempts.length === 1) throw {status: 429}; return model;};
    assert.equal(await call_with_fallback('fast', invoke, {...opts, retryable: e => e.status === 429}), mids[1]);
    assert.deepEqual(attempts, mids.slice(0, 2));
    attempts.length = 0;
    await assert.rejects(call_with_fallback('fast', invoke, {...opts, retryable: () => false}));
    assert.equal(attempts.length, 1);
    attempts.length = 0;
    await assert.rejects(call_with_fallback('fast', invoke, {...opts, retryable: () => true, max_attempts: 1}));
    assert.equal(attempts.length, 1);
  } finally {Date.now = originalNow; globalThis.fetch = originalFetch; console.warn = originalWarn;}
});
