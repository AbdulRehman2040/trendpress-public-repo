# trendpress — setup & go-live

Automated trend-based publishing to a WordPress site network. Read this fully
before running anything live: the **go-live sequence** at the bottom is what
keeps the network safe from Google's scaled-content-abuse penalties.

## 1. Install

```bash
cd /home/USER/trendpress
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure secrets (`.env`)

```bash
cp .env.example .env
```

Fill in `.env`:

- `GEMINI_API_KEY` — Google AI Studio key (topic scoring + article writing).
- `PEXELS_API_KEY` — Pexels image API key (featured images).
- `WP_PASS_SITE1`, `WP_PASS_SITE2`, … — one **application password** per site
  (WP admin → Users → Profile → Application Passwords). The variable name must
  match each site's `app_password_env` in `config/sites.yaml`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional; if both are set the daily
  digest is sent to Telegram, otherwise it is written to `data/trendpress.log`.

Then edit `config/sites.yaml` (one entry per site; `niche`/`audience` drive
matching) and review `config/settings.yaml` — especially **`review_mode`**
(start on `pending`) and `publish_window` (Europe/London by default).

## 3. cron (cPanel → Cron Jobs)

Daily run (06:30 server time) and weekly kill-switch (Mon 08:00). Both append to
a log so cron mail only fires on a non-zero exit (i.e. a total failure):

```cron
# Daily publish run
30 6 * * * cd /home/USER/trendpress && /home/USER/trendpress/venv/bin/python main.py >> data/cron.log 2>&1

# Weekly health / kill-switch check
0 8 * * 1 cd /home/USER/trendpress && /home/USER/trendpress/venv/bin/python main.py --health >> data/cron.log 2>&1
```

Notes:
- Use absolute paths (cron has a minimal environment).
- The daily run schedules posts *inside* `publish_window`; pick a cron time at
  or before the window start so scheduling has the full window to spread across.
- `main.py` exits non-zero **only** on total failure, so per-site errors won't
  spam you — they appear in the digest instead.

## 4. CLI flags

| Flag | Purpose |
|------|---------|
| `--dry-run` | Run the whole chain, write HTML previews to `data/preview/`, **never POST**. |
| `--sites a,b` | Limit the run to specific site ids. |
| `--canary-only` | Only run sites with `status: canary`. |
| `--stagger` | Publish to each site one at a time, waiting between them (network footprint control). |
| `--gap-minutes N` | Gap between sites in `--stagger` mode (default 10). |
| `--health` | Run the weekly kill-switch and exit. |
| `--verbose` | Debug logging. |

### Staggered mode, key rotation & email

- **`--stagger`** runs the network site-by-site with a `--gap-minutes` pause, so
  the sites never publish at the same instant. Dedup means each trend is used by
  at most one site per batch. One **email digest** with every link (and any
  failures) is sent at the end (SMTP_* + ADMIN_MAIL in `.env`).
- **Multiple Gemini keys** (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, …) are rotated
  round-robin automatically to spread load and dodge per-key rate limits.

## 4b. Automated scheduling

**Option 1 — cron on a server / cPanel (recommended).** Persistent disk keeps the
dedup DB; no CI-minute limits; sleeps are free. Run every 3 hours:

```cron
0 */3 * * * cd /home/USER/trendpress && /home/USER/trendpress/venv/bin/python main.py --stagger --gap-minutes 10 >> data/cron.log 2>&1
0 8 * * 1 cd /home/USER/trendpress && /home/USER/trendpress/venv/bin/python main.py --health >> data/cron.log 2>&1
```

**Option 2 — GitHub Actions** (`.github/workflows/publish.yml`, every 3 h). Push
this folder to a GitHub repo, then add each `.env` value as a **repository secret**
(Settings → Secrets and variables → Actions): `GEMINI_API_KEY`, `GEMINI_API_KEY_2/3/4`,
`PEXELS_API_KEY`, `WP_PASS_SITE1`…`WP_PASS_SITE10`, `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USER`, `SMTP_PASSWORD`, `ADMIN_MAIL`. Never commit `.env` (it's gitignored).

> ⚠️ **GitHub Actions caveats — read before relying on it:**
> - **Minutes cost:** a 9-site run with 10-min gaps takes ~90 min; every 3 h ≈
>   720 min/day. That's free only on a **public** repo. On a **private** repo the
>   free tier is ~2,000 min/month (gone in ~3 days). Use a public repo, lengthen
>   the interval, shrink the gap, or prefer cron (Option 1).
> - **Dedup state is cached, not permanent:** runners are ephemeral, so `data/` is
>   restored via `actions/cache`. If the cache is evicted (7 days unused / 10 GB),
>   dedup resets and trends can repeat. A real server (Option 1) avoids this.
> - You pay for the idle 10-min sleeps. Cron (Option 1) does not.
>
> Bottom line: GitHub Actions works, but **cron on a host with a real disk is the
> better fit** for the staggered + dedup pattern.

## 5. Go-live sequence (do not skip)

1. **Dry-run.** `python main.py --dry-run`. Inspect `data/preview/*.html` — open
   two articles for the same trend on different sites and confirm they read as
   genuinely different, factually grounded pieces. Fix prompts/config as needed.

2. **Canary, pending, for 2–4 weeks.** Set `review_mode: pending` in
   `config/settings.yaml`, mark 3–4 sites `status: canary`, and run live with
   `--canary-only` (or via cron). Every post lands as **pending** — approve good
   ones in wp-admin, reject the rest. This is your quality + safety gate.

3. **Widen to all sites.** Once the canary period proves quality, flip the rest
   of the network to `status: active`. Keep `review_mode: pending` initially.

4. **Only then consider `sample`.** Move to `review_mode: sample` (≈10% held for
   review via `sample_rate`, the rest scheduled) once you trust the output.

> `review_mode: auto` (everything scheduled, no human in the loop) is for **later**,
> after the canary period has proven quality over weeks. Do not start there.

## 6. State & operations

- `data/state.db` — SQLite: used topics (dedupe), posts, used images, site
  health / paused flags, angle rotation. Gitignored; back it up.
- The **kill-switch** (`--health`) pauses a site (in `site_health`, never by
  editing `sites.yaml`) when its WP API is unreachable or it has repeated
  publish failures; the daily run then skips it and the digest reports it. A
  later healthy check clears the pause automatically.
- Optional upgrade (see the TODO in `run_health`): auto-pause a site whose Google
  Search Console impressions drop >50% week-over-week.
