# kobo2readwise

Sync highlights from **sideloaded** Kobo books to [Readwise](https://readwise.io) —
straight from your browser, no desktop app to install.

> [!IMPORTANT]
> **Chromium-only, desktop-only.** kobo2readwise reads your Kobo database locally
> using the [File System Access API](https://developer.mozilla.org/en-US/docs/Web/API/File_System_API),
> which today only works in **Chrome or Edge on desktop**. Firefox, Safari and
> mobile browsers are not supported; the app shows a clear notice instead of
> failing cryptically.

## What it does

Highlights from books you put on your Kobo yourself (EPUB/PDF via Calibre, email
or USB) live only in the device's local `KoboReader.sqlite` — they never reach the
Kobo cloud, so Readwise can't fetch them automatically. kobo2readwise reads that
database **in the browser** (File System Access API + [sql.js](https://sql.js.org))
and forwards the highlights to Readwise through a thin proxy.

Books from the Kobo store, Kobo Plus and library loans already sync through
Readwise's own integration — those are out of scope here.

### Your token is never stored or logged

The proxy forwards your Readwise token to Readwise and then forgets it. There is
**no request-body logging, no persistence, and no database.** This is the whole
trust model of the app — see [`app.py`](app.py).

## Run it locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync
uv run uvicorn app:app --reload
```

Then open <http://localhost:8000> in Chrome or Edge.

### Tests & linting

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Deploy

Hosted on [Fly.io](https://fly.io) as a single scale-to-zero app.

### One-time setup

You don't need to create the Fly app by hand — the deploy workflow does it
idempotently on every run (`flyctl status … || flyctl apps create kobo2readwise`),
so the first release both creates `kobo2readwise.fly.dev` and deploys to it.

You only need to provide a token:

```bash
# A token that can create AND deploy the app (org-scoped, for hands-off first deploy):
fly tokens create org
#   → GitHub → Settings → Secrets and variables → Actions → new secret FLY_API_TOKEN
```

- The app is created in the `personal` org by default. To use a different org, set
  a repo **variable** `FLY_ORG`.
- Prefer a narrow, app-scoped `fly tokens create deploy` instead? Then create the app
  once yourself (`fly apps create kobo2readwise`); the workflow's `status` check will
  see it and skip creation.
- Optional custom domain: `fly certs add your-domain.example`.

No Readwise secrets are needed — every user supplies their own token in the browser.

### Release & deploy flow

Deploys are gated on releases, driven by
[release-please](https://github.com/googleapis/release-please):

```
feature branch ──PR──▶ CI (ruff · pytest · docker build · commitlint)
                         │  merge (conventional commit)
                         ▼
                       main ──▶ release.yml
                                 ├─ release-please keeps a release PR up to date
                                 └─ merge that release PR:
                                      ├─ tag + GitHub release + CHANGELOG + version bump
                                      └─ ensure Fly app exists → flyctl deploy
                                         (only when a release was created)
```

Ordinary feature merges update the pending release PR but **do not** deploy. Only
merging the release PR (a real version bump) triggers a production deploy.

## Commit convention

This repo uses [Conventional Commits](https://www.conventionalcommits.org/); commit
types drive the version bump:

| Type | Effect | Example |
| --- | --- | --- |
| `feat:` | minor bump | `feat: show synced highlight count in the UI` |
| `fix:` | patch bump | `fix: handle a missing .kobo folder gracefully` |
| `docs:` `chore:` `ci:` `refactor:` `test:` `style:` | no bump | `ci: cache uv dependencies` |
| `feat!:` / `BREAKING CHANGE:` footer | major bump | `feat!: drop Python 3.11 support` |

PR commits are checked by commitlint in CI, so a non-conventional message fails
the build.

## License

[MIT](LICENSE)
