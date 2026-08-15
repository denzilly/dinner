"""Configuration, read once from the environment.

Loaded from a local .env in development and from compose's env_file in Docker.

Empty values are treated as absent, so a bare `DATABASE_PATH=` in .env falls
back to the default rather than producing an unusable path. research_aggregator
lost a deployment afternoon to exactly that difference ("unable to open
database file"), so the distinction is removed here rather than documented.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _env(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key)
    if value is None or not value.strip():
        return default
    return value.strip()


# Where the SQLite file lives. In Docker this is /app/data/dinner.db, bind
# mounted to ./data on the host.
DATABASE_PATH = _env("DATABASE_PATH", str(BASE_DIR / "data" / "dinner.db"))

MIGRATIONS_DIR = BASE_DIR / "db" / "migrations"

# Signs the session cookie. Required whenever SITE_PASSWORD is set.
SECRET_KEY = _env("SECRET_KEY")

# Whole-site password gate. If unset, the gate is disabled entirely (fine for
# local development, never on the server).
SITE_PASSWORD = _env("SITE_PASSWORD")

# Bearer token for POST /api/recipes/ingest -- the Share Sheet shortcut,
# bookmarklet and (later) Hermes authenticate with this instead of the
# session cookie.
INGEST_TOKEN = _env("INGEST_TOKEN")

SESSION_DAYS = int(_env("SESSION_DAYS", "30"))
