# astrbot_plugin_pulse

Pulse 是一个 AstrBot 每日推送插件，用于定时推送 Epic 免费游戏和 AI 行业简报。

## 当前功能

- 获取 Epic Games 当前可领取的免费游戏。
- 通过 Cloudflare Worker 获取 AI 行业资讯。
- Worker 使用 Cron 定时抓取数据源，并将结果缓存到 Cloudflare KV。
- AstrBot 请求 Worker 时读取最近一次缓存结果，避免推送时等待上游站点实时抓取。
- Worker 提供 `/admin` 管理页，可配置 RSS、arXiv、Hugging Face Daily Papers 等数据源。
- Epic 和 AI 简报支持独立启用、独立推送时间、独立目标会话。
- 两类推送均使用 AstrBot HTML 渲染为图片后发送。
- AI 简报和 Epic 免费游戏图片已改为千恋万花丛雨风格的二次元卡片样式。
- 多目标会话之间加入随机延迟，降低瞬时批量发送风险。
- AI 简报采用单次 LLM 合成，同时生成 QQ 图片卡片、QQ 文本回退和本地网站发布用 Markdown 长文。
- AI 简报图片卡片由 LLM 生成结构化条目、分类标签、来源字段和一句话锐评。
- 可将生成的 AI Markdown 长文自动发布到 Halo SyncPostAI。

## 目录结构

```text
astrbot_plugin_pulse/
  main.py
  metadata.yaml
  _conf_schema.json
  requirements.txt
  imgs/
    开心.png
    趴着.png
    写作.png
    看这里.png
    玩游戏.png
  debug_scripts/
    news_murasame_style_debug.html
    epic_murasame_style_debug.html
  services/
    epic.py
    news.py
  worker/
    src/index.ts
    wrangler.toml.example
    DEPLOY_GUIDE.md
```

说明：

- `imgs/` 保存丛雨 Q 版表情包资源，插件渲染时会转为 base64 data URI 内嵌到 HTML，避免 t2i 容器无法读取本地文件。
- `debug_scripts/news_murasame_style_debug.html` 是 AI 简报图片样式调试页。
- `debug_scripts/epic_murasame_style_debug.html` 是 Epic 免费游戏图片样式调试页。
- 其他 `debug_scripts/` 下的临时脚本仍不建议提交。
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
  -> LLM 生成 QQ 图片卡片数据、QQ 文本简报和 Markdown 长文
  -> HTML 渲染为图片并推送
  -> 可选：发布 Markdown 长文到 Halo
```

Epic 免费游戏链路：

```text
AstrBot 定时任务
  -> 请求 Epic Games 当前免费游戏
  -> 使用丛雨风格 HTML 模板渲染游戏封面卡片
  -> 推送图片到已绑定会话
