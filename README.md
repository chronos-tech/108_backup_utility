# WordPress Sitemap Fetcher & Encrypted HTML Cache

Discovers post/page URLs from a WordPress site's core XML sitemaps, fetches the
HTML politely, and stores it **age-encrypted** in a **public** Git repository.
URLs never appear in plaintext: they are AES-256-GCM encrypted JSON keys.

> This is encryption of data at rest in a public repo. It is **not** a claim that
> the system is "secure" in general. Read [Security limitations](#security-limitations).

## Repository layout

```text
.github/workflows/refresh.yml   sitemap discovery (every 3 days / manual / dispatch)
.github/workflows/update.yml    page fetching     (daily / manual / dispatch)
cache/<sha256>.html.age         age-encrypted HTML, one file per URL
data/cache.json                 { AES-encrypted URL : metadata }
data/premium.json               same, for premium pages
src/config.py                   env-only config + validation
src/crypto.py                   AES-GCM URL tokens, SHA-256 names, age encryption
src/sitemap.py                  safe sitemap parsing + post/page filtering
src/fetcher.py                  HTTP client: throttle, retries, redirects, robots.txt
src/storage.py                  JSON load/validate/append/atomic save, locking
src/utils.py                    URL normalization, atomic writes, logging
refresh.py / update.py          the two entry points
scripts/gen_keys.py             local key generation
scripts/decrypt.py              decrypt one cached page locally
scripts/commit_push.sh          safe commit + rebase-retry push (never force)
tests/                          pytest suite (mocked HTTP, no real sites)
```

## Local setup

Python 3.11+ (CI uses 3.12). No age CLI is needed: encryption uses the `pyrage`
library (Python bindings for the Rust `rage` implementation) fully in memory.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

### Generate keys

```bash
python scripts/gen_keys.py
```

It prints an AES-256 key (64 hex chars), an age public recipient (`age1...`) and an
age private identity (`AGE-SECRET-KEY-...`). Alternatively: `openssl rand -hex 32`
for the AES key and `age-keygen -o key.txt` (official CLI) for the age pair.
Save the identity to a file **outside the repo** (`key.txt` is git-ignored as a safety net).

### Run locally

```bash
export BASE_URL="https://your-site.example" AES_KEY="<64 hex>" AGE_RECIPIENT="age1..."
python refresh.py                       # register new URLs
python update.py                        # fetch everything pending
MAX_VALID_PAGES=10 python update.py     # stop after 10 validly cached pages
```

Decrypt one page (only the URL's SHA-256 is used; no AES key needed):

```bash
python scripts/decrypt.py --identity key.txt --url https://your-site.example/some-post/ > page.html
```

## GitHub configuration

| Name | Kind | Used by | Notes |
|---|---|---|---|
| `AES_KEY` | **Secret** | both | 64 hex chars (or base64 of 32 bytes) |
| `COOKIE` | **Secret** (optional) | update, only when premium fetching is ticked | session cookie of an account with legitimate access |
| `BASE_URL` | **Secret recommended**, Variable accepted | both | see note below |
| `AGE_RECIPIENT` | Variable (public) | update | `age1...` public key |
| `MAX_WORKERS` | Variable (optional) | both | default 2, max 8 |
| `REQUEST_TIMEOUT` | Variable (optional) | both | seconds, default 30 |

**BASE_URL note:** GitHub does not mask Variables, and Actions prints step `env:`
values in the log - which is public in a public repo. The workflows read
`secrets.BASE_URL || vars.BASE_URL`; use a **Secret** to keep the site name out of logs.

Optional env (local or add to workflow): `REQUEST_DELAY_MIN` / `REQUEST_DELAY_MAX`
(default 2 / 5 s), `MAX_VALID_PAGES` (0 = all), `MAX_RUNTIME_MINUTES` (0 = no limit), `USER_AGENT`.

Also set **Settings -> Actions -> General -> Workflow permissions** to allow read/write
(the workflows request `contents: write` explicitly).

## Workflows

| Workflow | Schedule | Manual inputs | repository_dispatch type |
|---|---|---|---|
| `refresh.yml` | `17 3 */3 * *` (every 3rd day of the month) | none | `refresh-sitemap` |
| `update.yml` | `43 4 * * *` (daily) | `download_all` (default **on**), `fetch_premium` (default off) | `update-cache` |

* **Download all vs first 10 valid:** untick `download_all` on a manual run to stop after
  **10 validly cached pages** (failures, 404s, robots-skips and premium detections do not
  count; remaining pages stay pending). Scheduled runs and `repository_dispatch` always
  download everything. Trigger a dispatch with
  `gh api repos/OWNER/REPO/dispatches -f event_type=update-cache`.
* **Premium:** scheduled/dispatch runs always use `FETCH_PREMIUM=false` and never receive the cookie.
* Both workflows share the concurrency group `repo-data-writers` (queued, never cancelled
  mid-run; GitHub keeps only one *pending* run per group), run only on the default branch,
  have no `pull_request` trigger, and run the tests before touching data.
* The "every 3 days" cron is day-of-month based, so the gap shrinks at month ends.

**Interruptions (cancel, timeout, crash):**

* The commit step uses `if: always()`, so it also runs after a failure, a manual cancel or a timeout.
* `update.py` turns SIGTERM/SIGINT (what GitHub sends on cancel) into a graceful stop: it saves all
  pages fetched so far, then exits with code 130 without waiting for in-flight requests.
* `update.yml` sets `MAX_RUNTIME_MINUTES=320` (job timeout is 350), so a long run stops cleanly and
  still has time to commit. Remaining pages stay pending for the next run.
* While running, a background loop commits a checkpoint every 20 minutes (best effort; a rejected
  push is retried by the final step). If the runner itself dies hard (lost/killed with no cleanup
  step), at most the last 20 minutes of work is lost; already-pushed checkpoints are kept.
* GitHub gives cancelled jobs only a short window (about 5 minutes) for `always()` steps.

Exit codes: `0` ok, `1` fatal (crypto/storage - data untouched), `2` bad config,
`3` finished with problems (some child sitemaps failed / site kept answering 403/429).
The commit step runs even then, so partial progress is saved.

## Encryption architecture

* **URL identifier:** `v1.` + base64url(`nonce(12) || AES-256-GCM ciphertext+tag`), fresh
  random nonce per call (non-deterministic), AAD `wp-sitemap-cache:url:v1`. It is the JSON key.
  Duplicates are found by decrypting all keys in memory and comparing normalized URLs.
* **Filename:** `cache/<SHA-256 of the normalized URL>.html.age`. Commit messages and filenames never contain a URL.
* **HTML:** age X25519 to `AGE_RECIPIENT`, in memory, written atomically.
* **Normalization:** lowercase scheme/host, default port and fragment dropped, empty path -> `/`;
  path, trailing slash and query are kept.
* On load, every key must decrypt, every `sha` must equal SHA-256 of its decrypted URL, and
  no URL may appear under two tokens - otherwise the run stops without writing.

## Metadata semantics

`cache.json` record: `status` (true = HTML fetched, validated, encrypted), `sha`, `file`
(deterministic, assigned at registration), `type` (`post`/`page`), `registered_at`,
`fetched_at`, `http_status`, plus `last_checked_at` / `fetch_failures` after failed attempts.

`premium.json` record (a URL lives in exactly one file; the same token is moved):
`status: false, file: null` = detected, not cached (default); `status: true, file: "cache/..."`
= fetched with a valid session and encrypted. Extra: `detected_at`, `last_checked_at`, `http_status`.

`refresh.py` is **append-only** and verifies that before saving: it never removes or edits
a record. `update.py` only changes processing fields of the records it handles.

## Premium behaviour

Marker `/2014/11/login-VIP.jpg` in the raw HTML marks a page premium.

* Unknown pages are **always fetched anonymously first**, so premium detection never depends
  on the cookie. Premium pages are registered in `premium.json` and not cached.
* `FETCH_PREMIUM=true` **and** `COOKIE` set: a second, authenticated request is made; only if
  the marker is gone and the content validates as HTML is it encrypted and stored. A login
  redirect, marker still present, or 401/403 keeps the record unfetched.
* `FETCH_PREMIUM=true` without a cookie: warning, premium pages are not fetched.
* Known premium pages are not requested again unless premium fetching is enabled.
* The cookie is attached only to explicit premium requests and never to redirects off-site.
* Consequence: if your site requires login for *all* pages, they will be treated as premium.

## Politeness

Identifiable User-Agent, robots.txt honored (including `Crawl-delay`; unreachable robots.txt
means no page fetches that run), global 2-5 s spacing, max 2 workers, 3 retries with
exponential backoff, `Retry-After` honored (over 120 s = stop), no retries on 401/403,
stop after 5 consecutive 403/429, same-site redirects only. No evasion of any kind.
Sitemaps are fetched without a robots check (that is their purpose).
Only the default `wp-sitemap-posts-(post|page)-N.xml` names are recognized (not `?sitemap=` URLs).

## Security limitations

Threat model: protect URLs and content **at rest in a public repo**, against people who read
the repo or its history and do not have `AES_KEY` / the age identity.

* Anyone with `AES_KEY` can decrypt every URL identifier; anyone with the age **identity** can
  decrypt every page. Lose either and that protection is gone; rotating keys does not
  un-publish old ciphertext.
* **GitHub Actions runners see plaintext** (URLs, cookie, HTML, AES key) while running. A
  malicious or compromised workflow/dependency/Action could exfiltrate them. Secrets are not
  available to fork PRs, but anyone with write access can read them via a workflow change.
  Consider pinning Actions to commit SHAs and protecting the default branch.
* Public metadata still leaks: number of URLs, post/page split, premium count, timestamps,
  HTTP statuses, commit times and **encrypted file sizes**.
* Filenames are an **unsalted SHA-256 of the URL**: anyone can test whether a *guessed* URL is
  cached. Required by the design; it also reveals which URLs exist if guessable.
* AES-GCM tokens are randomized, so identical URLs cannot be linked across tokens - but
  duplicates in the file are impossible by construction anyway.
* Deleting a temp file is not secure erasure on hosted runners; this project avoids plaintext
  files entirely (in-memory encryption).
* **Git history is permanent.** If plaintext URLs/HTML were ever committed to this repo
  (before this system), they remain in history until the history is rewritten and caches
  purged; forks and clones keep them. Use a fresh repository.
* Premium handling relies on one marker string; a site change can break detection.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Invalid configuration: ...` (exit 2) | Missing/invalid env vars; the message lists every problem (never values). |
| `AES-GCM authentication failed` | Wrong `AES_KEY` for existing data. Nothing was written. |
| `... is not valid JSON` / `duplicate keys` | Corrupt metadata (e.g. bad merge). Restore from git. |
| `no post/page child sitemaps found` | Site doesn't expose `wp-sitemap-posts-post-N.xml` or `-page-N.xml` (SEO plugin sitemaps, disabled core sitemaps). |
| `robots.txt unreachable` | Page fetches are skipped for the run; retry later. |
| Stopped early, exit 3 | 5 consecutive 403/429. Wait; do not raise concurrency. |
| Push rejected | Script rebases and retries; real conflicts fail loudly. Never force-push. |
| Scheduled runs stop | GitHub disables schedules in public repos after 60 days without repository activity. |
