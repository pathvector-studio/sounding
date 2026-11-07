# Changelog

All notable changes to Sounding are written here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/) as described in [RELEASING.md](RELEASING.md#versioning).

Each pull request adds its lines under `[Unreleased]` (CI checks it; see RELEASING.md). Sections: Added, Changed, Deprecated, Removed, Fixed, Security, and **Upgrade notes** for anything an operator has to do (new `.env` keys, manual steps).

## [Unreleased]

### Added
- Project scaffold: concept, MVP design (`docs/DESIGN.md`), roadmap, development workflow, compose skeleton and `.env.example`. Nothing runs yet.
- Phase 0 spike: IETF mailing lists are fetched over anonymous IMAP (`imap.ietf.org`), with rsync monthly mbox files as the fallback. Measurements and the decision are in `docs/spike-phase0.md`.

### Upgrade notes
- `.env.example`: `IETF_IMAP_HOST` now defaults to `imap.ietf.org`; new `IETF_RSYNC_BASE`. `FETCH_CONTACT` is sent to IETF as the anonymous IMAP password.
