import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { createWorker } from '../src/index.mjs';

const base = 'https://decky.example.com';
const hash = 'a'.repeat(64);
const cdn = 'https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew';
const env = { PUBLIC_BASE_URL: base, ASSETS: { fetch: async () => new Response('web') } };
const js = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
function fixture() {
  return [{ id: 1, name: 'CSS Loader', author: 'Author', description: 'Theme', tags: ['Theme'], downloads: 100,
    image_url: cdn + '/artifact_images/cover.png', versions: [{ name: '2.1.3', hash }] }];
}
function release() {
  return { tag_name: 'v3.3.1', assets: [{ name: 'PluginLoader', size: 7, digest: 'sha256:' + hash,
    browser_download_url: 'https://github.com/SteamDeckHomebrew/decky-loader/releases/download/v3.3.1/PluginLoader' }] };
}
function call(worker, path, options = {}, config = env) {
  return worker.fetch(new Request('https://untrusted-host.example' + path, options), config);
}

test('shared client, digest and installers are identical across backends; Host cannot change origin', async () => {
  const worker = createWorker(() => { throw new Error('must stay offline'); });
  const client = await (await call(worker, '/client.py')).text();
  assert.equal(client, readFileSync(new URL('../../scripts/client.py', import.meta.url), 'utf8'));
  const digest = await (await call(worker, '/api/client')).json();
  assert.equal(digest.sha256, createHash('sha256').update(client).digest('hex'));
  const script = await (await call(worker, '/install.sh')).text();
  assert.ok(script.includes(`MIRROR='${base}'`));
  assert.ok(!script.includes('untrusted-host'));
  assert.equal(spawnSync('sh', ['-n'], { input: script }).status, 0);
  for (const mode of ['install', 'configure', 'restore']) {
    const result = await call(worker, `/launchers/${mode}.desktop`);
    const data = await result.text();
    const python = spawnSync('python3', ['-c',
      'from cndecky.launcher import desktop_entry; import sys; sys.stdout.write(desktop_entry(sys.argv[1], sys.argv[2]))', base, mode],
    { cwd: new URL('../..', import.meta.url), encoding: 'utf8' });
    assert.equal(python.status, 0, python.stderr);
    assert.equal(data, python.stdout);
    assert.equal(result.headers.get('Content-Disposition'), `attachment; filename="CNDecky-${mode}.desktop"`);
    const head = await call(worker, `/launchers/${mode}.desktop`, { method: 'HEAD' });
    assert.equal(await head.text(), '');
    assert.equal(Number(head.headers.get('Content-Length')), Buffer.byteLength(data));
  }
});

test('HTTP preview works but launchers reject it; invalid origins fail closed', async () => {
  const worker = createWorker();
  const local = { ...env, PUBLIC_BASE_URL: 'http://localhost:8787' };
  assert.equal((await call(worker, '/api/status', {}, local)).status, 200);
  assert.equal((await call(worker, '/launchers/install.desktop', {}, local)).status, 409);
  for (const origin of ['http://example.com', 'https://user:pass@example.com', 'https://example.com/path',
    'https://example.com?x=1', 'https://example.com/\n', 'https://example.com/$(touch bad)', 'https://example.com/%22', undefined]) {
    assert.equal((await call(worker, '/install.sh', {}, { ...env, PUBLIC_BASE_URL: origin })).status, 500, origin);
  }
});

test('status describes pure proxy without invented persistent cache counters', async () => {
  const status = await (await call(createWorker(), '/api/status')).json();
  assert.equal(status.mode, 'proxy');
  assert.equal(status.cache, null);
  assert.equal(status.storage, 'none');
  assert.equal(status.store_url, base + '/plugins');
});

test('catalog rewrites hashes, images and sorting without changing plugin contents', async () => {
  const worker = createWorker(async url => { assert.equal(url, 'https://plugins.deckbrew.xyz/plugins'); return js(fixture()); });
  const response = await call(worker, '/plugins?sort_by=downloads&sort_direction=desc');
  const [plugin] = await response.json();
  assert.equal(plugin.versions[0].hash, hash);
  assert.equal(plugin.versions[0].artifact, `${base}/artifacts/${hash}.zip`);
  assert.equal(plugin.versions[0].cached, false);
  assert.equal(plugin.image_url, base + '/images/cover.png');
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), 'https://steamloopback.host');
  assert.equal(response.headers.get('X-CNDecky-Cache'), 'BYPASS');
  assert.equal((await call(worker, '/plugins?sort_by=unknown')).status, 400);
});

