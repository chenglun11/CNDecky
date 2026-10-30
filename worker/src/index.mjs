import bundle from '../generated.mjs';

const STORE = 'https://plugins.deckbrew.xyz';
const CDN = 'https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew';
const GITHUB = 'https://api.github.com/repos/SteamDeckHomebrew/decky-loader';
const HASH = /^[0-9a-f]{64}$/;
const VERSION = /^v\d+\.\d+\.\d+$/;
const HOSTS = new Set(['plugins.deckbrew.xyz', 'cdn.tzatzikiweeb.moe', 'api.github.com',
  'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com']);
const REDIRECTS = new Set([301, 302, 303, 307, 308]);
const MAX_FILE = 256 * 1024 ** 2;
const encoder = new TextEncoder();

class GatewayError extends Error {
  constructor(message, status = 502) { super(message); this.status = status; }
}

function origin(value) {
  let url;
  try { url = new URL(value); } catch { throw new GatewayError('请配置 PUBLIC_BASE_URL', 500); }
  // This origin also enters shell/desktop templates. Only ASCII DNS/IP and a port
  // are accepted; do not derive an executable's origin from the request Host.
  if (!/^https?:\/\/[a-zA-Z0-9.:[\]-]+\/?$/.test(value) || url.username || url.password ||
      (url.protocol !== 'https:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) {
    throw new GatewayError('PUBLIC_BASE_URL 必须为 HTTPS origin；本机预览允许 HTTP', 500);
  }
  return url.origin;
}

function checkedURL(value) {
  let url;
  try { url = new URL(value); } catch { throw new GatewayError('上游地址无效'); }
  if (url.protocol !== 'https:' || !HOSTS.has(url.hostname) || url.username || url.password || url.port) {
    throw new GatewayError('拒绝访问未允许的上游地址');
  }
  return url;
}

function headers(type = 'application/json; charset=utf-8') {
  return new Headers({
    'Content-Type': type,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Access-Control-Allow-Origin': 'https://steamloopback.host',
    'Access-Control-Allow-Methods': 'GET, HEAD, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'X-Decky-Version, Content-Type',
    'Access-Control-Expose-Headers': 'X-CNDecky-Cache',
    'X-CNDecky-Cache': 'BYPASS',
    'Vary': 'Origin',
  });
}

function response(request, body, type, status = 200, filename) {
  const data = typeof body === 'string' ? body : JSON.stringify(body);
  const h = headers(type);
  if (filename) h.set('Content-Disposition', `attachment; filename="${filename}"`);
  if (status !== 204) h.set('Content-Length', String(encoder.encode(data).length));
  return new Response(request.method === 'HEAD' || status === 204 ? null : data, { status, headers: h });
}

async function boundedJSON(res) {
  const max = 4 * 1024 ** 2;
  if (Number(res.headers.get('Content-Length')) > max) {
    await res.body?.cancel();
    throw new GatewayError('上游元数据过大');
  }
  if (!res.body) throw new GatewayError('上游没有返回元数据');
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let size = 0, text = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > max) throw new GatewayError('上游元数据过大');
      text += decoder.decode(value, { stream: true });
    }
    return JSON.parse(text + decoder.decode());
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error instanceof GatewayError ? error : new GatewayError('上游返回了无效 JSON');
  } finally { reader.releaseLock(); }
}

function artifactURL(base, hash) {
  if (typeof hash !== 'string' || !HASH.test(hash)) throw new GatewayError('官方商店返回了无效的插件摘要');
  return `${base}/artifacts/${hash}.zip`;
}

function checkArtifact(hash, artifact) {
  if (artifact && artifact !== `${CDN}/versions/${hash}.zip`) {
    throw new GatewayError('官方插件下载协议已变化，需要更新 CNDecky');
  }
}

function imageName(value) {
  let name;
  try { name = decodeURIComponent(value); } catch { throw new GatewayError('无效图片路径', 400); }
  if (!/^[^/\\\x00-\x1f]{1,220}\.(?:png|jpg|jpeg|webp)$/i.test(name)) {
    throw new GatewayError('无效图片路径', 400);
  }
  return name;
}

