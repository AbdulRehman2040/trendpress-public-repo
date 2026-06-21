# TrendPress — Complete System Overview

**trendpress** is an automated, trend-based news publishing system for a UK WordPress site network. It discovers trending topics from Google Trends, assigns them intelligently to individual sites (avoiding "scaled content abuse"), generates factually-grounded articles using Google Gemini AI, attaches licensed stock photos from Pexels/Openverse, and publishes them with human-review gates.

**Core philosophy**: One trending topic is published by *only a subset of sites* (max 3 per topic) with unique local angles, ensuring Google does not penalize the network for broadcasting identical content.

---

## System Architecture — Data Flow

```
Google Trends RSS (geo=GB)
        │
        ▼
[Stage 1] trends.py
  • Keyword safety filter
  • Gemini AI safety classification (fails open)
  • 7-day deduplication
  • Sort by traffic volume
        │
        ▼
[Stage 2] matcher.py
  • Gemini scores all (trend × site) pairs (0–10)
  • Deterministic allocation: max 3 sites per topic, respects daily caps
  • Fails CLOSED — no assignments if Gemini unavailable
        │
        ▼
[Stage 3] writer.py
  • One article per assignment via Gemini
  • Validates structure, word count, source links
  • Retry once with errors appended; skip gracefully on failure
        │
        ▼
[Stage 4] images.py
  • Pexels API search → per-site unique photo selection
  • Fallback: Openverse (CC-licensed)
  • Resize/re-encode (Pillow), upload to WordPress
        │
        ▼
[Stage 5] publisher.py
  • WordPress REST API: create post with correct status
  • Schedule inside publish_window (staggered, 20+ min apart per site)
  • Write to SQLite state DB only on success
        │
        ▼
[notify.py] → Email (SMTP) + Telegram digest
```

---

## Every File — Full Walkthrough

---

### Root Level — Launch Scripts & Config

---

#### `.env` (gitignored, never committed)

