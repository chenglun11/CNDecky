# Cloudflare Worker 转发版

与 Docker 版提供相同的网页、安装器、换源和恢复入口。此版本只转发固定的 Decky 官方来源，**不需要服务器、Docker、SQLite、D1、KV 或 R2**。安装包流式传输，不落盘；没有持久缓存、最近下载记录或缓存命中统计。

## 本地预览

需要 Node.js 22+、npm 和 Python 3.10+。Python 只在构建时打包与 Docker 共用的安装脚本、客户端和桌面启动器；Worker 运行时不使用 Python。

在项目的 `worker` 目录运行：

```sh
npm ci
npm run dev
```

打开 <http://localhost:8787>。这里的商店和版本查询会访问真实官方源；本机 HTTP 模式禁用桌面启动器下载和网页命令复制；安装脚本仍可读取，但拒绝从 HTTP 地址执行安装。启动、测试和部署时，Wrangler 的构建步骤自动重新生成安装资源，避免两套客户端版本漂移。`generated.mjs` 不需要提交。

## 部署到自己的域名

1. 当前 `worker/wrangler.jsonc` 已将 `vars.PUBLIC_BASE_URL` 配为 `https://decky.lchnan.cn`，并绑定该自定义域名。部署到其他域名时，同时修改地址和 `routes`；安装器不会采用访问者传入的 Host。`npm run dev` 会将地址覆盖为本地 HTTP 预览。
2. 确认 `lchnan.cn` 已托管到要部署的 Cloudflare 账户。配置中的域名绑定如下：

   ```json
   "routes": [
     { "pattern": "decky.lchnan.cn", "custom_domain": true }
   ],
   "workers_dev": false
   ```

   如果该主机名已有 Docker 站点，先选择一个空闲子域名，例如 `decky-worker.lchnan.cn`，同时修改 `PUBLIC_BASE_URL`。两个版本可以各用一个域名，不能同时占用同一主机名。不要覆盖其他服务的 DNS 记录。
3. 登录并检查构建，再部署：

   ```sh
   npx wrangler login
   npm run check
   npm run deploy
   ```

4. 打开正式域名，确认页面显示「流式转发」、商店列表和稳定版正常。下载启动器，在掌机桌面模式双击，按提示允许执行并输入 sudo 密码。

没有自定义域名时，删除 `routes` 并将 `workers_dev` 改为 `true`，可先部署到自己的 `workers.dev` 地址，再将 `PUBLIC_BASE_URL` 改为实际分配的 HTTPS 地址并重新部署。掌机所在网络需能访问该地址；建议最终使用自己的域名。当前配置关闭 `workers.dev` 和版本预览地址，只通过指定域名提供服务。`npm run check` 是本地打包检查，不创建云资源。

GitHub 匿名 API 有请求限额。如果版本查询返回限流，可选用仅能读取公共发布信息的 token：

```sh
npx wrangler secret put GITHUB_TOKEN
```

按提示粘贴 token，不要写进 `vars`、Git 仓库或浏览器。token 只用于初始 `api.github.com` 请求，重定向和其他域名不会收到它。也可在被 Git 忽略的 `worker/.dev.vars` 中设置本地开发用 token。不要把开发文件包含在发布包中。

安装、换源、恢复动作与 Docker 版一致。两种部署间切换时，在目标站点选择「仅切换源」，无需重装 Decky。已有插件保留。

## 转发范围与校验

- 商店列表和版本检查：获取官方 JSON，将 ZIP 和封面地址改为本站路径，保持官方版本和 SHA-256。
- Decky 本体：从 GitHub 官方稳定版取得摘要、大小和下载地址；在 Worker 内跟随校验过的 HTTPS 重定向，掌机不会被跳回 GitHub。
- 插件 ZIP、安装包、封面：直接流式返回。支持 GET/HEAD；本版不提供断点续传，客户端重试时重新下载完整文件。
- 安装包最大 256 MiB、封面最大 8 MiB、元数据最大 4 MiB；异常状态返回 JSON 错误，传输中断不会伪装成完整文件。
- Worker 不把整个二进制放入内存计算摘要。共用的 Python 安装器在写入系统前校验 Decky 二进制；Decky 在安装插件前校验 ZIP 的官方哈希。网页手动下载的 ZIP 可自行比对页面所示 SHA-256。
- 不转发浏览器 Cookie、Authorization 或任意 URL。GitHub API 凭据仅发送给初始 API 请求，不随重定向传播。
- 安装计数接口本地确认，不向官方提交个人下载次数。应用不保存下载历史；Cloudflare 的运行日志与平台统计仍按账户设置工作，不要把「不落盘」理解为平台没有日志。
- Worker 不主动持久缓存，响应使用 `Cache-Control: no-store` 和 `X-CNDecky-Cache: BYPASS`。下载时上游仍需可用，不能离线供包。

部分插件的额外依赖、插件自身的联网服务，以及 Decky 自带的本体更新请求不经过此入口。本体更新可重新运行网页安装器。实际速度需从掌机网络测试；Cloudflare 转发不承诺国内线路加速。

## 验证

```sh
npm test
npm run check
```

测试覆盖共用安装资源一致性、GET/HEAD/CORS、下载链接重写、白名单与重定向、凭据隔离、上游失败、流式传输及截断。`npm run check` 使用 Wrangler 构建；本地运行时和真实上游的连通性需结合 `npm run dev` 验证。没有实际部署前，不代表 Cloudflare 线上出口或掌机线路已验证。

参考：[Worker 静态资源](https://developers.cloudflare.com/workers/static-assets/)、[自定义域名](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)、[流式响应](https://developers.cloudflare.com/workers/runtime-apis/streams/)、[平台限制](https://developers.cloudflare.com/workers/platform/limits/)。