```

## 安装与配置

1. 将插件放入 AstrBot 插件目录。
2. 安装 Python 依赖：

```bash
pip install -r requirements.txt
```

3. 部署 `worker/` 下的 Cloudflare Worker，参考 [Worker 部署指南](worker/DEPLOY_GUIDE.md)。
4. 在 AstrBot 插件配置中填写 Worker 地址和 token。
5. 将 `imgs/` 目录随插件一起部署到 AstrBot 插件目录。
6. 在目标会话中使用绑定指令，将当前会话加入定时推送目标。

## 文本转图片服务

插件依赖 AstrBot 的 HTML 渲染能力，将 HTML 模板转换为图片发送。

如果默认远端 t2i 服务不稳定，建议自部署 AstrBot t2i 服务：

```bash
docker run -itd -p 8999:8999 soulter/astrbot-t2i-service:latest
```

如果 AstrBot 使用 Docker Compose 运行，可以将 t2i 服务加入同一个网络，并在 AstrBot WebUI 中配置：

```text
http://t2i:8999
```

或按实际版本需要配置为：

```text
http://t2i:8999/text2img
```

注意：t2i 是 HTML 截图渲染服务，不是 Stable Diffusion、Flux、DALL-E 这类文生图模型。插件中的本地表情包会被转为 base64 data URI 内嵌，避免 t2i 容器无法访问 AstrBot 容器内文件路径。

## API 中转站推荐

如果需要为 AstrBot 配置统一的 AI 模型接口，可以使用 [Glosc AI One](https://one.gloscai.com/)：

```text
https://one.gloscai.com/
```

一个接口，接入所有 AI 模型。通过统一、标准的接口协议接入海量模型，承载 AI 应用，高效管理数字资产，连接未来。

## 指令

- `/pulse epic`：立即发送 Epic 免费游戏。
- `/pulse news`：立即发送 AI 行业简报。
- `/pulse publish_news`：将最近生成的 AI Markdown 长文手动发布到 Halo。
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
- `epic_max_items`：Epic 单次最大展示数量。
- `news_enabled`：启用 AI 行业简报推送。
- `news_daily_time`：AI 简报每日推送时间，默认 `08:35`。
- `news_target_sessions`：AI 简报推送目标会话。
- `news_endpoint`：已部署的 Cloudflare Worker 地址。
- `news_bearer_token`：与 Worker `SECRET_TOKEN` 一致的 Token。
- `news_max_items`：AI 简报最大处理资讯数，建议不超过 `15`。
- `halo_publish_enabled`：启用 AI 长文自动发布到 Halo。
- `halo_site_url`：Halo 站点地址，例如 `https://blog.example.com`。
- `halo_syncpost_token`：SyncPostAI 插件中配置的推送 Token。
- `halo_publish_direct`：是否直接发布文章，默认 `true`。
- `article_statement_enabled`：是否在文章底部添加 AI 生成声明。
- `halo_article_author`：写入 Markdown 顶部 `auther` 字段的作者名。
- `halo_article_cover`：写入 Markdown 顶部 `cover` 字段的封面图，支持网络地址或本地路径，获取不到时留空。
- `halo_excerpt_min_chars`：AI 生成摘要的最少字数。
- `halo_excerpt_max_chars`：AI 生成摘要的最多字数。
- `halo_slug_prefix`：Halo 文章 slug 前缀，默认生成 `ai-news-YYYYMMDD`。
- `halo_publish_tags`：发布到 Halo 时附加的默认标签；留空时用 AI 自动生成。
- `halo_publish_categories`：发布到 Halo 时附加的默认分类。
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

该文件用于后续发布到网站；群聊推送使用适合快速阅读的结构化图片卡片。

如果启用了 `halo_publish_enabled`，插件会在生成 Markdown 后调用 Halo SyncPostAI：

```http
POST /apis/api.starter.halo.run/v1alpha1/articles
Content-Type: application/json; charset=utf-8
X-SyncPost-Token: <halo_syncpost_token>
```

生成的 Markdown 顶部会写入 front matter，正文不再包含一级标题；标题、摘要、分类和标签也会作为 SyncPostAI 的 JSON 字段提交。slug 默认是 `ai-news-YYYYMMDD`。也可以用 `/pulse publish_news` 将本地最近一篇长文重新发布到 Halo，用于验证站点地址、Token、分类和标签配置。

front matter 示例：

```yaml
---
title: AI Agent 正从演示走向企业基础设施
auther: admin
cover: https://example.com/cover.png
excerpt: 今日 AI 资讯显示，企业级 Agent 的竞争正在从模型能力转向记忆、权限、证据链和业务工作流。
categories:
 - technologysharing
tags:
 - AI
 - Agent
 - 产业观察
---
```

## 样式调试

本仓库保留两个可直接用浏览器打开的调试页：

- [debug_scripts/news_murasame_style_debug.html](debug_scripts/news_murasame_style_debug.html)
- [debug_scripts/epic_murasame_style_debug.html](debug_scripts/epic_murasame_style_debug.html)

它们使用静态示例数据和 `imgs/` 下的表情包，用于调整最终图片样式。生产环境中的 HTML 模板位于 `main.py`，并会把图片资源转为 base64 内嵌后交给 t2i 渲染。

## 发布注意事项

不要提交以下本地或私有文件：

- `worker/wrangler.toml`
- `worker/.dev.vars`
- `worker/.wrangler/`
- `worker/node_modules/`
- `worker/dist/`

应提交：

- `main.py`
- `README.md`
- `.gitignore`
- `imgs/*.png`
- `debug_scripts/news_murasame_style_debug.html`
- `debug_scripts/epic_murasame_style_debug.html`
- `worker/wrangler.toml.example`
- `worker/.dev.vars.example`
- `worker/src/index.ts`
- `worker/DEPLOY_GUIDE.md`

`SECRET_TOKEN` 必须通过 `.dev.vars` 或 `wrangler secret put SECRET_TOKEN` 配置，不要写入仓库文件。
