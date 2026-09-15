# Cloudflare Workers 部署记录

日期：2026-09-02

## 已完成

- GitHub 私有仓库：`https://github.com/Tsin418/trade-calendar`；
- Cloudflare Worker：`trade-calendar`；
- 正式地址：`https://trade-calendar.chenandrew418.workers.dev`；
- Git 集成：监听 `main`，新提交自动构建部署；
- 构建命令：`cd apps/web && npm ci && npm run cf:build`；OpenNext 配置使用非自动探测文件 `open-next.cloudflare.config.ts`；
- 部署命令：`cd apps/web && npx wrangler deploy`；
- 运行时：OpenNext Cloudflare Adapter 1.20.5、Next.js 16；
- Access：Production 与 Preview 全流量保护；
- Access 策略：仅当前 Cloudflare 账户成员允许，Session 24 小时；
- Observability：Worker Logs 已启用。

`CALENDAR_API` 的 Wrangler 配置显式使用 `"remote": false`，实际发布时仍由 `service_id` 绑定到生产 Worker。OpenNext 1.20.5 的部署封装会在发布前调用 Wrangler 平台代理；VPC binding 会令该代理尝试连接受 Access 保护的远程 Worker，非交互 CI 因缺少 Access Service Token 而失败。本项目未使用 OpenNext R2 缓存填充或 skew mapping，因此构建完成后由原始 `wrangler deploy` 直接发布 `.open-next/worker.js`，避免不必要的平台代理连接。

## 当前架构边界

Cloudflare Worker 只部署 Next.js Web。FastAPI、PostgreSQL 和 APScheduler Worker 仍在本机 Docker Compose 中运行；Worker 通过 `CALENDAR_API` VPC Service binding 访问 Tunnel 后方的 FastAPI。

所有页面都以真实 API 为唯一事实来源。VPC Service、Tunnel 或 FastAPI 不可用时，页面会明确显示连接失败，不再回退到静态演示事件，也不会宣称来源健康。`INTERNAL_API_URL` 仅作为没有 VPC binding 时的受保护 HTTPS 回退；配置成 Web Worker 自身地址会被拒绝，避免代理递归。不得把本机 PostgreSQL 端口直接暴露到公网。

生产可用性仍依赖本机 Tunnel、API、Worker 和数据库持续在线；长期方案是把后端迁移到高可用服务器。日常本地开发使用 `http://localhost:3000`，Next.js 开发环境会跳过远程 VPC binding 并代理到本地 `INTERNAL_API_URL`。

## 重启后的连接恢复（2026-09-07）

本地 Docker Tunnel 必须使用 Compose 中的 `network_mode: "service:api"`，与 API
共享网络空间。对应 VPC Service `trade-calendar-fastapi` 的 Host/IP 必须为
`127.0.0.1`，HTTP port 为 `8000`，Tunnel 为 `trade-calendar-api`。这两个配置必须
同时生效；独立网络空间里的 Tunnel 无法通过自己的回环地址访问 API。

不再使用 VPC 主机名 `api` 或 Docker 分配的 IP。此次故障中 Docker Desktop
重启后 API 地址已变为 `172.19.0.3`，但 VPC 请求仍到达旧地址 `172.26.0.2`，
日志为 `unable to dial tcp to origin ... i/o timeout`。固定回环地址无需 DNS
解析，因此容器地址变化不再影响云端路由。Worker binding 和前端 URL 保持原值。

更新或重建后端时，始终把 Tunnel 一起交给 Compose 管理：

```powershell
docker compose -f infrastructure/docker-compose.yml --env-file .env --profile tunnel up -d api worker tunnel
```

Tunnel 依赖 API 健康检查，并设置 `depends_on.api.restart: true`；不要单独删除 API
容器或通过 `--no-deps` 重建 API，否则依附旧容器网络空间的 Tunnel 也需要重建。
普通 API 重启可用：

