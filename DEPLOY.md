# Deploy trendpress on GitHub Actions (auto-run every 3 hours + email)

This runs the staggered publish flow on a schedule **in the cloud** and emails you
the links each run. (Note: **GitHub _Pages_** hosts static sites and is **not** used
here — your articles publish to your WordPress sites. The scheduler is **GitHub
_Actions_**, which is what `.github/workflows/publish.yml` uses.)

> ⚠️ **Use a PUBLIC repo.** Actions minutes are unlimited on public repos. On a
> private repo the free tier is ~2,000 min/month and a staggered run every 3h will
> blow through it in days. If you must keep it private, run it on a server/cPanel
> cron instead (see `SETUP.md` → Automated scheduling). **Your secrets stay safe
> either way** — `.env` is gitignored and credentials live in GitHub Secrets, not
> in the code.

## 1. Put the project in a git repo (repo root = this folder)

From inside the `trendpress` folder:

```bash
git init
git add .
git status            # CONFIRM .env and data/ are NOT listed (they're gitignored)
git commit -m "trendpress"
```

`.gitignore` already excludes `.env` and `data/`. Double-check `.env` is not staged
before pushing — it holds all your keys and passwords.

## 2. Create the GitHub repo and push

Create an empty **public** repo on github.com (e.g. `trendpress`), then:

```bash
git branch -M main
git remote add origin https://github.com/<YOU>/trendpress.git
git push -u origin main
```

## 3. Add your secrets (Settings → Secrets and variables → Actions → New repository secret)

Add each of these with the value from your local `.env`:

```
GEMINI_API_KEY          GEMINI_API_KEY_2        GEMINI_API_KEY_3        GEMINI_API_KEY_4
PEXELS_API_KEY
WP_PASS_SITE1  WP_PASS_SITE2  WP_PASS_SITE3  WP_PASS_SITE4  WP_PASS_SITE5
WP_PASS_SITE6  WP_PASS_SITE7  WP_PASS_SITE8  WP_PASS_SITE9  WP_PASS_SITE10
SMTP_HOST  SMTP_PORT  SMTP_USER  SMTP_PASSWORD  ADMIN_MAIL
```

(The tuning values — `GEMINI_MODEL`, `STAGGER_GAP_MIN`, retry settings — are already
in the workflow file as plain env, so they are **not** secrets. Edit them there.)

**Fast path with the GitHub CLI** (run from the project folder; reads your `.env`):

```bash
gh secret set GEMINI_API_KEY            < /dev/null  # or: gh secret set NAME --body "value"
# Or bulk-import every line of .env as a secret:
while IFS='=' read -r k v; do [ -n "$k" ] && case "$k" in \#*) ;; *) gh secret set "$k" --body "$v";; esac; done < .env
```

## 4. Turn it on & test

1. **Actions** tab → enable workflows if prompted.
2. Open **trendpress publish** → **Run workflow** (manual `workflow_dispatch`) to test
   immediately without waiting for the schedule.
3. Watch the run log. At the end you should get the **email digest** at `ADMIN_MAIL`
   with the published links.
4. After that, it runs automatically on `cron: "0 */3 * * *"` (every 3 hours, UTC).

## 5. How it behaves

- Each run: 1 trends-safety call + 1 matcher call for the whole network, then it
  writes/images/publishes **site-by-site with a `STAGGER_GAP_MIN` gap** so sites
  don't post at once. One **email** with all links is sent at the end.
- Posts are **pending** (review_mode), so nothing goes public until you approve in
  wp-admin. Flip to `sample`/`auto` in `config/settings.yaml` only after the canary
  period (see `SETUP.md`).
- **Dedup state** (`data/state.db`) is kept between runs via `actions/cache`. If the
  cache is ever evicted, trends could repeat — a server/cron with a real disk avoids
  this entirely.

## 6. Tuning later

- Add more sites: edit `config/sites.yaml` + add `WP_PASS_SITEn` secret, then bump
  `STAGGER_GAP_MIN` to `4` in the workflow once you're near ~15 sites.
- Change cadence: edit the `cron:` line in `.github/workflows/publish.yml`.
- Model/backoff: edit the env block in the workflow (or your `.env` for local runs).
