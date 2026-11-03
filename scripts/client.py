#!/usr/bin/env python3
"""CNDecky installer. All downloads finish and verify before local changes."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class InstallError(Exception):
    pass


def origin(value, local=False):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" and not (local and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"))):
        raise InstallError("镜像地址必须使用 HTTPS")
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or re.search(r"[\s'\"\\]", value)):
        raise InstallError("镜像地址必须是 origin，例如 https://decky.lchnan.cn")
    return value.rstrip("/")


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(req.full_url)[:2] != urlsplit(newurl)[:2]:
            raise InstallError("镜像返回跨域跳转，已停止下载")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, destination, expected=None, limit=256 * 1024**2, *, progress=False, total=None):
    digest, size = hashlib.sha256(), 0
    request = Request(url, headers={"User-Agent": "CNDecky-Installer/0.1"})
    if progress:
        print("正在连接下载源，等待数据…（网络无响应时会超时报错）", file=sys.stderr, flush=True)
    with build_opener(ProxyHandler({}), SameOriginRedirect()).open(request, timeout=60) as response, open(destination, "wb") as output:
        if not isinstance(total, int) or not 0 < total <= limit:
            try:
                total = int(response.headers.get("Content-Length", ""))
            except (ValueError, TypeError):
                total = 0
            if not 0 < total <= limit:
                total = 0
        started = time.monotonic()
        last_update = started
        interactive = sys.stderr.isatty()

        def show_progress(final=False):
            elapsed = max(time.monotonic() - started, 0.001)
            speed = size / elapsed
            message = f"已下载 {size / 1024**2:.1f} MiB"
            if total:
                percent = min(100, size * 100 // total)
                remaining = max(0, int((total - size) / speed)) if speed else 0
                message += f" / {total / 1024**2:.1f} MiB  {percent:3d}%"
            message += f"  {speed / 1024**2:.2f} MiB/s"
            if total and speed:
                message += f"  预计剩余 {remaining // 60:02d}:{remaining % 60:02d}"
            print(("\r\033[2K" if interactive else "") + message,
                  end="\n" if final or not interactive else "", file=sys.stderr, flush=True)

        # read1 returns available data without waiting to fill a whole 64 KiB block.
        read = getattr(response, "read1", response.read)
        try:
            while chunk := read(64 * 1024):
                size += len(chunk)
                if size > limit:
                    raise InstallError("下载文件超过大小限制")
                output.write(chunk)
                digest.update(chunk)
                now = time.monotonic()
                if progress and (last_update == started or now - last_update >= 1):
                    show_progress()
                    last_update = now
        finally:
            if progress:
                show_progress(final=True)
    if progress and expected:
        print("下载结束，正在校验 SHA-256…", file=sys.stderr, flush=True)
    if expected and digest.hexdigest() != expected:
        raise InstallError("安装包 SHA-256 校验失败，现有 Decky 未被修改")
    if progress:
        print("SHA-256 校验通过。" if expected else "下载完成。", file=sys.stderr, flush=True)


def read_settings(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise InstallError(f"配置文件无法读取，未覆盖：{path}") from exc
    if not isinstance(data, dict):
        raise InstallError("Decky 配置不是 JSON 对象，未覆盖")
    return data


def settings_for(data, mirror, restore):
    result = dict(data)
    result["store"] = 0 if restore else 2
    if restore:
        result.pop("store-url", None)
    else:
        result["store-url"] = mirror + "/plugins"
    return result


def atomic_write(path, content, mode=0o644, owner=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".cndecky-", delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            os.chmod(temporary, mode)
            if owner:
                os.chown(temporary, *owner)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def safe_target(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise InstallError(f"拒绝覆盖符号链接路径：{part}")
    if path.exists() and not path.is_file():
        raise InstallError(f"目标不是普通文件：{path}")


class Snapshot:
    def __init__(self, paths, directory):
        self.directory = directory
        directory.mkdir(parents=True, mode=0o700)
        self.entries = []
        self.recovery = {}
        try:
            for index, path in enumerate(paths):
                safe_target(path)
                metadata = None
                backup = directory / str(index)
                if path.exists():
                    info = path.stat()
                    metadata = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
                    shutil.copyfile(path, backup)
                    # Keep the original inode on its own filesystem. Rollback must not
                    # allocate another binary, especially when the failure is ENOSPC.
                    recovery = path.with_name('.cndecky-rollback-' + uuid.uuid4().hex)
                    os.link(path, recovery)
                    self.recovery[path] = recovery
                self.entries.append((path, backup, metadata))
            (directory / "manifest.json").write_text(json.dumps([
                {"path": str(p), "backup": b.name, "metadata": m} for p, b, m in self.entries], indent=2))
        except BaseException:
            self.cleanup()
            raise

    def restore(self):
        errors = []
        for path, backup, metadata in self.entries:
            try:
                if metadata is None:
                    path.unlink(missing_ok=True)
                else:
                    self.recovery[path].replace(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        if errors:
            raise InstallError("部分文件恢复失败：" + '; '.join(errors))

    def cleanup(self):
        for recovery in self.recovery.values():
            recovery.unlink(missing_ok=True)


def run(*args, check=True):
    return subprocess.run(args, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def service_text(homebrew):
    # Home paths are constrained before this is used in a systemd unit.
    return f"""[Unit]
