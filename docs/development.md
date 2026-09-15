# 开发环境与验证

## 前置条件

- Docker Desktop（Compose v2）—— 仅「一条命令启动」需要；原生运行见下文；
- Node.js 24（仅前端本地开发需要）；
- Python 3.12（仅 API 本地开发需要）。

复制 `.env.example` 为 `.env` 并至少替换数据库密码、Session Secret 和 ICS Token。开发默认值只允许用于本机。

## 一条命令启动

在仓库根目录运行：

```powershell
.\scripts\dev.ps1
```

服务启动后：Web `http://localhost:3000`，API 文档 `http://localhost:8000/docs`，API 健康检查 `http://localhost:8000/health`。PostgreSQL 仅绑定本机回环地址。

## 本机原生运行（不使用 Docker）

Docker Desktop 不是必须的。PostgreSQL、FastAPI、APScheduler Worker 和 Next.js 四个进程可以原生跑起来。

**关键一步：Web 的环境变量要单独配。** Next.js 只读 `apps/web` 目录下的 `.env*`，**不会**读仓库根目录的 `.env`。所以除了根 `.env`，还需要 `apps/web/.env.local`：

```dotenv
INTERNAL_API_URL=http://127.0.0.1:8000
# 必须与根 .env 里的 INTERNAL_API_SECRET 完全一致
INTERNAL_API_SECRET=<粘贴根 .env 中的值>
```

漏掉 `INTERNAL_API_SECRET` 时的症状很有迷惑性：页面能正常打开，但所有 `/api/v1/*`
返回 401，而 API 容器/进程侧看不出任何异常 —— 因为该密钥是 Next 代理在服务端附加的，
浏览器永远拿不到它，也不接受客户端伪造的同名头。`.env.local` 已在 `.gitignore` 中。

## ICS 订阅地址与 `/calendar/*` 的路由

订阅地址由 `CALENDAR_PUBLIC_BASE_URL` + `/calendar/<token>.ics` 拼成（token 取根
`.env` 的 `CALENDAR_ICS_TOKEN`）。这条路径由 **API** 提供，而且**故意不在共享密钥网关
管辖范围内**：日历客户端无法发送自定义头，给它加头会直接废掉订阅，保护它的是不可猜的
路径 token。

因此 `/calendar/*` 必须被转发到 API，Web 源和反向代理都要做：

- 原生 / Worker 部署：`apps/web/src/app/calendar/[...path]/route.ts`；
- Docker Compose + Caddy：`infrastructure/Caddyfile` 的 `handle /calendar/*`。

注意 `CALENDAR_PUBLIC_BASE_URL` 同时用于生成每个 VEVENT 的 `URL:` 字段，而那是 Web
路由（`/events/<id>`）。所以不要把该值指向 API —— 那会修好订阅地址却打断 feed 内的
事件链接；正确做法是让 Web 源同源代理 `/calendar/*`。

## Migration

Compose 启动时由一次性 `migrate` 服务执行 `alembic upgrade head`。本地 API 环境可使用：

```powershell
cd apps/api
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\alembic.exe downgrade -1
```

发布顺序固定为：备份数据库 → 构建镜像 → 执行 Migration → 启动 API/Worker/Web → 健康检查。Migration 失败时不得启动新版本应用。

## 自动化检查

```powershell
.\scripts\check.ps1
```

它依次执行 Python Lint 与测试、前端 Lint、TypeScript、组件测试和生产构建。

## 时间与数据规则

- 所有带时间事件写入 API 时必须包含 UTC offset；数据库存 UTC。
- 仅日期事件写 `local_date`，可选写入成对的 `date_range_start/date_range_end`，不得填写 `starts_at`。
- `date_precision=window` 必须同时有 `starts_at` 与 `ends_at`。
- 人工创建通过内建 `manual` 来源建立来源关系；正式事件不能成为无来源记录。
- 重复写入使用 `idempotency_key`，同一内容不会产生重复版本。

## 停机与休眠后的采集恢复

Worker 启动时及运行期间每分钟检查一次数据库中的最近采集记录。若该次运行结束后
已错过来源的 UTC cron 计划，自动排入一次 `catchup` 任务；从未运行的来源排入
`initial` 任务。多天漏跑合并为一次最新抓取，不逐次重放历史计划。

补抓和正常定时任务共用来源行锁及每分钟唯一键。已有 `pending` 或 `running` 任务时
不重复排队；一次失败结束后等到下一次正常计划再重试，避免每分钟重复请求故障来源。
API 的 `/health`、`/ready` 只说明 API 和数据库在线，来源新鲜度还需检查
`/api/v1/sources` 的最近运行、成功时间和健康状态。

台湾央行来源从新闻索引第一页顺序查找当年会议日程，最多检查 20 页，同时保留已发现的
下一年日程；不再依赖公告所在的固定页码。找不到当年日程时仍报告解析故障。