Holds all secrets and runtime tuning. Never committed to git.

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` … `GEMINI_API_KEY_4` | Multiple Google AI Studio keys for round-robin quota rotation |
| `PEXELS_API_KEY` | Pexels image search credential |
| `GEMINI_MODEL` | Primary model (`gemini-2.5-flash`, pinned — never `-latest`) |
| `GEMINI_FALLBACK_MODEL` | Fallback after 2× consecutive 503 (`gemini-2.5-flash-lite`) |
| `GEMINI_MIN_SECONDS_BETWEEN_CALLS` | Min gap between Gemini API calls (default 30s) |
| `GEMINI_MAX_RETRIES` | Max retry attempts per call (default 5) |
| `GEMINI_RETRY_BASE_SECONDS` | Backoff base (default 30s) |
| `GEMINI_RETRY_MAX_SECONDS` | Backoff ceiling (default 300s) |
| `GEMINI_RETRY_JITTER` | Add 0–15s random jitter to backoff (default true) |
| `GEMINI_COOLDOWN_AFTER_429_SECONDS` | Sleep when ALL keys are rate-limited (default 90s) |
| `STAGGER_GAP_MIN` | Minutes gap between site publishes in staggered mode (default 3) |
| `WP_PASS_SITE1` … `WP_PASS_SITE10` | WordPress application passwords, one per site |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Optional Telegram notification credentials |
| `SMTP_HOST/PORT/USER/PASSWORD` | Email digest delivery (Gmail recommended) |
| `ADMIN_MAIL` | Digest recipient address |

---

#### `requirements.txt`

Python package dependencies:

| Package | Purpose |
|---|---|
| `requests>=2.31` | HTTP for WordPress REST API and Pexels |
| `feedparser>=6.0` | Google Trends RSS parsing |
| `pyyaml>=6.0` | YAML config loading |
| `python-dotenv>=1.0` | `.env` secret loading |
| `google-genai>=1.0` | Modern Gemini SDK |
| `Pillow>=10.0` | Image resize/convert before WordPress upload |
| `tzdata>=2024.1` | IANA timezone database (Windows compatibility) |

---

#### `SETUP.md`

Comprehensive setup and go-live guide. Key sections:

1. **Installation**: Python venv + `pip install -r requirements.txt`
2. **`.env` configuration**: API keys, WP application passwords
3. **Cron scheduling**: `30 6 * * *` (daily) and weekly health check
4. **CLI flags**: `--dry-run`, `--sites`, `--canary-only`, `--stagger`, `--gap-minutes`, `--health`, `--verbose`
5. **Go-live sequence** (5-step canary ramp):
   - Step 1: `python main.py --dry-run` — inspect previews
   - Step 2: Canary phase (3–4 sites, `review_mode: pending`) — human approval gate
   - Step 3: Widen to all sites, stay on pending
   - Step 4: Graduate to `sample` mode (10% held for review)
   - Step 5: Move to `auto` mode only after canary proves quality

---

#### `DEPLOY.md`

GitHub Actions deployment guide.

- **Must use public repo** — private free tier is ~2,000 min/month, consumed in ~3 days at 3-hour intervals
- Secrets: one GitHub Secret per env var (never commit `.env`)
- Workflow schedule: `cron: "0 */3 * * *"` (every 3 hours UTC)
- Dedup state cached via `actions/cache` — **ephemeral** (trends can repeat if cache evicted)
- **Better alternative**: VPS cron with persistent disk (no minute cost, reliable state)

---

#### `run.bat` / `run.ps1`

Windows launcher scripts (CMD batch and PowerShell).

- Find `python` on PATH; fallback to hardcoded path
- Pass all CLI args through to `main.py`
- Example: `run.bat --dry-run --sites site1`

---

#### `dry-run.bat`

Runs `python main.py --dry-run --sites site1`. Writes previews to `data/preview/`. Never posts anything. Pauses and waits for keypress.

---

#### `publish-site1.bat`

Runs `python main.py --sites site1`. Live publish to site1 in pending mode. Pauses and waits.

---

### Configuration Files (`config/`)

---

#### `config/settings.yaml`

Global runtime tuning. All keys have baked-in defaults; this file documents every available knob.

| Key | Default | Purpose |
|---|---|---|
| `geo` | `"GB"` | Google Trends region (ISO country code) |
| `gemini_model` | `"gemini-2.5-flash"` | Primary AI model (env overrides) |
| `gemini_fallback_model` | `"gemini-2.5-flash-lite"` | Fallback model |
| `sleep_between_calls_seconds` | `10` | Min gap between Gemini calls |
| `max_sites_per_topic` | `3` | Max sites one trend can run on |
| `min_match_score` | `5` | Minimum 0–10 relevance threshold |
| `dedupe_window_days` | `7` | Skip trend if used within this many days |
| `review_mode` | `"live"` | Post status gate (`pending`, `live`, `sample`, `auto`) |
| `sample_rate` | `0.1` | Fraction held pending when `review_mode: sample` |
| `publish_window.start` | `"09:00"` | Earliest schedule time (local timezone) |
| `publish_window.end` | `"18:00"` | Latest schedule time |
| `publish_window.timezone` | `"Europe/London"` | Window timezone |
| `posts_per_day_cap_default` | `2` | Fallback daily post limit per site |

**`review_mode` explained:**

| Value | Behavior |
|---|---|
| `"pending"` | Posts created as drafts — human must approve in wp-admin (RECOMMENDED for launch) |
| `"live"` | Posts publish immediately, no review |
| `"sample"` | `sample_rate` fraction held pending, rest scheduled |
| `"auto"` | All posts scheduled inside publish window, no human loop |

---

#### `config/sites.yaml`

WordPress site network profile — 10 UK sites configured.

**Per-site fields:**

| Field | Purpose |
|---|---|
| `id` | Identifier (`site1` … `site10`) |
| `name` | Human-readable name (e.g., "London Headline") |
| `url` | HTTPS URL, no trailing slash |
| `wp_username` | WP REST basic-auth username |
| `app_password_env` | Env var name holding the application password |
| `niche` | 2–3 sentence coverage description (used in AI prompts) |
| `audience` | Target reader description (used in AI prompts) |
| `tone` | Voice descriptor (e.g., "clear, factual, neutral newsroom style") |
| `angle_pool` | List of 5 editorial angles for rotation |
| `word_range` | `[low, high]` target word count per article |
| `posts_per_day_cap` | Daily post limit for this site |
| `status` | `"active"` / `"canary"` / `"paused"` |

**Status values:**
- `"active"` — included in all runs
- `"canary"` — only included when `--canary-only` flag is passed
- `"paused"` — skipped entirely (e.g., auth failure)

Sites: London Headline, The London Hub (paused — 401 auth), Manchester Evening Chronicle, Manchester City Pulse, Leeds Live Bulletin, Birmingham Daily, Liverpool News Network, Bristol City Post, Glasgow Daily, Oxford News Hub.

---

### Core Modules (`core/`)

---

#### `core/__init__.py`

Shared utilities and config loaders.

**Path constants:**
- `PROJECT_ROOT` — parent dir of `core/`
- `CONFIG_DIR` — `{PROJECT_ROOT}/config`
- `DATA_DIR` — `{PROJECT_ROOT}/data` (state.db, logs, preview HTMLs)

**Functions:**
- `load_settings()` — parse `config/settings.yaml`, return dict
- `load_sites()` — parse `config/sites.yaml`, return list of site dicts (handles both top-level list and dict with `sites:` key)

---

#### `core/db.py`

SQLite-backed persistent state stored at `data/state.db`.

**5 tables:**

| Table | Schema | Purpose |
|---|---|---|
| `used_topics` | `(trend_id PK, title, used_on DATE)` | Deduplication: topics used within window are not rerun |
| `posts` | `(id PK, site_id, trend_id, wp_post_id, title, url, status, created_at)` | Audit trail of all published/failed posts |
| `used_images` | `(trend_id, site_id, photo_id)` | Track which stock photos used per (trend, site) — ensures different sites get different photos |
| `site_health` | `(site_id, checked_on DATE, note, paused INTEGER)` | Health check results; most recent `paused` flag = authoritative kill-switch |
| `angle_rotation` | `(site_id PK, idx INTEGER)` | Rotation state for `angle_pool` — ensures angles vary across consecutive runs per site |

**Key functions:**

```python
add_used_topic(trend_id, title)          # mark trend as used
is_topic_used(trend_id, within_days)     # dedupe check

