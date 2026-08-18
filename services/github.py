from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from time import monotonic

import httpx


GITHUB_API_BASE = "https://api.github.com"
SEARCH_COMMITS_ENDPOINT = f"{GITHUB_API_BASE}/search/commits"
# 提交搜索接口要求预览 Accept 头（cloak-preview）。
SEARCH_ACCEPT = "application/vnd.github.cloak-preview+json"
SEARCH_PER_PAGE = 100
# 搜索结果最多返回 1000 条（10 页）。
MAX_SEARCH_PAGES = 10

# 无 token 搜索配额 10 次/分钟；带 token 30 次/分钟。留出余量。
_UNAUTH_SEARCH_INTERVAL = 6.8
_AUTH_SEARCH_INTERVAL = 2.5

_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


class GitHubEventsError(Exception):
    """Raised when GitHub data cannot be fetched."""


class GitHubRateLimitError(GitHubEventsError):
    """Raised when the GitHub API rate limit is exhausted."""


class GitHubUserNotFoundError(GitHubEventsError):
    """Raised when the subscribed GitHub username does not exist."""


@dataclass(frozen=True)
class GitHubRepoCount:
    name: str
    commits: int


@dataclass(frozen=True)
class GitHubCommitStats:
    username: str
    commits: int
    repos: list[GitHubRepoCount]
    last_commit: datetime | None
    truncated: bool  # 搜索结果超出 1000 条，仓库列表可能不完整