test('version-check tuples remain compatible and old Decky receives 426 without fetching', async () => {
  let calls = 0;
  const worker = createWorker(async () => { calls++; return js({ 'CSS Loader': ['2.1.3', hash] }); });
  const versions = await (await call(worker, '/plugins/versions')).json();
  assert.deepEqual(versions['CSS Loader'], ['2.1.3', hash, `${base}/artifacts/${hash}.zip`]);
  assert.equal((await call(worker, '/plugins', { headers: { 'X-Decky-Version': 'v3.2.0' } })).status, 426);
  assert.equal(calls, 1);
});

test('malformed hashes and changed artifact protocol fail closed; external images are removed', async () => {
  for (const mutation of [p => p.versions[0].hash = '../foo', p => p.versions[0].artifact = 'https://evil.example/file.zip']) {
    const data = fixture(); mutation(data[0]);
    assert.equal((await call(createWorker(async () => js(data)), '/plugins')).status, 502);
  }
  const data = fixture(); data[0].image_url = 'https://evil.example/pixel.png';
  const [plugin] = await (await call(createWorker(async () => js(data)), '/plugins')).json();
  assert.equal(plugin.image_url, '');
});

test('no arbitrary URL proxy or unapproved asset route; no client credentials forwarded', async () => {
  let calls = 0;
  const worker = createWorker(async (url, init) => {
    calls++;
    assert.equal(url, `${cdn}/versions/${hash}.zip`);
    assert.equal(init.headers.get('Authorization'), null);
    assert.equal(init.headers.get('Cookie'), null);
    assert.equal(init.headers.get('X-Forwarded-Host'), null);
    return new Response('zip');
  });
  for (const path of ['/proxy?url=https://evil.example', '/https://github.com/anything', '/.env', '/index.html', '/launchers/arbitrary.desktop']) {
    assert.equal((await call(worker, path)).status, 404);
  }
  assert.equal(calls, 0);
  const result = await call(worker, `/artifacts/${hash}.zip?url=https://evil.example`, {
    headers: { Authorization: 'private-browser-token', Cookie: 'private-cookie', 'X-Forwarded-Host': 'evil.example' },
  });
  assert.equal(await result.text(), 'zip');
  assert.equal(calls, 1);
});

test('GitHub redirects are followed server-side; secrets and Location never reach redirect target/client', async () => {
  const seen = [];
  const worker = createWorker(async (url, init) => {
    seen.push([url, init.headers.get('Authorization')]);
    assert.equal(init.redirect, 'manual');
    if (url.startsWith('https://api.github.com/')) return js(release());
    if (url.startsWith('https://github.com/')) return new Response(null, { status: 302,
      headers: { Location: 'https://release-assets.githubusercontent.com/asset?signature=temporary' } });
    return new Response('payload', { headers: { 'Content-Length': '7', 'Set-Cookie': 'upstream-secret' } });
  });
  const result = await call(worker, '/loader/v3.3.1/PluginLoader', {}, { ...env, GITHUB_TOKEN: 'api-only-secret' });
  assert.equal(result.status, 200);
  assert.equal(await result.text(), 'payload');
  assert.equal(result.headers.get('Location'), null);
  assert.equal(result.headers.get('Set-Cookie'), null);
  assert.deepEqual(seen.map(x => x[1]), ['Bearer api-only-secret', null, null]);
});

test('API secret is removed even if a redirect returns to another GitHub API endpoint', async () => {
  let calls = 0;
  const worker = createWorker(async (url, init) => {
    calls++;
    assert.equal(init.headers.get('Authorization'), calls === 1 ? 'Bearer secret' : null);
    return calls === 1 ? new Response(null, { status: 302, headers: { Location: 'https://api.github.com/redirected' } }) : js(release());
  });
  assert.equal((await call(worker, '/api/release', {}, { ...env, GITHUB_TOKEN: 'secret' })).status, 200);
  assert.equal(calls, 2);
});

