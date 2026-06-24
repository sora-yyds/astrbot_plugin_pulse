from __future__ import annotations

import asyncio
import html
import json
import random
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta, timezone, tzinfo
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import astrbot.api.message_components as Comp

from .services.epic import EpicFreeGame, EpicGamesClient
from .services.news import AiNewsClient, NewsItem, NewsSynthesisError


PLUGIN_NAME = "astrbot_plugin_pulse"

QQ_BRIEF_START = "[QQ_BRIEF_START]"
QQ_BRIEF_END = "[QQ_BRIEF_END]"
LOCAL_ARTICLE_START = "[LOCAL_ARTICLE_START]"
LOCAL_ARTICLE_END = "[LOCAL_ARTICLE_END]"

HTML_RENDER_OPTIONS = {
    "type": "png",
    "full_page": True,
    "animations": "disabled",
    "timeout": 30000,
}

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

EPIC_HTML_TEMPLATE = """
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

NEWS_HTML_TEMPLATE = """
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
        chain = await self._build_news_chain(event.unified_msg_origin)
        yield event.chain_result(chain)

    @pulse.command("now")
    async def pulse_now(self, event: AstrMessageEvent):
        """立即发送 Epic 免费游戏和 AI 行业简报。"""
        chain = []
        chain.extend(await self._build_epic_chain())
        chain.append(Comp.Plain("\n"))
        chain.extend(await self._build_news_chain(event.unified_msg_origin))
        yield event.chain_result(chain)

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
        )

    async def _push_news_to_targets(self):
        await self._push_to_targets(
            job_name="news",
            targets=self._target_sessions("news"),
            chain_factory=self._build_news_chain,
        )

    async def _push_to_targets(
        self,
        job_name: str,
        targets: list[str],
        chain_factory: Callable[[str], Awaitable[list]],
    ):
        if not targets:
            logger.warning(f"Pulse {job_name} has no targets; skip push")
            return

        logger.info(f"Pulse {job_name} pushing to {len(targets)} target(s)")
        for index, unified_msg_origin in enumerate(targets):
            if index > 0:
                delay = self._next_push_delay()
                logger.info(f"Pulse {job_name} waits {delay:.1f}s before next target")
                await asyncio.sleep(delay)

            try:
                chain = await chain_factory(unified_msg_origin)
                await self.context.send_message(unified_msg_origin, chain)
            except Exception as exc:
                logger.error(
                    f"Pulse {job_name} push failed for {unified_msg_origin}: {exc}",
                    exc_info=True,
                )

    async def _build_epic_chain(self) -> list:
        now = datetime.now(self._timezone())
        games = await self._fetch_epic_games(now)
        try:
            image_url = await self._render_epic_image_url(games, now)
            return [Comp.Image.fromURL(image_url)]
        except Exception as exc:
            logger.error(f"Pulse failed to render Epic image: {exc}", exc_info=True)
            return self._format_epic_text(games, now)

    async def _build_news_chain(self, unified_msg_origin: str) -> list:
        now = datetime.now(self._timezone())
        report = await self._fetch_ai_news(unified_msg_origin)
        try:
            image_url = await self._render_news_image_url(report, now)
            return [Comp.Image.fromURL(image_url)]
        except Exception as exc:
            logger.error(f"Pulse failed to render AI news image: {exc}", exc_info=True)
            return self._format_news_text(report, now)

    async def _fetch_epic_games(self, now: datetime) -> list[EpicFreeGame]:
        try:
            return await self._epic_client.fetch_free_games(now)
        except Exception as exc:
            logger.error(f"Pulse failed to fetch Epic games: {exc}", exc_info=True)
            return []

    async def _fetch_ai_news(self, unified_msg_origin: str) -> str:
        endpoint = str(self.config.get("news_endpoint", "")).strip()
        if not endpoint:
            return "未配置 AI 新闻聚合接口。"

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

            qq_brief, local_article = await self._synthesize_news_outputs(
                items,
                unified_msg_origin,
            )
            saved_path = self._save_news_markdown(local_article)
            logger.info(f"Pulse AI news markdown saved: {saved_path}")
            return qq_brief
        except NewsSynthesisError as exc:
            logger.error(f"Pulse 生成 AI 新闻简报失败: {exc}", exc_info=True)
            return "AI 新闻简报生成失败。"
        except Exception as exc:
            logger.error(f"Pulse 获取 AI 新闻源失败: {exc}", exc_info=True)
            return "AI 新闻源获取失败，请检查聚合接口配置。"

    async def _synthesize_news_outputs(
        self,
        items: list[NewsItem],
        unified_msg_origin: str,
    ) -> tuple[str, str]:
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
            system_prompt=NEWS_SYNTHESIS_SYSTEM_PROMPT,
        )
        qq_brief = self._strip_leading_icons(
            self._extract_tagged_section(output, QQ_BRIEF_START, QQ_BRIEF_END)
        )
        local_article = self._extract_tagged_section(
            output,
            LOCAL_ARTICLE_START,
            LOCAL_ARTICLE_END,
        )
        return qq_brief, local_article

    async def _llm_text(
        self,
        unified_msg_origin: str,
        prompt: str,
        system_prompt: str,
    ) -> str:
        get_provider = getattr(self.context, "get_using_provider", None)
        provider = get_provider(umo=unified_msg_origin) if get_provider else None
        if provider:
            llm_resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
            )
        else:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=unified_msg_origin
            )
            if not provider_id:
                raise NewsSynthesisError("当前会话没有可用的 LLM Provider")
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"系统指令：{system_prompt}\n\n{prompt}",
            )

        completion = getattr(llm_resp, "completion_text", "") or ""
        completion = completion.strip()
        if not completion:
            raise NewsSynthesisError("LLM 返回为空")
        return completion

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
        }
        return await self.html_render(
            EPIC_HTML_TEMPLATE,
            data,
            options=HTML_RENDER_OPTIONS,
        )

    async def _render_news_image_url(self, report: str, now: datetime) -> str:
        data = {
            "date": now.strftime("%Y-%m-%d"),
            "report_html": self._markdown_to_html(report),
        }
        return await self.html_render(
            NEWS_HTML_TEMPLATE,
            data,
            options=HTML_RENDER_OPTIONS,
        )

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
