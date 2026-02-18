# Daily Progress Update

Automated nightly tracker that summarizes GitHub activity across all your repos and generates AI-powered next steps. Produces a static HTML dashboard suitable for GitHub Pages.

## How it works

1. A **GitHub Actions cron job** runs every night at 06:00 UTC
2. The script fetches all repos you've pushed to in the last 14 days
3. For each repo it collects commits, PRs, and issues
4. **Claude** (Anthropic API) generates a narrative summary and actionable next steps
5. A daily JSON snapshot is saved to `data/`
6. An HTML dashboard is generated at `docs/index.html`

Repos that haven't been touched in 14 days automatically drop off the dashboard, but their historical data is retained in the `data/` directory.

## Setup

### 1. Create required secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|--------|-------------|
| `GH_PAT` | A GitHub [Personal Access Token](https://github.com/settings/tokens) with `repo` scope (needed to read activity across all your repos) |
| `ANTHROPIC_API_KEY` | Your [Anthropic API key](https://console.anthropic.com/) for AI-generated summaries |

### 2. Enable GitHub Pages (optional)

To host the dashboard:

1. Go to **Settings → Pages**
2. Set source to **Deploy from a branch**
3. Select the `main` branch and `/docs` folder
4. Save — your dashboard will be available at `https://<username>.github.io/daiily-progress-update/`

### 3. Initial backfill

Trigger the workflow manually from the **Actions** tab, or run locally:

```bash
export GH_PAT="ghp_..."
export ANTHROPIC_API_KEY="sk-ant-..."
pip install -r requirements.txt
python scripts/update.py --backfill
```

The first run automatically detects that no data exists and backfills the last 14 days.

## Running locally

```bash
pip install -r requirements.txt
export GH_PAT="your_github_pat"
export ANTHROPIC_API_KEY="your_anthropic_key"
python scripts/update.py           # daily run (last 24h)
python scripts/update.py --backfill  # full 14-day backfill
```

## Data model

Each day produces a JSON file in `data/YYYY-MM-DD.json`:

```json
{
  "date": "2026-02-18",
  "generated_at": "2026-02-18T06:00:00+00:00",
  "repos": {
    "owner/repo-name": {
      "url": "https://github.com/owner/repo-name",
      "description": "...",
      "language": "Python",
      "last_pushed": "2026-02-18T15:30:00Z",
      "activity": {
        "commits": [...],
        "pull_requests": [...],
        "issues": [...]
      },
      "summary": "AI-generated progress summary",
      "next_steps": "AI-generated next steps"
    }
  }
}
```

The dashboard always shows the **most recent** next steps for each repo, while progress summaries are displayed per-day in reverse chronological order.

## Excluding repos

Pass `--exclude` to skip specific repos:

```bash
python scripts/update.py --exclude owner/repo-to-skip owner/another-repo
```

The tracker repo itself (`<username>/daiily-progress-update`) is excluded by default to avoid noise from automated commits.

## Customization

- **Schedule**: Edit the cron expression in `.github/workflows/nightly-update.yml`
- **Active window**: Change `ACTIVE_WINDOW_DAYS` in `scripts/update.py` (default: 14)
- **Dashboard look**: Edit `templates/dashboard.html` (Jinja2 template with self-contained CSS)
- **AI model**: Change the model in `scripts/update.py` `AISummarizer.generate()`
