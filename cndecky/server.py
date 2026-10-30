"""Constrained, disk-cached Decky gateway. Run with python -m cndecky.server."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from .launcher import desktop_entry

ROOT = Path(__file__).resolve().parent.parent
STORE = "https://plugins.deckbrew.xyz"
CDN = "https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew"
GITHUB = "https://api.github.com/repos/SteamDeckHomebrew/decky-loader"
HASH = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"v\d+\.\d+\.\d+\Z")
ALLOWED_HOSTS = {
    "plugins.deckbrew.xyz", "cdn.tzatzikiweeb.moe", "api.github.com",
    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
}
LOG = logging.getLogger("cndecky")


class GatewayError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def check_upstream(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise GatewayError("拒绝访问未允许的上游地址")


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_upstream(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    check_upstream(url)
    request = Request(url, headers={"User-Agent": "CNDecky/0.1", "Accept-Encoding": "identity"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and urlsplit(url).hostname == "api.github.com":
        # Never propagate credentials to redirect destinations.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return build_opener(ProxyHandler({}), SafeRedirect()).open(request, timeout=45)


def validate_origin(value):
    p = urlsplit(value)
    local = p.hostname in ("localhost", "127.0.0.1", "::1")
    if (p.scheme not in ("http", "https") or (p.scheme != "https" and not local)
            or not p.hostname or p.username or p.password or p.path not in ("", "/")
            or p.query or p.fragment or re.search(r"[\s'\"\\]", value)):
        raise ValueError("PUBLIC_BASE_URL 必须是 HTTPS origin，例如 https://decky.lchnan.cn；本机开发允许 HTTP")
    return value.rstrip("/")


class Cache:
    """Serialized fills, atomic files, bounded disk usage, and bounded stale metadata."""
    def __init__(self, root, limit=2 * 1024**3, downloader=fetch):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # One process owns this cache directory. Recover only our interrupted files.
        for temporary in self.root.glob(".part-*"):
            if temporary.is_file():
                temporary.unlink()
        self.limit = limit
        self.downloader = downloader
        self.lock = threading.RLock()
        self.fill_locks = [threading.Lock() for _ in range(64)]
        self.download_slots = threading.BoundedSemaphore(4)
        self.db = sqlite3.connect(self.root / "index.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cache_entries (
                key TEXT PRIMARY KEY, url TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, fetched_at REAL NOT NULL, accessed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO counters VALUES ('hits', 0), ('misses', 0);
        """)
        self.db.commit()

    def count(self, name):
        self.db.execute("UPDATE counters SET value=value+1 WHERE name=?", (name,))
        self.db.commit()

    def get(self, url, ttl=300, expected=None, max_bytes=256 * 1024**2,
            stale_for=0, validator=None, open_file=False):
        key = hashlib.sha256(url.encode()).hexdigest()
        target = self.root / key
        # Different files download concurrently; a shared key is filled only once.
        # Fixed stripes keep lock memory bounded even for nonexistent hashes.
        with self.fill_locks[int(key[:8], 16) % len(self.fill_locks)]:
            now = time.time()
            with self.lock:
                age = now - target.stat().st_mtime if target.exists() else float("inf")
                if age < ttl:
                    self.count("hits")
                    self.db.execute("UPDATE cache_entries SET accessed_at=? WHERE key=?", (now, key))
                    self.db.commit()
                    return (open(target, "rb") if open_file else target), "HIT"
                self.count("misses")
            temporary = None
            try:
                with self.download_slots, self.downloader(url) as upstream, tempfile.NamedTemporaryFile(
                        dir=self.root, prefix=".part-", delete=False) as output:
                    temporary = Path(output.name)
                    digest, size = hashlib.sha256(), 0
                    while chunk := upstream.read(64 * 1024):
                        size += len(chunk)
                        if size > min(max_bytes, self.limit):
                            raise GatewayError("上游文件超过缓存大小限制")
                        digest.update(chunk)
                        output.write(chunk)
                if expected and digest.hexdigest() != expected:
                    raise GatewayError("上游文件 SHA-256 校验失败，未写入缓存")
                if validator:
                    validator(temporary)
                with self.lock:
                    self._evict(size, excluding=target)
                    temporary.replace(target)
                    self.db.execute("INSERT OR REPLACE INTO cache_entries VALUES (?, ?, ?, ?, ?, ?)",
                                    (key, url, size, digest.hexdigest(), now, now))
                    self.db.commit()
                    return (open(target, "rb") if open_file else target), "MISS"
            except (OSError, ValueError, GatewayError) as exc:
                # Never mask client errors, removals, incompatibility or checksum failures.
                transient = isinstance(exc, (URLError, TimeoutError, ConnectionError))
                if isinstance(exc, HTTPError):
                    transient = exc.code == 429 or exc.code >= 500
                with self.lock:
                    if transient and stale_for and age < ttl + stale_for and target.exists():
                        LOG.warning("Upstream unavailable; serving bounded stale metadata: %s", url)
                        return (open(target, "rb") if open_file else target), "STALE"
                if isinstance(exc, GatewayError):
                    raise
                if isinstance(exc, HTTPError):
                    status = exc.code if exc.code in (404, 426, 429) else 502
                    raise GatewayError(f"上游返回 HTTP {exc.code}，请稍后重试", status) from exc
                raise GatewayError("上游暂时不可用，请稍后重试") from exc
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)

    def _evict(self, incoming, excluding):
        files = [p for p in self.root.iterdir() if HASH.fullmatch(p.name) and p != excluding]
        total = sum(p.stat().st_size for p in files)
        for item in sorted(files, key=lambda p: p.stat().st_mtime):
            if total + incoming <= self.limit:
                break
            total -= item.stat().st_size
            item.unlink()
            self.db.execute("DELETE FROM cache_entries WHERE key=?", (item.name,))
        self.db.commit()

    def stats(self):
        with self.lock:
            files = [p for p in self.root.iterdir() if HASH.fullmatch(p.name)]
            counters = dict(self.db.execute("SELECT name, value FROM counters"))
            return {"files": len(files), "bytes": sum(p.stat().st_size for p in files),
                    "limit_bytes": self.limit, **counters}

    def recent(self):
        with self.lock:
            return [dict(zip(("url", "bytes", "fetched_at", "accessed_at"), row))
                    for row in self.db.execute(
                        "SELECT url, size, fetched_at, accessed_at FROM cache_entries ORDER BY accessed_at DESC LIMIT 12")]

    def contains(self, url):
        return (self.root / hashlib.sha256(url.encode()).hexdigest()).exists()


