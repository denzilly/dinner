"""SQLite connection helper and migration runner.

Run standalone to apply anything pending:

    python db.py

The container CMD does exactly that before gunicorn binds, so a restart always
brings the schema up to date before a single request is served. This is
deliberate: research_aggregator applied schema changes via ad-hoc ALTER TABLE
calls inside init_db(), which meant every deploy had a "run this first or every
page 500s" step that had to be remembered. Versioned migrations applied at
startup remove that whole class of incident.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config


def get_connection() -> sqlite3.Connection:
    Path(config.DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(verbose: bool = True) -> list[str]:
    """Apply every migration in db/migrations not yet recorded. Returns the
    versions applied this run."""
    conn = get_connection()
    applied_now = []
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_version (
                   version    TEXT PRIMARY KEY,
                   applied_at TEXT NOT NULL
               )"""
        )
        conn.commit()

        already = {row["version"] for row in conn.execute("SELECT version FROM schema_version")}

        for path in sorted(config.MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in already:
                continue

            # executescript() implicitly commits before running, so a crash
            # part-way through a file can leave it half applied. Every
            # migration is therefore written with IF NOT EXISTS, making a
            # re-run safe rather than requiring manual repair.
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

            applied_now.append(version)
            if verbose:
                print(f"applied {version}")

        if verbose and not applied_now:
            print("schema up to date")
    finally:
        conn.close()

    return applied_now


if __name__ == "__main__":
    print(f"database: {config.DATABASE_PATH}")
    migrate()
