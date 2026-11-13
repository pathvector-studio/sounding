# Releasing

A release is a `vX.Y.Z` tag. `install.sh` installs the latest tag, so a tag is what users get; `main` is only for people who ask for it (`SOUNDING_REF=main`). The site (`site/`, https://sounding.pathvector.dev) describes the latest tag, and its `/install.sh` is the `install.sh` attached to the latest GitHub Release.

Roles: whoever prepares the release (a person or an agent session) opens the release PR. The maintainer pushes the tag and publishes the GitHub Release.

## Versioning

[Semantic Versioning](https://semver.org/). The version is set in one place, [`VERSION`](VERSION). `scripts/version.py set X.Y.Z` writes every copy (`backend/pyproject.toml`, `backend/uv.lock`), and CI fails when a copy drifts or when a pushed tag is not `v$(cat VERSION)`. The app reads `VERSION` itself (it is copied into the image).

The public interface is what other people's setups depend on: `.env` keys, `config/profile.yaml`, the HTTP API and the Atom feed URLs, the database (through migrations), backups, the compose services and volumes, and `install.sh` options.

| Bump | When |
|---|---|
| **MAJOR** | A change that breaks the public interface (renamed or removed `.env` / profile keys without a transition, a migration that needs manual steps, renamed compose services or volumes) |
| **MINOR** | Backward-compatible additions (new source adapters, new delivery channels, new optional keys with defaults, migrations that run by themselves) |
| **PATCH** | Fixes, documentation, refactoring, prompt fixes that keep the output schema, patch and minor dependency updates |

**While the version is 0.x**: a breaking change bumps MINOR and must have a "Breaking" line and Upgrade notes in the changelog. 0.x releases are called "preview".

**Pre-releases**: use `vX.Y.Z-rc.N` only for a release that changes migrations or the install flow. The installer skips rc tags, so testers opt in with `SOUNDING_REF=vX.Y.Z-rc.N`. An rc is published as a GitHub "Pre-release".

## 1. Prepare (release PR)

On a branch `release/vX.Y.Z` from an up-to-date `main`:

```bash
scripts/version.py set X.Y.Z
```

In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a new empty `## [Unreleased]` above it, and check that the Upgrade notes cover every new `.env` key and manual step. If the site describes the version (`site/index.html`, `site/en/index.html`), update it in the same PR. Open the PR and merge it when CI is green (it changes `site/`, so the maintainer approves the merge).

## 2. Checklist before tagging

On the merged release commit:

- [ ] **Secrets and personal details**: CI's `private-info` job passed on the push to main (all files and every commit's e-mails and messages, plus gitleaks on the whole history).
- [ ] **CI green** on that commit: private-info, version, compose, backend, installer, audit-backend (no high or critical advisories in production dependencies, licenses).
- [ ] **Fresh install** of the commit with its own names, never against the dev stack: `SOUNDING_DIR=<empty dir> SOUNDING_INSTANCE=sounding-rel SOUNDING_PORT=18080 SOUNDING_REF=<branch or tag> bash install.sh`. All three containers healthy, `/healthz` and `/` return 200, `docker compose exec sounding-worker cat /app/VERSION` is the new version. Then `docker compose down -v` there.
- [ ] **Upgrade** (from the second release on): install the previous tag the same way, take `scripts/backup.sh`, run the installer again with the new ref, check that migrations ran, the digests are still there, and `scripts/restore.sh --check` restores the old backup.
- [ ] The changelog section reads correctly as release notes.

## 3. Tag and publish

Done by the maintainer (the release commit is on `main`):

```bash
git switch main && git pull
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

The tag push runs CI again, including `version.py check --tag`. When it is green:

```bash
awk '/^## \[X.Y.Z\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md > /tmp/notes.md
gh release create vX.Y.Z --title "vX.Y.Z" --notes-file /tmp/notes.md --latest install.sh
```

Attaching `install.sh` is what makes `https://sounding.pathvector.dev/install.sh` serve it (the site redirects to `releases/latest/download/install.sh`). 0.x releases are published as normal releases (not "Pre-release") so the latest one is marked Latest. For an rc, use `--prerelease --latest=false`.

Do not move or delete a published tag. A mistake is fixed in the next PATCH.

## 4. After publishing

- [ ] `curl -sSLI https://sounding.pathvector.dev/install.sh` redirects to the new release's `install.sh`, and `curl -sSL https://sounding.pathvector.dev/install.sh | SOUNDING_NO_START=1 bash` picks `vX.Y.Z`.
- [ ] Announce, linking the GitHub Release.

## Rolling back

Users go back with `SOUNDING_REF=vX.Y.(Z-1)` and the installer. Migrations are not undone by rolling back the code: if the new version migrated the database, restore the backup taken before the update (`scripts/restore.sh --yes <snapshot>`). Then fix the problem and release a PATCH.

## Site (Cloudflare Workers)

The introduction site is the static `site/` (Japanese at `/`, English at `/en/`, no build step), served as a Cloudflare Worker with static assets ([`wrangler.toml`](wrangler.toml)), the same way as the other pathvector sites. Pushing to `main` deploys it through Workers Builds.

- `install.sh` is not on the site. `site/_redirects` sends `/install.sh` (302) to the `install.sh` attached to the latest GitHub Release, so users get the released installer, never an unreleased one from `main`. Until the first Release exists, `/install.sh` is a 404 there.
- `site/_headers` sets the security headers.
- Preview locally: `npx wrangler dev` (http://localhost:8787).

Workers Builds settings (Cloudflare dashboard, done once by the maintainer):

- Workers & Pages → Create → Import a repository → `pathvector-studio/sounding`; production branch `main`; root directory `/`.
- Build command: `python3 scripts/check-private-info.py` (a failing check stops the deploy). Deploy command: `npx wrangler deploy`.
- Build watch paths: include `site/*` and `wrangler.toml`, so app changes do not redeploy the site.
- The custom domain `sounding.pathvector.dev` comes from `routes` in `wrangler.toml` (wrangler creates the DNS record and certificate on the first deploy).

Merging a PR that changes `site/`, `wrangler.toml` or `install.sh` is a production publish. It needs the maintainer's approval every time.

## If a secret or personal detail was published

1. Revoke or rotate the secret first (LLM keys, webhook URLs, SMTP passwords, database and session secrets). A rewritten history does not undo a leak.
2. Remove it from the files in a normal PR.
3. Only with the maintainer's approval: rewrite the history, or accept it and record the commit in `scripts/check-private-info.py` with the reason.

## Changelog

[`CHANGELOG.md`](CHANGELOG.md) uses Keep a Changelog, with an extra **Upgrade notes** section.

- The pull request that makes a change adds one to three lines under `[Unreleased]`, written for users. An agent session that merged a change without a line adds it before the session ends.
- CI fails a pull request that does not touch `CHANGELOG.md`, unless it has the `skip-changelog` label (CI-only, test-only or typo changes) or comes from Dependabot.
- At release, `[Unreleased]` becomes the version's section, and that section is the release notes.