add_post(site_id, trend_id, ...)         # audit trail
list_posts(site_id, days)                # list recent posts
count_posts_today(site_id)               # daily cap enforcement

add_used_image(trend_id, site_id, photo_id)  # track photo usage
is_image_used(trend_id, site_id, photo_id)   # check if photo used
images_used_for_trend(trend_id)              # all photos used for a trend

add_site_health(site_id, note, paused)   # record health check result
is_site_paused(site_id)                  # kill-switch check
recent_post_stats(site_id, days)         # successes/errors count

next_angle_index(site_id, pool_size)     # rotate angles
```

---

#### `core/notify.py`

Operational digests delivered via email (SMTP) and/or Telegram. Always logged regardless of delivery.

**Digest structure:**
```python
{
    "title": str,                    # e.g., "trendpress daily — 5 post(s)"
    "posts": [
        {"site", "site_name", "title", "status", "url", "edit_url"}
    ],
    "errors": [str],                 # per-site publish failures
    "skipped_trends": [str],         # trends matched no site
    "missing_images": [str],         # packages with no featured image
    "paused_sites": [str],           # kill-switch paused sites
}
```

**Functions:**
- `send_digest(digest, settings)` — render and deliver
- `format_digest()` — plain-text rendering with live + edit links
- `_send_email()` — SMTP (port 465 SSL or 587 STARTTLS)
- `_send_telegram()` — POST to Telegram (≤4096 chars per message)

---

#### `core/wp.py`

WordPress REST API client using HTTP Basic Auth with application passwords.

**`WPClient` class:**

Constructor takes site dict (`url`, `wp_username`, `app_password_env`), builds `/wp-json/wp/v2` base URL, reads password from env.

| Method | Purpose |
|---|---|
| `create_post(payload)` | Create a WP post (returns post dict with `id`, `link`) |
| `search_posts(query)` | Search existing posts (for internal linking) |
| `list_recent_posts(days=7)` | List recent posts from this site |
| `upload_media(binary, filename, mime, alt_text, caption)` | Upload image binary, set alt + caption |
| `list_categories()` | List all categories |
| `get_or_create_term(name, taxonomy)` | Reuse existing term (case-insensitive match) or create new |

- `WPError` exception on non-2xx responses
- 30s timeout on all HTTP calls

---

#### `core/gemini.py`

Gemini API wrapper with full resilience: key rotation, exponential backoff with jitter, fallback model.

**`GeminiClient` class:**

**Init:** Loads keys from env (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`–`5` or comma-separated `GEMINI_API_KEYS`). Creates one `google.genai.Client` per key. Reads model/retry settings from env or `settings.yaml`.