def json_validator(path):
    json.loads(path.read_bytes())


class Gateway:
    def __init__(self, origin, cache):
        self.origin, self.cache = validate_origin(origin), cache
        self.started = time.time()

    def json(self, url):
        file, state = self.cache.get(url, max_bytes=4 * 1024**2,
                                     stale_for=86400, validator=json_validator, open_file=True)
        with file:
            return json.load(file), state

    def artifact_url(self, digest):
        if not isinstance(digest, str) or not HASH.fullmatch(digest):
            raise GatewayError("官方商店返回了无效的插件摘要")
        return f"{self.origin}/artifacts/{digest}.zip"

    def plugins(self, query):
        data, state = self.json(STORE + "/plugins")
        if not isinstance(data, list):
            raise GatewayError("官方商店返回了无效列表")
        data = copy.deepcopy(data)
        for plugin in data:
            for version in plugin["versions"]:
                # The official current store has hash-only artifacts. Fail closed if it changes.
                canonical = f"{CDN}/versions/{version['hash']}.zip"
                if version.get("artifact") not in (None, "", canonical):
                    raise GatewayError("官方插件下载协议已变化，需要更新 CNDecky")
                version["artifact"] = self.artifact_url(version["hash"])
                version["cached"] = self.cache.contains(canonical)
            image = plugin.get("image_url", "")
            prefix = CDN + "/artifact_images/"
            if image.startswith(prefix) and "/" not in unquote(image[len(prefix):]):
                plugin["image_url"] = self.origin + "/images/" + image[len(prefix):]
            else:
                plugin["image_url"] = ""  # Do not force clients back onto unknown external hosts.
        sort_by = query.get("sort_by", ["name"])[0]
        if sort_by not in ("name", "date", "downloads"):
            raise GatewayError("不支持的排序方式", 400)
        direction = query.get("sort_direction", ["asc"])[0]
        if direction not in ("asc", "desc"):
            raise GatewayError("不支持的排序方向", 400)
        field = {"date": "updated", "name": "name", "downloads": "downloads"}[sort_by]
        data.sort(key=lambda p: (p.get(field) or (0 if field == "downloads" else "")),
                  reverse=direction == "desc")
        return data, state

    def versions(self):
        data, state = self.json(STORE + "/plugins/versions")
        if not isinstance(data, dict):
            raise GatewayError("官方商店返回了无效版本列表")
        for name, item in data.items():
            if not isinstance(item, list) or len(item) < 2:
                raise GatewayError("官方商店返回了无效版本信息")
            canonical = f"{CDN}/versions/{item[1]}.zip"
            if len(item) > 2 and item[2] not in (None, "", canonical):
                raise GatewayError("官方插件下载协议已变化，需要更新 CNDecky")
            data[name] = [item[0], item[1], self.artifact_url(item[1])]
        return data, state

    def release(self, tag=None):
        data, state = self.json(GITHUB + ("/releases/tags/" + tag if tag else "/releases/latest"))
        version = data.get("tag_name", "")
        if not VERSION.fullmatch(version) or data.get("draft") or data.get("prerelease"):
            raise GatewayError("未找到受支持的 Decky 稳定版本")
        assets = {}
        for asset in data.get("assets", []):
            name = asset["name"]
            if name != "PluginLoader":
                continue  # First release targets Steam Deck / SteamOS x86_64.
            expected_url = f"https://github.com/SteamDeckHomebrew/decky-loader/releases/download/{version}/{name}"
            digest = asset.get("digest", "")
            if asset.get("browser_download_url") != expected_url or not digest or not HASH.fullmatch(digest.removeprefix("sha256:")):
                raise GatewayError("官方发布缺少有效的下载地址或 SHA-256 摘要")
            assets[name] = {"url": f"{self.origin}/loader/{version}/{name}",
                            "sha256": digest.removeprefix("sha256:"), "size": asset["size"]}
        if "PluginLoader" not in assets:
            raise GatewayError("官方发布缺少 Steam Deck 安装包")
        return {"version": version, "assets": assets}, state


