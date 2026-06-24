# astrbot_plugin_pulse

Pulse 是一个 AstrBot 每日推送插件，用于定时推送 Epic 免费游戏和 AI 行业简报。

## 当前功能

- 获取 Epic Games 当前可领取的免费游戏。
- 通过 Cloudflare Worker 获取 AI 行业资讯。
- Worker 使用 Cron 定时抓取数据源，并将结果缓存到 Cloudflare KV。
- AstrBot 请求 Worker 时读取最近一次缓存结果，不再等待上游数据源实时抓取。
- Worker 提供 `/admin` 管理页，可配置 RSS、arXiv、Hugging Face Daily Papers 等数据源。
- Epic 和 AI 简报支持独立启用、独立推送时间、独立目标会话。
- 两类推送均使用 AstrBot HTML 渲染为图片后发送。
- 多目标会话之间加入随机延迟，降低瞬时批量发送风险。
- AI 简报采用单次 LLM 合成，同时生成 QQ 推送短简报和本地网站发布用 Markdown 长文。

## 目录结构

```text
astrbot_plugin_pulse/
  main.py
  metadata.yaml
  _conf_schema.json
  requirements.txt
  services/
    epic.py
    news.py
  worker/
    src/index.ts
    wrangler.toml.example
    DEPLOY_GUIDE.md
```

说明：

- `worker/wrangler.toml.example` 是可提交的 Worker 配置模板。
- `worker/wrangler.toml` 是本地真实部署配置，已被 `.gitignore` 忽略，不建议提交到 GitHub。
- Worker 详细部署流程见 [worker/DEPLOY_GUIDE.md](worker/DEPLOY_GUIDE.md)。

## 工作流程

AI 简报链路：

```text
Cloudflare Cron
  -> Worker 定时抓取已配置数据源
  -> 清洗、去重、排序
  -> 写入 Cloudflare KV
  -> AstrBot 定时请求 Worker 缓存结果
  -> LLM 生成 QQ 简报和 Markdown 长文
  -> HTML 渲染为图片并推送
```

这样可以避免 AstrBot 推送时临时等待多个上游站点响应，也能在 Worker 管理页中单独维护数据源。

## 安装与配置

1. 将插件放入 AstrBot 插件目录。
2. 安装 Python 依赖：

```powershell
pip install -r requirements.txt
```

3. 部署 `worker/` 下的 Cloudflare Worker，参考 [Worker 部署指南](worker/DEPLOY_GUIDE.md)。
4. 在 AstrBot 插件配置中填写 Worker 地址和 token。
5. 在目标会话中使用绑定指令，将当前会话加入定时推送目标。

## API 中转站推荐

如果需要为 AstrBot 配置统一的 AI 模型接口，可以使用
[Glosc AI One](https://one.gloscai.com/)（https://one.gloscai.com/ ）：

一个接口，接入所有 AI 模型。通过统一、标准的接口协议接入海量模型，
承载 AI 应用，高效管理数字资产，连接未来。

## 指令

- `/pulse epic`：立即发送 Epic 免费游戏。
- `/pulse news`：立即发送 AI 行业简报。
- `/pulse now`：立即发送两个模块。
- `/pulse bind_epic`：将当前会话绑定到 Epic 定时推送。
- `/pulse bind_news`：将当前会话绑定到 AI 简报定时推送。
- `/pulse bind`：将当前会话同时绑定到两个定时推送。
- `/pulse unbind_epic`：取消当前会话的 Epic 推送。
- `/pulse unbind_news`：取消当前会话的 AI 简报推送。
- `/pulse unbind`：取消当前会话的全部 Pulse 推送。
- `/pulse targets`：查看当前推送目标。

## 主要配置

- `enabled`：启用 Pulse 插件。
- `timezone`：调度时区，默认 `Asia/Shanghai`。
- `epic_enabled`：启用 Epic 免费游戏推送。
- `epic_daily_time`：Epic 每日推送时间，默认 `08:20`。
- `epic_target_sessions`：Epic 推送目标会话。
- `send_epic_images`：Epic 图片卡片中展示游戏封面。
- `epic_max_items`：Epic 单次最多展示数量。
- `news_enabled`：启用 AI 行业简报推送。
- `news_daily_time`：AI 简报每日推送时间，默认 `08:35`。
- `news_target_sessions`：AI 简报推送目标会话。
- `news_endpoint`：已部署的 Cloudflare Worker 地址。
- `news_bearer_token`：与 Worker `SECRET_TOKEN` 一致的 Token。
- `news_max_items`：AI 简报最多处理资讯数，建议不超过 `15`。
- `push_delay_min_seconds`：向下一个目标会话发送前的最小随机延迟。
- `push_delay_max_seconds`：向下一个目标会话发送前的最大随机延迟。

## Worker 管理页

部署 Worker 后访问：

```text
https://<your-worker>.workers.dev/admin
```

管理页使用浏览器 Basic Auth：

- 用户名：任意填写
- 密码：填写 Worker 的 `SECRET_TOKEN`

登录后页面会自动加载数据源配置。支持的数据源类型：

- `RSS`：填写完整 RSS / Atom URL。
- `RSS 备用源`：多个备用 URL 用英文逗号分隔。
- `arXiv 分类`：只填写分类号，例如 `cs.CL`、`cs.LG`、`cs.AI`。
- `Hugging Face Daily Papers`：内置源，无需填写 URL。

保存配置后，下一次 Worker Cron 会使用新数据源；也可以点击“立即抓取”马上刷新缓存。

## AI 简报产物

AI 简报会保存一份完整 Markdown 到 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_pulse/ai-news-YYYY-MM-DD.md
```

该文件用于后续发布到网站；群聊推送只发送适合快速阅读的一句话摘要图片。

## 发布注意事项

不要提交以下本地或私有文件：

- `worker/wrangler.toml`
- `worker/.dev.vars`
- `worker/.wrangler/`
- `worker/node_modules/`
- `worker/dist/`

应提交：

- `worker/wrangler.toml.example`
- `worker/.dev.vars.example`
- `worker/src/index.ts`
- `worker/DEPLOY_GUIDE.md`

`SECRET_TOKEN` 必须通过 `.dev.vars` 或 `wrangler secret put SECRET_TOKEN` 配置，不要写入仓库文件。
