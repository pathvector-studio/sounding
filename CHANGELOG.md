# Changelog

All notable changes to Sounding are written here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/) as described in [RELEASING.md](RELEASING.md#versioning).

Each pull request adds its lines under `[Unreleased]` (CI checks it; see RELEASING.md). Sections: Added, Changed, Deprecated, Removed, Fixed, Security, and **Upgrade notes** for anything an operator has to do (new `.env` keys, manual steps).

## [Unreleased]

## [0.1.0] - 2026-11-12

First preview release. Sounding fetches the IETF mailing lists and RSS / Atom feeds you choose, picks what matches your interest profile, and sends a daily digest of where each discussion stands, with links to the sources. It runs on your machine with Docker Compose and uses your own LLM key; with the default `LLM_PROVIDER=none` nothing is sent to an LLM.

### Added
- **Install**: `curl -sSL https://sounding.pathvector.dev/install.sh | bash` clones the latest release, creates `.env` with generated secrets (asking only for the fetch contact and an optional LLM provider and key), copies the example profile and starts the stack. Running it again updates and leaves `.env` alone.
- **Fetching**: IETF mailing lists over anonymous IMAP (thread reconstruction, auto-mail detection, missing ancestors fetched) and RSS / Atom with conditional GET, on a schedule (`FETCH_INTERVAL_MINUTES`). Senders are kept as display names and HMAC identifiers, never addresses; fetched bodies are deleted after `CONTENT_RETENTION_DAYS`.
- **"Where it stands"**: with `LLM_PROVIDER=anthropic` or `openai`, each followed IETF conversation gets a summary of what is contested, what is agreed, what is open and what moved, updated daily from new messages only, within `LLM_DAILY_BUDGET_USD`. RSS items get a short summary.
- **Digest and delivery**: one digest per day (`profile.digest.schedule`), chosen by your profile with the reason for every pick recorded; read it in the web UI, the JSON API or an Atom feed, or have it sent by email (SMTP) or webhook (Discord / Slack), each channel at most once.
- **Web UI** (`sounding-web`, `http://localhost:8080`): latest digest, history, each conversation's versions and why it was chosen, a status page. It connects to the database with a read-only role, has an optional scrypt password login, and refuses to listen beyond loopback without a password.
- **Deployment modes**: this machine only (default), LAN with a password (`BIND_ADDRESS`), Tailscale (`--profile tailscale`) or Cloudflare Tunnel + Access (`--profile cloudflare`); see README 「公開のしかた」.
- **Backup**: `scripts/backup.sh` and `scripts/restore.sh --check | --yes`.
- CLI: `python -m sounding status | fetch --once | summarize --once | digest --once | show | show-digest | deliver | hash-password`.
- Introduction site at https://sounding.pathvector.dev (Japanese and English).

### Upgrade notes
Only for checkouts of `main` from before this release (new installs need nothing):
- **Add `WEB_DB_PASSWORD` to `.env` before `docker compose up -d`** (generate it with `openssl rand -hex 32`; see `.env.example`). The worker sets it as the password of the `sounding_web` role at every start, and `sounding-web` starts only after the worker is healthy. New optional key `BIND_ADDRESS`.
- `sounding-web` now starts with `docker compose up` (it no longer needs `--profile web`) and refuses to start unless `SESSION_SECRET` is at least 32 bytes and not a `change-me` value (`openssl rand -hex 32`). With `WEB_PASSWORD_HASH` empty there is no login, a warning is logged and every page shows a banner: set a password (`docker compose exec sounding-worker python -m sounding hash-password`) unless the UI sits behind Tailscale or an access proxy. In `.env`, put the hash in single quotes (or write each `$` as `$$`) so compose does not expand it. The new dependencies are fastapi, uvicorn and jinja2.
- LLM settings (phase 2): with `LLM_PROVIDER=anthropic`, empty or `change-me` model names now mean `claude-opus-5-5` (main) and `claude-haiku-5-5` (cheap); `openai` needs both models set. Run `python -m sounding migrate` (adds `llm_calls`, `conversation_states`; phase 3 adds `item_summaries`, `digests`, `selections`, `deliveries`).
- IETF lists are skipped (job status `skipped`) until both `FETCH_CONTACT` and `IDENTITY_SECRET` are set; RSS still runs. The image is built with `docker build --build-context repo=. backend` (compose does this itself).
- `.env.example`: `IETF_IMAP_HOST` now defaults to `imap.ietf.org`; new `IETF_RSYNC_BASE`. `FETCH_CONTACT` is sent to IETF as the anonymous IMAP password.
- New keys for phase 1 (see the example env file): `IDENTITY_SECRET` (generate with `openssl rand -hex 32`; do not change it after the first run, because author keys are derived from it), `IETF_BACKFILL_DAYS`, `IETF_MAX_MESSAGES_PER_RUN`, `IETF_ANCESTOR_MAX_DEPTH`, `IETF_ANCESTOR_MAX_AGE_DAYS`.
