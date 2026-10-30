"""Generate KDE desktop launchers for the existing HTTPS bootstrap."""
from urllib.parse import urlsplit

MODES = {
    "install": ("Install Decky", "安装 Decky", ""),
    "configure": ("Configure CNDecky", "切换 CNDecky 镜像源", "--configure-only"),
    "restore": ("Restore Decky Store", "恢复 Decky 官方源", "--restore-official"),
}

# The origin and action are separate argv, never interpolated into shell code.
# Konsole --hold leaves success/error output visible without requiring a keypress.
BOOTSTRAP = (
    'set -eu; '
    'case "$1" in https://*) ;; *) echo "CNDecky requires an HTTPS mirror."; exit 1;; esac; '
    'work=$(mktemp -d); '
    'trap \'rm -rf "$work"\' EXIT; '
    'echo "CNDecky: downloading installer..."; '
    'if curl --fail --show-error --location --proto "=https" --proto-redir "=https" '
    '--connect-timeout 15 --max-time 120 "$1/install.sh" -o "$work/install.sh"; then '
    'if [ -n "$2" ]; then sh "$work/install.sh" "$2"; else sh "$work/install.sh"; fi; '
    'else echo "Download failed. Please check your connection and try again."; exit 1; fi'
)


def exec_quote(argument):
    """Encode one argv using the Desktop Entry spec's two escaping layers."""
    quoted = ''.join('\\' + char if char in '\\"`$' else char for char in argument)
    return '"' + quoted.replace('\\', '\\\\').replace('%', '%%') + '"'


def desktop_entry(origin, mode):
    # The caller supplies an already validated origin. Reject HTTP previews here too.
    if urlsplit(origin).scheme != 'https':
        raise ValueError('桌面安装器需要已部署的 HTTPS 域名，本机 HTTP 页面仅供预览')
    english, chinese, flag = MODES[mode]
    args = ['konsole', '--separate', '--hold', '-e', 'sh', '-c', BOOTSTRAP,
            'cndecky-launcher', origin, flag]
    command = ' '.join(exec_quote(arg) for arg in args)
    return (
        '#!/usr/bin/env xdg-open\n'
        '[Desktop Entry]\n'
        'Type=Application\n'
        f'Name=CNDecky - {english}\n'
        f'Name[zh_CN]=CNDecky - {chinese}\n'
        'Comment=Open Konsole to run the CNDecky installer\n'
        'Icon=utilities-terminal\n'
        'Terminal=false\n'
        'StartupNotify=false\n'
        f'Exec={command}\n'
    )