**Public method:** `generate_json(prompt, system)` → returns `dict` from JSON response.

**Retry logic (`_call_with_retry`):**

1. Enforce min gap between calls (`GEMINI_MIN_SECONDS_BETWEEN_CALLS`, default 30s)
2. Round-robin key rotation seeded per-call
3. Up to `GEMINI_MAX_RETRIES` (default 5) attempts

**Error handling:**

| Error | Action |
|---|---|
| 400/401/403 | Non-retryable — raise immediately |
| 429 (rate-limit) | Rotate to next key + exponential backoff. If ALL keys 429 → sleep `GEMINI_COOLDOWN_AFTER_429_SECONDS` (90s) |
| 503/5xx (overload) | Keep same key + backoff. After 2× consecutive 503s → switch to fallback model |

**Backoff formula:** `min(retry_max, base × 2^(attempt-1) + jitter[0–15s])`

**JSON parsing (`_parse_json`):** Strips code fences, tolerates surrounding text, uses `json.JSONDecoder.raw_decode()` to salvage partial JSON responses.

---

### Pipeline Modules (`pipeline/`)

---

#### `pipeline/__init__.py` — Data Contracts

Defines lightweight dataclasses. No ORM — modules are decoupled.

```python
NewsItem(title, snippet, url, source)
# One supporting news story from the trend feed

Trend(trend_id, title, traffic, news_items: list[NewsItem], picture_url)
# trend_id = SHA1(first 16 hex chars of lowercased, normalized title)

Assignment(trend, site_id, score, angle)
# Trend matched to one site with AI-suggested editorial angle

FaqItem(q, a)
# One Q&A pair for the article FAQ block

ArticlePackage(site_id, trend_id, title, slug, meta_description,
               focus_keyword, tags, category, html_content,
               image_query, faq, sources, featured_media_id)
# Ready-to-publish article

PublishResult(site_id, wp_post_id, url, status, error)
# Outcome of one publish attempt
```

**Flow:** `Trend → Assignment → ArticlePackage → PublishResult`

---

#### `pipeline/trends.py` — Stage 1: Discover Safe Trends

**Public entry:** `get_safe_trends(settings, db, gemini) -> list[Trend]`

**Pipeline:**

1. **Fetch & parse**: GET `https://trends.google.com/trending/rss?geo=GB` (namespace-aware XML)
2. **Build Trend objects**: Extract `<item>` elements + `<ht:news_item>` children. Drop trends with no news facts.
3. **Dedupe**: Drop trends used within `dedupe_window_days` (default 7 days)
4. **Keyword pre-filter**: Cheap regex on unsafe keywords:
   - `"dies"`, `"dead"`, `"murder"`, `"stabbing"`, `"court"`, `"trial"`, `"inquest"`, `"suicide"`, `"overdose"`, `"cancer diagnosis"`, `"crash victims"`
5. **Gemini safety classification**: ONE batched call classifying all remaining trends. Per-trend verdict: `{trend_id, safe: bool, reason: str}`. Drop unsafe ones. **Fails OPEN** — Gemini unavailable keeps keyword-filtered trends.
6. **Sort by traffic**: Parse strings like `"200K+"`, `"1M"` to integers
7. **Return**: Safe, unused trends sorted by traffic descending

**Failure policy:** Network/parse error → empty list, never crashes. Gemini unavailable → log warning, fail open.

**Key functions:**
- `_fetch_and_parse()` — fetch RSS with browser User-Agent
- `_parse_feed()`, `_detect_namespace()`, `_parse_item()`, `_parse_news_items()` — XML parsing with namespace support
- `_keyword_filter()` — fast regex pre-filter
- `_gemini_safety_filter()` — AI safety classification
- `render_trends_table()` — pretty-print table for `--dry-run`

**Smoke test:** `python -m pipeline.trends`

---

#### `pipeline/matcher.py` — Stage 2: Assign Topics to Sites

**Public entry:** `assign_topics(trends, sites, settings, gemini) -> list[Assignment]`

**Why this stage exists:** Prevent "scaled content abuse" — one trending topic must NOT run on every site. AI scores fit for each (trend, site) pair, then deterministic Python greedily selects highest-scoring assignments.

