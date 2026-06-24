# Cloudflare Worker 部署指南

本 Worker 用于为 `astrbot_plugin_pulse` 聚合、清洗并缓存 AI 行业资讯。

当前 Worker 的工作方式：

- Cloudflare Cron Trigger 定时抓取上游数据源。
- 抓取结果写入 Cloudflare KV。
- AstrBot 请求 Worker 时只读取最近一次缓存结果，不再同步抓取上游。
- `/admin` 提供数据源管理页面，可增删改 RSS、arXiv、内置数据源配置。
- 管理页使用浏览器 Basic Auth 登录；AstrBot 调用接口仍使用 Bearer Token。

## 本地开发

先从示例文件创建本地 Wrangler 配置：

```powershell
cd worker
copy wrangler.toml.example wrangler.toml
```

按需编辑 `wrangler.toml` 中的 Worker 名称、KV 绑定和 Cron 配置。
真实 `wrangler.toml` 可能包含部署相关标识，已被 git 忽略；开源发布时提交
`wrangler.toml.example` 即可。

创建本地密钥文件：

```powershell
copy .dev.vars.example .dev.vars
```

编辑 `.dev.vars`：

```text
SECRET_TOKEN=your-long-random-token
```

启动本地 Worker：

```powershell
npx wrangler dev
```

测试缓存读取接口：

```powershell
curl.exe -H "Authorization: Bearer your-long-random-token" http://localhost:8787
```

打开管理页：

```text
http://localhost:8787/admin
```

浏览器会弹出 Basic Auth 登录框：

- 用户名：任意填写
- 密码：填写 `.dev.vars` 中的 `SECRET_TOKEN`

登录成功后，页面会自动加载当前数据源配置。

## 生产部署

如果是 fresh clone，先创建本地 Wrangler 配置：

```powershell
cd worker
copy wrangler.toml.example wrangler.toml
```

创建生产和预览 KV namespace：

```powershell
npx wrangler kv namespace create PULSE_KV
npx wrangler kv namespace create PULSE_KV --preview
```

将命令返回的 `id` 和 `preview_id` 写入本地 `wrangler.toml`：

```toml
[[kv_namespaces]]
binding = "PULSE_KV"
id = "你的生产 KV namespace id"
preview_id = "你的预览 KV namespace id"
```

不要提交真实 `wrangler.toml`。其中可能包含 Worker 名称、路由、KV namespace id
等部署环境信息。

设置生产环境密钥：

```powershell
npx wrangler secret put SECRET_TOKEN
```

部署 Worker：

```powershell
npx wrangler deploy
```

部署完成后，将 Worker 地址配置到 AstrBot 插件：

- `news_endpoint`：部署后的 Worker 地址，例如 `https://xxx.workers.dev`
- `news_bearer_token`：与 Worker `SECRET_TOKEN` 一致的 token

## 数据源配置

访问管理页：

```text
https://<your-worker>.workers.dev/admin
```

浏览器会弹出 Basic Auth 登录框：

- 用户名：任意填写
- 密码：填写生产环境 `SECRET_TOKEN`

管理页支持的数据源类型：

- `RSS`：填写完整 RSS / Atom URL。
- `RSS 备用源`：同一个来源的多个备用 URL，用英文逗号分隔；Worker 会按顺序尝试。
- `arXiv 分类`：只填写分类号，例如 `cs.CL`、`cs.LG`、`cs.AI`，不要填写完整 URL。
- `Hugging Face Daily Papers`：内置源，无需填写 URL。

保存配置后，下一次 Cron 会使用新的数据源。也可以点击管理页中的“立即抓取”
手动刷新缓存。

## 定时抓取

默认 Cron 配置在 `wrangler.toml.example` 中：

```toml
[triggers]
crons = ["*/30 * * * *"]
```

这表示每 30 分钟抓取一次。你可以在本地 `wrangler.toml` 中调整该表达式。

## 接口说明

- `GET /`：返回最近一次缓存的资讯数组。需要 `Authorization: Bearer <SECRET_TOKEN>`。
- `GET /admin`：管理页。使用 Basic Auth。
- `GET /api/config`：读取数据源配置。需要 Bearer Token 或 Basic Auth。
- `PUT /api/config`：保存数据源配置。需要 Bearer Token 或 Basic Auth。
- `POST /api/refresh`：立即抓取并刷新缓存。需要 Bearer Token 或 Basic Auth。

## 注意事项

- `wrangler.toml.example` 是可提交的模板；本地部署请复制为 `wrangler.toml`。
- `wrangler.toml`、`.dev.vars`、`.wrangler/`、`node_modules/`、`dist/` 已被 git 忽略。
- `SECRET_TOKEN` 不要写入 `wrangler.toml`，请使用 `.dev.vars` 或 `wrangler secret put`。
- 如果未配置 `SECRET_TOKEN`，Worker 会返回 `{"error":"Worker secret is not configured"}`。
- 如果请求未携带正确认证信息，Worker 会返回 `{"error":"Unauthorized"}` 或 Basic Auth 登录框。
- `compatibility_date` 固定为 `2026-05-03`，用于匹配当前 Wrangler 本地运行环境。