```powershell
docker compose -f infrastructure/docker-compose.yml --env-file .env --profile tunnel restart api
```

验证时同时检查 `/health`、`/ready` 和线上 `/api/v1/events?limit=1`，仅看到 Tunnel
Healthy 不足以证明它能访问 API。前端读取设置、总览、日/周/月历、变更和来源失败后
每 15 秒自动重试，并在网络恢复或窗口获得焦点时重试；单次读取超时为 20 秒。
成功后停止重试，写入和同步操作不会被自动重放。

参考：[Docker Compose 网络空间与依赖](https://docs.docker.com/reference/compose-file/services/)、
[Cloudflare VPC Service 路由配置](https://developers.cloudflare.com/workers-vpc/configuration/vpc-services/)。

## 迁移到 prom418/calendar（2026-09-15）

上表的仓库与 Worker 属于另一个 Cloudflare 账户。工作账户
`andrew.chen@prominenceim.com`（account id `35efe967224b7ce2325d8846583ef3d3`）
此前没有 workers.dev 子域，因此 `wrangler deploy` 在注册子域之前必然失败。现在：

- 代码已推送到 `https://github.com/prom418/calendar`（`main`，remote 名 `prom418`）；
- 已通过 `PUT /accounts/<id>/workers/subdomain` 注册子域 `prom418`，
  将来的正式地址是 `https://trade-calendar.prom418.workers.dev`。

### 该账户的两个硬约束

1. **邮箱未验证。** `wrangler deploy` 在脚本上传阶段返回
   `code: 10034 - You need to verify your email address to use Workers`。
   必须先在 `andrew.chen@prominenceim.com` 的收件箱点击 Cloudflare 的验证链接，
   任何部署（本地 wrangler 或 Git 集成）在此之前都不可能成功。
2. **没有 VPC Service。** `/accounts/<id>/vpc_services` 返回
   `No route for that URI`，说明该账户未开通 Workers VPC。原先 `wrangler.jsonc`
   里的 `CALENDAR_API` binding 同时指向别的账户的 `service_id`，会让
   `wrangler deploy` 直接报错；该段已注释掉。

因此云端暂时连不到本机后端。恢复方式二选一：在本账户创建 VPC Service 与 Tunnel
后还原 binding，或把本机后端经公网 Tunnel 暴露后设置 `INTERNAL_API_URL`。
在两者都未配置时，`/api/*` 会返回 `503 api_unavailable`，页面本身仍可正常打开。

### 用 Git 集成创建 Worker

Cloudflare 没有「连接 GitHub 仓库」的 API（Workers Builds 的
`/accounts/<id>/builds/*` 只暴露 triggers、builds、环境变量和 deploy hooks，
GitHub App 授权只能走控制台）。所以这一步必须在浏览器完成：

1. Workers & Pages → Create → Workers → **Connect to Git**；
2. 授权 Cloudflare GitHub App，选择 `prom418/calendar`（可只授权该仓库）；
3. 构建配置：Root directory `apps/web`，Build command `npm ci && npm run cf:build`，
   Deploy command `npx wrangler deploy`；
4. Worker 名称保持 `trade-calendar`，监听分支 `main`。

### 用 wrangler 直接发布

`.open-next/` 已构建完成时可直接发布（无需重新构建）：

```powershell
cd apps/web
npx wrangler deploy
```

## API 共享密钥网关（2026-09-15）

**为什么需要**：`apps/api/trade_calendar/main.py` 里的 6 个 router 全部直接
`include_router`，**没有任何鉴权依赖**——API 一直只靠网络隔离。一旦经 Tunnel 暴露到
公网，任何人知道域名就能读写设置、触发同步、改事件。所以先把网关做出来，再谈暴露。

**做法**：新增 `Settings.internal_api_secret`（`CALENDAR_INTERNAL_API_SECRET` 或
`INTERNAL_API_SECRET`，两种写法等价）。只要它被配置，中间件
`require_internal_api_secret` 就要求每个请求带 `X-Internal-Api-Secret` 头，不匹配返回
401。未配置则网关关闭，这是本地开发与单测的默认状态。

故意保持开放的路径：

- `/health`、`/ready`：Tunnel 与本地工具探活需要；
- `/calendar/*`：ICS 订阅本身用不可猜的路径 token 保护，而且日历客户端**无法发送自定义
  头**，给这条路径加头会直接废掉订阅。

**谁来带头**：`apps/web/src/app/api/[...path]/route.ts` 在代理时附加
`X-Internal-Api-Secret`（取自 `process.env.INTERNAL_API_SECRET`），位置与已有的
`CF-Access-Client-Id/Secret` 一致。浏览器永远拿不到这个值——它不在
`FORWARDED_REQUEST_HEADERS` 里，客户端伪造的同名头会被丢弃。

**密钥来源**：仓库根 `.env` 的 `INTERNAL_API_SECRET` 是唯一事实来源。
`.local/dev-native.ps1` 会把它取出来注入 Next.js 开发进程，因此本地开发照常可用。
Cloudflare 侧必须配置同名 secret：

```powershell
cd apps/web
npx wrangler secret put INTERNAL_API_SECRET   # 粘贴 .env 里的值
```

**实测结果**（quick tunnel，2026-09-15）：

| 请求 | 结果 |
| --- | --- |
| `GET /health`（无密钥） | 200 |
| `GET /api/v1/sources`（无密钥） | 401 |
| `GET /api/v1/sources`（错误密钥） | 401 |
| `GET /api/v1/sources`（正确密钥） | 200 |
| `GET /api/v1/events`（正确密钥） | 200，`total` 549 |

用例在 `apps/api/tests/test_internal_api_secret.py`。`tests/conftest.py` 有一个
autouse fixture 在单测中关闭网关，这样即使 `.env` 里存在密钥，既有用例仍确定性通过。

## 无域名账户的连通方案：quick tunnel

该账户没有 zone，也没有 Workers VPC，所以 named tunnel + VPC Service 那条路走不通。
改用 quick tunnel（`trycloudflare.com`），不需要登录、域名或 token：

```powershell
.\.local\tunnel-quick.ps1
```

脚本会拒绝在 `INTERNAL_API_SECRET` 未配置时启动（否则等于把可写 API 公开）、检查本地
API 是否存活、启动 cloudflared、抓出分配到的公网域名，并**自检**「无密钥 401 / 有密钥
200」，最后打印需要执行的 wrangler 命令。

quick tunnel 的域名是随机且每次重启都变的，所以 `INTERNAL_API_URL` 要跟着更新：

```powershell
cd apps/web
npx wrangler deploy --var INTERNAL_API_URL:https://<随机>.trycloudflare.com
```

走控制台 Git 集成时，把 `INTERNAL_API_URL` 配成构建变量、`INTERNAL_API_SECRET` 配成
secret。想要稳定域名，就得给账户挂一个域名（走 Access 保护的 named tunnel），或者升级
Workers Paid 改用 VPC Service。

## 首次上线后页面全空：`next-env.mjs` 把本机地址烤进了 Worker（2026-09-15）

**症状**：`https://trade-calendar.prom418.workers.dev` 能打开，但总览、日/周/月历、数据源
全部为空；页面顶部报「事件 API 返回 403」「来源 API 返回 403」。而同一时刻本机
`http://localhost:3000` 一切正常。

**根因**：部署到 Cloudflare 的 Worker 在向 `http://127.0.0.1:8000` 发请求。

链路是这样的：

1. `apps/web/.env.local`（原生开发用，git 忽略）写着
   `INTERNAL_API_URL=http://127.0.0.1:8000`；
2. `@opennextjs/cloudflare` 构建时会把项目的 `.env*` 编译成
   `.open-next/cloudflare/next-env.mjs`，Worker 运行时用它填充 `process.env`
   （见 `dist/cli/utils/extract-project-env-vars.js`：读取 `.env`、`.env.{mode}`、
   `.env.local`、`.env.{mode}.local`）；
3. 于是**生产 Worker 也拿到了本机回环地址**，`resolveApiOrigin()` 认为它合法，
   代理就去 fetch 自己的 loopback；
4. Cloudflare 边缘拒绝连回环地址，返回 `403` + 纯文本 `error code: 1003`。
   因为 `content-type` 是 `text/plain`，`isAccessRejection()` 不认它，
   原样透传给浏览器 —— 前端只能显示「事件 API 返回 403」，看起来像应用 bug。

注意 Worker 上**根本没有** `INTERNAL_API_URL` 绑定：`wrangler.jsonc` 里没有，也没用
`--var` 传过。所以这不是「忘了配」，而是构建产物里自带了一个错的。

**修法（两层）**：

1. `src/app/api/[...path]/route.ts` 新增 `isLoopbackHost()`，生产环境拒绝回环来源
   （`localhost`、`127.0.0.0/8`、`0.0.0.0`、`::1`）。Docker Compose 用的
   `http://api:8000` 是单标签主机名，不受影响；开发环境照旧允许回环。
   这样即使本机地址又被烤进去，也只会得到诚实的 `503 api_unavailable`，
   而不是误导性的 403。用例在 `route.test.ts`。
2. `.local/build-cloudflare.ps1` 在构建前把 `.env.local` / `.env.production.local`
   挪开、构建后还原，并在部署前检查 `next-env.mjs` 里是否还有回环值，
   有就直接失败、拒绝部署。

**接线（让线上真的显示本机数据）**：该账户没有 VPC Service，只能走公网
quick tunnel。`.local/arm-cloudflare.ps1` 一条命令做完：校验密钥 → 探活本机 API →
起 cloudflared 并抓域名 → 自检「匿名 401 / 带密钥 200」→ 写入 `tunnel-url.txt` →
调用 `build-cloudflare.ps1 -ApiUrl <url>` 构建并部署。

**前提是 API 的共享密钥网关必须真的开着**。本机 API 之前在跑的是加网关之前的代码，
匿名请求也能 200；直接挂公网等于把可写 API 公开。重启 API（`dev-native.ps1` 或手动
uvicorn）后它才从根 `.env` 读到 `INTERNAL_API_SECRET` 并启用网关，实测：

| 请求 | 结果 |
| --- | --- |
| 公网隧道 `GET /health`（无密钥） | 200 |
| 公网隧道 `GET /api/v1/events`（无密钥） | 401 |
| 公网隧道 `GET /api/v1/sources`（无密钥） | 401 |
| 公网隧道 `GET /openapi.json`（无密钥） | 401 |
| 公网隧道 `GET /api/v1/events`（正确密钥） | 200 |

**部署时必须带 `--var`**：quick tunnel 域名每次重启都变，所以
`INTERNAL_API_URL` 不能写进 `wrangler.jsonc`，而是由 `.local/tunnel-url.txt` 记录、
部署时用 `--var INTERNAL_API_URL:<url>` 传入。**裸跑 `npx wrangler deploy` 会把这个
变量丢掉、站点重新变空** —— 始终通过 `build-cloudflare.ps1` 或 `arm-cloudflare.ps1` 部署。

**遗留脆弱点**：本机必须开着，cloudflared 必须常驻；隧道一停，线上立刻回到 503。
稳定方案仍是给账户挂域名走 named tunnel，或升级 Workers Paid 用 VPC Service。

## 安全注意

- GitHub 仓库保持 Private；
- `.env`、数据库、备份、Token、Webhook 和本地缓存均被 Git 忽略；
- Cloudflare Access 不应关闭；
- Cloudflare 构建变量不得写入仓库；
- 飞书 Webhook 当前未配置。
