<p align="center">
  <img src="./logo.png" width="128" height="128" alt="Pulse logo" />
</p>

# Pulse 每日推送

Pulse 是一个面向 AstrBot 的每日推送插件，用于定时推送 Epic 免费游戏、AI 行业简报、Arena 代码模型排行榜和 GitHub 订阅用户提交排行。它会把推送内容渲染成图片发送到指定会话，并可将 AI 长文同步发布到 Halo SyncPostAI。

## 功能概览

- Epic 免费游戏监控：抓取当前可领取的 Epic Games 免费促销，展示游戏封面和领取截止时间。
- AI 行业简报：从 Cloudflare Worker 聚合服务读取已清洗的 AI 资讯 JSON。
- Arena 排行榜：直接抓取 arena.ai 代码竞技场排行榜（Next.js RSC 数据），渲染模型排名卡片图片。
- GitHub 提交排行：通过 GitHub 提交搜索按天统计订阅用户的公开提交数与涉及仓库，横向条形图排行，支持多用户自动分图与周五周报。
- Worker 缓存链路：Worker 使用 Cron 定时抓取 RSS、arXiv、Hugging Face Daily Papers 等来源，并缓存到 Cloudflare KV。
- 单次 LLM 合成：一次 LLM 调用同时生成 QQ 图片卡片、文本回退内容和网站发布用 Markdown 长文。
- 专用 LLM Provider：可为 AI 简报单独多选 Provider；失败时自动切换，单 Provider 失败后等待 10 秒重试一次。
- 图片推送：Epic 和 AI 简报均使用 AstrBot HTML 渲染能力转为图片发送。
- 多群延迟：多个目标会话之间自动加入随机延迟，降低瞬时批量发送风险。
- Halo 发布：可将 AI Markdown 长文自动推送到 Halo SyncPostAI。

## 目录结构

```text
astrbot_plugin_pulse/
  main.py
  metadata.yaml
  _conf_schema.json
  requirements.txt
  logo.png
  assets/
    icon.svg
  imgs/
    写作.png
    开心.png
    玩游戏.png
    看这里.png
    趴着.png
  services/
    epic.py
    news.py
    arena.py
    github.py
  worker/
    src/index.ts
    wrangler.toml.example
    DEPLOY_GUIDE.md
```

说明：

- `logo.png` 是 AstrBot 插件 Logo，推荐尺寸 256x256。
- `assets/icon.svg` 是可编辑的图标源文件。
- `imgs/` 保存推送卡片使用的本地图片资源，插件会转为 base64 data URI 内嵌到 HTML。
- `worker/DEPLOY_GUIDE.md` 包含 Cloudflare Worker 的部署说明。
- `worker/wrangler.toml`、`worker/.dev.vars`、`.wrangler/` 等本地部署文件不应提交。

## 安装

1. 将插件目录放入 AstrBot 插件目录。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 部署 `worker/` 下的 Cloudflare Worker，并按 [Worker 部署指南](worker/DEPLOY_GUIDE.md) 配置 `SECRET_TOKEN`。
4. 在 AstrBot WebUI 中配置 `news_endpoint` 和 `news_bearer_token`。
5. 在目标会话中使用 `/pulse bind_epic`、`/pulse bind_news` 或 `/pulse bind` 绑定推送目标。

## 指令

