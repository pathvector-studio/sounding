# Releasing

A release is a `vX.Y.Z` tag. Once `install.sh` exists it will install the latest tag, so a tag is what users get; `main` is only for people who ask for it. The site (`site/`, https://sounding.pathvector.dev) describes the latest tag.

Roles: whoever prepares the release (a person or an agent session) opens the release PR. The maintainer pushes the tag and publishes the GitHub Release.

## Versioning

[Semantic Versioning](https://semver.org/). The version is set in one place, [`VERSION`](VERSION). (A `scripts/version.py` that writes every copy and a CI check for drift come with the first code, as in biblioflow.)

The public interface is what other people's setups depend on: `.env` keys, `config/profile.yaml`, the HTTP API and the Atom feed URLs, the database (through migrations), backups, the compose services and volumes, and `install.sh` options.

| Bump | When |
|---|---|
| **MAJOR** | A change that breaks the public interface (renamed or removed `.env` / profile keys without a transition, a migration that needs manual steps, renamed compose services or volumes) |
| **MINOR** | Backward-compatible additions (new source adapters, new delivery channels, new optional keys with defaults, migrations that run by themselves) |
| **PATCH** | Fixes, documentation, refactoring, prompt fixes that keep the output schema, patch and minor dependency updates |

**While the version is 0.x**: a breaking change bumps MINOR and must have a "Breaking" line and Upgrade notes in the changelog. 0.x releases are called "preview".

## Checklist before tagging

- CI is green on the release commit.
- `[Unreleased]` in `CHANGELOG.md` has become the version's section, with Upgrade notes where needed.
- `VERSION` matches the tag.
- Changes to `site/` or `install.sh` are approved by the maintainer (they publish to the live site).

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
