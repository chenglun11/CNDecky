#!/bin/sh
# Served with the configured origin substituted by CNDecky.
set -eu
MIRROR=@ORIGIN@
case "${1:-}" in
    --help|-h)
        echo 'CNDecky: install.sh [--mirror https://decky.example.com] [--configure-only | --restore-official] [--dry-run]'
        echo '请在 Steam Deck 桌面模式中运行。安装需要已有的 sudo 密码。'
        exit 0 ;;
esac
if [ "${1:-}" = '--mirror' ]; then
    [ "$#" -ge 2 ] || { echo '缺少镜像地址' >&2; exit 1; }
    MIRROR=${2%/}
    shift 2
fi
case "$MIRROR" in
    https://*) ;;
    *) echo '镜像地址必须使用 HTTPS；先部署服务，再运行此脚本。' >&2; exit 1 ;;
esac
for dependency in curl python3 sha256sum; do
    command -v "$dependency" >/dev/null 2>&1 || { echo "缺少依赖：$dependency" >&2; exit 1; }
done
CNDECKY_TMP=$(mktemp -d)
trap 'rm -rf "$CNDECKY_TMP"' EXIT HUP INT TERM
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 120 "$MIRROR/client.py" -o "$CNDECKY_TMP/client.py"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 30 "$MIRROR/api/client" -o "$CNDECKY_TMP/client.json"
CNDECKY_SHA=$(python3 -c 'import json,sys,re; h=json.load(open(sys.argv[1]))["sha256"]; assert re.fullmatch("[0-9a-f]{64}",h); print(h)' "$CNDECKY_TMP/client.json")
printf '%s  %s\n' "$CNDECKY_SHA" "$CNDECKY_TMP/client.py" | sha256sum --check --status
# Let the client complete read-only planning without elevating privileges.
for arg in "$@"; do
    if [ "$arg" = '--dry-run' ]; then
        python3 "$CNDECKY_TMP/client.py" --mirror "$MIRROR" "$@"
        exit $?
    fi
done
if [ "$(id -u)" -eq 0 ]; then
    python3 "$CNDECKY_TMP/client.py" --mirror "$MIRROR" "$@"
else
    sudo python3 "$CNDECKY_TMP/client.py" --mirror "$MIRROR" "$@"
fi
