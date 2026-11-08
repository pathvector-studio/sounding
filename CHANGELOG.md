# Changelog

All notable changes to Sounding are written here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/) as described in [RELEASING.md](RELEASING.md#versioning).

Each pull request adds its lines under `[Unreleased]` (CI checks it; see RELEASING.md). Sections: Added, Changed, Deprecated, Removed, Fixed, Security, and **Upgrade notes** for anything an operator has to do (new `.env` keys, manual steps).

## [Unreleased]

### Added
- Project scaffold: concept, MVP design (`docs/DESIGN.md`), roadmap, development workflow, compose skeleton and `.env.example`. Nothing runs yet.
- Phase 0 spike: IETF mailing lists are fetched over anonymous IMAP (`imap.ietf.org`), with rsync monthly mbox files as the fallback. Measurements and the decision are in `docs/spike-phase0.md`.
- Backend skeleton (`backend/`): the shared ingestion schema (sources, items, conversations, events, job_runs) with its first Alembic migration, and a CLI with `python -m sounding migrate` and `status`. The worker and fetching come in the next pull requests of phase 1.
- Phase 1 (fetching only, no summaries or delivery yet): IETF mailing lists are fetched over anonymous IMAP with thread reconstruction, auto-mail detection and ancestor completion; RSS/Atom feeds are fetched with conditional GET.
- `sounding-worker` runs the fetch on a schedule (`FETCH_INTERVAL_MINUTES`, once at start) and clears old message bodies daily (`CONTENT_RETENTION_DAYS`); `python -m sounding` gains `fetch --once`, `retention --once`, `status` and `sources`. `sounding-web` is off by default (`--profile web`) until phase 3.

### Upgrade notes
- IETF lists are skipped (job status `skipped`) until both `FETCH_CONTACT` and `IDENTITY_SECRET` are set; RSS still runs. The image is built with `docker build --build-context repo=. backend` (compose does this itself).
- `.env.example`: `IETF_IMAP_HOST` now defaults to `imap.ietf.org`; new `IETF_RSYNC_BASE`. `FETCH_CONTACT` is sent to IETF as the anonymous IMAP password.
- New keys for phase 1 (see the example env file): `IDENTITY_SECRET` (generate with `openssl rand -hex 32`; do not change it after the first run, because author keys are derived from it), `IETF_BACKFILL_DAYS`, `IETF_MAX_MESSAGES_PER_RUN`, `IETF_ANCESTOR_MAX_DEPTH`, `IETF_ANCESTOR_MAX_AGE_DAYS`.
