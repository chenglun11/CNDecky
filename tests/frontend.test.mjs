import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Run the real request helper without booting the catalog/DOM event handlers.
const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const end = source.indexOf('function updateCommand()');
assert.ok(end > 0);
function requestWith(response) {
  const context = vm.createContext({ fetch: async () => response, AbortSignal });
  vm.runInContext(source.slice(0, end), context);
  return context.request('/plugins');
}

test('empty API failures retain path and HTTP status instead of hiding them behind JSON parsing', async () => {
  for (const status of [404, 502]) {
    await assert.rejects(requestWith(new Response('', { status })), error =>
      error.message.includes('/plugins') && error.message.includes(`HTTP ${status}`) && !error.message.includes('Unexpected end'));
  }
});

test('empty success and HTML fallback produce distinguishable deployment diagnostics', async () => {
  await assert.rejects(requestWith(new Response('')), /\/plugins.*空.*HTTP 200/);
  await assert.rejects(requestWith(new Response('<html>Static asset fallback</html>')), /\/plugins.*JSON.*HTTP 200/);
});

test('valid upstream errors are preserved; successful and stale JSON still work', async () => {
  await assert.rejects(requestWith(new Response('{"error":"上游返回 HTTP 429，请稍后重试"}', { status: 429 })), /上游返回 HTTP 429/);
  const result = await requestWith(new Response('[{"name":"CSS Loader"}]', { headers: { 'X-CNDecky-Cache': 'STALE' } }));
  assert.equal(result.body[0].name, 'CSS Loader');
  assert.equal(result.stale, true);
});
