# 运行结构

## Docker / SQLite

```text
Steam Deck / Web UI
        │ HTTPS
        ▼
现有反向代理（TLS）
        │ loopback:8080
        ▼
CNDecky Docker
 ├─ web/：原生 HTML + CSS + JavaScript
 ├─ cndecky/server.py：受限下载网关与 HTTP API
 └─ /data/cache/
     ├─ index.sqlite3：索引、访问时间、命中统计
     └─ SHA256(URL) 命名文件：官方响应及安装包
        │ 缓存未命中
        ▼
官方商店 / 官方 CDN / Decky GitHub Release
```

第一版仅一个实例写入数据目录，不应让多个容器共享同一个缓存目录。SQLite 使用 WAL；同一进程的数据库修改由锁保护。下载使用固定数量的条带锁合并同一 URL 请求，并使用信号量限制并行下载。网络传输不持有全局索引锁，因此查看状态不受下载等待影响。

下载先写临时文件，检查大小和摘要/JSON，再原子替换缓存路径与更新索引。发布与读取时在锁内打开文件描述符，随后释放锁再传输；即使缓存被回收，正在传输的文件仍可读。进程崩溃可能留下 `.part-*` 临时文件；单实例下次启动时只清理本缓存目录中的这些下载临时文件。最多 4 个并发下载会临时占用配额之外的磁盘空间。

官方的插件 `hash` 保持原样，通过新增 `artifact` 字段指定本镜像下载地址；`/plugins/versions` 的第三项同样指定镜像。客户端不需要修改 Decky 的 JavaScript 或安装自定义 Loader。本地安装器仅修改官方已支持的商店配置。

镜像范围不包含 ZIP 内的 `remote_binary` 依赖或插件运行时联网。若未来加入依赖镜像，需要单独设计并测试重打包/哈希变更或客户端代理方案；本版本不隐式改包。

## Cloudflare Worker

```text
Steam Deck / Web UI
        │ HTTPS
        ▼
Worker 自定义域名
 ├─ ASSETS：与 Docker 共用的 web/
 ├─ 生成资源：共用的 install.sh、client.py、三种 .desktop
 ├─ 商店 / Release JSON：有界解析、校验并改写下载地址
 └─ 下载流：跟随白名单重定向，字节直接传回客户端
        │ 每次请求
        ▼
官方商店 / 官方 CDN / Decky GitHub Release
```

Worker 无持久存储、不使用 D1/R2/SQLite，也不克隆二进制响应写入边缘缓存。`/api/status` 以 `mode=proxy`、`cache=null` 告知共用前端显示转发说明，不能将空计数伪装为缓存统计。Docker API 保持原有兼容性。

`worker/build.py` 将现有客户端和 Python 生成的桌面启动器打包为静态模块，避免复制后独立演进。启动器使用固定占位 origin，运行时仅替换为严格验证后的 `PUBLIC_BASE_URL`，不采用请求 Host。下载原始字节保留官方哈希；流式 Worker 不在服务器完成整包 SHA 校验，最终校验发生在安装器和 Decky 客户端。

两种部署的接口地址及恢复操作相同，差别在存储和服务端预校验行为；平台无关的安装器、权限、备份和回滚逻辑不变。