**Constraints enforced:**
- Per-trend cap: max `max_sites_per_topic` sites (default 3)
- Per-site cap: daily `posts_per_day_cap` minus already-posted-today
- Min score: `min_match_score` (default 5/10)

**Pipeline:**

1. **Gemini scoring**: One call scoring ALL (trend × site) pairs. Strict prompt: *"Reserve 8+ for strong, natural fits; be strict on niche specificity; score weak/tangential fits low."*
   - Input: `[{trend_id, title, top_snippet}]` + `[{site_id, niche, audience}]`
   - Output: `{scores: [{trend_id, site_id, score 0-10, angle, reason}]}`

2. **Deterministic selection (`_select`)**:
   - Filter: known trends/sites, score ≥ min_match_score
   - Sort: score DESC, trend_id, site_id (deterministic tiebreaker)
   - Greedy assign: skip if per-trend cap hit; skip if site capacity = 0

3. **Fill uncovered (`fill_uncovered`)**: Guarantees every active site gets ≥1 assignment in staggered runs. Uncovered sites get least-used trend with default angle.

**Failure policy:** Gemini unavailable → return NO assignments. Refuses to broadcast blindly. Loud warning logged.

**Key functions:**
- `_score_with_gemini()` — one Gemini call with strict evaluation system prompt
- `_extract_scores()` — normalize Gemini output (array vs dict vs wrapped)
- `_valid_candidates()` — filter to known, on-threshold pairs
- `_remaining_cap()` — today's remaining capacity = daily cap − posts today
- `_resolve_angle()` — prefer AI angle; fallback to round-robin from site's `angle_pool`
- `render_plan()` — pretty-print assignment plan for `--dry-run`

**Smoke test:** `python -m pipeline.matcher`

---

#### `pipeline/writer.py` — Stage 3: Generate Articles

**Public entry:** `write_articles(assignments, sites, settings, gemini, db) -> list[ArticlePackage]`

**Philosophy:** Generate factually-grounded articles using ONLY facts from the trend's `news_items`. Validate structure, word count, source links, FAQ completeness. Retry once with validation errors before skipping.

**Per-assignment generation (`_write_one`):**

1. **Fetch internal context** (best-effort, optional):
   - Recent posts from this site (for internal linking)
   - Existing categories (for reuse)

2. **Build generation prompt (`_build_prompt`)**: Large, detailed prompt embedding:
   - Site identity: name, niche, audience, tone, assigned angle
   - Target word count: random pick from site's `word_range`
   - Trend title + source material formatted as `[1] TITLE / SNIPPET / SOURCE / URL`
   - **Hard rules**: UK English only, use ONLY source facts, attribute claims, neutral register
   - **Required HTML structure**:
     - Opening paragraph(s)
     - `<h2>Background</h2>` context section
     - 1–2 main development `<h2>` sections
     - `<h2>FAQ</h2>` with 3–4 Q&As (also in `faq` JSON field)
     - `<h2>What this means for you</h2>` takeaway
     - ≥2 source links woven naturally
   - **HTML rules**: Only `h2, h3, p, ul, li, a, strong`; no `<h1>`, no markdown, no inline styles, no `<html>` wrapper
   - **Output**: Strict JSON with keys `{title, slug, meta_description, focus_keyword, tags[], category, html_content, image_query, faq[]}`

3. **Call Gemini (up to 2 attempts)**:
   - Initial call
   - **Validate (`_validate`)**: Check required keys, ≥3 `<h2>` sections, ≥2 source links in HTML, word count ±25% of target, FAQ has 3–4 items
   - **Repair locally (`_repair`)**: Trim title (≤60 chars), trim meta_description (≤155 chars), auto-append `<h2>Sources</h2>` if needed
   - If still errored: append errors to prompt, **retry once**
   - After 2 attempts: skip cleanly with warning

4. **Build package**: Trim/slugify, add FAQ as JSON-LD `<script>` tag, extract sources from trend

**Key functions:**
- `_build_prompt()` — heart of the system; multi-part, detailed prompt
- `_validate()` — structure/word count/link checks
- `_repair()` — local fixes to avoid unnecessary regeneration
- `_word_count()` — strip HTML, count whitespace-split words
- `_trim()` — trim to limit on word boundary
- `_slugify()` — kebab-case, max 80 chars
- `_faq_jsonld()` — Schema.org FAQ markup as JSON-LD `<script>` tag
- `write_preview()`, `_render_preview_html()` — browser-openable HTML previews for `--dry-run`

