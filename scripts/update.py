#!/usr/bin/env python3
"""
Daily Progress Update — GitHub Activity Tracker

Fetches recent GitHub activity across all repos, generates AI-powered
summaries and next steps, stores daily snapshots, and builds an HTML dashboard.
"""

import os
import sys
import json
import re
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from anthropic import Anthropic
from jinja2 import Environment, FileSystemLoader

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GITHUB_API = "https://api.github.com"
ACTIVE_WINDOW_DAYS = 14  # repos untouched for this long drop off the dashboard

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DOCS_DIR = ROOT_DIR / "docs"
TEMPLATES_DIR = ROOT_DIR / "templates"

DEFAULT_OBSIDIAN_TODO = "/Users/djw/Documents/pCloud_synced/Obsidian/January2026/daily-to-dos.md"
OBSIDIAN_TODO_PATH = os.environ.get("OBSIDIAN_TODO_PATH", DEFAULT_OBSIDIAN_TODO)


# ---------------------------------------------------------------------------
# GitHub client
# ---------------------------------------------------------------------------
class GitHubClient:
    """Thin wrapper around the GitHub REST API."""

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            }
        )

    # -- helpers ----------------------------------------------------------
    def _get(self, url: str, params: dict | None = None) -> requests.Response:
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        return resp

    def _paginate(self, url: str, params: dict | None = None, max_pages: int = 10):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        items: list = []
        for page in range(1, max_pages + 1):
            params["page"] = page
            resp = self._get(url, params)
            batch = resp.json()
            if not batch:
                break
            items.extend(batch)
        return items

    # -- public API -------------------------------------------------------
    def get_user(self) -> dict:
        return self._get(f"{GITHUB_API}/user").json()

    def get_active_repos(self, since: datetime) -> list[dict]:
        """Return repos pushed to since *since*, sorted most-recent first."""
        repos: list[dict] = []
        page = 1
        while True:
            batch = self._get(
                f"{GITHUB_API}/user/repos",
                params={
                    "sort": "pushed",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                    "type": "all",
                },
            ).json()
            if not batch:
                break
            for repo in batch:
                pushed = _parse_gh_date(repo["pushed_at"])
                if pushed < since:
                    return repos  # sorted → everything after is older
                repos.append(repo)
            page += 1
        return repos

    def get_commits(self, owner: str, repo: str, since: datetime) -> list[dict]:
        params = {"since": since.isoformat(), "per_page": 100}
        try:
            raw = self._get(
                f"{GITHUB_API}/repos/{owner}/{repo}/commits", params=params
            ).json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                return []  # empty repo
            raise
        return [
            {
                "sha": c["sha"][:7],
                "message": c["commit"]["message"].split("\n")[0][:120],
                "author": (c["commit"]["author"] or {}).get("name", "unknown"),
                "date": c["commit"]["author"]["date"] if c["commit"]["author"] else "",
                "url": c["html_url"],
            }
            for c in raw
        ]

    def get_pull_requests(self, owner: str, repo: str, since: datetime) -> list[dict]:
        raw = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            params={
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": 50,
            },
        ).json()
        prs = []
        for pr in raw:
            if _parse_gh_date(pr["updated_at"]) < since:
                break
            prs.append(
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "state": "merged" if pr.get("merged_at") else pr["state"],
                    "url": pr["html_url"],
                    "date": pr["updated_at"],
                }
            )
        return prs

    def get_issues(self, owner: str, repo: str, since: datetime) -> list[dict]:
        raw = self._get(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues",
            params={
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "since": since.isoformat(),
                "per_page": 50,
            },
        ).json()
        return [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "url": i["html_url"],
                "date": i["updated_at"],
                "labels": [l["name"] for l in i.get("labels", [])],
            }
            for i in raw
            if "pull_request" not in i  # exclude PRs from issues endpoint
        ]