- `/pulse epic`：立即发送 Epic 免费游戏。
- `/pulse news`：立即发送 AI 行业简报。
- `/pulse now`：立即发送 Epic 和 AI 简报。
- `/pulse leaderboard [数量]`：立即抓取 Arena 代码模型排行榜并渲染成图片，例如 `/pulse leaderboard 15`。
- `/pulse arena`：`/pulse leaderboard` 的别名。
- `/pulse github`：立即发送订阅用户的 GitHub 今日提交排行。
- `/pulse github week`：立即发送近 7 天 GitHub 推送周报（`/pulse gh` 为别名）。
- `/pulse publish_news`：将最近生成的 AI Markdown 长文手动发布到 Halo。
- `/pulse bind_epic`：将当前会话绑定到 Epic 定时推送。
- `/pulse bind_news`：将当前会话绑定到 AI 简报定时推送。
- `/pulse bind_arena`：将当前会话绑定到 Arena 排行榜定时推送。
- `/pulse bind_github`：将当前会话绑定到 GitHub 提交排行（日推与周报共用）。
- `/pulse bind`：将当前会话同时绑定到全部定时推送。
- `/pulse unbind_epic`：取消当前会话的 Epic 推送。
- `/pulse unbind_news`：取消当前会话的 AI 简报推送。
- `/pulse unbind_arena`：取消当前会话的 Arena 排行榜推送。
- `/pulse unbind_github`：取消当前会话的 GitHub 提交排行。
- `/pulse unbind`：取消当前会话的全部 Pulse 推送。
- `/pulse targets`：查看当前推送目标。
- `/pulse providers`：查看 AstrBot 已配置的 LLM Provider，已选中的 Provider 会用 `*` 标记。

## 核心配置