**Failure policy:** Gemini unavailable → make no articles. Per-assignment failures skip cleanly.

---

#### `pipeline/images.py` — Stage 4: Attach Featured Images

**Public entry:** `attach_images(packages, sites, settings, db, dry_run) -> dict`

Returns `{(site_id, trend_id): media_id | None}`

**Sourcing strategy (commercial-reuse licensed only):**

1. **Pexels API** — search on article's `image_query` (result cached per trend; one API call serves all sites covering that trend)
2. **Per-site variety** — each site covering same trend gets a DIFFERENT photo (tracked via `used_images` table + in-run set)
3. **Download + process** — resize to ≤1200px wide (downscale only), re-encode JPEG q80 (Pillow)
4. **Upload to WordPress** — with correct attribution caption
5. **Fallback chain** (on Pexels empty):
   - (a) Simplify query to first word, re-search Pexels
   - (b) Search Openverse (`license_type=commercial`) with CC attribution
   - (c) Proceed with no image (flagged in digest)

**Safety:** Never use trend's RSS `picture_url` — those are news-publisher images, not reuse-licensed. All errors degrade to "no image", never raise exceptions.

**Key functions:**
- `_attach_one()` — source, process, upload one package
- `_candidates_for_trend()` — Pexels + fallback Openverse, cached per trend
- `_pick_unused()` — first candidate not yet used for this trend; reuse first if exhausted
- `_pexels_search()`, `_openverse_search()` — query providers, normalize to `_Candidate` namedtuple
- `_pexels_caption()`, `_openverse_caption()` — attribution HTML with links
- `_process_and_upload()` — download → resize (Pillow) → upload (WPClient)
- `_download()` — stream with `MAX_DOWNLOAD_BYTES` safety cap (25 MB)
- `_to_jpeg()` — convert to RGB if needed, resize, encode JPEG q80 with optimization

---

#### `pipeline/publisher.py` — Stage 5: Publish to WordPress

**Public entry:** `publish(packages, media_map, sites, settings, db, dry_run) -> list[PublishResult]`

**Safety model (makes unattended runs safe):**

`review_mode` in `settings.yaml` controls what status each post gets:

| review_mode | WordPress post status | Human review? |
|---|---|---|
| `"pending"` | `pending` — sits in wp-admin drafts queue | Yes — must manually approve |
| `"live"` | `publish` — immediately public | No |
| `"sample"` | `sample_rate` fraction = pending, rest = `future` (scheduled) | Partial |
| `"auto"` | `future` (scheduled inside publish_window) | No |

**Scheduling logic (`_Scheduler` class):**

Determines UTC `date_gmt` for scheduled posts:

- Window: e.g., 09:00–18:00 `Europe/London` (converts to UTC)
- **Per-site constraint**: ≥20 min between consecutive posts on same site
- **Network-wide constraint**: unique UTC minute-keys (no two sites post at identical UTC minute)
- **Lead time**: ≥10 min from "now"
- **Algorithm**: Try 60 random times in window; if crowded, step past site's latest scheduled time

**Per-package publish (`_publish_one`):**

1. Resolve terms: get/create category + tag IDs via `WPClient`
2. Build payload: `{title, slug, content, excerpt, status, categories[], tags[], featured_media, date_gmt}`
3. Create post: `WPClient.create_post()`
4. Record success: `db.add_post()` (audit), then `db.add_used_topic()` (dedupe — only on success)
5. Return `PublishResult`

**Idempotency**: `used_topics` written ONLY after post created → failed run can retry next day.

**Per-site robustness**: One broken site never stops the rest. Errors caught, logged, added to digest.

**Dry-run mode**: Logs would-be posts, returns status `"would-pending"` / `"would-future"`. No DB writes, no actual WordPress posts.

**Key functions:**
- `_decide_status()` — map `review_mode` + `sample_rate` to WordPress post status string
- `_resolve_terms()` — best-effort WPClient calls for category/tag resolution
- `_Scheduler` — staggered scheduling engine
- `_resolve_tz()`, `_parse_hhmm()`, `_random_dt()`, `_minute_key()` — timezone + time utilities

