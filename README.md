# CNDecky

给自己的 Steam Deck 一个方便的 Decky 下载入口。

提供两种独立部署方式，共用网页、安装器和换源协议：

| 部署方式 | 数据保存 | 适合场景 |
| --- | --- | --- |
| **Docker + Python + SQLite** | 本地文件缓存、索引、命中统计 | 已有新加坡服务器，需要重复下载复用缓存 |
| **Cloudflare Worker** | 纯流式转发，不保存文件或下载历史 | 主要解决 GitHub / 官方源连不上或慢，不想维护服务器 |

Worker 的正式地址配置为 `cndecky.lchnan.cn`；下方 Docker 章节使用 `decky.lchnan.cn` 作为可修改的服务器示例。Worker 的安装、命令行部署和 Cloudflare 网页构建配置见 **[worker/README.md](worker/README.md)**。两者可同时存在于不同域名，在目标站点运行「仅切换源」即可切换。

## 能做什么

- 浏览、搜索官方插件，按标签筛选，查看版本、下载原始 ZIP。
- 提供下载后双击打开 Konsole 的桌面启动器，支持 Decky 稳定版安装、镜像换源、恢复官方源；命令作为备用入口。
- 转发商店列表、版本更新列表、插件 ZIP、封面图和 Decky 安装包。
- 按需获取，不预先同步整个商店；插件包保持官方 SHA-256，不修改内容。
- Docker 版以 SQLite 保存缓存索引、下载时间、最近访问和命中统计；文件保存在服务器本地数据卷。
- Docker 版默认缓存文件上限 2 GiB，超过后回收最早写入的文件。SQLite 和并发下载的临时文件另占空间。
- Worker 版转发安装包与商店，不需要数据库或对象存储；网页明确显示转发状态。
- 安装器修改前备份，失败或中断时尝试恢复旧文件与服务；保留插件与其他设置。

**当前边界：**安装器仅支持 SteamOS x86_64、systemd、当前用户默认 `~/homebrew` 路径。插件的 `remote_binary` 依赖和运行时访问的外部服务不经过镜像；Decky 自带的本体更新仍访问 GitHub，可重新运行本站安装命令更新。不是“所有插件完全不需要外网”的承诺。

## 本地运行

需要 Python 3.10+，没有第三方 Python 依赖：

```sh
python3 -m cndecky.server
```

打开 <http://localhost:8080>。本地 HTTP 页面只用于预览，启动器下载和安装命令复制会禁用；掌机安装需要正式的 HTTPS 域名。

## Docker

```sh
cp .env.example .env
docker compose up -d --build
```

默认打开 <http://localhost:8080>。容器以非 root 用户运行，只向宿主机 loopback 发布端口。默认 named volume `cndecky-data` 存储 `/data/cache/index.sqlite3`、SQLite WAL 文件和缓存对象；`docker compose down` 不删除它，**不要用 `down -v`，除非确实要清空数据**。

要直接把数据放到指定宿主机目录，将 Compose 的 volume 改为 `/srv/cndecky/data:/data`，并在部署前为该目录设置 UID/GID `10001:10001` 的写入权限。不要与其他服务共用目录。

### 新加坡服务器 + decky.lchnan.cn

1. 将 `decky.lchnan.cn` 的 DNS A/AAAA 记录指向服务器；没有可用 IPv6 时不要添加 AAAA。
2. 把项目放到服务器，例如 `/opt/cndecky`，复制 `.env.example` 为 `.env`，修改：

   ```dotenv
   PUBLIC_BASE_URL=https://decky.lchnan.cn
   PORT=8080
   CACHE_MAX_MB=2048
   ```

3. `docker compose up -d --build`。
4. 用服务器上现有的反向代理将该域名转发至 `127.0.0.1:8080` 并启用 HTTPS。`deploy/Caddyfile` 提供 Caddy 配置片段；不要覆盖服务器原有的其他站点。Caddy 不包含在此 Compose 中。
5. 打开 `https://decky.lchnan.cn`，确认服务在线、插件列表和稳定版都正常，再点击「下载安装器」。

如果服务器已有服务占用 8080，修改 `.env` 的 `PORT`，同步调整反向代理目标。网页、商店地址和安装器使用同一个 `PUBLIC_BASE_URL`，必须与对外域名一致。国内连接新加坡的实际效果取决于线路，需从掌机所在网络实测。

GitHub 匿名 API 可能限流。可选在 `.env` 设置 `GITHUB_TOKEN`，仅需读取公共发布信息；token 只发送给 `api.github.com`，不写入页面、安装脚本或 SQLite。`.env` 已被 Git 与 Docker build context 排除。

默认是无需登录的个人镜像入口，知道域名的人也能下载固定上游的资源。不要把后台放在 HTTP Basic Auth 后面而期望掌机能直接使用：Decky 不会为商店和 ZIP 自动附带这种凭据。如果要限制访问，可在反向代理/网络层使用适合自己设备的访问规则。

## Steam Deck 上使用

先在桌面模式为当前用户设置 sudo 密码，确保系统已有 `curl`、`python3`、`sha256sum`。本项目不会自动设置临时密码。

推荐直接在网页选择操作，点击「下载安装器 / 下载换源工具 / 下载恢复工具」。在文件管理器的下载文件夹双击对应的 `CNDecky-*.desktop`，它会打开 Konsole，下载并运行已有的安装脚本。系统首次可能要求确认信任或允许执行；sudo 密码在 Konsole 中输入。

