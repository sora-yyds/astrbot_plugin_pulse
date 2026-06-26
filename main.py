from __future__ import annotations

import asyncio
import base64
import html
import inspect
import json
import mimetypes
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import astrbot.api.message_components as Comp
import httpx

from .services.epic import EpicFreeGame, EpicGamesClient
from .services.news import AiNewsClient, NewsItem, NewsSynthesisError


PLUGIN_NAME = "astrbot_plugin_pulse"

QQ_BRIEF_START = "[QQ_BRIEF_START]"
QQ_BRIEF_END = "[QQ_BRIEF_END]"
LOCAL_ARTICLE_START = "[LOCAL_ARTICLE_START]"
LOCAL_ARTICLE_END = "[LOCAL_ARTICLE_END]"
ARTICLE_TITLE_START = "[ARTICLE_TITLE_START]"
ARTICLE_TITLE_END = "[ARTICLE_TITLE_END]"
ARTICLE_EXCERPT_START = "[ARTICLE_EXCERPT_START]"
ARTICLE_EXCERPT_END = "[ARTICLE_EXCERPT_END]"
ARTICLE_TAGS_START = "[ARTICLE_TAGS_START]"
ARTICLE_TAGS_END = "[ARTICLE_TAGS_END]"
NEWS_CARD_START = "[NEWS_CARD_START]"
NEWS_CARD_END = "[NEWS_CARD_END]"

HTML_RENDER_OPTIONS = {
    "type": "png",
    "full_page": True,
    "animations": "disabled",
    "timeout": 30000,
}

SYNCPOST_PATH = "/apis/api.syncpostai.sora.run/v1alpha1/articles"


class HaloPublishError(Exception):
    """Raised when a generated article cannot be published to Halo."""


@dataclass(frozen=True)
class HaloPublishResult:
    success: bool
    message: str
    article_name: str = ""
    snapshot_name: str = ""
    status: str = ""
    article_url: str = ""


@dataclass(frozen=True)
class NewsArticleDraft:
    title: str
    excerpt: str
    tags: list[str]
    body: str


@dataclass(frozen=True)
class NewsCardItem:
    category: str
    source: str
    title: str
    summary: str


@dataclass(frozen=True)
class NewsCardDraft:
    subtitle: str
    items: list[NewsCardItem]


