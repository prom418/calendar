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

## 安全注意

- GitHub 仓库保持 Private；
- `.env`、数据库、备份、Token、Webhook 和本地缓存均被 Git 忽略；
- Cloudflare Access 不应关闭；
- Cloudflare 构建变量不得写入仓库；
- 飞书 Webhook 当前未配置。
