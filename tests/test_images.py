"""Recipe photos: sniffing, storage, serving and the ingest hand-off.

Network is never touched -- extract.fetch_image is stubbed, the same way
test_ingest.py stubs extract.from_url.
"""
import struct
import zlib

import pytest

from app import extract, images, queries


def png_bytes() -> bytes:
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
        + chunk(b"IEND", b"")
    )


SAMPLE = extract.ExtractedRecipe(
    title="Photographed Salad",
    source_url="https://example.com/recipes/photographed-salad",
    source_name="Example",
    servings=4,
    image_url="https://example.com/photo.png",
    ingredient_lines=["200 g pasta", "1 ui"],
    extraction="jsonld",
)


@pytest.fixture
def stub_extract(monkeypatch):
    def fake_from_url(url):
        recipe = extract.ExtractedRecipe(**{**SAMPLE.__dict__})
        recipe.source_url = url
        return recipe

    monkeypatch.setattr(extract, "from_url", fake_from_url)
    monkeypatch.setattr(extract, "fetch_image", lambda url: (png_bytes(), "png"))


# --- sniffing --------------------------------------------------------------

@pytest.mark.parametrize("content,expected", [
    (b"\xff\xd8\xff\xe0 rest", "jpg"),
    (b"\x89PNG\r\n\x1a\n rest", "png"),
    (b"GIF89a rest", "gif"),
    (b"RIFF\x00\x00\x00\x00WEBP rest", "webp"),
])
def test_sniff_recognises_real_image_headers(content, expected):
    assert extract._sniff_image(content) == expected


def test_sniff_rejects_html_dressed_as_an_image():
    assert extract._sniff_image(b"<!doctype html><html></html>") is None


def test_fetch_image_refuses_content_that_is_not_an_image(monkeypatch):
    monkeypatch.setattr(
        extract, "_fetch_raw",
        lambda url, accept, max_bytes, too_large: extract._RawResponse(
            url=url, content=b"<!doctype html>", content_type="image/jpeg", encoding="utf-8"),
    )
    with pytest.raises(extract.FetchError, match="did not look like an image"):
        extract.fetch_image("https://example.com/not-really.jpg")


# --- storage ---------------------------------------------------------------

def test_store_is_content_addressed_and_idempotent(app):
    with app.app_context():
        first = images.store(png_bytes(), "png")
        second = images.store(png_bytes(), "png")
    assert first == second
    assert images.is_safe_name(first)


@pytest.mark.parametrize("name", [
    "../../config.py", "evil.svg", "abc.png", "", "a" * 40 + ".png",
])
def test_unsafe_names_are_rejected(name):
    assert not images.is_safe_name(name)


# --- ingest hand-off -------------------------------------------------------

def test_import_stores_the_photo_and_links_it(client, auth, stub_extract, app):
    response = client.post(
        "/api/recipes/ingest", json={"url": SAMPLE.source_url}, headers=auth
    )
    assert response.status_code == 201

    with app.app_context():
        recipe = queries.get_recipe(response.get_json()["id"])
        assert recipe["image_path"]
        assert (images.image_dir() / recipe["image_path"]).exists()


def test_a_broken_photo_warns_but_still_keeps_the_recipe(client, auth, stub_extract, app, monkeypatch):
    def refuse(url):
        raise extract.FetchError("The site returned HTTP 403.")

    monkeypatch.setattr(extract, "fetch_image", refuse)
    response = client.post(
        "/api/recipes/ingest", json={"url": SAMPLE.source_url}, headers=auth
    )

    assert response.status_code == 201
    assert response.get_json()["warnings"] >= 1
    with app.app_context():
        recipe = queries.get_recipe(response.get_json()["id"])
        assert recipe["image_path"] is None
        assert recipe["title"] == SAMPLE.title


# --- serving ---------------------------------------------------------------

def test_stored_photo_is_served(client, auth, stub_extract, app):
    created = client.post(
        "/api/recipes/ingest", json={"url": SAMPLE.source_url}, headers=auth
    ).get_json()
    with app.app_context():
        name = queries.get_recipe(created["id"])["image_path"]

    response = client.get(f"/recipes/images/{name}")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"


@pytest.mark.parametrize("name", ["..%2f..%2fconfig.py", "evil.svg", "nope.png"])
def test_the_image_route_serves_nothing_else(client, name):
    assert client.get(f"/recipes/images/{name}").status_code == 404


# --- the edit form must not blank what it does not collect -----------------

def test_editing_a_recipe_keeps_its_photo_and_source(client, auth, stub_extract, app):
    created = client.post(
        "/api/recipes/ingest", json={"url": SAMPLE.source_url}, headers=auth
    ).get_json()
    recipe_id = created["id"]

    with app.app_context():
        before = queries.get_recipe(recipe_id)
        keep = (before["image_path"], before["source_name"])

    client.post(f"/recipes/{recipe_id}/edit", data={
        "title": "Renamed Salad", "ingredients": "200 g pasta\n1 ui", "servings": "4",
    })

    with app.app_context():
        after = queries.get_recipe(recipe_id)
        assert (after["image_path"], after["source_name"]) == keep
        assert after["title"] == "Renamed Salad"
