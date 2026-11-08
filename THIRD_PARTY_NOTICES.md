# Third-party notices

Sounding itself is MIT-licensed (see `LICENSE`). The source repository contains no third-party code.
The Docker image built from `backend/` and the images `docker-compose.yml` pulls contain the software
below. All of it is used unmodified.

`scripts/check-licenses.py` runs in CI (the Audit workflow) on every pull request. It refuses
production dependencies under GPL, AGPL, SSPL, BUSL, Elastic, Commons Clause or non-commercial terms.

## Components that need attribution

| Component | Where | License | Source |
|---|---|---|---|
| psycopg, psycopg-binary (PostgreSQL driver; installed from the PyPI wheel, dynamically linked with libpq, never modified) | backend image | LGPL-3.0 | https://github.com/psycopg/psycopg |
| certifi (dependency of `httpx`) | backend image | MPL-2.0 | https://github.com/certifi/python-certifi |
| feedparser-sgmllib (dependency of `feedparser`) | backend image | PSF-2.0 | https://github.com/kurtmckee/feedparser |
| PostgreSQL 16 (`postgres:16` image) | database container | PostgreSQL License | https://www.postgresql.org/ |

psycopg is LGPL-3.0: you may replace it with another build of the same library, for example by
rebuilding the image with a different pinned version in `backend/pyproject.toml` and `backend/uv.lock`.

## Production dependencies

| Package | License |
|---|---|
| SQLAlchemy, Alembic (and Mako), pydantic, pydantic-settings, pydantic-core, PyYAML, APScheduler, tzlocal, h11, anyio, annotated-types, typing-inspection, jiter, docstring-parser, truststore, sniffio | MIT |
| MarkupSafe, httpx, httpcore, httpx2, httpcore2 (dependencies of the LLM SDKs), idna, python-dotenv | BSD-3-Clause |
| anthropic, openai (official LLM SDKs), opentelemetry-api | Apache-2.0 |
| feedparser | BSD-2-Clause |
| typing_extensions | PSF-2.0 |

The license texts are in each package (`site-packages/<name>-<version>.dist-info/` in the backend image).