test('reject malicious, HTTP, credentialed and excessive redirects before connecting', async () => {
  for (const location of ['http://github.com/asset', 'https://github.com.evil.example/asset',
    'https://127.0.0.1/private', 'https://user:pass@github.com/asset', 'https://github.com:8443/asset']) {
    let calls = 0;
    const worker = createWorker(async () => { calls++; return new Response(null, { status: 302, headers: { Location: location } }); });
    assert.equal((await call(worker, `/artifacts/${hash}.zip`)).status, 502);
    assert.equal(calls, 1);
  }
  let calls = 0;
  const looping = createWorker(async () => { calls++; return new Response(null, { status: 302, headers: { Location: 'https://github.com/loop' } }); });
  assert.equal((await call(looping, `/artifacts/${hash}.zip`)).status, 502);
  assert.equal(calls, 6);
});

test('stream starts before upstream completes and preserves bytes without buffering whole file', async () => {
  let upstream;
  const worker = createWorker(async () => new Response(new ReadableStream({ start(c) { upstream = c; c.enqueue(new Uint8Array([1, 2])); } })));
  const result = await call(worker, `/artifacts/${hash}.zip`);
  const reader = result.body.getReader();
  assert.deepEqual((await reader.read()).value, new Uint8Array([1, 2]));
  upstream.enqueue(new Uint8Array([3, 4])); upstream.close();
  assert.deepEqual((await reader.read()).value, new Uint8Array([3, 4]));
  assert.equal((await reader.read()).done, true);
});

test('HEAD does not download file body, images are bounded, and upstream errors are not cached as files', async () => {
  let method;
  const worker = createWorker(async (url, init) => { method = init.method; return new Response(null, { headers: { 'Content-Length': '100' } }); });
  const head = await call(worker, `/artifacts/${hash}.zip`, { method: 'HEAD' });
  assert.equal(method, 'HEAD'); assert.equal(await head.text(), ''); assert.equal(head.headers.get('Content-Length'), '100');
  for (const status of [404, 429, 500, 403]) {
    const result = await call(createWorker(async () => new Response('failed', { status })), `/artifacts/${hash}.zip`);
    assert.equal(result.status, [404, 429].includes(status) ? status : 502);
    assert.match(result.headers.get('Content-Type'), /json/);
    assert.equal(result.headers.get('Cache-Control'), 'no-store');
  }
  const huge = createWorker(async () => new Response('small', { headers: { 'Content-Length': String(9 * 1024 ** 2) } }));
  assert.equal((await call(huge, '/images/cover.png')).status, 502);
  assert.equal((await call(huge, '/images/%2Fetc%2Fpasswd.png')).status, 400);
});

test('release manifests validate official URL, size, version and digest', async () => {
  const normal = await (await call(createWorker(async () => js(release())), '/api/release')).json();
  assert.equal(normal.assets.PluginLoader.url, base + '/loader/v3.3.1/PluginLoader');
  assert.equal(normal.assets.PluginLoader.sha256, hash);
  for (const mutate of [r => r.assets[0].digest = '', r => r.assets[0].size = -1,
    r => r.prerelease = true, r => r.assets[0].browser_download_url = 'https://evil.example/PluginLoader']) {
    const data = release(); mutate(data);
    assert.equal((await call(createWorker(async () => js(data)), '/api/release')).status, 502);
  }
  assert.equal((await call(createWorker(async () => js(release())), '/loader/v3.3.0/PluginLoader')).status, 502);
});

test('a truncated binary stream fails before the installer can accept it', async () => {
  const worker = createWorker(async url => url.startsWith('https://api.github.com/') ? js(release()) : new Response('bad'));
  const result = await call(worker, '/loader/v3.3.1/PluginLoader');
  await assert.rejects(result.arrayBuffer(), /Truncated/);
});

test('oversized and invalid metadata fail cleanly, including chunked JSON', async () => {
  for (const payload of ['not json', ' '.repeat(4 * 1024 ** 2 + 1)]) {
    assert.equal((await call(createWorker(async () => new Response(payload)), '/plugins')).status, 502);
  }
});

test('preflight and install counters stay local; unsupported methods cannot mutate upstream', async () => {
  const worker = createWorker(() => { throw new Error('must not connect'); });
  const options = await call(worker, '/plugins', { method: 'OPTIONS' });
  assert.equal(options.status, 204); assert.equal(await options.text(), '');
  const increment = await call(worker, '/plugins/1/versions/2/increment', { method: 'POST' });
  assert.deepEqual(await increment.json(), { ok: true, forwarded: false });
  assert.equal((await call(worker, '/plugins', { method: 'DELETE' })).status, 405);
});