如果文件被 Kate 当作文本打开，在文件属性 → 权限中勾选「可执行」后再打开。Firefox 如果保存为 `.desktop.download`，先删除末尾的 `.download`。网页无法直接注册一个掌机原本没有的 Konsole URL 协议；这个入口采用与 Decky 官方安装器相同的桌面启动文件形式，不需要手输命令。

启动器只接受本站预置的安装、换源和恢复三个动作。它使用 HTTPS 下载到独立临时目录，下载失败不会执行不完整文件，结束后清理临时文件；Konsole 窗口保留结果。启动器接口是 `/launchers/install.desktop`、`/launchers/configure.desktop` 和 `/launchers/restore.desktop`，响应带固定附件文件名。

如需手动操作，展开网页的「手动安装 / 复制命令」。示例：

```sh
curl -fSL --proto '=https' --proto-redir '=https' \
  https://decky.lchnan.cn/install.sh -o ~/cndecky-install.sh \
  && sh ~/cndecky-install.sh
```

其他模式：

```sh
# 预览安装计划，不写入系统
sh ~/cndecky-install.sh --dry-run

# 已经安装了 Decky，只换源
sh ~/cndecky-install.sh --configure-only

# 恢复官方商店，不卸载插件
sh ~/cndecky-install.sh --restore-official
```

脚本先从本站下载客户端并校验摘要。安装客户端再下载官方稳定版二进制，校验 GitHub release 的 SHA-256，下载完整后才停止 Decky、备份配置并变更文件。所有请求都使用 HTTPS；这里的信任根是你自己部署的域名和服务器，摘要检测损坏，不能防止镜像服务器本身被篡改。

配置文件为 `~/homebrew/settings/loader.json`。切换时设置 `store=2`、`store-url=https://你的域名/plugins`；恢复时设 `store=0` 并删除 `store-url`，其他设置保留。修改期间会停止并恢复原本运行的 Decky 服务，避免配置被旧进程覆盖。

备份保存在 `~/homebrew/.cndecky-backups/<时间戳>/`，其中 `manifest.json` 对应原文件路径与数字命名的备份文件。重装不清空整个 `services`、`plugins` 或 `settings` 目录。遇到异常后可结合备份和 `journalctl -u plugin_loader` 排查。

如果镜像服务器已不可用，可以在掌机的 Decky 设置中把商店通道切回「默认」。网页恢复命令需要先下载本站客户端，断网时不能依赖它。

## Docker 缓存与运维

```sh
docker compose ps
docker compose logs --tail=100 cndecky
curl -f http://localhost:8080/healthz
curl -f http://localhost:8080/api/status
```

- `/healthz` 只检查进程可用；不代表上游通畅。网页会分别显示商店与版本请求结果。
- 商店/发布元数据缓存 5 分钟。遇到上游网络故障、429 或 5xx 时可返回额外 24 小时内的旧缓存，并通过 `X-CNDecky-Cache: STALE` 告知客户端。404、版本不兼容、摘要错误不会被当作正常数据掩盖。
- ZIP 与版本固定的 Decky 安装包按摘要校验后缓存；图片缓存一天。缓存回收不会中断已经打开的下载。
- 同一文件的并发请求合并；最多 4 个上游文件并发下载，单文件最大 256 MiB，图片最大 8 MiB。
- 没有任意 URL 代理接口。只有明确允许的 Decky/GitHub/CDN 路径和重定向目标可用。
- SQLite 索引和命中计数跨重启保留，插件列表的 JSON 响应本身以缓存文件保存。容量统计为缓存 payload，不包含 SQLite 和下载临时文件。
- 安装计数接口在本站确认，不向官方发送个人下载计数。
- 不提供删除缓存的网页接口；需要完整重置时，先停止容器，再备份/清空专用数据卷。

## 开发与验证

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile cndecky/server.py scripts/client.py
node --check web/app.js
node --test tests/frontend.test.mjs
sh -n scripts/install.sh
docker compose config --quiet
# Worker 版另在 worker/ 目录运行 npm ci、npm test、npm run check
```

测试使用临时目录和模拟 systemd，只替换网络/系统边界，不会安装到开发机器。覆盖摘要失败、并发缓存、SQLite 持久化、上游错误、CORS、API 地址重写、配置保留、重复安装、失败/中断回滚等行为。真实 Steam Deck 的 Steam UI 注入和硬件兼容需要设备实测。

## 上游协议依据

对照 Decky Loader **v3.3.1** 的官方源码和当前官方 API 实现：

- [商店地址、artifact 字段及版本列表](https://github.com/SteamDeckHomebrew/decky-loader/blob/v3.3.1/frontend/src/store.tsx)
- [插件安装、SHA-256 和额外 remote_binary 依赖](https://github.com/SteamDeckHomebrew/decky-loader/blob/v3.3.1/backend/decky_loader/browser.py)
- [官方安装器](https://github.com/SteamDeckHomebrew/decky-installer/blob/main/cli/install_release.sh)
- [Decky 发布版本](https://github.com/SteamDeckHomebrew/decky-loader/releases)

CNDecky 与 Decky 官方无隶属关系；插件及 Decky 二进制的版权与许可归各自项目。页面中的描述、作者、版本来自官方商店。
