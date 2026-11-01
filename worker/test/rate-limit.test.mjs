import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createWorker } from '../src/index.mjs';

const config = JSON.parse(readFileSync(new URL('../wrangler.jsonc', import.meta.url), 'utf8'));
const hash = 'a'.repeat(64);
const paths = ['/launchers/install.desktop', '/launchers/configure.desktop', '/launchers/restore.desktop',
  '/install.sh', '/client.py', `/artifacts/${hash}.zip`, '/loader/v3.3.1/PluginLoader'];
const ip = '198.51.100.7';
const json = value => new Response(JSON.stringify(value));

function harness(upstreamStatus = 200) {
  let now = 0, upstreamCalls = 0, limiterCalls = 0;
  const buckets = new Map();
  // Simulate only Cloudflare's external counter API, using the deploy configuration.
  const limiter = { async limit({ key }) {
    limiterCalls++;
    const binding = config.ratelimits.find(item => item.name === 'rate limit');
    const window = Math.floor(now / (binding.simple.period * 1000));
    const bucket = buckets.get(key);
    const count = bucket?.window === window ? bucket.count + 1 : 1;
    buckets.set(key, { window, count });
    return { success: count <= binding.simple.limit };
  } };
  const env = { PUBLIC_BASE_URL: 'https://decky.example.com', 'rate limit': limiter,
    ASSETS: { fetch: async () => new Response('page') } };
  const worker = createWorker(async url => {
    upstreamCalls++;
    if (upstreamStatus !== 200) return new Response('failed', { status: upstreamStatus });
    if (url.startsWith('https://api.github.com/')) return json({ tag_name: 'v3.3.1', assets: [{
      name: 'PluginLoader', size: 7, digest: 'sha256:' + hash,
      browser_download_url: 'https://github.com/SteamDeckHomebrew/decky-loader/releases/download/v3.3.1/PluginLoader',
    }] });
    if (url === 'https://plugins.deckbrew.xyz/plugins') return json([]);
    if (url === 'https://plugins.deckbrew.xyz/plugins/versions') return json({});
    return new Response('payload');
  });
  return {
    env,
    advance: ms => { now += ms; },
    counts: () => ({ upstreamCalls, limiterCalls }),
    request: (path, headers = { 'CF-Connecting-IP': ip }, method = 'GET') =>
      worker.fetch(new Request('https://decky.example.com' + path, { method, headers }), env),
  };
}

test('ten mixed downloads share an IP quota; eleventh is rejected before upstream work', async () => {
  const h = harness();
  for (let i = 0; i < 10; i++) {
    const res = await h.request(paths[i % paths.length] + '?attempt=' + i);
    assert.equal(res.status, 200);
    await res.arrayBuffer();
  }
  const before = h.counts().upstreamCalls;
  for (const path of paths) {
    const res = await h.request(path);
    assert.equal(res.status, 429, path);
    assert.equal(res.headers.get('Retry-After'), '60');
    assert.equal(res.headers.get('Cache-Control'), 'no-store');
    assert.equal(res.headers.get('Access-Control-Allow-Origin'), 'https://steamloopback.host');
    assert.match(res.headers.get('Access-Control-Expose-Headers'), /Retry-After/i);
    assert.match((await res.json()).error, /10/);
  }
  assert.equal(h.counts().upstreamCalls, before);
  assert.equal((await h.request('/install.sh', { 'CF-Connecting-IP': '2001:db8::7' })).status, 200);
  h.advance(59000);
  assert.equal((await h.request('/install.sh')).status, 429);
  h.advance(1000);
  assert.equal((await h.request('/install.sh')).status, 200);
});

test('forwarding headers cannot change the bucket or substitute for Cloudflare client IP', async () => {
  const h = harness();
  for (let i = 0; i < 11; i++) {
    const res = await h.request('/client.py', { 'CF-Connecting-IP': ip,
      'X-Forwarded-For': `203.0.113.${i}`, 'X-Real-IP': `203.0.113.${i}` });
    assert.equal(res.status, i < 10 ? 200 : 429);
    await res.arrayBuffer();
  }
  assert.equal((await h.request('/client.py', { 'X-Forwarded-For': '203.0.113.1' })).status, 400);
});

test('pages, metadata, images, HEAD and OPTIONS never use the download quota', async () => {
  const h = harness();
  h.env['rate limit'] = { limit() { throw new Error('must not call the limiter'); } };
  for (const path of ['/', '/app.js', '/style.css', '/healthz', '/api/status', '/api/client',
    '/api/release', '/plugins', '/plugins/versions', '/images/cover.png']) {
    const res = await h.request(path, {});
    assert.equal(res.status, 200, path);
    await res.arrayBuffer();
  }
  for (const path of paths) {
    const head = await h.request(path, {}, 'HEAD');
    assert.equal(head.status, 200, path);
    assert.equal(await head.text(), '');
    assert.equal((await h.request(path, {}, 'OPTIONS')).status, 204);
    assert.equal((await h.request(path, {}, 'POST')).status, 405);
  }
  assert.equal((await h.request('/launchers/unknown.desktop')).status, 404);
  assert.equal((await h.request('/artifacts/invalid.zip')).status, 404);
});

test('missing or failed limiter blocks downloads without upstream work or leaking errors', async () => {
  const h = harness();
  for (const binding of [undefined, {}, { limit() { throw new Error('private error details'); } },
    { limit: async () => ({}) }]) {
    h.env['rate limit'] = binding;
    const res = await h.request(`/artifacts/${hash}.zip`);
    assert.equal(res.status, 503);
    assert.doesNotMatch(await res.text(), /private error details/);
    assert.equal(h.counts().upstreamCalls, 0);
    assert.equal((await h.request('/api/status')).status, 200);
  }
});

test('failed upstream downloads still consume quota and retries stop before another upstream call', async () => {
  const h = harness(500);
  for (let i = 0; i < 10; i++) {
    assert.equal((await h.request(`/artifacts/${hash}.zip`)).status, 502);
  }
  assert.equal((await h.request(`/artifacts/${hash}.zip`)).status, 429);
  assert.equal(h.counts().upstreamCalls, 10);
});