// Only the network boundary is injected in tests. Production uses Worker fetch.
export function createWorker(fetchUpstream = (...args) => fetch(...args)) {
  async function upstream(value, env, method = 'GET') {
    let url = checkedURL(value);
    for (let hop = 0; hop <= 5; hop++) {
      const h = new Headers({ 'User-Agent': 'CNDecky/0.1', 'Accept-Encoding': 'identity' });
      if (hop === 0 && url.hostname === 'api.github.com' && env.GITHUB_TOKEN) {
        h.set('Authorization', `Bearer ${env.GITHUB_TOKEN}`);
      }
      // Construct fresh headers: no browser cookies, Authorization, or forwarding
      // headers reach GitHub. Redirects are checked before each network request.
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 60000);
      let res;
      try {
        res = await fetchUpstream(url.href, {
          method, headers: h, redirect: 'manual', signal: controller.signal,
        });
      } finally {
        // Bound connection/header waits, not total download duration. Slow clients
        // must not lose an otherwise healthy stream after 60 seconds.
        clearTimeout(timeout);
      }
      if (REDIRECTS.has(res.status)) {
        const location = res.headers.get('Location');
        await res.body?.cancel();
        if (!location || hop === 5) throw new GatewayError('上游重定向异常');
        try { url = checkedURL(new URL(location, url).href); }
        catch { throw new GatewayError('拒绝访问未允许的上游重定向地址'); }
        continue;
      }
      if (res.status !== 200) {
        await res.body?.cancel();
        throw new GatewayError(`上游返回 HTTP ${res.status}，请稍后重试`,
          [404, 426, 429].includes(res.status) ? res.status : 502);
      }
      return res;
    }
  }

  async function json(url, env) { return boundedJSON(await upstream(url, env)); }

  async function release(env, base, tag) {
    const data = await json(GITHUB + (tag ? `/releases/tags/${tag}` : '/releases/latest'), env);
    if (!data || typeof data !== 'object' || !VERSION.test(data.tag_name) || data.draft || data.prerelease ||
        (tag && data.tag_name !== tag) || !Array.isArray(data.assets)) {
      throw new GatewayError('未找到受支持的 Decky 稳定版本');
    }
    const asset = data.assets.find(a => a.name === 'PluginLoader');
    if (!asset || !/^sha256:[0-9a-f]{64}$/.test(asset.digest) ||
        !Number.isSafeInteger(asset.size) || asset.size < 1 || asset.size > MAX_FILE ||
        asset.browser_download_url !== `https://github.com/SteamDeckHomebrew/decky-loader/releases/download/${data.tag_name}/PluginLoader`) {
      throw new GatewayError('官方发布缺少有效的安装包地址或 SHA-256 摘要');
    }
    return { version: data.tag_name, assets: { PluginLoader: {
      url: `${base}/loader/${data.tag_name}/PluginLoader`, sha256: asset.digest.slice(7), size: asset.size,
    } } };
  }

  async function stream(request, env, url, type, max = MAX_FILE, expectedSize) {
    const res = await upstream(url, env, request.method);
    const size = res.headers.get('Content-Length');
    if (size !== null && (!/^\d+$/.test(size) || Number(size) > max ||
        (expectedSize !== undefined && Number(size) !== expectedSize))) {
      await res.body?.cancel();
      throw new GatewayError('上游文件大小异常');
    }
    const h = headers(type);
    for (const key of ['Content-Length', 'Content-Encoding', 'ETag', 'Last-Modified']) {
      if (res.headers.has(key)) h.set(key, res.headers.get(key));
    }
    if (request.method === 'HEAD') {
      await res.body?.cancel();
      return new Response(null, { headers: h });
    }
    if (!res.body) throw new GatewayError('上游没有返回文件');
    let transferred = 0;
    const bounded = new TransformStream({
      transform(chunk, controller) {
        transferred += chunk.byteLength;
        if (transferred > max) throw new Error('Upstream file exceeds size limit');
        controller.enqueue(chunk);
      },
      flush() {
        if (expectedSize !== undefined && transferred !== expectedSize) throw new Error('Truncated upstream file');
      },
    });
    // No buffering or Cache API clone: backpressure keeps binary memory bounded.
    // SHA-256 is checked by the shared installer / Decky before installation.
    return new Response(res.body.pipeThrough(bounded), { headers: h });
  }

  return {
    async fetch(request, env) {
      try {
        const url = new URL(request.url), path = url.pathname;
        if (request.method === 'OPTIONS') return response(request, '', undefined, 204);
        if (request.method === 'POST' && /^\/plugins\/[^/]+\/versions\/[^/]+\/increment$/.test(path)) {
          return response(request, { ok: true, forwarded: false });
        }
        if (!['GET', 'HEAD'].includes(request.method)) return response(request, { error: '不支持的请求方法' }, undefined, 405);
        const base = origin(env.PUBLIC_BASE_URL);
        if (path === '/healthz') return response(request, { ok: true });
        if (path === '/api/status') return response(request, {
          name: 'CNDecky', origin: base, store_url: base + '/plugins', mode: 'proxy',
          storage: 'none', cache: null, recent: [],
        });
        if (path === '/api/client') return response(request, { sha256: bundle.clientSha256 });
        if (path === '/client.py') return response(request, bundle.client, 'text/x-python; charset=utf-8');
        if (path === '/install.sh') return response(request,
          bundle.installer.replace('@ORIGIN@', `'${base}'`), 'text/x-shellscript; charset=utf-8');
        let match;
        if ((match = /^\/launchers\/(install|configure|restore)\.desktop$/.exec(path))) {
          if (!base.startsWith('https:')) throw new GatewayError('桌面安装器需要已部署的 HTTPS 域名，本机 HTTP 页面仅供预览', 409);
          return response(request, bundle.launchers[match[1]].replaceAll(bundle.placeholder, base),
            'application/x-desktop; charset=utf-8', 200, `CNDecky-${match[1]}.desktop`);
        }
        if (path === '/api/release') return response(request, await release(env, base));
        if (path === '/plugins' || path === '/plugins/versions') {
          const version = /^v?(\d+)\.(\d+)\.(\d+)/.exec(request.headers.get('X-Decky-Version') || '');
          if (version && (+version[1] < 3 || (+version[1] === 3 && +version[2] < 3))) {
            throw new GatewayError('请先更新到 Decky 3.3.0 或更新版本', 426);
          }
          const data = await json(STORE + path, env);
          if (path === '/plugins/versions') {
            if (!data || typeof data !== 'object' || Array.isArray(data)) throw new GatewayError('官方商店返回了无效版本列表');
            for (const [name, item] of Object.entries(data)) {
              if (!Array.isArray(item) || item.length < 2) throw new GatewayError('官方商店返回了无效版本信息');
              checkArtifact(item[1], item[2]);
              data[name] = [item[0], item[1], artifactURL(base, item[1])];
            }
          } else {
            if (!Array.isArray(data)) throw new GatewayError('官方商店返回了无效列表');
            for (const plugin of data) {
              if (!plugin || !Number.isSafeInteger(plugin.id) || !Array.isArray(plugin.versions)) throw new GatewayError('官方商店返回了无效插件');
              for (const version of plugin.versions) {
                checkArtifact(version.hash, version.artifact);
                version.artifact = artifactURL(base, version.hash);
                version.cached = false;
              }
              const prefix = CDN + '/artifact_images/';
              try {
                plugin.image_url = typeof plugin.image_url === 'string' && plugin.image_url.startsWith(prefix)
                  ? base + '/images/' + encodeURIComponent(imageName(plugin.image_url.slice(prefix.length))) : '';
              } catch { plugin.image_url = ''; }
            }
            const sort = url.searchParams.get('sort_by') || 'name';
            const direction = url.searchParams.get('sort_direction') || 'asc';
            if (!['name', 'date', 'downloads'].includes(sort) || !['asc', 'desc'].includes(direction)) {
              throw new GatewayError('不支持的排序方式或方向', 400);
            }
            const field = { name: 'name', date: 'updated', downloads: 'downloads' }[sort];
            data.sort((a, b) => {
              const x = a[field] ?? (field === 'downloads' ? 0 : ''), y = b[field] ?? (field === 'downloads' ? 0 : '');
              return (x < y ? -1 : x > y ? 1 : 0) * (direction === 'desc' ? -1 : 1);
            });
          }
          return response(request, data);
        }
        if ((match = /^\/artifacts\/([0-9a-f]{64})\.zip$/.exec(path))) {
          return await stream(request, env, `${CDN}/versions/${match[1]}.zip`, 'application/zip');
        }
        if ((match = /^\/loader\/(v\d+\.\d+\.\d+)\/PluginLoader$/.exec(path))) {
          const meta = await release(env, base, match[1]);
          return await stream(request, env,
            `https://github.com/SteamDeckHomebrew/decky-loader/releases/download/${match[1]}/PluginLoader`,
            'application/octet-stream', MAX_FILE, meta.assets.PluginLoader.size);
        }
        if (path.startsWith('/images/')) {
          const name = imageName(path.slice('/images/'.length));
          const extension = name.split('.').pop().toLowerCase();
          return await stream(request, env, `${CDN}/artifact_images/${encodeURIComponent(name)}`,
            `image/${extension === 'jpg' ? 'jpeg' : extension}`, 8 * 1024 ** 2);
        }
        if (['/', '/app.js', '/style.css'].includes(path)) return env.ASSETS.fetch(request);
        return response(request, { error: '不存在的接口' }, undefined, 404);
      } catch (error) {
        return response(request, { error: error instanceof GatewayError ? error.message : '上游暂时不可用，请稍后重试' },
          undefined, error instanceof GatewayError ? error.status : 502);
      }
    },
  };
}

export default createWorker();
