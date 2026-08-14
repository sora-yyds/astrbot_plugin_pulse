from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


ARENA_LEADERBOARD_URLS = (
    "https://arena.ai/leaderboard/code",
    "https://arena.ai/leaderboard/code/webdev",
)

# The leaderboard table is rendered client-side, but the server returns the
# full dataset inside the Next.js RSC (flight) payload when requested with
# the `RSC: 1` header. This works against the official site directly.
ARENA_RSC_HEADERS = {
    "RSC": "1",
    "Accept": "text/x-component",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
}

_ENTRIES_RE = re.compile(r'"entries"\s*:\s*\[')
_VOTE_CUTOFF_RE = re.compile(r'"voteCutoffISOString"\s*:\s*"([^"]+)"')
_TOTAL_VOTES_RE = re.compile(r'"totalVotes"\s*:\s*(\d+)')
_TOTAL_MODELS_RE = re.compile(r'"totalModels"\s*:\s*(\d+)')


@dataclass(frozen=True)
class ArenaModelEntry:
    rank: int
    rank_upper: int
    rank_lower: int
    model: str
    organization: str
    license: str
    rating: float
    rating_upper: float
    rating_lower: float
    votes: int
    input_price: float | None
    output_price: float | None
    context_length: int | None
    model_url: str
    release_type: str


@dataclass(frozen=True)
class ArenaLeaderboard:
    display_title: str
    description: str
    vote_cutoff: datetime | None
    total_votes: int
    total_models: int
    entries: list[ArenaModelEntry]


class ArenaLeaderboardError(Exception):
    """Raised when the arena.ai leaderboard cannot be fetched or parsed."""


class ArenaLeaderboardClient:
    def __init__(self, timeout: float = 30.0, trust_env: bool = False):
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)

    async def aclose(self):
        await self._client.aclose()

    async def fetch_leaderboard(self, url: str | None = None) -> ArenaLeaderboard:
        if not url:
            url = ARENA_LEADERBOARD_URLS[0]

        last_error: Exception | None = None
        candidates = [url] + [candidate for candidate in ARENA_LEADERBOARD_URLS if candidate != url]
        for candidate in candidates:
            try:
                response = await self._client.get(candidate, headers=ARENA_RSC_HEADERS)
                response.raise_for_status()
                return parse_leaderboard_payload(response.text, candidate)
            except (httpx.HTTPError, ValueError, ArenaLeaderboardError) as exc:
                last_error = exc

        raise ArenaLeaderboardError(
            "Arena 排行榜页面全部请求失败"
        ) from last_error


def parse_leaderboard_payload(text: str, source_url: str = "") -> ArenaLeaderboard:
    entries = _extract_entries(text)
    if not entries:
        raise ArenaLeaderboardError(
            f"Arena 排行榜页面未包含模型数据（可能页面结构已变化）: {source_url}"
        )

    display_title = _first_string(text, '"displayTitle"') or _first_string(
        text, '"title"'
    ) or "Arena Leaderboard"
    description = _first_string(text, '"navDescription"') or ""

    vote_cutoff: datetime | None = None
    cutoff_match = _VOTE_CUTOFF_RE.search(text)
    if cutoff_match:
        vote_cutoff = _parse_datetime(cutoff_match.group(1))

    total_votes = _first_int(text, _TOTAL_VOTES_RE)
    total_models = _first_int(text, _TOTAL_MODELS_RE)

    return ArenaLeaderboard(
        display_title=display_title,
        description=description,
        vote_cutoff=vote_cutoff,
        total_votes=total_votes,
        total_models=total_models,
        entries=entries,
    )


def _extract_entries(text: str) -> list[ArenaModelEntry]:
    match = _ENTRIES_RE.search(text)
    if not match:
        return []

    start = match.end()
    depth = 1
    in_string = False
    escaped = False
    end = -1
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index + 1
                break

    if end <= start:
        return []

    try:
        # `start` points just after the opening `[`; include it for json.loads.
        raw_entries = json.loads(text[start - 1 : end])
    except ValueError:
        return []

    if not isinstance(raw_entries, list):
        return []

    entries: list[ArenaModelEntry] = []
    for item in raw_entries:
        entry = _parse_entry(item)
        if entry:
            entries.append(entry)
    return entries


def _parse_entry(item: Any) -> ArenaModelEntry | None:
    if not isinstance(item, dict):
        return None

    model = str(item.get("modelDisplayName") or "").strip()
    if not model:
        return None

    rating = _as_float(item.get("rating"))
    if rating is None:
        return None

    rank = _as_int(item.get("rank"), 0)
    return ArenaModelEntry(
        rank=rank,
        rank_upper=_as_int(item.get("rankUpper"), rank),
        rank_lower=_as_int(item.get("rankLower"), rank),
        model=model,
        organization=str(item.get("modelOrganization") or "").strip(),
        license=str(item.get("license") or "").strip(),
        rating=rating,
        rating_upper=_as_float(item.get("ratingUpper")) or rating,
        rating_lower=_as_float(item.get("ratingLower")) or rating,
        votes=_as_int(item.get("votes"), 0),
        input_price=_as_float(item.get("inputPricePerMillion")),
        output_price=_as_float(item.get("outputPricePerMillion")),
        context_length=_as_int(item.get("contextLength")),
        model_url=str(item.get("modelUrl") or "").strip(),
        release_type=str(item.get("releaseType") or "").strip(),
    )


def _first_string(text: str, key: str) -> str:
    match = re.search(re.escape(key) + r'\s*:\s*"([^"]+)"', text)
    if not match:
        return ""
    value = match.group(1)
    if value in ("$undefined",):
        return ""
    return value


def _first_int(text: str, pattern: re.Pattern) -> int:
    match = pattern.search(text)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except ValueError:
        return None
