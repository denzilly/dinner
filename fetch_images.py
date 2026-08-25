"""Download the recipe photos for rows that don't have one yet.

Imports save the photo as they go (app/routes_api.py), but recipes added
before that existed have an empty `image_path`. This re-fetches each one's
source page, reads the image URL out of it, and stores the file:

    python fetch_images.py              # only rows with no image
    python fetch_images.py --all        # re-fetch every row, replacing photos
    python fetch_images.py --dry-run    # report what it would do

Rows without a source_url are skipped -- there is nothing to re-read. A recipe
whose page has moved is reported and left alone rather than blanked, so a dead
link never costs you the row.
"""
import argparse
import sys

import db
from app import extract, images


def _candidates(conn, refetch_all: bool):
    sql = "SELECT id, title, source_url, image_path FROM recipes WHERE source_url IS NOT NULL"
    if not refetch_all:
        sql += " AND (image_path IS NULL OR image_path = '')"
    return conn.execute(sql + " ORDER BY id").fetchall()


def run(conn, refetch_all: bool = False, dry_run: bool = False) -> tuple[int, int, int]:
    saved = skipped = failed = 0

    for row in _candidates(conn, refetch_all):
        label = f"#{row['id']} {row['title'][:48]}"
        try:
            extracted = extract.from_url(row["source_url"])
        except extract.FetchError as exc:
            print(f"  fail  {label} — {exc}", file=sys.stderr)
            failed += 1
            continue

        if not extracted.image_url:
            print(f"  none  {label} — page declares no image", file=sys.stderr)
            skipped += 1
            continue

        if dry_run:
            print(f"  would {label} — {extracted.image_url}", file=sys.stderr)
            saved += 1
            continue

        name, warning = images.download_quietly(extracted.image_url)
        if not name:
            print(f"  fail  {label} — {warning}", file=sys.stderr)
            failed += 1
            continue

        previous = row["image_path"]
        conn.execute("UPDATE recipes SET image_path = ? WHERE id = ?", (name, row["id"]))
        conn.commit()
        if previous and previous != name:
            images.delete_if_unused(previous, conn)
        print(f"  ok    {label} — {name}", file=sys.stderr)
        saved += 1

    return saved, skipped, failed


def _cli():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true",
                        help="re-fetch every recipe, not just those without a photo")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be fetched without writing anything")
    args = parser.parse_args()

    conn = db.get_connection()
    saved, skipped, failed = run(conn, refetch_all=args.all, dry_run=args.dry_run)
    conn.close()

    print(f"\nsaved {saved}, no image {skipped}, failed {failed}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _cli()