- `enabled`：启用 Pulse 插件。
- `timezone`：调度时区，默认 `Asia/Shanghai`。
- `push_delay_min_seconds` / `push_delay_max_seconds`：多目标推送之间的随机延迟范围。
- `epic_enabled`：启用 Epic 免费游戏推送。
- `epic_daily_time`：Epic 每日推送时间，默认 `08:20`。
- `epic_target_sessions`：Epic 推送目标会话。
- `send_epic_images`：是否展示 Epic 游戏封面。
- `epic_max_items`：Epic 单次最多展示数量。
- `news_enabled`：启用 AI 行业简报推送。
- `news_daily_time`：AI 简报每日推送时间，默认 `08:35`。
- `news_target_sessions`：AI 简报推送目标会话。
- `news_endpoint`：Cloudflare Worker 聚合接口地址。
- `news_bearer_token`：与 Worker `SECRET_TOKEN` 一致的 Bearer Token。
- `news_llm_provider_ids`：AI 简报专用 LLM Provider，可多选。[没有 key？现在获取](https://one.gloscai.com/keys)
- `news_max_items`：AI 简报最多处理资讯数，建议不超过 `15`。

`news_llm_provider_ids` 留空时使用当前会话默认 Provider；选择多个 Provider 时按顺序尝试，失败自动切换到下一个；只选择一个 Provider 时失败后等待 10 秒重试一次。

## Arena 排行榜配置

- `arena_enabled`：启用 Arena 排行榜定时推送。
- `arena_daily_time`：Arena 排行榜每日推送时间，默认 `09:00`。
- `arena_target_sessions`：Arena 排行榜推送目标会话。
- `arena_leaderboard_url`：要抓取的排行榜页面 URL，默认 `https://arena.ai/leaderboard/code`（Code Arena WebDev 整体排行）。也可填写分类子榜，例如：
  - `https://arena.ai/leaderboard/code/webdev-fullstack`
  - `https://arena.ai/leaderboard/code/webdev-frontend`
  - `https://arena.ai/leaderboard/code/webdev-html`
  - `https://arena.ai/leaderboard/code/webdev-react`
- `arena_max_models`：图片卡片最多展示的模型行数，默认 `10`。

排行榜数据直接抓取自 arena.ai 官方页面：插件以 Next.js RSC（`RSC: 1`）请求头获取页面，并从返回的 flight payload 中解析 `entries` 数据（排名、分数、置信区间、票数、价格、上下文长度、许可证等），无需第三方接口。

## GitHub 提交排行配置

- `github_enabled`：启用 GitHub 提交排行定时推送。
- `github_daily_time`：GitHub 每日推送时间，默认 `22:00`，统计当天 00:00 至推送时刻的公开提交。
- `github_weekly_enabled`：启用周五周报，默认 `true`。
- `github_weekly_time`：周报推送时间（周五），默认 `22:30`。
- `github_target_sessions`：GitHub 提交排行推送目标会话（日推与周报共用）。
- `github_usernames`：订阅的 GitHub 用户名，逗号分隔多个，例如 `torvalds, soulter`。
- `github_token`：可选 GitHub Personal Access Token。留空使用无鉴权提交搜索（10 次/分钟）；订阅用户较多时可填入以提升至 30 次/分钟。仅保存在 AstrBot 本地配置。
- `github_max_users_per_image`：每张图片最多展示用户数，默认 `8`，超出自动拆分多张图片依次推送。
- `github_max_repos_per_user`：每个用户最多展示仓库数，默认 `3`，超出以 `+N` 折叠。

行为说明：

- 数据来自 GitHub 提交搜索接口（`author:` + `author-date:` 按插件时区当天窗口精确统计），统计的是**提交（commit）数**而非推送次数，仅覆盖公开仓库。
- 按提交次数降序排行；当日 0 提交的用户不会出现在卡片中，全员 0 提交时当天不推送。
- 周报统计滚动近 7 天，与日推共用同一组目标会话。
- 首次安装当天触发周报时，缺失的历史日期会回退实时搜索补齐（受无 token 配额预算保护，预留 5 次请求给其他任务；抓取失败或配额不足的日期在周报迷你柱上显示「数据缺失」）。随着每日任务运行，快照逐渐累积，后续周报不再需要回补。
- 每日推送成功后会把当天统计保存为快照（`data/plugin_data/astrbot_plugin_pulse/github_stats/YYYY-MM-DD.json`），周报聚合快照；快照只保留最近 8 天，自动清理不堆积。
- 抓取结果带内存缓存（当天 15 分钟、过去日期 6 小时），避免频繁触发指令耗尽无 token 配额。
- 不存在的用户名会在 0 提交时通过用户接口校验并提示，避免被误判为 0 提交。

## Halo 发布配置

- `halo_publish_enabled`：启用 AI 长文自动发布到 Halo。
- `halo_site_url`：Halo 站点地址，例如 `https://blog.example.com`；也可以填写完整 SyncPostAI articles 接口地址。
- `halo_syncpost_token`：SyncPostAI 插件中配置的推送 Token。
- `halo_publish_direct`：是否直接发布文章，默认 `true`。
- `article_statement_enabled`：是否在文章底部添加 AI 生成声明。
- `halo_article_author`：写入 Markdown front matter 的 `author` 字段。
- `halo_article_cover`：写入 Markdown front matter 的 `cover` 字段，支持网络地址或本地路径。
- `halo_excerpt_min_chars` / `halo_excerpt_max_chars`：AI 生成摘要的字数范围。
- `halo_slug_prefix`：文章 slug 前缀，默认生成 `ai-news-YYYYMMDD`。
- `halo_publish_tags`：发布到 Halo 时附加的默认标签；留空时由 AI 生成。
- `halo_publish_categories`：发布到 Halo 时附加的默认分类。

Pulse 会调用：

```http
POST /apis/api.syncpostai.sora.run/v1alpha1/articles
Content-Type: application/json; charset=utf-8
X-SyncPost-Token: <halo_syncpost_token>
```

请求体格式：

```json
{
  "source": "astrbot-pulse",
  "content": "---\ntitle: 示例\n---\n\n正文",
  "contentType": "markdown",
  "slug": "ai-news-20260626",
  "publish": true
}
```

标题、作者、封面、摘要、分类和标签由 SyncPostAI 从 Markdown front matter 解析。示例：

```yaml
---
title: AI Agent 正从演示走向企业基础设施
author: admin
cover: https://example.com/cover.png
excerpt: 今日 AI 资讯显示，企业级 Agent 的竞争正从模型能力转向记忆、权限、证据链和业务工作流。
categories:
 - AI 行业简报
tags:
 - AI
 - Agent
 - 产业观察
---
```

## 工作流

AI 简报链路：

```text
Cloudflare Cron
  -> Worker 定时抓取已配置数据源
  -> 清洗、去重、排序
  -> 写入 Cloudflare KV
  -> AstrBot 定时请求 Worker 缓存结果
  -> LLM 生成 QQ 图片卡片、文本回退和 Markdown 长文
  -> HTML 渲染为图片并推送
  -> 可选：发布 Markdown 长文到 Halo
```

Epic 免费游戏链路：

```text
AstrBot 定时任务
  -> 请求 Epic Games 免费游戏接口
  -> 解析当前仍在领取期的免费促销
  -> HTML 渲染游戏封面卡片
  -> 推送图片到已绑定会话
```

Arena 排行榜链路：

```text
AstrBot 定时任务或 /pulse leaderboard 指令
  -> 以 Next.js RSC 请求头抓取 arena.ai 排行榜页面
  -> 从 flight payload 解析 entries（排名/分数/票数/价格等）
  -> HTML 渲染排行榜卡片
  -> 推送图片到已绑定会话
```

GitHub 提交排行链路：

```text
AstrBot 定时任务或 /pulse github 指令
  -> 通过 GitHub 提交搜索（author: + author-date: 当天窗口）统计订阅用户提交数
  -> 按插件时区聚合当日提交数与涉及仓库
  -> 保存当日快照并清理 8 天前旧快照
  -> 按提交次数降序渲染 GitHub 风格横向条形图（多用户自动分图）
  -> 推送图片到已绑定会话
  -> 周五周报：聚合近 7 天快照（缺失日期回退实时搜索）
```

## Worker 管理页

部署 Worker 后访问：

```text
https://<your-worker>.workers.dev/admin
```

管理页使用浏览器 Basic Auth：

- 用户名：任意填写
- 密码：Worker 的 `SECRET_TOKEN`

支持的数据源类型：

- `RSS`：完整 RSS / Atom URL。
- `RSS 备用源`：多个备用 URL 使用英文逗号分隔。
- `arXiv 分类`：填写分类号，例如 `cs.CL`、`cs.LG`、`cs.AI`。
- `Hugging Face Daily Papers`：内置源，无需填写 URL。

## 文本转图片服务

插件依赖 AstrBot 的 HTML 渲染能力。如果默认远程 t2i 服务不稳定，建议自部署：

```bash
docker run -itd -p 8999:8999 soulter/astrbot-t2i-service:latest
```

如果 AstrBot 使用 Docker Compose 运行，可以把 t2i 服务加入同一网络，并在 WebUI 中配置：

```text
http://t2i:8999
```

或按实际版本配置为：

```text
http://t2i:8999/text2img
```

## AI 简报产物

AI 简报会保存完整 Markdown 到 AstrBot 数据目录：

```text
data/plugin_data/astrbot_plugin_pulse/ai-news-YYYY-MM-DD.md
```

该文件用于后续网站发布；群聊推送使用结构化图片卡片。

## 调试页面

仓库保留四个可直接用浏览器打开的静态调试页：

- [debug_scripts/news_murasame_style_debug.html](debug_scripts/news_murasame_style_debug.html)
- [debug_scripts/epic_murasame_style_debug.html](debug_scripts/epic_murasame_style_debug.html)
- [debug_scripts/arena_murasame_style_debug.html](debug_scripts/arena_murasame_style_debug.html)
- [debug_scripts/github_light_style_debug.html](debug_scripts/github_light_style_debug.html)

生产环境中的 HTML 模板位于 `main.py`，并会将图片资源转为 base64 后交给 t2i 渲染。

## 发布注意事项

不要提交以下本地或私有文件：

- `worker/wrangler.toml`
- `worker/.dev.vars`
- `worker/.wrangler/`
- `worker/node_modules/`
- `worker/dist/`

`SECRET_TOKEN` 必须通过 `.dev.vars` 或 `wrangler secret put SECRET_TOKEN` 配置，不要写入仓库文件。
