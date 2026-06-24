from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


EPIC_FREE_GAMES_URLS = (
    "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions",
    "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions",
    "https://store-site-backend-ecom-prod04.ak.epicgames.com/freeGamesPromotions",
)


@dataclass(frozen=True)
class EpicFreeGame:
    title: str
    image_url: str
    end_date: datetime


class EpicGamesClient:
    def __init__(self, timeout: float = 20.0, trust_env: bool = False):
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)

    async def aclose(self):
        await self._client.aclose()

    async def fetch_free_games(self, now: datetime) -> list[EpicFreeGame]:
        params = {"locale": "zh-CN", "country": "CN", "allowCountries": "CN"}
        response = await self._get_first_available(params)
        payload = response.json()
        elements = (
            payload.get("data", {})
            .get("Catalog", {})
            .get("searchStore", {})
            .get("elements", [])
        )
        if not isinstance(elements, list):
            return []

        now_utc = _ensure_utc(now)
        games: list[EpicFreeGame] = []
        seen_titles: set[str] = set()
        for element in elements:
            game = _parse_current_free_game(element, now_utc)
            if game and game.title not in seen_titles:
                games.append(game)
                seen_titles.add(game.title)

        games.sort(key=lambda item: item.end_date)
        return games

    async def _get_first_available(self, params: dict[str, str]) -> httpx.Response:
        last_error: Exception | None = None
        for url in EPIC_FREE_GAMES_URLS:
            try:
                response = await self._client.get(url, params=params)
                response.raise_for_status()
                return response
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc

        raise RuntimeError("Epic 免费游戏接口全部请求失败") from last_error


def _parse_current_free_game(
    element: dict[str, Any], now_utc: datetime
) -> EpicFreeGame | None:
    title = str(element.get("title") or "").strip()
    if not title:
        return None

    offers = _promotion_offer_items(element)
    for offer in offers:
        start_date = _parse_datetime(offer.get("startDate"))
        end_date = _parse_datetime(offer.get("endDate"))
        if not start_date or not end_date:
            continue
        if not (start_date <= now_utc <= end_date):
            continue
        discount = offer.get("discountSetting", {})
        if not isinstance(discount, dict):
            continue
        if not _is_free_discount(discount):
            continue
        return EpicFreeGame(
            title=title,
            image_url=_select_image_url(element),
            end_date=end_date,
        )
    return None


def _promotion_offer_items(element: dict[str, Any]) -> list[dict[str, Any]]:
    promotions = element.get("promotions") or {}
    if not isinstance(promotions, dict):
        return []

    result: list[dict[str, Any]] = []
    groups = promotions.get("promotionalOffers") or []
    if not isinstance(groups, list):
        return result

    for group in groups:
        if not isinstance(group, dict):
            continue
        offers = group.get("promotionalOffers") or []
        if not isinstance(offers, list):
            continue
        result.extend(offer for offer in offers if isinstance(offer, dict))
    return result


def _is_free_discount(discount: dict[str, Any]) -> bool:
    for key in ("discountValue", "discountPercentage"):
        value = discount.get(key)
        if value is None:
            continue
        try:
            return int(value) == 0
        except (TypeError, ValueError):
            continue
    return False


def _select_image_url(element: dict[str, Any]) -> str:
    images = element.get("keyImages") or []
    if not isinstance(images, list):
        return ""

    preferred_types = ("Thumbnail", "DieselStoreFrontWide")
    for image_type in preferred_types:
        for image in images:
            if not isinstance(image, dict):
                continue
            if image.get("type") == image_type and image.get("url"):
                return str(image["url"])

    for image in images:
        if isinstance(image, dict) and image.get("url"):
            return str(image["url"])
    return ""


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except ValueError:
        return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