---

### Main Orchestrator (`main.py`)

CLI entry point. Coordinates all 5 pipeline stages. Handles logging, site selection, digest assembly.

**Logging setup:** Console + rotating file handler (2 MB files, keep 5, UTF-8 safe for unicode titles).

**Site selection (`select_sites`):**
- Filter by `--sites` (comma-separated IDs) or `--canary-only`
- Skip kill-switch paused sites (from `site_health` DB)

**Three main modes:**

**1. Daily run (`run_daily`):**
- Fetch trends → match sites → write articles → attach images → publish → send digest
- All 5 stages run sequentially; one Gemini safety call + one matcher call, then per-assignment generation

**2. Staggered run (`run_staggered`, via `--stagger` or GitHub Actions):**
- Fetch trends once → match sites once → fill uncovered (guarantee every site ≥1 assignment)
- Group assignments by site, publish **site-by-site** with `--gap-minutes` (default 3 min) pause between sites
- One email digest at the end with all links
- Result: network never posts simultaneously; each trend reaches ≤3 sites

**3. Health check (`run_health`, weekly kill-switch):**
- Probe each site: check WP REST reachability, count recent successes/errors (7 days)
- Auto-pause if unreachable or ≥3 errors in last 7 days
- Kill-switch flag lives in DB; `select_sites()` skips flagged sites. Config file never modified.
- TODO: Auto-pause on Google Search Console impressions drop >50% WoW

**Digest assembly (`_build_digest`):**
- Collects published posts with live + edit URLs
- Lists errors (per-site failures), skipped trends, missing images, paused sites

**CLI flags:**

| Flag | Purpose |
|---|---|
| `--dry-run` | Full pipeline but never POST to WordPress |
| `--sites site1,site3` | Only run on specified site IDs |
| `--canary-only` | Only `status: canary` sites |
| `--health` | Run weekly kill-switch health check and exit |
| `--stagger` | Publish site-by-site with gap between each |
| `--gap-minutes N` | Gap between sites in staggered mode (default 3) |
| `--verbose` | Debug-level logging |

**Exit code:** Non-zero only on unhandled exception — cron alerts only on real failures, not per-site errors.

---

## SQLite State Database — Full Schema

**Location:** `data/state.db`

```sql
-- 7-day deduplication
CREATE TABLE used_topics (
    trend_id TEXT PRIMARY KEY,
    title    TEXT,
    used_on  DATE
);

-- Audit trail
CREATE TABLE posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     TEXT,
    trend_id    TEXT,
    wp_post_id  INTEGER,
    title       TEXT,
    url         TEXT,
    status      TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-(trend, site) image tracking
CREATE TABLE used_images (
    trend_id TEXT,
    site_id  TEXT,
    photo_id TEXT
);

-- Kill-switch
CREATE TABLE site_health (
    site_id    TEXT,
    checked_on DATE,
    note       TEXT,
    paused     INTEGER
);

-- Angle rotation
CREATE TABLE angle_rotation (
    site_id TEXT PRIMARY KEY,
    idx     INTEGER
);
```

---

## Resilience & Safety Summary

### Gemini API Resilience

- Multiple API keys round-robined per call
- Exponential backoff with jitter on 503/5xx
- Fallback model (`gemini-2.5-flash-lite`) after 2× consecutive 503s on primary
- Long cooldown (90s) when ALL keys simultaneously rate-limited

### Scaled-Content-Abuse Avoidance

- Max 3 sites per trend (configurable via `max_sites_per_topic`)
- Each site gets a unique editorial angle (AI-suggested or rotated from `angle_pool`)
- 7-day deduplication window: same trend never reused within a week

### Human Review Gates

- Default `review_mode: pending` = all posts as drafts, human must approve
- Sample mode: 10% held pending, rest scheduled
- Auto mode: all scheduled (only after canary proves quality over weeks)

### Kill-Switch

- Weekly health check auto-pauses unreachable sites or those with ≥3 publish errors in 7 days
- Flag stored in DB, never written to config YAML
- `select_sites()` checks DB flag at start of every run
- Paused sites reported in digest

### Per-Site Robustness

- One broken site never stops the network (try/except around each site)
- Errors logged and reported in digest, not raised to stop the run

---

## Go-Live Sequence