class HaloSyncPostClient:
    def __init__(self, timeout: float = 30.0, trust_env: bool = False):
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)

    async def aclose(self):
        await self._client.aclose()

    async def publish_markdown(
        self,
        site_url: str,
        token: str,
        content: str,
        *,
        title: str = "",
        slug: str = "",
        excerpt: str = "",
        tags: list[str] | None = None,
        categories: list[str] | None = None,
        publish: bool = True,
    ) -> HaloPublishResult:
        endpoint = self._article_endpoint(site_url)
        payload: dict[str, Any] = {
            "source": "astrbot-pulse",
            "content": content,
            "contentType": "markdown",
            "publish": publish,
        }
        if slug:
            payload["slug"] = slug

        response = await self._client.post(
            endpoint,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-SyncPost-Token": token,
            },
            json=payload,
        )
        response_text = response.text.strip()
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            raise HaloPublishError(
                f"Halo SyncPostAI returned redirect HTTP {response.status_code}: "
                f"location={location or '<empty>'}"
            )
        if response.status_code >= 400:
            raise HaloPublishError(
                f"Halo SyncPostAI returned HTTP {response.status_code}: "
                f"{response_text[:500]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "")
            raise HaloPublishError(
                "Halo SyncPostAI returned non-JSON response: "
                f"HTTP {response.status_code}, content-type={content_type or 'unknown'}, "
                f"body={response_text[:500] or '<empty>'}"
            ) from exc

        success = bool(data.get("success", True))
        message = str(data.get("message") or "")
        if not success:
            raise HaloPublishError(message or "Halo SyncPostAI reported failure")

        return HaloPublishResult(
            success=success,
            message=message,
            article_name=str(data.get("articleName") or ""),
            snapshot_name=str(data.get("snapshotName") or ""),
            status=str(data.get("status") or ""),
            article_url=str(data.get("articleUrl") or ""),
        )

    async def resolve_cover(self, value: str) -> str:
        cover = value.strip()
        if not cover:
            return ""
        if cover.startswith(("https://", "http://")):
            try:
                response = await self._client.head(cover, follow_redirects=True)
                if response.status_code == 405:
                    response = await self._client.get(
                        cover,
                        follow_redirects=True,
                        headers={"Range": "bytes=0-0"},
                    )
                return cover if response.status_code < 400 else ""
            except Exception:
                return ""
        return cover if Path(cover).exists() else ""

    def _article_endpoint(self, site_url: str) -> str:
        value = site_url.strip().rstrip("/")
        if not value:
            raise HaloPublishError("Halo site URL is empty")
        if value.endswith(SYNCPOST_PATH):
            return value
        return value + SYNCPOST_PATH

NEWS_SYNTHESIS_SYSTEM_PROMPT = f"""
你是资深 AI 科技媒体主编、产业分析师和信息架构师。
我会提供一个 JSON 数组，包含今日最多 15 条 AI 新闻、模型发布、论文和评测资讯。

你必须基于这些材料一次性生成两个结果，并严格使用以下标签分隔：
{QQ_BRIEF_START}/{QQ_BRIEF_END}
{LOCAL_ARTICLE_START}/{LOCAL_ARTICLE_END}

任务一：QQ群推送简报
- 面向移动端快速阅读。
- 只输出 5-7 行，每行是一条独立、锋利、可扫读的一句话。
- 不要在每行开头添加 emoji、图标、项目符号或装饰字符。
- 这里才允许做一句话总结；不要把详细分析写进 QQ 简报。
- 不要贴原始 URL。

任务二：本地网站 Markdown 长文
1. 严禁逐条罗列：正文严禁出现“1. 标题”“文章一：”“第一篇”“逐篇文章详情”等断裂的序号和单篇结构，也不要按来源顺序机械复述。
2. 社论流叙述：必须将全部材料融合成一篇前后呼应的中文科技综述文章，用主题、趋势、冲突和因果关系组织内容。
3. 独家行业洞察：每一段在叙述事实的同时，都要自然织入 AI 自身的行业透视和深度点评，例如技术背后的行业洗牌、厂商战略意图、监管与资本影响，或学术研究对工业界的潜在震荡。分析篇幅至少占全文三分之一。
4. 动态标题：用单井号 Markdown 标题生成一个有媒体感、能体现核心观点的标题，不要使用固定标题。
5. 多级结构：使用 Markdown 二级标题组织 2-3 个分析维度，可按需要使用三级标题，但不要用文章编号作为标题。
6. 中文出版格式：正文每个自然段开头使用两个全角空格“　　”缩进。
7. 链接处理：正文中可以提及媒体名、机构名、论文名或公司名，但绝对不能直接粘贴 URL，也不要出现 [1]、[2]、<sup>1</sup> 这类正文脚注。
8. 参考文献统一置底：文章末尾必须包含“## 参考文献”板块，用标准 Markdown 链接列出材料来源，格式为：`- [媒体名称: 文章标题](URL)`。
9. AI 生成声明：文章最后必须包含“## 声明”板块，并写明“本文由 AI 基于公开资讯自动生成，仅供信息参考，不构成投资、法律或技术决策建议。”
10. 字数控制：本地长文控制在 1500-2200 个中文字符，内容必须紧凑、有判断，不要填充空话。

严格输出结构：
{QQ_BRIEF_START}
一句话简报...
另一条重要趋势...
{QQ_BRIEF_END}

{LOCAL_ARTICLE_START}
# 动态生成的科技媒体社论标题

　　正文自然段...

## 自动生成的分析维度标题

　　正文自然段...

## 参考文献

- [媒体名称: 文章标题](https://example.com)

## 声明

本文由 AI 基于公开资讯自动生成，仅供信息参考，不构成投资、法律或技术决策建议。
{LOCAL_ARTICLE_END}
""".strip()

OLD_EPIC_HTML_TEMPLATE = """
<div class="pulse-card epic">
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #121212;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #e5e7eb;
    }
    .pulse-card {
      width: 100vw;
      min-width: 900px;
      padding: 0;
      background: #121212;
    }
    .panel {
      background: #121212;
      border-radius: 0;
      overflow: hidden;
    }
    .header {
      padding: 24px 32px;
      color: #fff;
      background: #1a1a1a;
      border-bottom: 4px solid #0078f2;
    }
    .eyebrow {
      display: inline-block;
      margin-left: 14px;
      padding: 4px 12px;
      border-radius: 4px;
      background: rgba(0, 120, 242, .16);
      font-size: 28px;
      letter-spacing: 0;
      text-transform: uppercase;
      color: #0078f2;
      font-weight: 740;
    }
    h1 {
      display: inline-block;
      margin: 0;
      font-size: 54px;
      line-height: 1.1;
      font-weight: 900;
      letter-spacing: 0;
    }
    .date {
      margin-top: 10px;
      font-size: 24px;
      color: #a3a3a3;
    }
    .content { padding: 34px 42px 42px; }
    .empty {
      padding: 26px;
      border-left: 8px solid #0078f2;
      border-radius: 6px;
      background: #1e1e1e;
      font-size: 28px;
      color: #cccccc;
    }
    .games {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 22px;
    }
    .game {
      border: 1px solid #2a2a2a;
      border-radius: 8px;
      background: #1e1e1e;
      overflow: hidden;
    }
    .cover-box {
      width: 100%;
      aspect-ratio: 16 / 9;
      background: #202020;
      overflow: hidden;
    }
    .cover {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .cover-placeholder {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      height: 100%;
      color: #0078f2;
      font-size: 28px;
      font-weight: 800;
    }
    .game-body { padding: 16px 18px 18px; }
    .game-title {
      margin: 0 0 12px;
      font-size: 28px;
      line-height: 1.24;
      font-weight: 740;
      color: #ffffff;
      letter-spacing: 0;
    }
    .meta {
      display: inline-block;
      padding: 7px 10px;
      border-radius: 4px;
      background: #202020;
      color: #e5a93c;
      font-size: 18px;
      font-weight: 650;
    }
  </style>
  <div class="panel">
    <div class="header">
      <div class="eyebrow">AstrBot Pulse</div>
      <h1>Epic 免费游戏</h1>
      <div class="date">{{ date }}</div>
    </div>
    <div class="content">
      {% if games %}
      <div class="games">
        {% for game in games %}
        <div class="game">
          <div class="cover-box">
          {% if game.image_url %}
            <img class="cover" src="{{ game.image_url }}" />
          {% else %}
            <div class="cover-placeholder">EPIC GAMES</div>
          {% endif %}
          </div>
          <div class="game-body">
            <h2 class="game-title">{{ game.title }}</h2>
            <div class="meta">领取截止 {{ game.end_date }}</div>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty">今日未发现正在进行的免费促销。</div>
      {% endif %}
    </div>
  </div>
</div>
"""

OLD_NEWS_HTML_TEMPLATE = """
<div class="pulse-card news">
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #f8fafc;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #1e293b;
    }
    .pulse-card {
      width: 100vw;
      min-width: 900px;
      padding: 0;
      background: #f8fafc;
    }
    .panel {
      background: #f8fafc;
      border-radius: 0;
      overflow: hidden;
    }
    .header {
      padding: 24px 32px;
      color: #fff;
      background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%);
      box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);
    }
    .eyebrow {
      display: inline-block;
      margin-left: 16px;
      font-size: 28px;
      letter-spacing: 0;
      color: #94a3b8;
      font-weight: 500;
    }
    h1 {
      display: inline-block;
      margin: 0;
      font-size: 54px;
      line-height: 1.1;
      font-weight: 800;
      letter-spacing: 1px;
    }
    .date {
      margin-top: 8px;
      font-size: 22px;
      color: #cbd5e1;
    }
    .content {
      max-width: 88%;
      margin: 0 auto;
      padding: 34px 1em 32px;
      font-size: 30px;
      line-height: 1.78;
      word-break: break-word;
    }
    .content h1,
    .content h2,
    .content h3 {
      margin: 40px 0 16px;
      line-height: 1.35;
      color: #0f172a;
      font-weight: 700;
    }
    .content h1:first-child { margin-top: 0; }
    .content h1 { font-size: 40px; }
    .content h2 {
      padding-bottom: 8px;
      border-bottom: 2px solid #e2e8f0;
      font-size: 34px;
    }
    .content h3 { font-size: 30px; }
    .content p {
      margin: 24px 0;
      text-align: justify;
    }
    .content ul {
      padding-left: 0;
      margin: 20px 0;
      list-style: none;
    }
    .content li {
      margin: 0 0 12px;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      list-style: none;
    }
    .content strong {
      color: #4f46e5;
      font-weight: 600;
    }
    .content code {
      padding: 2px 5px;
      border-radius: 5px;
      background: #edf2f7;
      color: #334155;
      font-size: .88em;
    }
  </style>
  <div class="panel">
    <div class="header">
      <h1>AI PULSE</h1>
      <div class="eyebrow">每日科技前沿观察</div>
      <div class="date">{{ date }}</div>
    </div>
    <div class="content">{{ report_html | safe }}</div>
  </div>
</div>
"""


EPIC_HTML_TEMPLATE = """
<div class="pulse-card epic">
  <style>
    * { box-sizing: border-box; }
    :root {
      --ink: #332c3f;
      --muted: #7a6f89;
      --plum: #6e4f8f;
      --plum-deep: #3d2c5a;
      --wisteria: #b99adb;
      --sakura: #f4a9bd;
      --paper: #fff9f1;
      --paper-soft: #fffdf8;
      --gold: #c59445;
      --line: rgba(110, 79, 143, .18);
      --shadow: 0 28px 70px rgba(61, 44, 90, .20);
    }
    body {
      margin: 0;
      width: 100vw;
      background:
        radial-gradient(circle at 12% 10%, rgba(244, 169, 189, .38), transparent 30%),
        radial-gradient(circle at 90% 8%, rgba(185, 154, 219, .45), transparent 28%),
        linear-gradient(135deg, #f7e8ef 0%, #efe5fa 38%, #f8f0df 100%);
      color: var(--ink);
      font-family: "Noto Serif SC", "Source Han Serif SC", "Microsoft YaHei", "PingFang SC", serif;
      letter-spacing: 0;
    }
    .pulse-card {
      position: relative;
      overflow: hidden;
      width: 100vw;
      min-width: 960px;
      min-height: 680px;
      border: 1px solid rgba(255, 255, 255, .82);
      border-radius: 26px;
      background:
        linear-gradient(90deg, rgba(110, 79, 143, .055) 1px, transparent 1px),
        linear-gradient(0deg, rgba(110, 79, 143, .055) 1px, transparent 1px),
        linear-gradient(180deg, rgba(255, 255, 255, .84), rgba(255, 249, 241, .94));
      background-size: 34px 34px, 34px 34px, auto;
      box-shadow: var(--shadow);
    }
    .pulse-card::before {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        radial-gradient(circle at 8% 18%, rgba(255, 255, 255, .75) 0 3px, transparent 4px),
        radial-gradient(circle at 88% 68%, rgba(255, 255, 255, .65) 0 3px, transparent 4px),
        linear-gradient(135deg, rgba(255, 255, 255, .42), transparent 34%, rgba(255, 255, 255, .36));
    }
    .header {
      position: relative;
      padding: 28px 36px 30px;
      color: #fff;
      background:
        linear-gradient(120deg, rgba(61, 44, 90, .96), rgba(112, 82, 145, .95) 48%, rgba(184, 112, 151, .94)),
        repeating-linear-gradient(90deg, rgba(255, 255, 255, .09) 0 1px, transparent 1px 18px);
    }
    .header::before {
      content: "";
      position: absolute;
      inset: 0;
      background:
        radial-gradient(circle at 82% 20%, rgba(255, 255, 255, .23), transparent 24%),
        linear-gradient(90deg, transparent 0 68%, rgba(255, 244, 205, .16));
      pointer-events: none;
    }
    .header::after {
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      bottom: -11px;
      height: 22px;
      background: radial-gradient(circle at 12px 11px, var(--paper) 0 10px, transparent 11px) 0 0 / 30px 22px repeat-x;
    }
    .brand-row {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 270px;
      align-items: center;
      gap: 18px;
    }
    .kicker {
      margin-bottom: 8px;
      color: rgba(255, 244, 225, .84);
      font-size: 20px;
      font-weight: 700;
    }
    .title {
      display: flex;
      align-items: baseline;
      gap: 18px;
      margin: 0;
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 54px;
      line-height: 1;
      font-weight: 900;
      letter-spacing: 1px;
    }
    .title span {
      color: rgba(255, 244, 225, .9);
      font-size: 32px;
      font-weight: 800;
    }
    .date-strip {
      position: relative;
      display: inline-flex;
      align-items: center;
      gap: 12px;
      z-index: 1;
      margin-top: 18px;
      padding: 8px 15px;
      border: 1px solid rgba(255, 244, 205, .54);
      border-radius: 999px;
      background: rgba(255, 255, 255, .12);
      color: rgba(255, 248, 236, .95);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 20px;
      font-weight: 750;
    }
    .date-strip::before {
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #ffd98a;
      box-shadow: 0 0 16px rgba(255, 217, 138, .95);
    }
    .sticker-img {
      display: block;
      object-fit: contain;
      filter: drop-shadow(0 14px 22px rgba(61, 44, 90, .24));
    }
    .sticker-img[src=""] {
      display: none;
    }
    .header-sticker {
      width: 260px;
      height: 188px;
      justify-self: end;
      align-self: end;
      margin-bottom: -10px;
    }
    .content-wrap {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: 70px 1fr;
      gap: 20px;
      padding: 48px 42px 42px 34px;
    }
    .side-ribbon {
      position: relative;
      min-height: 420px;
      border-radius: 999px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, .92), rgba(255, 244, 248, .78)),
        linear-gradient(180deg, var(--sakura), var(--wisteria));
      border: 1px solid rgba(185, 154, 219, .42);
      box-shadow: inset 0 0 0 6px rgba(255, 255, 255, .45);
    }
    .side-ribbon-label {
      position: absolute;
      top: 34px;
      left: 50%;
      transform: translateX(-50%);
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 8px;
      color: var(--plum-deep);
      font-size: 22px;
      line-height: 1;
      font-weight: 800;
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    }
    .side-sticker {
      position: absolute;
      left: -22px;
      bottom: 12px;
      width: 124px;
      height: 124px;
    }
    .games-board {
      position: relative;
      padding: 26px 28px 30px;
      border: 1px solid rgba(110, 79, 143, .16);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255, 253, 248, .94), rgba(255, 249, 241, .88)), var(--paper-soft);
      box-shadow: 0 18px 45px rgba(92, 67, 123, .12);
    }
    .board-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 22px;
      padding: 0 6px 18px;
      border-bottom: 1px dashed rgba(110, 79, 143, .28);
    }
    .board-title {
      margin: 0;
      color: var(--plum-deep);
      font-size: 32px;
      line-height: 1.25;
      font-weight: 900;
    }
    .board-subtitle {
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 19px;
      line-height: 1.5;
      font-family: Inter, "Microsoft YaHei", sans-serif;
    }
    .inline-sticker {
      width: 220px;
      height: 166px;
      flex: 0 0 auto;
    }
    .games {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 20px;
    }
    .game {
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 18px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, .74), rgba(255, 250, 244, .62)),
        linear-gradient(90deg, rgba(244, 169, 189, .12), transparent 34%);
      box-shadow: 0 10px 26px rgba(92, 67, 123, .10);
    }
    .cover-box {
      position: relative;
      width: 100%;
      aspect-ratio: 16 / 9;
      overflow: hidden;
      background: #ede4f4;
    }
    .cover {
      display: block;
      width: 100%;
      height: 100%;
      object-fit: cover;
    }
    .cover-placeholder {
      display: grid;
      place-items: center;
      width: 100%;
      height: 100%;
      color: var(--plum);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 28px;
      font-weight: 900;
    }
    .cover-box::after {
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(180deg, transparent 52%, rgba(61, 44, 90, .46)),
        radial-gradient(circle at 82% 18%, rgba(255, 255, 255, .28), transparent 22%);
      pointer-events: none;
    }
    .game-body {
      padding: 20px 22px 22px;
    }
    .game-title {
      margin: 0 0 14px;
      color: var(--plum-deep);
      font-size: 30px;
      line-height: 1.24;
      font-weight: 900;
    }
    .meta {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 12px;
      border: 1px solid rgba(197, 148, 69, .34);
      border-radius: 999px;
      background: rgba(255, 244, 205, .58);
      color: #8a642a;
      font-family: Inter, "Microsoft YaHei", sans-serif;
      font-size: 18px;
      font-weight: 800;
    }
    .meta::before {
      content: "";
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--gold);
      box-shadow: 0 0 12px rgba(197, 148, 69, .75);
    }
    .empty {
      padding: 28px;
      border: 1px dashed rgba(110, 79, 143, .28);
      border-radius: 16px;
      background: rgba(255, 255, 255, .58);
      color: var(--muted);
      font-size: 26px;
      line-height: 1.6;
    }
    .footer-note {
      display: block;
      margin-top: 24px;
      padding: 18px 8px 0;
      border-top: 1px dashed rgba(110, 79, 143, .28);
      color: var(--muted);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 18px;
    }
    .petals {
      position: absolute;
      inset: 0;
      pointer-events: none;
      overflow: hidden;
    }
    .petal {
      position: absolute;
      width: 22px;
      height: 14px;
      border-radius: 70% 30% 70% 30%;
      background: rgba(244, 169, 189, .58);
      transform: rotate(var(--r));
      filter: blur(.1px);
    }
    .petal:nth-child(1) { left: 102px; top: 186px; --r: 28deg; }
    .petal:nth-child(2) { right: 160px; top: 258px; --r: -18deg; }
    .petal:nth-child(3) { right: 68px; top: 560px; --r: 36deg; opacity: .7; }
    .petal:nth-child(4) { left: 164px; bottom: 118px; --r: -32deg; opacity: .62; }
  </style>
  <div class="header">
    <div class="brand-row">
      <div>
        <div class="kicker">Yuzusoft Shrine Game Dispatch</div>
        <h1 class="title">EPIC PULSE <span>限时免费游戏</span></h1>
      </div>
      <img class="sticker-img header-sticker" src="{{ sticker_look }}" alt="丛雨酱看这里表情" />
    </div>
    <div class="date-strip">{{ date }}</div>
  </div>

  <div class="content-wrap">
    <aside class="side-ribbon" aria-hidden="true">
      <div class="side-ribbon-label"><span>今</span><span>日</span><span>免</span><span>费</span></div>
      <img class="sticker-img side-sticker" src="{{ sticker_lay }}" alt="丛雨酱趴着表情" />
    </aside>

    <article class="games-board">
      <header class="board-heading">
        <div>
          <h2 class="board-title">今日可领取游戏</h2>
          <p class="board-subtitle">丛雨酱提醒：入库不亏，过期就只能等下次缘分。</p>
        </div>
        <img class="sticker-img inline-sticker" src="{{ sticker_game }}" alt="丛雨酱玩游戏表情" />
      </header>

      {% if games %}
      <div class="games">
        {% for game in games %}
        <section class="game">
          <div class="cover-box">
            {% if game.image_url %}
            <img class="cover" src="{{ game.image_url }}" />
            {% else %}
            <div class="cover-placeholder">EPIC GAMES</div>
            {% endif %}
          </div>
          <div class="game-body">
            <h3 class="game-title">{{ game.title }}</h3>
            <div class="meta">领取截止 {{ game.end_date }}</div>
          </div>
        </section>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty">今日未发现正在进行的免费促销。</div>
      {% endif %}

      <div class="footer-note">
        <span>信息来自 Epic Games 免费游戏列表，请以商店页面为准</span>
      </div>
    </article>
  </div>
  <div class="petals" aria-hidden="true">
    <i class="petal"></i>
    <i class="petal"></i>
    <i class="petal"></i>
    <i class="petal"></i>
  </div>
</div>
"""


NEWS_HTML_TEMPLATE = """
<div class="pulse-card news">
  <style>
    * { box-sizing: border-box; }
    :root {
      --ink: #332c3f;
      --muted: #7a6f89;
      --plum: #6e4f8f;
      --plum-deep: #3d2c5a;
      --wisteria: #b99adb;
      --sakura: #f4a9bd;
      --paper: #fff9f1;
      --paper-soft: #fffdf8;
      --gold: #c59445;
      --line: rgba(110, 79, 143, .18);
      --shadow: 0 28px 70px rgba(61, 44, 90, .20);
    }
    body {
      margin: 0;
      width: 100vw;
      background:
        radial-gradient(circle at 12% 10%, rgba(244, 169, 189, .38), transparent 30%),
        radial-gradient(circle at 90% 8%, rgba(185, 154, 219, .45), transparent 28%),
        linear-gradient(135deg, #f7e8ef 0%, #efe5fa 38%, #f8f0df 100%);
      color: var(--ink);
      font-family: "Noto Serif SC", "Source Han Serif SC", "Microsoft YaHei", "PingFang SC", serif;
      letter-spacing: 0;
    }
    .pulse-card {
      position: relative;
      overflow: hidden;
      width: 100vw;
      min-width: 960px;
      min-height: 900px;
      padding: 0;
      border: 1px solid rgba(255, 255, 255, .82);
      border-radius: 26px;
      background:
        linear-gradient(90deg, rgba(110, 79, 143, .055) 1px, transparent 1px),
        linear-gradient(0deg, rgba(110, 79, 143, .055) 1px, transparent 1px),
        linear-gradient(180deg, rgba(255, 255, 255, .84), rgba(255, 249, 241, .94));
      background-size: 34px 34px, 34px 34px, auto;
      box-shadow: var(--shadow);
    }
    .pulse-card::before {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        radial-gradient(circle at 8% 18%, rgba(255, 255, 255, .75) 0 3px, transparent 4px),
        radial-gradient(circle at 18% 82%, rgba(255, 255, 255, .55) 0 2px, transparent 3px),
        radial-gradient(circle at 88% 68%, rgba(255, 255, 255, .65) 0 3px, transparent 4px),
        linear-gradient(135deg, rgba(255, 255, 255, .42), transparent 34%, rgba(255, 255, 255, .36));
    }
    .header {
      position: relative;
      padding: 28px 32px 28px 36px;
      color: #fff;
      background:
        linear-gradient(120deg, rgba(61, 44, 90, .96), rgba(112, 82, 145, .95) 48%, rgba(184, 112, 151, .94)),
        repeating-linear-gradient(90deg, rgba(255, 255, 255, .09) 0 1px, transparent 1px 18px);
    }
    .header::before {
      content: "";
      position: absolute;
      inset: 0;
      background:
        radial-gradient(circle at 86% 26%, rgba(255, 255, 255, .22), transparent 22%),
        linear-gradient(90deg, transparent 0 70%, rgba(255, 244, 205, .16));
      pointer-events: none;
    }
    .header::after {
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      bottom: -11px;
      height: 22px;
      background: radial-gradient(circle at 12px 11px, var(--paper) 0 10px, transparent 11px) 0 0 / 30px 22px repeat-x;
    }
    .brand-row {
      position: relative;
      display: grid;
      grid-template-columns: 54px minmax(0, 1fr) 270px;
      align-items: center;
      gap: 18px;
      z-index: 1;
    }
    .crest {
      display: grid;
      place-items: center;
      width: 54px;
      height: 54px;
      border: 2px solid rgba(255, 244, 205, .88);
      border-radius: 50%;
      background: radial-gradient(circle, rgba(255, 255, 255, .18), transparent 58%), rgba(255, 255, 255, .08);
      box-shadow:
        inset 0 0 0 7px rgba(255, 255, 255, .08),
        0 0 0 1px rgba(255, 255, 255, .18);
    }
    .crest::before,
    .crest::after {
      content: "";
      position: absolute;
      border-radius: 999px;
    }
    .crest::before {
      width: 22px;
      height: 22px;
      border: 2px solid rgba(255, 244, 205, .86);
    }
    .crest::after {
      width: 7px;
      height: 7px;
      background: #ffe9a8;
      box-shadow: 0 0 14px rgba(255, 233, 168, .9);
    }
    .kicker {
      margin-bottom: 6px;
      color: rgba(255, 244, 225, .82);
      font-size: 18px;
      font-weight: 600;
    }
    .title {
      margin: 0;
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 46px;
      line-height: 1;
      font-weight: 900;
      letter-spacing: 2px;
    }
    .subtitle {
      margin-left: 4px;
      color: rgba(255, 244, 225, .88);
      font-size: 24px;
      font-weight: 650;
      white-space: nowrap;
    }
    .date-strip {
      position: relative;
      display: inline-flex;
      align-items: center;
      gap: 12px;
      z-index: 1;
      margin-top: 16px;
      padding: 8px 15px;
      border: 1px solid rgba(255, 244, 205, .54);
      border-radius: 999px;
      background: rgba(255, 255, 255, .12);
      color: rgba(255, 248, 236, .95);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 19px;
      font-weight: 650;
    }
    .date-strip::before {
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #ffd98a;
      box-shadow: 0 0 16px rgba(255, 217, 138, .95);
    }
    .sticker-img {
      display: block;
      object-fit: contain;
      filter: drop-shadow(0 12px 20px rgba(61, 44, 90, .22));
    }
    .sticker-img[src=""] {
      display: none;
    }
    .sticker-img.header-sticker {
      width: 230px;
      height: 154px;
      justify-self: end;
      align-self: end;
      margin-bottom: -10px;
    }
    .sticker-img.side-sticker {
      position: absolute;
      left: -20px;
      bottom: 18px;
      width: 120px;
      height: 120px;
    }
    .sticker-img.inline-sticker {
      width: 220px;
      height: 166px;
      flex: 0 0 auto;
    }
    .content-wrap {
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: 82px 1fr;
      gap: 20px;
      padding: 42px 34px 42px 32px;
    }
    .side-ribbon {
      position: relative;
      min-height: 650px;
      border-radius: 999px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, .92), rgba(255, 244, 248, .78)),
        linear-gradient(180deg, var(--sakura), var(--wisteria));
      border: 1px solid rgba(185, 154, 219, .42);
      box-shadow: inset 0 0 0 6px rgba(255, 255, 255, .45);
    }
    .side-ribbon-label {
      position: absolute;
      top: 34px;
      left: 50%;
      transform: translateX(-50%);
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 8px;
      color: var(--plum-deep);
      font-size: 22px;
      line-height: 1;
      font-weight: 800;
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    }
    .news-board {
      position: relative;
      padding: 26px 28px 30px;
      border: 1px solid rgba(110, 79, 143, .16);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255, 253, 248, .94), rgba(255, 249, 241, .88)), var(--paper-soft);
      box-shadow: 0 18px 45px rgba(92, 67, 123, .12);
    }
    .board-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 22px;
      padding: 0 6px 18px;
      border-bottom: 1px dashed rgba(110, 79, 143, .28);
    }
    .board-title {
      margin: 0;
      color: var(--plum-deep);
      font-size: 30px;
      line-height: 1.25;
      font-weight: 900;
    }
    .board-subtitle {
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 19px;
      line-height: 1.5;
      font-family: Inter, "Microsoft YaHei", sans-serif;
    }
    .news-list {
      display: grid;
      gap: 16px;
    }
    .news-item {
      position: relative;
      display: grid;
      grid-template-columns: 52px minmax(0, 1fr);
      gap: 16px;
      padding: 20px 22px 20px 18px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, .74), rgba(255, 250, 244, .62)),
        linear-gradient(90deg, rgba(244, 169, 189, .12), transparent 34%);
    }
    .item-index {
      display: grid;
      place-items: center;
      width: 52px;
      height: 52px;
      border-radius: 50%;
      background:
        radial-gradient(circle at 34% 28%, #fff 0 16%, transparent 18%),
        linear-gradient(135deg, var(--sakura), var(--wisteria));
      color: #fff;
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 21px;
      font-weight: 900;
      box-shadow: 0 8px 22px rgba(110, 79, 143, .22);
    }
    .item-meta {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 8px;
      color: var(--muted);
      font-family: Inter, "Microsoft YaHei", sans-serif;
      font-size: 17px;
      font-weight: 700;
    }
    .tag {
      padding: 4px 9px;
      border: 1px solid rgba(197, 148, 69, .34);
      border-radius: 999px;
      background: rgba(255, 244, 205, .52);
      color: #8a642a;
    }
    .item-title {
      margin: 0 0 8px;
      color: var(--plum-deep);
      font-size: 26px;
      line-height: 1.32;
      font-weight: 900;
    }
    .item-summary {
      margin: 0;
      color: var(--ink);
      font-size: 24px;
      line-height: 1.64;
      text-align: justify;
      word-break: break-word;
    }
    .footer-note {
      display: block;
      margin-top: 26px;
      padding: 20px 8px 0;
      border-top: 1px dashed rgba(110, 79, 143, .28);
      color: var(--muted);
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      font-size: 18px;
    }
    .petals {
      position: absolute;
      inset: 0;
      pointer-events: none;
      overflow: hidden;
    }
    .petal {
      position: absolute;
      width: 22px;
      height: 14px;
      border-radius: 70% 30% 70% 30%;
      background: rgba(244, 169, 189, .58);
      transform: rotate(var(--r));
      filter: blur(.1px);
    }
    .petal:nth-child(1) { left: 102px; top: 186px; --r: 28deg; }
    .petal:nth-child(2) { right: 160px; top: 258px; --r: -18deg; }
    .petal:nth-child(3) { right: 68px; top: 560px; --r: 36deg; opacity: .7; }
    .petal:nth-child(4) { left: 164px; bottom: 118px; --r: -32deg; opacity: .62; }
    .petal:nth-child(5) { right: 302px; bottom: 74px; --r: 12deg; opacity: .5; }
    @media (min-width: 1120px) {
      .content-wrap {
        grid-template-columns: 82px 1fr;
        padding-right: 46px;
      }
      .news-list {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        align-items: stretch;
      }
      .news-item {
        grid-template-columns: 46px minmax(0, 1fr);
        gap: 14px;
        padding: 18px 18px 18px 16px;
      }
      .item-index {
        width: 46px;
        height: 46px;
        font-size: 19px;
      }
      .item-meta {
        font-size: 15px;
        gap: 8px;
      }
      .tag {
        padding: 3px 8px;
      }
      .item-title {
        font-size: 23px;
        line-height: 1.28;
      }
      .item-summary {
        font-size: 21px;
        line-height: 1.55;
      }
    }
  </style>
  <div class="header">
    <div class="brand-row">
      <div class="crest" aria-hidden="true"></div>
      <div class="title-group">
        <div class="kicker">Yuzusoft Shrine Intelligence Bulletin</div>
        <h1 class="title">AI PULSE <span class="subtitle">每日科技前沿观察</span></h1>
      </div>
      <img class="sticker-img header-sticker" src="{{ sticker_happy }}" alt="丛雨酱开心表情" />
    </div>
    <div class="date-strip">{{ date }}</div>
  </div>

  <div class="content-wrap">
    <aside class="side-ribbon" aria-hidden="true">
      <div class="side-ribbon-label"><span>丛</span><span>雨</span><span>通</span><span>讯</span></div>
      <img class="sticker-img side-sticker" src="{{ sticker_lay }}" alt="丛雨酱趴着表情" />
    </aside>
    <article class="news-board">
      <header class="board-heading">
        <div>
          <h2 class="board-title">今日 AI 资讯摘要</h2>
          <p class="board-subtitle">{{ subtitle }}</p>
        </div>
        <img class="sticker-img inline-sticker" src="{{ sticker_write }}" alt="丛雨酱写作表情" />
      </header>
      <div class="news-list">
        {% for item in items %}
        <section class="news-item">
          <div class="item-index">{{ "%02d"|format(loop.index) }}</div>
          <div>
            <div class="item-meta"><span class="tag">{{ item.category }}</span><span>{{ item.source }}</span></div>
            <h3 class="item-title">{{ item.title }}</h3>
            <p class="item-summary">{{ item.summary }}</p>
          </div>
        </section>
        {% endfor %}
      </div>
      <div class="footer-note">
        <span>AI 基于公开资讯自动生成，仅供信息参考</span>
      </div>
    </article>
  </div>
  <div class="petals" aria-hidden="true">
    <i class="petal"></i>
    <i class="petal"></i>
    <i class="petal"></i>
    <i class="petal"></i>
    <i class="petal"></i>
  </div>
</div>
"""


@register(
    PLUGIN_NAME,
    "Sora",
    "每日推送 Epic 免费游戏与 AI 行业简报。",
    "0.3.0",
)
class PulsePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._tasks: list[asyncio.Task] = []
        self._epic_client = EpicGamesClient()
        self._news_client = AiNewsClient()
        self._halo_client = HaloSyncPostClient()
        self._plugin_data_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        )

    async def initialize(self):
        if not self._config_bool("enabled", True):
            logger.info("Pulse 定时推送已禁用")
            return

        if self._config_bool("epic_enabled", True):
            self._tasks.append(
                asyncio.create_task(
                    self._scheduled_loop(
                        job_name="epic",
                        time_key="epic_daily_time",
                        push_func=self._push_epic_to_targets,
                    ),
                    name="pulse_epic_loop",
                )
            )

        if self._config_bool("news_enabled", True):
            self._tasks.append(
                asyncio.create_task(
                    self._scheduled_loop(
                        job_name="news",
                        time_key="news_daily_time",
                        push_func=self._push_news_to_targets,
                    ),
                    name="pulse_news_loop",
                )
            )

        logger.info(f"Pulse 已启动 {len(self._tasks)} 个定时任务")

    @filter.command_group("pulse")
    def pulse(self):
        """Pulse 每日推送。"""
        pass

    @pulse.command("epic")
    async def pulse_epic(self, event: AstrMessageEvent):
        """立即发送 Epic 免费游戏。"""
        chain = await self._build_epic_chain()
        yield event.chain_result(chain)

    @pulse.command("news")
    async def pulse_news(self, event: AstrMessageEvent):
        """立即发送 AI 行业简报。"""
        chain, publish_message = await self._build_news_chain(event.unified_msg_origin)
        yield event.chain_result(chain)
        if publish_message:
            yield event.plain_result(publish_message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pulse.command("publish_news")
    async def pulse_publish_news(self, event: AstrMessageEvent):
        """将最近一篇 AI Markdown 长文发布到 Halo。"""
        result = await self._publish_latest_news_to_halo()
        yield event.plain_result(result)

    @pulse.command("now")
    async def pulse_now(self, event: AstrMessageEvent):
        """立即发送 Epic 免费游戏和 AI 行业简报。"""
        chain = []
        chain.extend(await self._build_epic_chain())
        chain.append(Comp.Plain("\n"))
        news_chain, publish_message = await self._build_news_chain(event.unified_msg_origin)
        chain.extend(news_chain)
        yield event.chain_result(chain)
        if publish_message:
            yield event.plain_result(publish_message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pulse.command("bind_epic")
    async def pulse_bind_epic(self, event: AstrMessageEvent):
        """将当前会话绑定到 Epic 定时推送。"""
        self._add_target("epic", event.unified_msg_origin)
        yield event.plain_result("已绑定当前会话到 Epic 免费游戏推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pulse.command("bind_news")
    async def pulse_bind_news(self, event: AstrMessageEvent):
        """将当前会话绑定到 AI 简报定时推送。"""
        self._add_target("news", event.unified_msg_origin)
        yield event.plain_result("已绑定当前会话到 AI 行业简报推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pulse.command("bind")
    async def pulse_bind(self, event: AstrMessageEvent):
        """将当前会话同时绑定到两个定时推送。"""
        self._add_target("epic", event.unified_msg_origin)
        self._add_target("news", event.unified_msg_origin)
        yield event.plain_result("已绑定当前会话到 Epic 和 AI 行业简报推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pulse.command("unbind_epic")
    async def pulse_unbind_epic(self, event: AstrMessageEvent):
        """取消当前会话的 Epic 定时推送。"""
        self._remove_target("epic", event.unified_msg_origin)
        yield event.plain_result("已取消当前会话的 Epic 免费游戏推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pulse.command("unbind_news")
    async def pulse_unbind_news(self, event: AstrMessageEvent):
        """取消当前会话的 AI 简报定时推送。"""
        self._remove_target("news", event.unified_msg_origin)
        yield event.plain_result("已取消当前会话的 AI 行业简报推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pulse.command("unbind")
    async def pulse_unbind(self, event: AstrMessageEvent):
        """取消当前会话的全部 Pulse 定时推送。"""
        self._remove_target("epic", event.unified_msg_origin)
        self._remove_target("news", event.unified_msg_origin)
        yield event.plain_result("已取消当前会话的全部 Pulse 推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pulse.command("targets")
    async def pulse_targets(self, event: AstrMessageEvent):
        """查看推送目标。"""
        epic_targets = self._target_sessions("epic")
        news_targets = self._target_sessions("news")
        text = (
            "Pulse 推送目标\n"
            f"Epic 免费游戏 ({len(epic_targets)}):\n{self._format_target_list(epic_targets)}\n\n"
            f"AI 行业简报 ({len(news_targets)}):\n{self._format_target_list(news_targets)}"
        )
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @pulse.command("providers")
    async def pulse_providers(self, event: AstrMessageEvent):
        """查看当前 AstrBot 已配置的 LLM Provider。"""
        configured_ids = self._configured_news_provider_ids()
        providers = await self._all_llm_providers()
        if not providers:
            yield event.plain_result("未读取到已配置的 LLM Provider。")
            return

        lines = ["当前已配置的 LLM Provider："]
        for index, provider in enumerate(providers, start=1):
            provider_id = self._provider_attr(provider, "id", "provider_id", "provider")
            provider_name = self._provider_attr(
                provider,
                "name",
                "provider_name",
                "display_name",
                "model_name",
            )
            marker = " *" if provider_id in configured_ids else ""
            if provider_name and provider_name != provider_id:
                lines.append(f"{index}. {provider_id} ({provider_name}){marker}")
            else:
                lines.append(f"{index}. {provider_id}{marker}")

        lines.append("")
        lines.append("带 * 的 Provider 已配置为 AI 简报专用 Provider。")
        lines.append("在插件配置 news_llm_provider_ids 中可多选；留空时使用当前会话默认 Provider。")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        for task in self._tasks:
            if not task.done():
                task.cancel()

        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self._epic_client.aclose()
        await self._news_client.aclose()
        await self._halo_client.aclose()
        logger.info("Pulse plugin stopped")

    async def _scheduled_loop(
        self,
        job_name: str,
        time_key: str,
        push_func: Callable[[], Awaitable[None]],
    ):
        while True:
            try:
                seconds = self._seconds_until_next_run(time_key)
                logger.info(f"Pulse {job_name} next run in {seconds:.0f}s")
                await asyncio.sleep(seconds)
                await push_func()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"Pulse {job_name} scheduled task failed: {exc}", exc_info=True)
                await asyncio.sleep(60)

    async def _push_epic_to_targets(self):
        await self._push_to_targets(
            job_name="epic",
            targets=self._target_sessions("epic"),
            chain_factory=lambda umo: self._build_epic_chain(),
            build_once=True,
        )

    async def _push_news_to_targets(self):
        await self._push_to_targets(
            job_name="news",
            targets=self._target_sessions("news"),
            chain_factory=self._build_news_chain,
            build_once=True,
        )

    async def _push_to_targets(
        self,
        job_name: str,
        targets: list[str],
        chain_factory: Callable[[str], Awaitable[list | tuple[list, str]]],
        *,
        build_once: bool = False,
    ):
        if not targets:
            logger.warning(f"Pulse {job_name} has no targets; skip push")
            return

        logger.info(f"Pulse {job_name} pushing to {len(targets)} target(s)")
        prebuilt_result: list | tuple[list, str] | None = None
        if build_once:
            try:
                prebuilt_result = await chain_factory(targets[0])
            except Exception as exc:
                logger.error(f"Pulse {job_name} build failed: {exc}", exc_info=True)
                return

        for index, unified_msg_origin in enumerate(targets):
            if index > 0:
                delay = self._next_push_delay()
                logger.info(f"Pulse {job_name} waits {delay:.1f}s before next target")
                await asyncio.sleep(delay)

            try:
                result = prebuilt_result if prebuilt_result is not None else await chain_factory(unified_msg_origin)
                if isinstance(result, tuple):
                    chain, publish_message = result
                else:
                    chain = result
                    publish_message = ""
                await self.context.send_message(
                    unified_msg_origin,
                    self._message_chain(chain),
                )
                if publish_message:
                    await self.context.send_message(
                        unified_msg_origin,
                        self._message_chain([Comp.Plain(publish_message)]),
                    )
            except Exception as exc:
                logger.error(
                    f"Pulse {job_name} push failed for {unified_msg_origin}: {exc}",
                    exc_info=True,
                )

    def _message_chain(self, components: list) -> MessageChain:
        chain = MessageChain()
        if hasattr(chain, "chain"):
            chain.chain.extend(components)
        else:
            chain.chain = list(components)
        return chain

    async def _build_epic_chain(self) -> list:
        now = datetime.now(self._timezone())
        games = await self._fetch_epic_games(now)
        try:
            image_url = await self._render_epic_image_url(games, now)
            return [Comp.Image.fromURL(image_url)]
        except Exception as exc:
            logger.error(f"Pulse failed to render Epic image: {exc}", exc_info=True)
            return self._format_epic_text(games, now)

    async def _build_news_chain(self, unified_msg_origin: str) -> tuple[list, str]:
        now = datetime.now(self._timezone())
        report, publish_message, card = await self._fetch_ai_news(unified_msg_origin)
        try:
            image_url = await self._render_news_image_url(card, now)
            return [Comp.Image.fromURL(image_url)], publish_message
        except Exception as exc:
            logger.error(f"Pulse failed to render AI news image: {exc}", exc_info=True)
            chain = self._format_news_text(report, now)
            return chain, publish_message

    async def _fetch_epic_games(self, now: datetime) -> list[EpicFreeGame]:
        try:
            return await self._epic_client.fetch_free_games(now)
        except Exception as exc:
            logger.error(f"Pulse failed to fetch Epic games: {exc}", exc_info=True)
            return []

    async def _fetch_ai_news(self, unified_msg_origin: str) -> tuple[str, str, NewsCardDraft]:
        endpoint = str(self.config.get("news_endpoint", "")).strip()
        if not endpoint:
            message = "未配置 AI 新闻聚合接口。"
            return message, "", self._fallback_news_card(message)

        try:
            bearer_token = str(self.config.get("news_bearer_token", "")).strip()
            items = await self._news_client.fetch_aggregated_items(
                endpoint,
                bearer_token=bearer_token,
            )
            if not items:
                source_text = await self._news_client.fetch_aggregated_text(
                    endpoint,
                    bearer_token=bearer_token,
                )
                items = self._fallback_text_to_items(source_text)

            qq_brief, article, card = await self._synthesize_news_outputs(
                items,
                unified_msg_origin,
            )
            local_article = await self._build_article_markdown(article)
            saved_path = self._save_news_markdown(local_article)
            logger.info(f"Pulse AI news markdown saved: {saved_path}")
            publish_message = await self._publish_news_to_halo(local_article)
            return qq_brief, publish_message, card
        except NewsSynthesisError as exc:
            logger.error(f"Pulse 生成 AI 新闻简报失败: {exc}", exc_info=True)
            message = "AI 新闻简报生成失败。"
            return message, "", self._fallback_news_card(message)
        except Exception as exc:
            logger.error(f"Pulse 获取 AI 新闻源失败: {exc}", exc_info=True)
            message = "AI 新闻源获取失败，请检查聚合接口配置。"
            return message, "", self._fallback_news_card(message)

    async def _synthesize_news_outputs(
        self,
        items: list[NewsItem],
        unified_msg_origin: str,
    ) -> tuple[str, NewsArticleDraft, NewsCardDraft]:
        max_items = self._config_int("news_max_items", 15)
        selected_items = items[: max(1, max_items)]
        payload = self._news_items_payload(selected_items)
        prompt = (
            "Here is today's raw news data in JSON format:\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )
        output = await self._llm_text(
            unified_msg_origin=unified_msg_origin,
            prompt=prompt,
            system_prompt=self._news_system_prompt(),
        )
        qq_brief = self._strip_leading_icons(
            self._extract_tagged_section(output, QQ_BRIEF_START, QQ_BRIEF_END)
        )
        title = self._clean_single_line(
            self._extract_tagged_section(output, ARTICLE_TITLE_START, ARTICLE_TITLE_END)
        )
        excerpt = self._clean_single_line(
            self._extract_tagged_section(
                output,
                ARTICLE_EXCERPT_START,
                ARTICLE_EXCERPT_END,
            )
        )
        tags = self._parse_generated_tags(
            self._extract_tagged_section(output, ARTICLE_TAGS_START, ARTICLE_TAGS_END)
        )
        try:
            card = self._parse_news_card(
                self._extract_tagged_section(output, NEWS_CARD_START, NEWS_CARD_END)
            )
        except NewsSynthesisError as exc:
            logger.warning(f"Pulse news card fallback: {exc}")
            card = self._fallback_news_card(qq_brief)
        body = self._strip_first_heading(
            self._extract_tagged_section(
                output,
                LOCAL_ARTICLE_START,
                LOCAL_ARTICLE_END,
            )
        )
        if not title:
            raise NewsSynthesisError("LLM 未输出文章标题")
        if not excerpt:
            raise NewsSynthesisError("LLM 未输出文章摘要")
        if not body:
            raise NewsSynthesisError("LLM 未输出文章正文")
        article = NewsArticleDraft(
            title=title,
            excerpt=excerpt,
            tags=tags,
            body=body,
        )
        return qq_brief, article, card

    def _news_system_prompt(self) -> str:
        excerpt_min = self._config_int("halo_excerpt_min_chars", 120)
        excerpt_max = self._config_int("halo_excerpt_max_chars", 180)
        if excerpt_min < 20:
            excerpt_min = 20
        if excerpt_max < excerpt_min:
            excerpt_max = excerpt_min

        statement_rule = (
            "文章末尾必须包含“## 声明”板块，并写明“本文由 AI 基于公开资讯自动生成，仅供信息参考，不构成投资、法律或技术决策建议。”"
            if self._config_bool("article_statement_enabled", True)
            else "文章末尾不要输出“声明”板块，也不要写 AI 生成声明。"
        )

        return f"""
你是资深 AI 科技媒体主编、产业分析师和信息架构师。
我会提供一个 JSON 数组，包含今日最多 15 条 AI 新闻、模型发布、论文和评测资讯。

你必须基于这些材料一次性生成六个结果，并严格使用以下标签分隔：
{QQ_BRIEF_START}/{QQ_BRIEF_END}
{ARTICLE_TITLE_START}/{ARTICLE_TITLE_END}
{ARTICLE_EXCERPT_START}/{ARTICLE_EXCERPT_END}
{ARTICLE_TAGS_START}/{ARTICLE_TAGS_END}
{NEWS_CARD_START}/{NEWS_CARD_END}
{LOCAL_ARTICLE_START}/{LOCAL_ARTICLE_END}

任务一：QQ群推送简报
- 面向移动端快速阅读。
- 只输出 5-7 行，每行是一条独立、锋利、可扫读的一句话。
- 不要在每行开头添加 emoji、图标、项目符号或装饰字符。
- 不要贴原始 URL。

任务二：网站文章元数据
- 标题只输出在 {ARTICLE_TITLE_START}/{ARTICLE_TITLE_END} 中，不要在正文里写一级标题。
- 摘要只输出在 {ARTICLE_EXCERPT_START}/{ARTICLE_EXCERPT_END} 中，长度控制在 {excerpt_min}-{excerpt_max} 个中文字符之间。
- 标签只输出在 {ARTICLE_TAGS_START}/{ARTICLE_TAGS_END} 中，每行一个标签，输出 2-5 个中文或英文标签；不要带 #、逗号或项目符号。

任务三：QQ 图片新闻卡片结构化数据
- 只输出在 {NEWS_CARD_START}/{NEWS_CARD_END} 中，内容必须是合法 JSON，不要使用 Markdown 代码块。
- JSON 顶层对象包含 subtitle 和 items。
- subtitle 是对今日资讯的一句话锐评，必须以“吾的评价是”开头，长度 24-45 个中文字符，语气可以锐利但不要低俗。
- items 输出 6 条；每条对应一个独立资讯来源或主题聚合，不要把多条新闻混成一条。
- 每个 item 必须包含 category、source、title、summary 四个字段。
- category 是 2-6 个字的主题标签，例如 Agent、模型、OCR、视频、论文、开源、芯片、融资。
- source 是媒体、机构、论文方向或场景来源，8 个字以内；没有明确媒体时写主题方向。
- title 是该条资讯的中文短标题，18-32 个中文字符。
- summary 是该条资讯的独立总结，45-85 个中文字符；只讲这一条资讯，不要串联其他条。

任务四：本地网站 Markdown 正文
1. 正文严禁出现一级标题，也不要输出 YAML front matter；正文可以使用 Markdown 二级标题和三级标题。
2. 严禁逐条罗列，必须把全部材料融合成一篇前后呼应的中文科技综述文章。
3. 每一段在叙述事实的同时，都要自然织入行业透视和深度点评，分析篇幅至少占全文三分之一。
4. 正文每个自然段开头使用两个全角空格缩进。
5. 正文中可以提及媒体名、机构名、论文名或公司名，但不要直接粘贴 URL，也不要出现脚注标记。
6. 文章末尾必须包含“## 参考文献”板块，用 Markdown 链接列出材料来源，格式为：- [媒体名称: 文章标题](URL)。
7. {statement_rule}
8. 正文控制在 1500-2200 个中文字符，内容必须紧凑、有判断，不要填充空话。

严格输出结构：
{QQ_BRIEF_START}
一句话简报
另一条重要趋势
{QQ_BRIEF_END}

{ARTICLE_TITLE_START}
动态生成的科技媒体标题
{ARTICLE_TITLE_END}

{ARTICLE_EXCERPT_START}
摘要内容
{ARTICLE_EXCERPT_END}

{ARTICLE_TAGS_START}
AI
大模型
产业观察
{ARTICLE_TAGS_END}

{NEWS_CARD_START}
{{"subtitle":"吾的评价是今日 AI 竞争正在从模型表演转向证据、记忆和工作流控制。","items":[{{"category":"Agent","source":"企业协作","title":"AI Agent 正进入企业基础设施","summary":"Anthropic 和 MoEngage 的动作说明，AI 入口正在从单次问答转向协作语境、客户触点和可执行工作流。"}}]}}
{NEWS_CARD_END}

{LOCAL_ARTICLE_START}
　　正文自然段...

## 自动生成的分析维度标题

　　正文自然段...

## 参考文献

- [媒体名称: 文章标题](https://example.com)
{LOCAL_ARTICLE_END}
""".strip()

    async def _build_article_markdown(self, article: NewsArticleDraft) -> str:
        categories = self._config_string_list("halo_publish_categories")
        configured_tags = self._config_string_list("halo_publish_tags")
        tags = configured_tags or article.tags
        front_matter = await self._article_front_matter(
            title=article.title,
            excerpt=article.excerpt,
            categories=categories,
            tags=tags,
        )
        body = self._strip_first_heading(article.body).strip()
        return f"{front_matter}\n\n{body}\n"

    async def _article_front_matter(
        self,
        *,
        title: str,
        excerpt: str,
        categories: list[str],
        tags: list[str],
    ) -> str:
        author = str(self.config.get("halo_article_author", "")).strip()
        cover = await self._halo_client.resolve_cover(
            str(self.config.get("halo_article_cover", "")).strip()
        )
        lines = [
            "---",
            f"title: {self._yaml_scalar(title)}",
            f"author: {self._yaml_scalar(author)}",
            f"cover: {self._yaml_scalar(cover)}",
            f"excerpt: {self._yaml_scalar(excerpt)}",
            "categories:",
        ]
        lines.extend(f" - {self._yaml_scalar(category)}" for category in categories)
        lines.append("tags:")
        lines.extend(f" - {self._yaml_scalar(tag)}" for tag in tags)
        lines.append("---")
        return "\n".join(lines)

    def _parse_generated_tags(self, value: str) -> list[str]:
        tags: list[str] = []
        for line in value.replace(",", "\n").replace("，", "\n").splitlines():
            tag = line.strip().lstrip("-*#").strip()
            if tag and tag not in tags:
                tags.append(tag)
        return tags[:5]

    def _parse_news_card(self, value: str) -> NewsCardDraft:
        try:
            data = json.loads(value.strip())
        except json.JSONDecodeError as exc:
            raise NewsSynthesisError("LLM 输出的新闻卡片 JSON 无效") from exc

        if not isinstance(data, dict):
            raise NewsSynthesisError("LLM 输出的新闻卡片不是 JSON 对象")

        subtitle = self._clean_card_text(str(data.get("subtitle") or ""), limit=60)
        if not subtitle:
            subtitle = "吾的评价是今日 AI 行业仍在把模型能力压进真实业务流程。"
        if not subtitle.startswith("吾的评价是"):
            subtitle = f"吾的评价是{subtitle}"

        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise NewsSynthesisError("LLM 输出的新闻卡片 items 不是数组")

        items: list[NewsCardItem] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            category = self._clean_card_text(str(raw_item.get("category") or "AI"), limit=12)
            source = self._clean_card_text(str(raw_item.get("source") or "综合资讯"), limit=16)
            title = self._clean_card_text(str(raw_item.get("title") or ""), limit=40)
            summary = self._clean_card_text(str(raw_item.get("summary") or ""), limit=110)
            if not title or not summary:
                continue
            items.append(
                NewsCardItem(
                    category=category or "AI",
                    source=source or "综合资讯",
                    title=title,
                    summary=summary,
                )
            )
            if len(items) >= 6:
                break

        if not items:
            raise NewsSynthesisError("LLM 输出的新闻卡片条目为空")

        return NewsCardDraft(subtitle=subtitle, items=items)

    def _fallback_news_card(self, report: str) -> NewsCardDraft:
        lines = [line.strip() for line in report.splitlines() if line.strip()]
        items: list[NewsCardItem] = []
        for line in lines[:6]:
            text = self._clean_card_text(line, limit=120)
            if not text:
                continue
            title = text[:32].rstrip("，。；、 ")
            items.append(
                NewsCardItem(
                    category="AI",
                    source="综合资讯",
                    title=title or "AI 新闻简报",
                    summary=text,
                )
            )
        if not items:
            items.append(
                NewsCardItem(
                    category="AI",
                    source="系统消息",
                    title="AI 新闻简报暂无内容",
                    summary="当前没有可用于生成图片卡片的新闻内容，请稍后重试或检查新闻聚合接口。",
                )
            )
        return NewsCardDraft(
            subtitle="吾的评价是今日资讯暂未成形，先把信息源检查清楚。",
            items=items,
        )

    def _clean_card_text(self, value: str, *, limit: int) -> str:
        text = re.sub(r"\s+", " ", value).strip()
        text = text.strip("`*_#- ")
        if len(text) > limit:
            return text[:limit].rstrip("，。；、 ") + "…"
        return text

    def _strip_first_heading(self, markdown_text: str) -> str:
        lines: list[str] = []
        for line in markdown_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def _clean_single_line(self, value: str) -> str:
        return re.sub(r"\s+", " ", value.strip()).strip(" -#")

    def _yaml_scalar(self, value: str) -> str:
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        if not text:
            return ""
        if re.search(r"[:#\[\]{},&*!|>'\"%@`]|^[-?]", text):
            return json.dumps(text, ensure_ascii=False)
        return text

    def _front_matter_text(self, markdown_text: str) -> str:
        text = markdown_text.lstrip()
        if not text.startswith("---"):
            return ""
        match = re.match(r"^---\s*\n(.*?)\n---\s*", text, flags=re.DOTALL)
        return match.group(1) if match else ""

    def _front_matter_value(self, markdown_text: str, key: str) -> str:
        front_matter = self._front_matter_text(markdown_text)
        if not front_matter:
            return ""
        match = re.search(rf"^{re.escape(key)}:\s*(.*)$", front_matter, flags=re.MULTILINE)
        if not match:
            return ""
        return self._unquote_yaml_scalar(match.group(1).strip())

    def _front_matter_list(self, markdown_text: str, key: str) -> list[str]:
        front_matter = self._front_matter_text(markdown_text)
        if not front_matter:
            return []
        pattern = rf"^{re.escape(key)}:\s*\n((?:\s+-\s+.*\n?)*)"
        match = re.search(pattern, front_matter, flags=re.MULTILINE)
        if not match:
            return []
        values: list[str] = []
        for line in match.group(1).splitlines():
            item = re.sub(r"^\s+-\s+", "", line).strip()
            if item:
                values.append(self._unquote_yaml_scalar(item))
        return values

    def _unquote_yaml_scalar(self, value: str) -> str:
        if not value:
            return ""
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                return str(json.loads(value))
            except json.JSONDecodeError:
                return value[1:-1]
        return value

    async def _llm_text(
        self,
        unified_msg_origin: str,
        prompt: str,
        system_prompt: str,
    ) -> str:
        provider_ids = self._configured_news_provider_ids()
        if provider_ids:
            llm_resp = await self._llm_text_with_configured_providers(
                provider_ids=provider_ids,
                prompt=prompt,
                system_prompt=system_prompt,
            )
        else:
            llm_resp = await self._llm_text_with_default_provider(
                unified_msg_origin=unified_msg_origin,
                prompt=prompt,
                system_prompt=system_prompt,
            )

        completion = getattr(llm_resp, "completion_text", "") or ""
        completion = completion.strip()
        if not completion:
            raise NewsSynthesisError("LLM 返回为空")
        return completion

    async def _llm_text_with_configured_providers(
        self,
        provider_ids: list[str],
        prompt: str,
        system_prompt: str,
    ):
        errors: list[str] = []

        if len(provider_ids) == 1:
            provider_id = provider_ids[0]
            try:
                return await self._call_provider_by_id(provider_id, prompt, system_prompt)
            except Exception as exc:
                errors.append(f"{provider_id}: {exc}")
                logger.warning(
                    f"Pulse LLM Provider {provider_id} failed, retry in 10s: {exc}",
                    exc_info=True,
                )
                await asyncio.sleep(10)
                try:
                    return await self._call_provider_by_id(provider_id, prompt, system_prompt)
                except Exception as retry_exc:
                    errors.append(f"{provider_id} retry: {retry_exc}")
                    raise NewsSynthesisError(
                        "AI 简报专用 LLM Provider 调用失败: " + " | ".join(errors)
                    ) from retry_exc

        for provider_id in provider_ids:
            try:
                return await self._call_provider_by_id(provider_id, prompt, system_prompt)
            except Exception as exc:
                errors.append(f"{provider_id}: {exc}")
                logger.warning(
                    f"Pulse LLM Provider {provider_id} failed, switch to next: {exc}",
                    exc_info=True,
                )

        raise NewsSynthesisError(
            "所有 AI 简报专用 LLM Provider 均调用失败: " + " | ".join(errors)
        )

    async def _llm_text_with_default_provider(
        self,
        unified_msg_origin: str,
        prompt: str,
        system_prompt: str,
    ):
        try:
            get_provider = getattr(self.context, "get_using_provider", None)
            provider = get_provider(umo=unified_msg_origin) if get_provider else None
            if provider:
                return await provider.text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                )

            provider_id = await self.context.get_current_chat_provider_id(
                umo=unified_msg_origin
            )
            if not provider_id:
                raise NewsSynthesisError("当前会话没有可用的 LLM Provider")
            return await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"系统指令：{system_prompt}\n\n{prompt}",
            )
        except NewsSynthesisError:
            raise
        except Exception as exc:
            raise NewsSynthesisError(f"默认 LLM Provider 调用失败: {exc}") from exc

    async def _call_provider_by_id(
        self,
        provider_id: str,
        prompt: str,
        system_prompt: str,
    ):
        provider = await self._llm_provider_by_id(provider_id)
        if not provider:
            raise NewsSynthesisError(f"未找到配置的 LLM Provider: {provider_id}")
        return await provider.text_chat(
            prompt=prompt,
            system_prompt=system_prompt,
        )

    def _configured_news_provider_ids(self) -> list[str]:
        values: list[str] = []

        raw_multi = self.config.get("news_llm_provider_ids", [])
        if isinstance(raw_multi, list):
            values.extend(str(item).strip() for item in raw_multi)
        elif isinstance(raw_multi, str):
            values.extend(self._split_provider_ids(raw_multi))

        deduped: list[str] = []
        for value in values:
            if value and value not in deduped:
                deduped.append(value)
        return deduped

    def _split_provider_ids(self, value: str) -> list[str]:
        return [
            item.strip()
            for item in re.split(r"[,，\n;；]+", value)
            if item.strip()
        ]

    async def _llm_provider_by_id(self, provider_id: str):
        get_provider = getattr(self.context, "get_provider_by_id", None)
        if not get_provider:
            return None
        provider = get_provider(provider_id=provider_id)
        if inspect.isawaitable(provider):
            provider = await provider
        return provider

    async def _all_llm_providers(self) -> list:
        get_all = getattr(self.context, "get_all_providers", None)
        if not get_all:
            return []
        providers = get_all()
        if inspect.isawaitable(providers):
            providers = await providers
        if isinstance(providers, dict):
            return [
                {"id": str(provider_id), "provider": provider}
                for provider_id, provider in providers.items()
            ]
        if isinstance(providers, list):
            return providers
        if isinstance(providers, tuple):
            return list(providers)
        return []

    def _provider_attr(self, provider, *names: str) -> str:
        wrapped_provider = None
        if isinstance(provider, dict) and "provider" in provider:
            wrapped_provider = provider["provider"]

        for name in names:
            value = ""
            if isinstance(provider, dict):
                value = provider.get(name, "")
            else:
                value = getattr(provider, name, "")
            if not value and wrapped_provider is not None:
                if isinstance(wrapped_provider, dict):
                    value = wrapped_provider.get(name, "")
                else:
                    value = getattr(wrapped_provider, name, "")
            if value:
                return str(value)
        return str(wrapped_provider if wrapped_provider is not None else provider)

    def _news_items_payload(self, items: list[NewsItem]) -> list[dict[str, str]]:
        return [
            {
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "summary": item.summary,
            }
            for item in items
        ]

    def _fallback_text_to_items(self, source_text: str) -> list[NewsItem]:
        text = source_text.strip()
        if not text:
            raise NewsSynthesisError("新闻源返回为空")
        return [
            NewsItem(
                title="聚合资讯",
                url="",
                source="Aggregator",
                summary=text[:24000],
            )
        ]

    def _extract_tagged_section(self, value: str, start_tag: str, end_tag: str) -> str:
        pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
        match = re.search(pattern, value, flags=re.DOTALL)
        if not match:
            raise NewsSynthesisError(f"LLM 未按要求输出标签 {start_tag}")
        section = match.group(1).strip()
        if not section:
            raise NewsSynthesisError(f"LLM 输出标签 {start_tag} 内容为空")
        return section

    def _strip_leading_icons(self, value: str) -> str:
        cleaned_lines: list[str] = []
        for line in value.splitlines():
            stripped = line.strip()
            stripped = re.sub(
                r"^[\s\-\*\+•·●■◆◇▶▷▸▹☞✓✔★☆#]+",
                "",
                stripped,
            ).strip()
            stripped = re.sub(
                r"^[\U0001F000-\U0001FAFF\u2600-\u27BF]+\s*",
                "",
                stripped,
            ).strip()
            if stripped:
                cleaned_lines.append(stripped)
        return "\n".join(cleaned_lines)

    def _save_news_markdown(self, markdown_text: str) -> Path:
        self._plugin_data_dir.mkdir(parents=True, exist_ok=True)
        date_text = datetime.now(self._timezone()).strftime("%Y-%m-%d")
        path = self._plugin_data_dir / f"ai-news-{date_text}.md"
        path.write_text(markdown_text, encoding="utf-8")
        return path

    async def _publish_latest_news_to_halo(self) -> str:
        latest_path = self._latest_news_markdown_path()
        if not latest_path:
            return "未找到本地 AI 长文 Markdown，请先执行 /pulse news 生成文章。"

        markdown_text = latest_path.read_text(encoding="utf-8").strip()
        if not markdown_text:
            return f"本地 Markdown 为空：{latest_path}"

        result = await self._publish_news_to_halo(markdown_text, force=True)
        return result or "Halo 发布未启用或配置不完整。"

    async def _publish_news_to_halo(
        self,
        markdown_text: str,
        *,
        force: bool = False,
    ) -> str:
        if not force and not self._config_bool("halo_publish_enabled", False):
            return ""

        site_url = str(self.config.get("halo_site_url", "")).strip()
        token = str(self.config.get("halo_syncpost_token", "")).strip()
        if not site_url or not token:
            message = "Halo 发布配置不完整：请填写 halo_site_url 和 halo_syncpost_token。"
            if force:
                return message
            logger.warning(f"Pulse {message}")
            return ""

        now = datetime.now(self._timezone())
        slug = self._halo_article_slug(now)
        publish = self._config_bool("halo_publish_direct", True)

        try:
            result = await self._halo_client.publish_markdown(
                site_url,
                token,
                markdown_text,
                slug=slug,
                publish=publish,
            )
        except HaloPublishError as exc:
            message = f"Halo 发布失败：{exc}"
            logger.error(f"Pulse {message}", exc_info=True)
            return message if force else ""
        except Exception as exc:
            message = f"Halo 发布异常：{exc}"
            logger.error(f"Pulse {message}", exc_info=True)
            return message if force else ""

        status = result.status or ("published" if publish else "draft")
        article_name = result.article_name or slug
        article_url = result.article_url or self._halo_article_url(site_url, article_name)
        if article_url:
            message = f"文章已推送到：{article_url}"
        else:
            message = f"Halo 发布成功：{article_name} ({status})"
        logger.info(f"Pulse {message}")
        return message

    def _latest_news_markdown_path(self) -> Path | None:
        if not self._plugin_data_dir.exists():
            return None
        paths = sorted(self._plugin_data_dir.glob("ai-news-*.md"), reverse=True)
        return paths[0] if paths else None

    def _halo_article_slug(self, now: datetime) -> str:
        prefix = str(self.config.get("halo_slug_prefix", "ai-news")).strip()
        prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", prefix).strip("-")
        if not prefix:
            prefix = "ai-news"
        return f"{prefix}-{now:%Y%m%d}"

    def _halo_article_url(self, site_url: str, article_name: str) -> str:
        name = article_name.strip().strip("/")
        if not name:
            return ""
        base_url = site_url.strip().rstrip("/")
        if base_url.endswith(SYNCPOST_PATH):
            base_url = base_url[: -len(SYNCPOST_PATH)].rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            return ""
        return f"{base_url}/archives/{name}"

    def _extract_markdown_title(self, markdown_text: str) -> str:
        for line in markdown_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                return stripped[2:].strip()
        return ""

    def _markdown_excerpt(self, markdown_text: str, max_chars: int = 160) -> str:
        for line in markdown_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped)
            stripped = re.sub(r"[*_`>#-]+", "", stripped).strip()
            if stripped:
                return stripped[:max_chars]
        return ""

    async def _render_epic_image_url(
        self,
        games: list[EpicFreeGame],
        now: datetime,
    ) -> str:
        max_items = self._config_int("epic_max_items", 6)
        include_images = self._config_bool("send_epic_images", True)
        data = {
            "date": now.strftime("%Y-%m-%d"),
            "games": [
                {
                    "title": html.escape(game.title),
                    "image_url": self._safe_image_url(game.image_url)
                    if include_images
                    else "",
                    "end_date": html.escape(game.end_date.strftime("%Y-%m-%d %H:%M UTC")),
                }
                for game in games[:max_items]
            ],
            "sticker_look": self._asset_data_uri("imgs", "看这里.png"),
            "sticker_game": self._asset_data_uri("imgs", "玩游戏.png"),
            "sticker_lay": self._asset_data_uri("imgs", "趴着.png"),
        }
        return await self.html_render(
            EPIC_HTML_TEMPLATE,
            data,
            options=HTML_RENDER_OPTIONS,
        )

    async def _render_news_image_url(self, card: NewsCardDraft, now: datetime) -> str:
        data = {
            "date": now.strftime("%Y-%m-%d"),
            "subtitle": html.escape(card.subtitle),
            "items": [
                {
                    "category": html.escape(item.category),
                    "source": html.escape(item.source),
                    "title": html.escape(item.title),
                    "summary": html.escape(item.summary),
                }
                for item in card.items[:6]
            ],
            "sticker_happy": self._asset_data_uri("imgs", "开心.png"),
            "sticker_lay": self._asset_data_uri("imgs", "趴着.png"),
            "sticker_write": self._asset_data_uri("imgs", "写作.png"),
        }
        return await self.html_render(
            NEWS_HTML_TEMPLATE,
            data,
            options=HTML_RENDER_OPTIONS,
        )

    def _asset_data_uri(self, *parts: str) -> str:
        path = Path(__file__).resolve().parent.joinpath(*parts)
        if not path.exists():
            logger.warning(f"Pulse image asset not found: {path}")
            return ""
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _format_epic_text(self, games: list[EpicFreeGame], now: datetime) -> list:
        if not games:
            return [
                Comp.Plain(
                    f"Epic 免费游戏 | {now:%Y-%m-%d}\n\n"
                    "今日未发现正在进行的免费促销。"
                )
            ]

        chain: list = [Comp.Plain(f"Epic 免费游戏 | {now:%Y-%m-%d}\n\n")]
        max_items = self._config_int("epic_max_items", 6)
        for index, game in enumerate(games[:max_items], start=1):
            end_date = game.end_date.strftime("%Y-%m-%d %H:%M %Z")
            chain.append(Comp.Plain(f"{index}. {game.title}\n领取截止：{end_date}\n"))
            if game.image_url and self._config_bool("send_epic_images", True):
                chain.append(Comp.Image.fromURL(game.image_url))
            chain.append(Comp.Plain("\n"))
        return chain

    def _format_news_text(self, report: str, now: datetime) -> list:
        text = report.strip() or "AI 新闻简报暂无内容。"
        return [Comp.Plain(f"AI 行业简报 | {now:%Y-%m-%d}\n\n{text}")]

    def _seconds_until_next_run(self, time_key: str) -> float:
        tz = self._timezone()
        now = datetime.now(tz)
        run_time = self._parse_time(str(self.config.get(time_key, "08:30")))
        next_run = datetime.combine(now.date(), run_time, tzinfo=tz)
        if next_run <= now:
            next_run += timedelta(days=1)
        return max((next_run - now).total_seconds(), 1.0)

    def _parse_time(self, value: str) -> time:
        try:
            hour, minute = value.strip().split(":", 1)
            return time(hour=int(hour), minute=int(minute))
        except Exception:
            logger.warning(f"Invalid Pulse time config: {value}; fallback to 08:30")
            return time(hour=8, minute=30)

    def _timezone(self) -> tzinfo:
        name = str(self.config.get("timezone", "Asia/Shanghai")).strip()
        if ZoneInfo:
            try:
                return ZoneInfo(name)
            except ZoneInfoNotFoundError:
                logger.warning(f"Invalid Pulse timezone: {name}; fallback to Asia/Shanghai")
                return ZoneInfo("Asia/Shanghai")

        if name in ("Asia/Shanghai", "UTC+8", "UTC+08:00"):
            return timezone(timedelta(hours=8), "Asia/Shanghai")
        if name in ("UTC", "Etc/UTC"):
            return timezone.utc

        logger.warning(
            f"Python zoneinfo is unavailable and timezone={name} is unsupported; "
            "fallback to Asia/Shanghai"
        )
        return timezone(timedelta(hours=8), "Asia/Shanghai")

    def _target_sessions(self, kind: str) -> list[str]:
        key = f"{kind}_target_sessions"
        targets = self.config.get(key)
        if not isinstance(targets, list):
            return []
        return [str(item).strip() for item in targets if str(item).strip()]

    def _add_target(self, kind: str, unified_msg_origin: str):
        key = f"{kind}_target_sessions"
        targets = self._target_sessions(kind)
        if unified_msg_origin not in targets:
            targets.append(unified_msg_origin)
            self.config[key] = targets
            self.config.save_config()

    def _remove_target(self, kind: str, unified_msg_origin: str):
        key = f"{kind}_target_sessions"
        targets = [
            item for item in self._target_sessions(kind) if item != unified_msg_origin
        ]
        self.config[key] = targets
        self.config.save_config()

    def _format_target_list(self, targets: list[str]) -> str:
        if not targets:
            return "(无)"
        return "\n".join(targets)

    def _next_push_delay(self) -> float:
        minimum = self._config_float("push_delay_min_seconds", 8.0)
        maximum = self._config_float("push_delay_max_seconds", 25.0)
        if maximum < minimum:
            maximum = minimum
        return random.uniform(minimum, maximum)

    def _safe_image_url(self, value: str) -> str:
        url = str(value).strip()
        if url.startswith("https://") or url.startswith("http://"):
            return html.escape(url, quote=True)
        return ""

    def _markdown_to_html(self, markdown_text: str) -> str:
        text = markdown_text.strip() or "AI 新闻简报暂无内容。"
        lines = text.splitlines()
        html_lines: list[str] = []
        in_list = False
        in_code_block = False

        for raw_line in lines:
            line = raw_line.strip()
            if line.startswith("```"):
                in_code_block = not in_code_block
                continue

            if not line:
                continue

            if in_code_block:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<p>{html.escape(line)}</p>")
                continue

            heading_level = self._heading_level(line)
            if heading_level:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                content = line[heading_level + 1 :].strip()
                html_lines.append(
                    f"<h{heading_level}>{self._inline_markdown(content)}</h{heading_level}>"
                )
                continue

            list_content = self._list_item_content(line)
            if list_content:
                if not in_list:
                    html_lines.append("<ul>")
                    in_list = True
                html_lines.append(f"<li>{self._inline_markdown(list_content)}</li>")
                continue

            if in_list:
                html_lines.append("</ul>")
                in_list = False

            if line.startswith(">"):
                quote = line.lstrip("> ").strip()
                html_lines.append(f"<p>{self._inline_markdown(quote)}</p>")
                continue

            if self._is_table_line(line):
                html_lines.append(
                    f"<p>{self._inline_markdown(self._table_line_text(line))}</p>"
                )
                continue

            if self._is_horizontal_rule(line):
                continue

            html_lines.append(f"<p>{self._inline_markdown(line)}</p>")

        if in_list:
            html_lines.append("</ul>")

        return "".join(html_lines)

    def _list_item_content(self, line: str) -> str:
        bullet_match = re.match(r"^[-*+]\s+(.+)$", line)
        if bullet_match:
            return bullet_match.group(1).strip()

        ordered_match = re.match(r"^\d+[\.)]\s+(.+)$", line)
        if ordered_match:
            return ordered_match.group(1).strip()

        return ""

    def _heading_level(self, line: str) -> int:
        if not line.startswith("#"):
            return 0
        level = len(line) - len(line.lstrip("#"))
        if 1 <= level <= 3 and len(line) > level and line[level] == " ":
            return level
        return 0

    def _inline_markdown(self, value: str) -> str:
        value = self._replace_markdown_links(value)
        escaped = html.escape(value)
        escaped = self._replace_inline_code(escaped)
        return self._replace_strong(escaped)

    def _replace_markdown_links(self, value: str) -> str:
        return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", value)

    def _replace_inline_code(self, value: str) -> str:
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", value)

    def _replace_strong(self, value: str) -> str:
        parts = value.split("**")
        if len(parts) < 3:
            return value

        rebuilt: list[str] = []
        for index, part in enumerate(parts):
            if index % 2 == 1:
                rebuilt.append(f"<strong>{part}</strong>")
            else:
                rebuilt.append(part)
        return "".join(rebuilt)

    def _is_table_line(self, line: str) -> bool:
        return "|" in line and line.count("|") >= 2

    def _table_line_text(self, line: str) -> str:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        cells = [cell for cell in cells if cell and set(cell) != {"-"}]
        return " / ".join(cells)

    def _is_horizontal_rule(self, line: str) -> bool:
        compact = line.replace(" ", "")
        return compact in ("---", "***", "___")

    def _config_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        return value if isinstance(value, bool) else default

    def _config_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _config_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _config_string_list(self, key: str) -> list[str]:
        value = self.config.get(key, [])
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return []
