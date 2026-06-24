from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


class NewsSynthesisError(Exception):
    """Raised when AI news synthesis cannot produce a usable report."""


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    source: str
    summary: str = ""


class AiNewsClient:
    def __init__(self, timeout: float = 30.0, trust_env: bool = False):
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)

    async def aclose(self):
        await self._client.aclose()

    async def fetch_aggregated_text(self, endpoint: str, bearer_token: str = "") -> str:
        payload = await self.fetch_aggregated_payload(endpoint, bearer_token)
        return _json_to_text(payload)

    async def fetch_aggregated_items(
        self,
        endpoint: str,
        bearer_token: str = "",
    ) -> list[NewsItem]:
        payload = await self.fetch_aggregated_payload(endpoint, bearer_token)
        return _json_to_items(payload)

    async def fetch_aggregated_payload(
        self,
        endpoint: str,
        bearer_token: str = "",
    ) -> Any:
        headers = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        response = await self._client.get(endpoint, headers=headers)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()

        text = response.text.strip()
        if not text:
            raise ValueError("News endpoint returned an empty response")
        return text


def _json_to_text(payload: Any) -> str:
    if isinstance(payload, str):
        return _limit_text(payload)

    if isinstance(payload, dict):
        for key in ("text", "content", "data", "items", "articles", "news"):
            value = payload.get(key)
            if value:
                return _json_to_text(value)
        return _limit_text(json.dumps(payload, ensure_ascii=False))

    if isinstance(payload, list):
        lines: list[str] = []
        for index, item in enumerate(payload[:80], start=1):
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("headline") or "").strip()
                summary = str(
                    item.get("summary")
                    or item.get("description")
                    or item.get("content")
                    or ""
                ).strip()
                url = str(item.get("url") or item.get("link") or "").strip()
                parts = [part for part in (title, summary, url) if part]
                if parts:
                    lines.append(f"{index}. " + " | ".join(parts))
            else:
                text = str(item).strip()
                if text:
                    lines.append(f"{index}. {text}")
        text = "\n".join(lines).strip()
        if not text:
            raise ValueError("News endpoint JSON did not contain readable items")
        return _limit_text(text)

    text = str(payload).strip()
    if not text:
        raise ValueError("News endpoint JSON content is empty")
    return _limit_text(text)


def _json_to_items(payload: Any) -> list[NewsItem]:
    if isinstance(payload, dict):
        for key in ("items", "articles", "news", "data"):
            value = payload.get(key)
            if value:
                return _json_to_items(value)
        return []

    if not isinstance(payload, list):
        return []

    items: list[NewsItem] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("headline") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        source = str(item.get("source") or "").strip()
        summary = str(
            item.get("summary")
            or item.get("description")
            or item.get("content")
            or ""
        ).strip()
        if title and url:
            items.append(
                NewsItem(
                    title=title,
                    url=url,
                    source=source or "unknown",
                    summary=summary,
                )
            )
    return items


def _limit_text(text: str, max_chars: int = 24000) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[content truncated]"
