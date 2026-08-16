"""JSON dump-and-load for the whole database.

The recipe bank -- and the week plan and grocery history that reference it --
live in one SQLite file. This is the backup/restore path for that file
without needing sqlite3 on hand:

    python backup.py dump backup.json      # or: python backup.py dump > backup.json
    python backup.py load backup.json

`dump` with no path writes to stdout, so it composes with anything --
`python backup.py dump | ssh host 'cat > dinner-$(date +%F).json'` works today,
ahead of whatever the real backup sweep ends up being.

`load` replaces every table below wholesale rather than merging. A merge would
need to resolve id collisions across every foreign key in the schema
(recipe_ingredients.ingredient_id, plan_days.recipe_id, ...) for a case that
doesn't come up in practice -- you restore onto an empty/disposable database,
or you don't restore at all. IDs are preserved on the way back in specifically
so every foreign key still points at the row it used to.

Excluded on purpose:
- `recipes_fts` -- derived. The AFTER INSERT/DELETE triggers on `recipes`
  (db/migrations/001_initial.sql) keep it in sync as rows are cleared and
  reloaded, so it never needs handling here directly.
- `schema_version` -- migration bookkeeping, not app data. Loading an old
  export must never roll the schema back; `db.migrate()` runs before this
  script would ever be reached from the container CMD anyway.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

import config
import db

# Parent-first: the order load() inserts in, and dump() reads in for a
# stable, readable file. load() deletes in reverse.
TABLES = [
    "ingredients",
    "tags",
    "recipes",
    "recipe_ingredients",
    "recipe_tags",
    "plan_days",
    "grocery_lists",
    "grocery_items",
    # Every row is a human decision made once at a review screen -- the most
    # expensive data here to reproduce, per row, of anything in this list.
    "picnic_products",
]

FORMAT_VERSION = 1


def dump(conn) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tables": {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES
        },
    }


def load(conn, data: dict) -> dict[str, int]:
    """Replace every table's contents with what's in `data`. Returns row counts.

    Runs as one transaction with foreign key checks suspended -- deleting a
    table with children still pointing at it would otherwise fail regardless
    of insert order, and there's no ordering that dodges that for every table
    here simultaneously (recipe_ingredients references both recipes and
    ingredients). Checks are back on before this returns either way.
    """
    if data.get("format_version") != FORMAT_VERSION:
        raise ValueError(
            f"unsupported format_version {data.get('format_version')!r} "
            f"(this script reads/writes {FORMAT_VERSION})"
        )

    tables = data.get("tables", {})
    unknown = set(tables) - set(TABLES)
    if unknown:
        raise ValueError(f"unknown table(s) in export: {sorted(unknown)}")

    counts = {}
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for table in reversed(TABLES):
            conn.execute(f"DELETE FROM {table}")

        for table in TABLES:
            rows = tables.get(table, [])
            for row in rows:
                columns = ", ".join(row.keys())
                placeholders = ", ".join("?" * len(row))
                conn.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                    list(row.values()),
                )
            counts[table] = len(rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    return counts


def _cli():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    dump_p = sub.add_parser("dump", help="write the whole database as JSON")
    dump_p.add_argument("path", nargs="?", help="output file (default: stdout)")

    load_p = sub.add_parser("load", help="replace the database with a JSON export")
    load_p.add_argument("path", help="input file, or - for stdin")
    load_p.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt (for scripted/non-interactive use)",
    )

    args = parser.parse_args()
    conn = db.get_connection()

    if args.command == "dump":
        data = dump(conn)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if args.path:
            with open(args.path, "w", encoding="utf-8") as f:
                f.write(text)
            total = sum(len(rows) for rows in data["tables"].values())
            print(f"wrote {args.path} ({total} rows across {len(TABLES)} tables)",
                  file=sys.stderr)
        else:
            print(text)

    elif args.command == "load":
        text = sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8").read()
        data = json.loads(text)

        if not args.yes:
            reply = input(
                f"This replaces every row in {config.DATABASE_PATH} with the "
                "contents of this export. Continue? [y/N] "
            )
            if reply.strip().lower() != "y":
                print("Aborted.", file=sys.stderr)
                sys.exit(1)

        counts = load(conn, data)
        for table, n in counts.items():
            print(f"{table}: {n}", file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    _cli()
