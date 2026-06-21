# Migration: SQLite → Neon Postgres (durable, dashboard-ready state)

trendpress used to keep state in a local SQLite file at `data/state.db`. On
GitHub Actions that file is cached and **ephemeral** — it can be evicted, which
loses dedup history and gives a dashboard nothing durable to read.

State now lives in **Neon Postgres** (free tier, online 24/7). The pipeline reads
and writes the same database a dashboard reads, and it records:

- a **`runs`** row per pipeline run (status, trigger, counts, error, captured log),
- **`run_id` + `image_url`** on every `posts` row,
- a **`sites`** table (sites moved out of `config/sites.yaml`) so a dashboard can
  add/pause sites and the change applies on the next run.

Nothing about trends discovery, the Gemini matcher/writer, image sourcing,
scheduling, or the email/Telegram digest changed.

---

## What changed in the code

| File | Change |
|---|---|
| `core/db.py` | Rewritten for psycopg 3 / Neon. **Same public functions, same signatures.** `add_post` gained optional `run_id` + `image_url`. New: `start_run`, `finish_run`, `load_sites_from_db`, `upsert_site`, `set_site_status`, `site_exists`. New tables: `runs`, `sites` (plus `run_id`/`image_url` on `posts`). |
| `core/__init__.py` | `load_sites()` now reads the `sites` table. `load_sites_from_yaml()` kept for the importer. |
| `pipeline/__init__.py` | `ArticlePackage` gained `featured_image_url`. |
| `pipeline/images.py` | Captures the uploaded image's `source_url` and sets it on the package. `media_map` shape is unchanged (still `{(site,trend): media_id|None}`). |
| `pipeline/publisher.py` | `publish(...)` accepts `run_id`; `add_post` is called with `run_id` + `image_url`. |
| `main.py` | `run_daily` / `run_staggered` open a run (`start_run`), tee the log to a buffer, and `finish_run` with counts + status (`success` / `partial` / `failed`) even on crash. **Dry-run never writes** (no run row). |
| `.github/workflows/publish.yml` | Adds `DATABASE_URL` + `RUN_TRIGGER` env; `workflow_dispatch` takes a `trigger` input (so dashboard-fired runs are tagged `manual`). |
| `scripts/import_sites.py` | One-time seeder: `config/sites.yaml` → `sites` table. |
| `db/schema.sql` | The full schema (also auto-created on first connect). |

> **Local dev note:** because state is now in Neon, **local runs need `DATABASE_URL`
> too** (put it in `.env`). A dry-run still reads the DB (dedup, caps, sites) but
> writes nothing.

---

## Go-live steps (do these once, in order)

### 1. Create a Neon project
- Sign up at <https://neon.tech> (free), create a project.
- Copy the **pooled** connection string (the one whose host contains `-pooler`).
  It looks like:
  `postgresql://USER:PASSWORD@ep-xxx-pooler.REGION.aws.neon.tech/neondb?sslmode=require`

### 2. Add `DATABASE_URL`
- **Local:** add to `.env`:
  ```
  DATABASE_URL=postgresql://...-pooler...sslmode=require
  ```
- **GitHub Actions:** repo → Settings → Secrets and variables → Actions → New
  repository secret → name `DATABASE_URL`, value = the pooled string.
  (Until you add it, the workflow editor shows a harmless "Context access might be
  invalid: DATABASE_URL" warning.)

### 3. Install the new dependency
```bash
pip install -r requirements.txt        # adds psycopg[binary]
```

### 4. Seed the sites table (one time)
```bash
python scripts/import_sites.py
```
You should see your 10 sites listed and `The sites table now holds 10 site(s).`
Tables are created automatically; you don't need to run `db/schema.sql` by hand.

### 5. Verify
```bash
python main.py --dry-run --sites site1     # reads from Neon, writes nothing
```
Then let one real run happen (or trigger the workflow from the Actions tab). After
it finishes, a `runs` row exists and its `posts` carry `run_id` + `image_url`:
```sql
select id, status, trigger, posts_published, posts_failed from runs order by id desc limit 5;
select title, site_id, run_id, image_url from posts order by id desc limit 10;
```

---

## Rollback

The old SQLite code is in git history. To revert, check out the previous
`core/db.py`, `core/__init__.py`, `pipeline/*`, `main.py` and the workflow. Your
Neon data is unaffected by a code rollback.