class GitHubCommitSearchClient:
    """通过 GitHub 提交搜索接口按天统计用户提交数与涉及仓库。

    查询形如 `author:{u} author-date:{start_iso}..{end_iso}`，其中起止时间
    为插件时区当天窗口换算成的 UTC ISO 时间；total_count 即为该窗口内
    精确提交数。仅统计公开仓库提交，无需 token（可选 token 提升配额）。
    """

    def __init__(self, timeout: float = 25.0, trust_env: bool = False):
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)
        # 最近一次响应的搜索配额余量（无鉴权 10/分钟，带 token 30/分钟）。
        self.quota_remaining: int | None = None
        self._last_search_at = 0.0

    async def aclose(self):
        await self._client.aclose()

    async def fetch_commit_stats(
        self,
        username: str,
        window_start: datetime,
        window_end: datetime,
        token: str = "",
    ) -> GitHubCommitStats:
        """统计某用户在 [window_start, window_end] 内的提交数与涉及仓库。

        window_start/window_end 必须带时区；查询按该时区窗口的 UTC 等价区间执行。
        """
        if window_start.tzinfo is None or window_end.tzinfo is None:
            raise ValueError("GitHub commit stats window must be timezone-aware")

        start_iso = window_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = window_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = f"author:{username} author-date:{start_iso}..{end_iso}"

        items: list[dict[str, Any]] = []
        total_count = 0
        for page in range(1, MAX_SEARCH_PAGES + 1):
            response = await self._search(query, page, token)
            payload = response.json()
            if not isinstance(payload, dict):
                raise GitHubEventsError(f"GitHub 提交搜索返回异常数据: {username}")
            if page == 1:
                total_count = int(payload.get("total_count") or 0)
            page_items = payload.get("items")
            if not isinstance(page_items, list) or not page_items:
                break
            items.extend(item for item in page_items if isinstance(item, dict))
            if len(page_items) < SEARCH_PER_PAGE:
                break

        if total_count == 0:
            # 不存在的用户名会让查询退化，必须校验存在性以区分「0 提交」与「用户不存在」。
            if not await self.fetch_user_exists(username, token):
                raise GitHubUserNotFoundError(f"GitHub 用户不存在: {username}")

        repo_counts: dict[str, int] = {}
        last_commit: datetime | None = None
        for item in items:
            repo = item.get("repository")
            commit = item.get("commit")
            if not isinstance(repo, dict) or not isinstance(commit, dict):
                continue
            repo_name = str(repo.get("full_name") or "").strip()
            if repo_name:
                repo_counts[repo_name] = repo_counts.get(repo_name, 0) + 1
            commit_time = _parse_datetime(
                (commit.get("author") or {}).get("date")
            )
            if commit_time and (last_commit is None or commit_time > last_commit):
                last_commit = commit_time

        repos = [
            GitHubRepoCount(name=name, commits=count)
            for name, count in sorted(
                repo_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        return GitHubCommitStats(
            username=username,
            commits=total_count,
            repos=repos,
            last_commit=last_commit,
            truncated=total_count > len(items),
        )

    async def fetch_user_exists(self, username: str, token: str = "") -> bool:
        headers = {"User-Agent": "astrbot-pulse", "Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = await self._client.get(
            f"{GITHUB_API_BASE}/users/{username}",
            headers=headers,
        )
        if response.status_code == 404:
            return False
        if response.status_code in (403, 429):
            self.quota_remaining = 0
            raise GitHubRateLimitError(
                f"GitHub API 限流（校验用户 {username}），"
                "可配置 github_token 提升配额。"
            )
        response.raise_for_status()
        return True

    async def _search(
        self,
        query: str,
        page: int,
        token: str,
    ) -> httpx.Response:
        headers = {"User-Agent": "astrbot-pulse", "Accept": SEARCH_ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        interval = _AUTH_SEARCH_INTERVAL if token else _UNAUTH_SEARCH_INTERVAL
        elapsed = monotonic() - self._last_search_at
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        self._last_search_at = monotonic()

        response = await self._client.get(
            SEARCH_COMMITS_ENDPOINT,
            params={"q": query, "per_page": SEARCH_PER_PAGE, "page": page},
            headers=headers,
        )
        if response.status_code >= 500:
            # 对 5xx 网关异常重试一次（如 GitHub 偶发 504）。
            await asyncio.sleep(2.0)
            self._last_search_at = monotonic()
            response = await self._client.get(
                SEARCH_COMMITS_ENDPOINT,
                params={"q": query, "per_page": SEARCH_PER_PAGE, "page": page},
                headers=headers,
            )

        self._track_quota(response)
        if response.status_code in (403, 429):
            self.quota_remaining = 0
            raise GitHubRateLimitError(
                "GitHub 提交搜索限流，请稍后再试或配置 github_token 提升配额。"
            )
        if response.status_code >= 400:
            raise GitHubEventsError(
                f"GitHub 提交搜索请求失败（HTTP {response.status_code}）"
            )
        return response

    def _track_quota(self, response: httpx.Response):
        remaining = response.headers.get("x-ratelimit-remaining", "")
        if remaining.isdigit():
            self.quota_remaining = int(remaining)


def avatar_url(username: str) -> str:
    """GitHub 头像直链，无需调用 API（返回 302 至 avatars 或直接输出 png）。"""
    return f"https://github.com/{username}.png"


# ---------------------------------------------------------------- snapshots


@dataclass
class GitHubDailySnapshot:
    date: str
    users: list[dict[str, Any]] = field(default_factory=list)

    def stats_for(self, username: str) -> GitHubCommitStats | None:
        for user in self.users:
            if user.get("username") == username:
                # 旧版快照（推送口径）没有 commits 字段，返回 None
                # 让调用方回退实时统计，避免旧数据被当作 0 提交。
                if "commits" not in user:
                    return None
                repos = [
                    GitHubRepoCount(
                        name=str(item.get("name") or ""),
                        commits=int(item.get("commits") or 0),
                    )
                    for item in user.get("repos") or []
                ]
                return GitHubCommitStats(
                    username=username,
                    commits=int(user.get("commits") or 0),
                    repos=repos,
                    last_commit=_parse_datetime(user.get("last_commit")),
                    truncated=bool(user.get("truncated")),
                )
        return None


def snapshot_dir(data_dir: Path) -> Path:
    return data_dir / "github_stats"


def save_daily_snapshot(
    data_dir: Path,
    date_str: str,
    stats_list: list[GitHubCommitStats],
):
    """将当日全部订阅用户（含 0 提交）的统计落盘，供周报聚合。"""
    directory = snapshot_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date_str,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "users": [
            {
                "username": stats.username,
                "commits": stats.commits,
                "repos": [
                    {"name": repo.name, "commits": repo.commits} for repo in stats.repos
                ],
                "last_commit": (
                    stats.last_commit.isoformat(timespec="seconds")
                    if stats.last_commit
                    else None
                ),
                "truncated": stats.truncated,
            }
            for stats in stats_list
        ],
    }
    path = directory / f"{date_str}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_snapshots(data_dir: Path) -> dict[str, GitHubDailySnapshot]:
    """读取全部快照，返回 {date: snapshot}。"""
    directory = snapshot_dir(data_dir)
    result: dict[str, GitHubDailySnapshot] = {}
    if not directory.exists():
        return result
    for path in directory.glob("*.json"):
        match = _DATE_FILE_RE.match(path.name)
        if not match:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        date_str = match.group(1)
        users = payload.get("users") if isinstance(payload, dict) else None
        result[date_str] = GitHubDailySnapshot(
            date=date_str,
            users=list(users) if isinstance(users, list) else [],
        )
    return result


def cleanup_snapshots(data_dir: Path, keep_days: int = 8):
    """删除 keep_days 天以前的快照，避免本地堆积。

    滚动 7 天窗口 + 1 天安全余量，默认保留最近 8 天。
    """
    directory = snapshot_dir(data_dir)
    if not directory.exists():
        return
    cutoff_date = (datetime.now().astimezone() - timedelta(days=keep_days)).strftime(
        "%Y-%m-%d"
    )
    removed = 0
    for path in directory.glob("*.json"):
        if not _DATE_FILE_RE.match(path.name):
            continue
        if path.stem < cutoff_date:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
