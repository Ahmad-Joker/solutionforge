"""Start a throwaway API for end-to-end tests: fresh SQLite DB, migrations, embedded worker.

Never use for anything but tests: rate limiting is off and secrets are generated per run.
"""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

API_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
    db = Path(tempfile.gettempdir()) / f"sf_e2e_{port}.db"
    db.unlink(missing_ok=True)
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    os.environ.update(
        SF_ENVIRONMENT="test",
        SF_DATABASE_URL=url,
        SF_JWT_SECRET=secrets.token_urlsafe(48),
        SF_CREDENTIALS_KEYS=Fernet.generate_key().decode(),
        SF_EMBEDDED_WORKER="true",
        SF_WORKER_POLL_INTERVAL_SECONDS="0.2",
        SF_RATE_LIMIT_ENABLED="false",
        SF_PASSWORD_HASH_PROFILE="fast-insecure-test",  # noqa: S106 - a profile name
        SF_LOG_JSON="false",
        SF_LOG_LEVEL="WARNING",
    )
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    uvicorn.run(
        "solutionforge.main:app_factory",
        factory=True,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
