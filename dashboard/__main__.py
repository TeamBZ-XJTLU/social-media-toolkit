from __future__ import annotations

import os
from pathlib import Path

from .app import create_app


def main() -> None:
    db_path = Path(os.environ.get("WEIBO_DB", "data/weibo.duckdb"))
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "5000"))
    debug = os.environ.get("DASHBOARD_DEBUG", "").lower() in {"1", "true", "yes"}

    app = create_app(db_path=db_path)
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
