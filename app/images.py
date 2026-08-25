"""Recipe photos, stored on disk next to the database.

Downloading rather than hotlinking: recipe sites rotate their CDN URLs and
some refuse requests that carry a foreign referrer, so a stored URL rots.
Files live in `config.IMAGE_DIR`, which is inside the same `data/` directory
the SQLite file uses -- already gitignored, and already the bind mount in
docker-compose, so images survive a rebuild without any new volume.

Filenames are the SHA-256 of the file's own bytes, which makes writes
idempotent: re-importing a recipe whose photo has not changed rewrites the same
name instead of accumulating near-duplicates. Only the bare filename is stored
in `recipes.image_path`, so moving the data directory does not invalidate rows.
"""
import hashlib
import re
from pathlib import Path

import config
from app import extract

# Anything written here is served straight back out, so the stored name is
# restricted to exactly the shape this module produces.
_SAFE_NAME = re.compile(r"^[0-9a-f]{32}\.(jpg|png|gif|webp)$")


def image_dir() -> Path:
    path = Path(config.IMAGE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_safe_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name or ""))


def store(content: bytes, extension: str) -> str:
    """Write `content` and return the filename to put in `image_path`."""
    name = f"{hashlib.sha256(content).hexdigest()[:32]}.{extension}"
    destination = image_dir() / name
    if not destination.exists():
        destination.write_bytes(content)
    return name


def download(url: str) -> str:
    """Fetch `url` and store it. Raises extract.FetchError on any problem."""
    content, extension = extract.fetch_image(url)
    return store(content, extension)


def download_quietly(url: str | None) -> tuple[str | None, str | None]:
    """Return (filename, warning).

    A missing photo is a cosmetic problem and a recipe is worth keeping without
    one, so a failure here is reported as a warning rather than raised -- an
    unreachable CDN must not cost the user an otherwise clean import.
    """
    if not url:
        return None, None
    try:
        return download(url), None
    except extract.FetchError as exc:
        return None, f"could not save the recipe photo: {exc}"
    except OSError as exc:
        return None, f"could not write the recipe photo to disk: {exc}"


def delete_if_unused(name: str | None, conn) -> None:
    """Remove a stored file once no recipe row references it."""
    if not name or not is_safe_name(name):
        return
    still_used = conn.execute(
        "SELECT 1 FROM recipes WHERE image_path = ? LIMIT 1", (name,)
    ).fetchone()
    if still_used:
        return
    (image_dir() / name).unlink(missing_ok=True)