```
Phase 1 — DRY RUN (day 0)
  python main.py --dry-run
  → inspect data/preview/*.html
  → confirm 2 sites on same trend have genuinely different angles
  → confirm articles are factually grounded

Phase 2 — CANARY (weeks 1–4)
  config/settings.yaml: review_mode: pending
  config/sites.yaml:    3-4 sites → status: canary
  command: python main.py --canary-only
  → every post lands as draft → approve/reject in wp-admin

Phase 3 — WIDEN (weeks 4+)
  config/sites.yaml: all sites → status: active
  keep: review_mode: pending
  command: python main.py (all sites, still pending)

Phase 4 — SAMPLE (weeks 6+)
  config/settings.yaml: review_mode: sample
  → 90% scheduled, 10% pending for spot-check

Phase 5 — AUTO (months 2+)
  config/settings.yaml: review_mode: auto
  → all posts scheduled, no human loop
  → only after quality proven over weeks of canary
```

---

## Cron / Scheduling Options

**Linux/Mac cron (recommended — persistent disk, no minute cost):**
```bash
# Daily at 06:30 UTC
30 6 * * * cd /home/USER/trendpress && python main.py >> logs/cron.log 2>&1

# Weekly health check — every Monday 07:00 UTC
0 7 * * 1 cd /home/USER/trendpress && python main.py --health >> logs/health.log 2>&1
```

**GitHub Actions (`.github/workflows/publish.yml`):**
```yaml
on:
  schedule:
    - cron: "0 */3 * * *"   # every 3 hours UTC
```
Note: state cache is ephemeral. Trends may repeat if cache is evicted. VPS cron preferred.

**Windows (Task Scheduler or startup scripts):**
```batch
run.bat --stagger --gap-minutes 10
```

---

## Example Digest Output

```
trendpress daily — 5 post(s)

Posts (5):
- [site1] "UK Budget Announcement: What You Need to Know" — pending
    url:  https://londonheadline.uk/?p=1234
    edit: https://londonheadline.uk/wp-admin/post.php?post=1234&action=edit
- [site3] "Manchester Angle on UK Budget 2025" — future
    url:  https://manchestereveningchronicle.co.uk/?p=567
    edit: https://manchestereveningchronicle.co.uk/wp-admin/post.php?post=567&action=edit
...

Errors (1):
- [site2] WPError: POST /wp-json/wp/v2/posts → 401: Invalid credentials

Skipped trends (2):
- "Celebrity Death" (unsafe — keyword filter)
- "Local Weather" (already used 3 days ago)

Missing images (1):
- [site5] "Rail Strike Latest" — no licensed image found

Paused sites (1):
- site2: API unreachable (auto-paused by kill-switch)
```

---

## Directory Structure

```
trendpress-public-repo/
├── main.py                  # CLI entry point + orchestrator
├── requirements.txt         # Python dependencies
├── SETUP.md                 # Setup and go-live guide
├── DEPLOY.md                # GitHub Actions deployment guide
├── run.bat / run.ps1        # Windows launchers
├── dry-run.bat              # Quick dry-run on site1
├── publish-site1.bat        # Quick live run on site1
│
├── config/
│   ├── settings.yaml        # Global runtime tuning
│   └── sites.yaml           # 10-site WordPress network profile
│
├── core/
│   ├── __init__.py          # Path constants, config loaders
│   ├── db.py                # SQLite state (5 tables)
│   ├── gemini.py            # Gemini API wrapper (key rotation, backoff)
│   ├── notify.py            # Email + Telegram digest delivery
│   └── wp.py                # WordPress REST API client
│
├── pipeline/
│   ├── __init__.py          # Data contracts (Trend, Assignment, ArticlePackage, ...)
│   ├── trends.py            # Stage 1: Google Trends RSS → safe Trend objects
│   ├── matcher.py           # Stage 2: AI scoring + deterministic site assignment
│   ├── writer.py            # Stage 3: Gemini article generation + validation
│   ├── images.py            # Stage 4: Pexels/Openverse image sourcing + WP upload
│   └── publisher.py         # Stage 5: WordPress post creation + scheduling
│
└── data/                    # Created at runtime (gitignored)
    ├── state.db             # SQLite persistent state
    ├── preview/             # Dry-run HTML previews
    └── logs/                # Rotating log files
```