# ---------------------------------------------------------------------------
# AI summariser (Claude)
# ---------------------------------------------------------------------------
class AISummarizer:
    """Uses the Anthropic API to generate per-repo summaries & next steps."""

    def __init__(self, api_key: str):
        self.client = Anthropic(api_key=api_key)

    def generate(
        self, repo_name: str, description: str, activity: dict
    ) -> dict:
        activity_text = _format_activity_for_prompt(activity)

        if not activity_text.strip():
            return {
                "summary": "No significant activity recorded.",
                "next_steps": "No recent activity to base next steps on.",
            }

        prompt = (
            f'Analyze the following recent GitHub activity for "{repo_name}" '
            f'({description or "no description"}).\n\n'
            f"Activity:\n{activity_text}\n\n"
            "Return a JSON object with exactly two keys:\n"
            '  "summary"     – 1 sentence, max 20 words, describing what was accomplished.\n'
            '  "next_steps"  – a string with 2-3 bullet points using \u2022 as the bullet character, '
            "each bullet under 10 words, separated by newlines.\n\n"
            "Be terse and direct. No filler words. "
            "Respond ONLY with the JSON object, no markdown fences."
        )

        resp = self.client.messages.create(
            model="claude-sonnet-4-6-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        text = resp.content[0].text.strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                try:
                    result = json.loads(m.group())
                except json.JSONDecodeError:
                    result = None
            else:
                result = None

        if result is None:
            return {
                "summary": text[:500],
                "next_steps": "Could not parse structured next steps.",
            }

        # Normalize next_steps to always be a string
        if isinstance(result.get("next_steps"), list):
            result["next_steps"] = "\n".join(result["next_steps"])
        return result


# ---------------------------------------------------------------------------
# Data persistence  (one JSON file per calendar day)
# ---------------------------------------------------------------------------
def save_daily_data(date_str: str, repos_data: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{date_str}.json"
    payload = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repos": repos_data,
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_recent_data(days: int = ACTIVE_WINDOW_DAYS) -> dict[str, dict]:
    """Return {date_str: data} for the most recent *days* calendar days."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    result: dict[str, dict] = {}
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        p = DATA_DIR / f"{d}.json"
        if p.exists():
            result[d] = json.loads(p.read_text())
    return result


# ---------------------------------------------------------------------------
# Dashboard generation
# ---------------------------------------------------------------------------
def build_dashboard(
    current_repos: dict,
    historical: dict[str, dict],
    username: str,
    generated_at: str,
) -> None:
    """Render the Jinja2 template and write docs/index.html."""

    # Aggregate per-repo data across all historical days
    repo_history: dict[str, list[dict]] = {}  # repo_name -> [{date, summary, activity}, ...]
    for date_str in sorted(historical.keys(), reverse=True):
        day_data = historical[date_str]
        for repo_name, rdata in day_data.get("repos", {}).items():
            repo_history.setdefault(repo_name, []).append(
                {
                    "date": date_str,
                    "summary": rdata.get("summary", ""),
                    "next_steps": rdata.get("next_steps", ""),
                    "activity": rdata.get("activity", {}),
                }
            )

    # Build the view-model for the template
    repos_view = []
    for repo_name, rdata in current_repos.items():
        history = repo_history.get(repo_name, [])

        # Latest next_steps comes from the most recent day
        latest_next_steps = rdata.get("next_steps", "")
        if isinstance(latest_next_steps, list):
            latest_next_steps = "\n".join(latest_next_steps)

        # Collect daily summaries (reverse-chron, skip if empty)
        daily_entries = []
        for h in history:
            entry_activity = h.get("activity", {})
            total = sum(len(v) for v in entry_activity.values() if isinstance(v, list))
            daily_entries.append(
                {
                    "date": h["date"],
                    "summary": h["summary"],
                    "commits": entry_activity.get("commits", []),
                    "pull_requests": entry_activity.get("pull_requests", []),
                    "issues": entry_activity.get("issues", []),
                    "total_items": total,
                }
            )

        last_pushed = rdata.get("last_pushed", "")
        repos_view.append(
            {
                "name": repo_name,
                "display_name": repo_name.split("/")[-1],
                "url": rdata.get("url", f"https://github.com/{repo_name}"),
                "description": rdata.get("description", ""),
                "language": rdata.get("language", ""),
                "last_pushed": last_pushed,
                "last_pushed_relative": _relative_time(last_pushed),
                "next_steps": latest_next_steps,
                "daily_entries": daily_entries,
                "total_commits": sum(
                    len(e["commits"]) for e in daily_entries
                ),
            }
        )

    # Sort by most recently pushed
    repos_view.sort(
        key=lambda r: r["last_pushed"] or "", reverse=True
    )

    # Stats
    stats = {
        "active_repos": len(repos_view),
        "total_commits": sum(r["total_commits"] for r in repos_view),
        "languages": list({r["language"] for r in repos_view if r["language"]}),
    }

    # Render
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("dashboard.html")
    html = template.render(
        repos=repos_view,
        username=username,
        generated_at=generated_at,
        stats=stats,
        active_window_days=ACTIVE_WINDOW_DAYS,
    )

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html)


# ---------------------------------------------------------------------------
# Obsidian todo generation
# ---------------------------------------------------------------------------
def generate_obsidian_todos(repos_data: dict, date_str: str) -> Path | None:
    """Generate an Obsidian-compatible markdown todo file with coding next steps."""
    out_path = Path(OBSIDIAN_TODO_PATH)

    # Skip if parent directory doesn't exist (e.g. running in CI)
    if not out_path.parent.exists():
        return None

    now = datetime.now(timezone.utc)

    lines = [
        "---",
        "type: todo",
        f"created: {date_str}",
        f"modified: {now.strftime('%Y-%m-%d %H:%M')}",
        "tags:",
        "  - coding",
        "  - auto-generated",
        "categories:",
        "  - coding",
        "projects:",
        "due date:",
        "priority:",
        "complete: false",
        "permalink:",
        "---",
        "",
        "## Coding",
        "",
    ]

    for repo_name, rdata in repos_data.items():
        display_name = repo_name.split("/")[-1]
        next_steps = rdata.get("next_steps", "")

        # Normalize next_steps
        if isinstance(next_steps, list):
            next_steps = "\n".join(next_steps)

        if not next_steps.strip():
            continue

        lines.append(f"### {display_name}")

        # Convert bullet points to Obsidian checkbox format
        for bullet_line in next_steps.strip().split("\n"):
            bullet_line = bullet_line.strip()
            if not bullet_line:
                continue
            # Strip leading bullet characters
            cleaned = bullet_line.lstrip("\u2022-* ").strip()
            if cleaned:
                lines.append(f"- [ ] {cleaned}")

        lines.append("")  # blank line between repos

    out_path.write_text("\n".join(lines))
    return out_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_gh_date(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _relative_time(iso_str: str) -> str:
    if not iso_str:
        return "unknown"
    dt = _parse_gh_date(iso_str)
    delta = datetime.now(timezone.utc) - dt
    if delta.days > 1:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours}h ago"
    minutes = delta.seconds // 60
    return f"{minutes}m ago"


def _format_activity_for_prompt(activity: dict) -> str:
    lines: list[str] = []
    for c in activity.get("commits", [])[:30]:
        lines.append(f"  commit {c['sha']}: {c['message']} (by {c['author']})")
    for pr in activity.get("pull_requests", []):
        lines.append(f"  PR #{pr['number']}: {pr['title']} [{pr['state']}]")
    for iss in activity.get("issues", []):
        lines.append(f"  Issue #{iss['number']}: {iss['title']} [{iss['state']}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Progress Update")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=f"Look back {ACTIVE_WINDOW_DAYS} days instead of 1 day",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        help="Repo full-names to exclude (e.g. owner/repo)",
    )
    args = parser.parse_args()

    github_token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if not github_token:
        sys.exit("Error: set GH_PAT, GITHUB_TOKEN, or GH_TOKEN env var")
    if not anthropic_key:
        sys.exit("Error: set ANTHROPIC_API_KEY env var")

    gh = GitHubClient(github_token)
    ai = AISummarizer(anthropic_key)

    user = gh.get_user()
    username = user["login"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ACTIVE_WINDOW_DAYS)

    # Auto-detect backfill: if no data files exist yet, backfill
    existing_data = list(DATA_DIR.glob("*.json")) if DATA_DIR.exists() else []
    is_backfill = args.backfill or len(existing_data) == 0
    since = cutoff if is_backfill else (now - timedelta(days=1))

    mode = "backfill" if is_backfill else "daily"
    print(f"[{mode}] Fetching repos active since {cutoff.date()} ...")
    repos = gh.get_active_repos(cutoff)

    # Apply exclusions
    tracker_repo = f"{username}/daiily-progress-update"
    excluded = set(args.exclude) | {tracker_repo}
    repos = [r for r in repos if r["full_name"] not in excluded]

    print(f"Found {len(repos)} active repo(s)  (excluded: {', '.join(excluded)})")

    repos_data: dict[str, dict] = {}
    for repo in repos:
        full_name = repo["full_name"]
        owner, name = full_name.split("/")
        print(f"\n>>> {full_name}")

        activity = {
            "commits": gh.get_commits(owner, name, since),
            "pull_requests": gh.get_pull_requests(owner, name, since),
            "issues": gh.get_issues(owner, name, since),
        }
        total = sum(len(v) for v in activity.values())
        print(f"    {total} activity items since {since.date()}")

        if total > 0:
            print("    Generating AI summary ...")
            ai_result = ai.generate(full_name, repo.get("description") or "", activity)
        else:
            ai_result = {
                "summary": "Pushed but no new commits, PRs, or issues detected.",
                "next_steps": "• Review in-progress work\n• Check pending branch merges",
            }

        repos_data[full_name] = {
            "url": repo["html_url"],
            "description": repo.get("description") or "",
            "language": repo.get("language") or "",
            "last_pushed": repo["pushed_at"],
            "activity": activity,
            "summary": ai_result["summary"],
            "next_steps": ai_result["next_steps"],
        }

    # Persist
    today_str = now.date().isoformat()
    save_daily_data(today_str, repos_data)
    print(f"\nSaved → data/{today_str}.json")

    # Build dashboard from all recent data
    historical = load_recent_data(ACTIVE_WINDOW_DAYS)
    generated_at = now.strftime("%b %d, %Y at %I:%M %p UTC")
    build_dashboard(repos_data, historical, username, generated_at)
    print(f"Dashboard → docs/index.html")

    # Generate Obsidian todo file
    todo_path = generate_obsidian_todos(repos_data, today_str)
    if todo_path:
        print(f"Obsidian todos → {todo_path}")


if __name__ == "__main__":
    main()