Description=Decky Loader (installed by CNDecky)
After=network.target
[Service]
Type=simple
User=root
Restart=always
KillMode=process
TimeoutStopSec=15
ExecStart={homebrew}/services/PluginLoader
WorkingDirectory={homebrew}/services
Environment=UNPRIVILEGED_PATH={homebrew}
Environment=PRIVILEGED_PATH={homebrew}
Environment=LOG_LEVEL=INFO
[Install]
WantedBy=multi-user.target
"""


@contextmanager
def install_lock():
    import fcntl
    with open("/run/lock/cndecky.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("另一个 CNDecky 安装任务正在运行") from exc
        yield


def apply(home, mirror, staged=None, version=None, restore=False, runner=run,
          system_root=Path("/"), owner=None):
    homebrew = home / "homebrew"
    settings = homebrew / "settings/loader.json"
    unit = system_root / "etc/systemd/system/plugin_loader.service"
    binary = homebrew / "services/PluginLoader"
    version_file = homebrew / "services/.loader.version"
    saved_unit = homebrew / "services/.systemd/plugin_loader-release.service"
    if staged is None and not binary.is_file():
        raise InstallError("没有找到 Decky，请先运行安装命令")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(home)):
        raise InstallError("暂不支持含空格或特殊字符的用户目录")
    targets = [settings]
    steam_flags = []
    if staged:
        targets += [unit, binary, version_file, saved_unit]
        # Steam's .steam/steam is normally a symlink: resolve only this known Steam directory.
        steam = (home / ".steam/steam").resolve()
        if not steam.is_dir() or not steam.is_relative_to(home.resolve()):
            raise InstallError("未找到当前用户的 Steam 目录，请先启动 Steam")
        steam_flags.append(steam / ".cef-enable-remote-debugging")
        targets += steam_flags
    for target in targets:
        safe_target(target)
    if unit.exists() and f"ExecStart={binary}" not in unit.read_text():
        raise InstallError("检测到自定义 Decky 服务路径，请手动检查后再安装")
    read_settings(settings)  # Refuse malformed settings before stopping anything.
    active = runner("systemctl", "is-active", "--quiet", "plugin_loader", check=False).returncode == 0
    enabled = runner("systemctl", "is-enabled", "--quiet", "plugin_loader", check=False).returncode == 0
    snapshot = None
    try:
        if active:
            runner("systemctl", "stop", "plugin_loader")
        backup_dir = homebrew / ".cndecky-backups" / f"{time.time_ns()}"
        # Prevent following pre-existing user-controlled symlinks into unrelated paths.
        for part in (backup_dir, *backup_dir.parents):
            if part.is_symlink():
                raise InstallError(f"备份目录不能是符号链接：{part}")
        snapshot = Snapshot(targets, backup_dir)
        data = settings_for(read_settings(settings), mirror, restore)
        if staged:
            atomic_write(binary, staged.read_bytes(), 0o755)
            atomic_write(version_file, (version + "\n").encode())
            content = service_text(homebrew).encode()
            atomic_write(unit, content)
            atomic_write(saved_unit, content)
            for flag in steam_flags:
                if not flag.exists():
                    atomic_write(flag, b"", owner=owner)
            (homebrew / "plugins").mkdir(parents=True, exist_ok=True)
        atomic_write(settings, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(), owner=owner)
        if staged:
            runner("systemctl", "daemon-reload")
            runner("systemctl", "enable", "plugin_loader")
        if staged or active:
            runner("systemctl", "start", "plugin_loader")
            runner("systemctl", "is-active", "--quiet", "plugin_loader")
    except BaseException as original_error:
        errors = []
        def attempt(function, *args, **kwargs):
            try:
                function(*args, **kwargs)
            except BaseException as exc:
                errors.append(str(exc))
        if snapshot:
            attempt(runner, "systemctl", "stop", "plugin_loader")
            attempt(snapshot.restore)
            attempt(runner, "systemctl", "daemon-reload")
            if not enabled:
                attempt(runner, "systemctl", "disable", "plugin_loader")
        if active:
            attempt(runner, "systemctl", "start", "plugin_loader")
        if not errors and snapshot:
            snapshot.cleanup()
        if errors:
            raise InstallError(f"安装未完成，自动恢复也遇到错误：{'; '.join(errors)}。请检查 {homebrew / '.cndecky-backups'} 和服务日志") from original_error
        raise
    else:
        snapshot.cleanup()
        return backup_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--configure-only", action="store_true")
    mode.add_argument("--restore-official", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user", help="用 root 运行时指定 Steam 用户，通常是 deck")
    args = parser.parse_args()
    mirror = origin(args.mirror, local=args.dry_run)
    user = args.user or os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    account = pwd.getpwnam(user)
    home = Path(account.pw_dir)
    changing_binary = not (args.configure_only or args.restore_official)
    if not args.dry_run:
        if os.geteuid() != 0:
            raise InstallError("请通过 install.sh 运行，或使用 sudo")
        if account.pw_uid == 0:
            raise InstallError("请使用 --user deck 指定 Steam 用户")
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise InstallError("首版安装器仅支持 SteamOS x86_64")
        release_file = Path("/etc/os-release").read_text()
        if not re.search(r'^ID=[\"\']?steamos[\"\']?$', release_file, re.M):
            raise InstallError("首版安装器仅支持 SteamOS；其他系统请手动配置商店")
    with tempfile.TemporaryDirectory(prefix="cndecky-") as temp:
        temp = Path(temp)
        staged, version = None, None
        if changing_binary:
            download(mirror + "/api/release", temp / "release.json", limit=1024**2)
            release = json.loads((temp / "release.json").read_bytes())
            version = release["version"]
            if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
                raise InstallError("镜像返回了无效版本")
            asset = release["assets"]["PluginLoader"]
            if asset["url"] != f"{mirror}/loader/{version}/PluginLoader" or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
                raise InstallError("镜像返回了无效安装包地址或摘要")
            if not args.dry_run:
                staged = temp / "PluginLoader"
                print(f"准备下载 Decky {version}，下载完成后校验官方 SHA-256。", flush=True)
                download(asset["url"], staged, expected=asset["sha256"],
                         progress=True, total=asset.get("size"))
                if staged.read_bytes()[:4] != b"\x7fELF":
                    raise InstallError("下载文件不是 Linux 可执行文件")
        elif not args.restore_official:
            download(mirror + "/plugins", temp / "plugins.json", limit=4 * 1024**2)
            if not isinstance(json.loads((temp / "plugins.json").read_bytes()), list):
                raise InstallError("镜像商店不可用")
        action = "恢复官方商店" if args.restore_official else ("切换镜像商店" if args.configure_only else f"安装 Decky {version} 并切换镜像商店")
        print(f"{action}；用户：{user}；目录：{home / 'homebrew'}")
        if args.dry_run:
            print("预览完成，没有修改文件、没有操作 systemd。")
            return
        with install_lock():
            # Ctrl+C and termination during local mutation must take the rollback path.
            import signal
            def interrupted(signum, frame):
                raise KeyboardInterrupt(f"收到中断信号 {signum}")
            signal.signal(signal.SIGTERM, interrupted)
            backup = apply(home, mirror, staged, version, args.restore_official, owner=(account.pw_uid, account.pw_gid))
        print(f"完成。配置/旧版本备份：{backup}")
        print("请返回游戏模式查看 Decky；恢复官方源不会卸载已安装的插件。")


if __name__ == "__main__":
    try:
        main()
    except (InstallError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"CNDecky: {exc}")
