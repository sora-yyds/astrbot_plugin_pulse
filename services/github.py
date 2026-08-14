from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


GITHUB_API_BASE = "https://api.github.com"
EVENTS_PER_PAGE = 100
# GitHub 公开事件流只保留每个用户最近 300 条事件（3 页），无法继续翻页。
MAX_EVENT_PAGES = 3

_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


class GitHubEventsError(Exception):
    """Raised when GitHub events cannot be fetched."""


class GitHubRateLimitError(GitHubEventsError):
    """Raised when the GitHub API rate limit is exhausted."""


@dataclass(frozen=True)
class GitHubPushRecord:
    created_at: datetime  # aware datetime (UTC)
    repo: str
    branch: str


@dataclass(frozen=True)
class GitHubRepoCount:
    name: str
    pushes: int


@dataclass(frozen=True)
class GitHubPushStats:
    username: str
    pushes: int
    repos: list[GitHubRepoCount]
    last_push: datetime | None
    truncated: bool


class GitHubEventsClient:
    def __init__(self, timeout: float = 20.0, trust_env: bool = False):
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)

    async def aclose(self):
        await self._client.aclose()

    async def fetch_push_stats(
        self,
        username: str,
        window_start: datetime,
        window_end: datetime,
        token: str = "",
    ) -> GitHubPushStats:
        """统计某用户在 [window_start, window_end] 内的推送次数与涉及仓库。

        window_start/window_end 必须是带时区的 datetime；内部按该时区聚合。
        """
        if window_start.tzinfo is None or window_end.tzinfo is None:
            raise ValueError("GitHub push stats window must be timezone-aware")

        records, truncated = await self._fetch_push_records(username, token)

        repo_counts: dict[str, int] = {}
        push_count = 0
        last_push: datetime | None = None
        for record in records:
            local_time = record.created_at.astimezone(window_start.tzinfo)
            if not (window_start <= local_time <= window_end):
                continue
            push_count += 1
            repo_counts[record.repo] = repo_counts.get(record.repo, 0) + 1
            if last_push is None or record.created_at > last_push:
                last_push = record.created_at

        repos = [
            GitHubRepoCount(name=name, pushes=count)
            for name, count in sorted(
                repo_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        return GitHubPushStats(
            username=username,
            pushes=push_count,
            repos=repos,
            last_push=last_push,
            truncated=truncated,
        )

    async def _fetch_push_records(
        self,
        username: str,
        token: str,
    ) -> tuple[list[GitHubPushRecord], bool]:
        headers = {
            "User-Agent": "astrbot-pulse",
            "Accept": "application/vnd.github+json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        records: list[GitHubPushRecord] = []
        truncated = False
        for page in range(1, MAX_EVENT_PAGES + 1):
            response = await self._client.get(
                f"{GITHUB_API_BASE}/users/{username}/events/public",
                params={"per_page": EVENTS_PER_PAGE, "page": page},
                headers=headers,
            )
            self._check_response(response, username)
            events = response.json()
            if not isinstance(events, list):
                raise GitHubEventsError(f"GitHub 事件接口返回异常数据: {username}")
            for event in events:
                if not isinstance(event, dict) or event.get("type") != "PushEvent":
                    continue
                record = self._parse_push_event(event)
                if record:
                    records.append(record)

            if len(events) < EVENTS_PER_PAGE:
                return records, False

        # 3 页均满 100 条，探测第 4 页判断是否被 300 条深度截断。
        probe = await self._client.get(
            f"{GITHUB_API_BASE}/users/{username}/events/public",
            params={"per_page": EVENTS_PER_PAGE, "page": MAX_EVENT_PAGES + 1},
            headers=headers,
        )
        try:
            self._check_response(probe, username)
            probe_events = probe.json()
            truncated = isinstance(probe_events, list) and len(probe_events) > 0
        except GitHubEventsError:
            # 第 4 页不可用（422 等）说明事件流已到底，未截断。
            truncated = False
        return records, truncated

    def _check_response(self, response: httpx.Response, username: str):
        if response.status_code in (403, 429):
            remaining = response.headers.get("x-ratelimit-remaining", "")
            if remaining == "0" or response.status_code == 429:
                reset = response.headers.get("x-ratelimit-reset", "")
                raise GitHubRateLimitError(
                    f"GitHub API 限流（用户 {username}），"
                    f"重置时间 {reset or '未知'}。可配置 github_token 提升配额。"
                )
            raise GitHubRateLimitError(
                f"GitHub 请求被拒绝（用户 {username}，HTTP {response.status_code}），"
                "可能触发次级限流，请稍后再试或配置 github_token。"
            )
        if response.status_code == 404:
            raise GitHubEventsError(f"GitHub 用户不存在: {username}")
        if response.status_code >= 400:
            raise GitHubEventsError(
                f"GitHub 事件接口请求失败（用户 {username}，"
                f"HTTP {response.status_code}）"
            )

    def _parse_push_event(self, event: dict[str, Any]) -> GitHubPushRecord | None:
        payload = event.get("payload")
        repo = event.get("repo")
        if not isinstance(payload, dict) or not isinstance(repo, dict):
            return None

        created_at = _parse_datetime(event.get("created_at"))
        repo_name = str(repo.get("name") or "").strip()
        if not created_at or not repo_name:
            return None

        branch = str(payload.get("ref") or "")
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        return GitHubPushRecord(
            created_at=created_at,
            repo=repo_name,
            branch=branch,
        )


def avatar_url(username: str) -> str:
    """GitHub 头像直链，无需调用 API（返回 302 至 avatars 或直接输出 png）。"""
    return f"https://github.com/{username}.png"


# ---------------------------------------------------------------- snapshots


@dataclass
class GitHubDailySnapshot:
    date: str
    users: list[dict[str, Any]] = field(default_factory=list)

    def stats_for(self, username: str) -> GitHubPushStats | None:
        for user in self.users:
            if user.get("username") == username:
                repos = [
                    GitHubRepoCount(name=str(item.get("name") or ""), pushes=int(item.get("pushes") or 0))
                    for item in user.get("repos") or []
                ]
                return GitHubPushStats(
                    username=username,
                    pushes=int(user.get("pushes") or 0),
                    repos=repos,
                    last_push=_parse_datetime(user.get("last_push")),
                    truncated=bool(user.get("truncated")),
                )
        return None


def snapshot_dir(data_dir: Path) -> Path:
    return data_dir / "github_stats"


def save_daily_snapshot(
    data_dir: Path,
    date_str: str,
    stats_list: list[GitHubPushStats],
):
    """将当日全部订阅用户（含 0 推送）的统计落盘，供周报聚合。"""
    directory = snapshot_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date_str,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "users": [
            {
                "username": stats.username,
                "pushes": stats.pushes,
                "repos": [
                    {"name": repo.name, "pushes": repo.pushes} for repo in stats.repos
                ],
                "last_push": (
                    stats.last_push.isoformat(timespec="seconds")
                    if stats.last_push
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