class Handler(BaseHTTPRequestHandler):
    server_version = "CNDecky/0.1"

    @property
    def gateway(self):
        return self.server.gateway

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.client_address[0], fmt % args)

    def headers_for(self, status, content_type, size, state=None, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Access-Control-Allow-Origin", "https://steamloopback.host")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-Decky-Version, Content-Type")
        self.send_header("Access-Control-Expose-Headers", "X-CNDecky-Cache")
        self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        if state:
            self.send_header("X-CNDecky-Cache", state)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()

    def send_data(self, data, content_type="application/json; charset=utf-8", status=200, state=None, filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.headers_for(status, content_type, len(data), state, filename)
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_file(self, path, content_type, state=None):
        self.send_stream(open(path, "rb"), content_type, state)

    def send_stream(self, file, content_type, state=None):
        # Descriptor was acquired while publishing/looking up the cache entry.
        # Eviction can unlink it without interrupting this active download.
        with file:
            self.headers_for(200, content_type, os.fstat(file.fileno()).st_size, state)
            if self.command != "HEAD":
                shutil.copyfileobj(file, self.wfile, 64 * 1024)

    def do_OPTIONS(self):
        self.send_data(b"", status=204)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        # Acknowledge Decky's install counter locally; never send personal usage upstream.
        if re.fullmatch(r"/plugins/[^/]+/versions/[^/]+/increment", urlsplit(self.path).path):
            self.send_data({"ok": True, "forwarded": False})
        else:
            self.send_data({"error": "不存在的接口"}, status=404)

    def do_GET(self):
        try:
            self.route()
        except GatewayError as exc:
            self.send_data({"error": str(exc)}, status=exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("Request failed")
            self.send_data({"error": "服务内部错误，请查看容器日志"}, status=500)

    def route(self):
        request = urlsplit(self.path)
        path, gw = request.path, self.gateway
        if path in ("/plugins", "/plugins/versions"):
            client_version = self.headers.get("X-Decky-Version", "")
            match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", client_version)
            if match and tuple(map(int, match.groups())) < (3, 3, 0):
                raise GatewayError("请先更新到 Decky 3.3.0 或更新版本", 426)
            data, state = gw.plugins(parse_qs(request.query)) if path == "/plugins" else gw.versions()
            self.send_data(data, state=state)
        elif path == "/healthz":
            self.send_data({"ok": True})
        elif path == "/api/status":
            self.send_data({"name": "CNDecky", "origin": gw.origin, "store_url": gw.origin + "/plugins",
                            "uptime_seconds": int(time.time() - gw.started), "cache": gw.cache.stats(),
                            "recent": gw.cache.recent(), "storage": "SQLite + local files"})
        elif path == "/api/release":
            data, state = gw.release()
            self.send_data(data, state=state)
        elif path == "/install.sh":
            import shlex
            script = (ROOT / "scripts/install.sh").read_text().replace("@ORIGIN@", shlex.quote(gw.origin))
            self.send_data(script.encode(), "text/x-shellscript; charset=utf-8")
        elif match := re.fullmatch(r"/launchers/(install|configure|restore)\.desktop", path):
            try:
                launcher = desktop_entry(gw.origin, match[1])
            except ValueError as exc:
                raise GatewayError(str(exc), 409) from exc
            self.send_data(launcher.encode(), "application/x-desktop; charset=utf-8",
                           filename=f"CNDecky-{match[1]}.desktop")
        elif path == "/client.py":
            self.send_file(ROOT / "scripts/client.py", "text/x-python; charset=utf-8")
        elif path == "/api/client":
            body = (ROOT / "scripts/client.py").read_bytes()
            self.send_data({"sha256": hashlib.sha256(body).hexdigest()})
        elif match := re.fullmatch(r"/artifacts/([0-9a-f]{64})\.zip", path):
            digest = match[1]
            cached, state = gw.cache.get(f"{CDN}/versions/{digest}.zip", ttl=float("inf"), expected=digest, open_file=True)
            self.send_stream(cached, "application/zip", state)
        elif path.startswith("/images/"):
            name = unquote(path[len("/images/"):])
            if not re.fullmatch(r"[^/\\\x00-\x1f]{1,220}\.(?:png|jpg|jpeg|webp)", name, re.I):
                raise GatewayError("无效图片路径", 400)
            cached, state = gw.cache.get(CDN + "/artifact_images/" + quote(name, safe=""),
                                         ttl=86400, max_bytes=8 * 1024**2, open_file=True)
            ext = name.rsplit(".", 1)[1].lower().replace("jpg", "jpeg")
            self.send_stream(cached, "image/" + ext, state)
        elif match := re.fullmatch(r"/loader/(v\d+\.\d+\.\d+)/PluginLoader", path):
            release, _ = gw.release(match[1])
            upstream = f"https://github.com/SteamDeckHomebrew/decky-loader/releases/download/{match[1]}/PluginLoader"
            cached, state = gw.cache.get(upstream, ttl=float("inf"), expected=release["assets"]["PluginLoader"]["sha256"], open_file=True)
            self.send_stream(cached, "application/octet-stream", state)
        elif path in ("/", "/app.js", "/style.css"):
            name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
            mime = {"/": "text/html", "/app.js": "text/javascript", "/style.css": "text/css"}[path]
            self.send_file(ROOT / "web" / name, mime + "; charset=utf-8")
        else:
            self.send_data({"error": "不存在的接口"}, status=404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, gateway):
        self.gateway = gateway
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(120)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("BIND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    parser.add_argument("--origin", default=os.getenv("PUBLIC_BASE_URL", "http://localhost:8080"))
    parser.add_argument("--cache", default=os.getenv("CACHE_DIR", ".cache"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cache = Cache(args.cache, limit=int(os.getenv("CACHE_MAX_MB", "2048")) * 1024**2)
    server = Server((args.host, args.port), Gateway(args.origin, cache))
    LOG.info("Listening at %s:%s; public origin %s", args.host, args.port, args.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
