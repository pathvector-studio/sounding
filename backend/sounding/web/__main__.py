"""``python -m sounding.web``: uvicorn on 0.0.0.0:8080 inside the container (compose publishes it on 127.0.0.1)."""

from __future__ import annotations

import logging
import sys

from sounding.web.auth import WebConfigError

HOST, PORT = "0.0.0.0", 8080  # noqa: S104 - the container's own interface; compose maps it to the host's loopback


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    from sounding.config import get_settings
    from sounding.web.app import create_app

    try:
        app = create_app(get_settings())
    except WebConfigError as exc:
        print(f"sounding-web: refusing to start: {exc}", file=sys.stderr)
        return 1
    # access_log off: the Atom feed's secret token is part of its URL and must not be written to logs.
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
