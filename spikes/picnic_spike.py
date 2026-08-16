"""Phase 6 feasibility spike: can we log in, search, and build a cart?

project.md says to prove the three risky calls before committing to the phase.
This does exactly that and nothing else -- it is not the integration, and
nothing here is meant to survive into `app/`.

    docker run --rm -it --env-file .env \
        -v "$PWD/spikes:/spikes" -v "$PWD/data:/data" \
        dinner-dinner sh -c "pip install -q python-picnic-api2 && python /spikes/picnic_spike.py"

Reads PICNIC_USERNAME / PICNIC_PASSWORD from the environment. They are never
printed, and the only thing written to disk is the auth token (see below).

What it proves, in order:

1. **Login, including 2FA.** Picnic sends an SMS code; this prompts for it.
2. **Token reuse.** The token from step 1 is saved to data/picnic-token.txt and
   used on every later run, so the unattended weekly job never needs the SMS.
   project.md lists 2FA as a risk to accept for unattended runs -- it isn't one,
   as long as the token is captured here and kept.
3. **Search.** Runs the real ingredient strings from the grocery list through
   `search()` and prints what comes back, so the matching problem can be looked
   at with actual data instead of guessed at.
4. **Cart add, then remove.** Adds one item, reads the cart back to confirm it
   landed, then removes it again. It touches a real account, so it cleans up
   after itself -- pass --keep to leave the item in the basket.

Deliberately NOT here: placing an order. project.md's scope is build the cart,
check out in the app.
"""
import argparse
import os
import sys
from pathlib import Path

from python_picnic_api2 import PicnicAPI

TOKEN_PATH = Path("/data/picnic-token.txt")

# Real lines from the bank, chosen to cover the cases that make matching hard:
# a weight that won't match a pack size, a countable item, a vague staple, and
# a Dutch product name that search may or may not handle.
SEARCH_TERMS = ["rundergehakt", "uien", "olijfolie", "rookworst"]


def load_token() -> str | None:
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text().strip()
        if token:
            return token
    return None


def save_token(token: str) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token)
    # Same reasoning as any credential on this host: readable by the owner only.
    TOKEN_PATH.chmod(0o600)


def connect() -> PicnicAPI:
    """Token first, credentials only as a fallback -- so 2FA happens once ever."""
    token = load_token()
    if token:
        picnic = PicnicAPI(auth_token=token, country_code="NL")
        if picnic.logged_in():
            print(f"[auth] reused saved token from {TOKEN_PATH}")
            return picnic
        print("[auth] saved token rejected, falling back to password login")

    username = os.environ.get("PICNIC_USERNAME")
    password = os.environ.get("PICNIC_PASSWORD")
    if not (username and password):
        sys.exit("set PICNIC_USERNAME and PICNIC_PASSWORD (in .env) and re-run")

    picnic = PicnicAPI(username=username, password=password, country_code="NL")

    if not picnic.logged_in():
        # The library raises on a wrong password, so reaching here generally
        # means 2FA is required rather than that the credentials were bad.
        print("[auth] login needs a 2FA code; requesting an SMS")
        picnic.generate_2fa_code(channel="SMS")
        picnic.verify_2fa_code(input("code from SMS: ").strip())

    token = picnic.session.auth_token
    if token:
        save_token(token)
        print(f"[auth] logged in; token saved to {TOKEN_PATH} for future runs")
    else:
        print("[auth] logged in, but no token exposed -- unattended runs would re-auth")

    return picnic


def show_search(picnic: PicnicAPI) -> str | None:
    """Print real results, and hand back one product id for the cart test."""
    first_id = None

    for term in SEARCH_TERMS:
        print(f"\n[search] {term!r}")
        try:
            result = picnic.search(term)
        except Exception as exc:                      # noqa: BLE001 -- spike
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            continue

        items = getattr(result, "items", []) or []
        if not items:
            print("  (no results)")
            continue

        for item in items[:5]:
            # unit_quantity is the whole matching problem in one field: the
            # recipe says 500 g and the shelf says "300 g" or "per stuk".
            print(f"  {item.id:<16} {item.unit_quantity or '?':<14} "
                  f"{str(item.display_price or '?'):<8} {item.name}")
        print(f"  ({len(items)} results total)")

        if first_id is None:
            first_id = items[0].id

    return first_id


def show_cart(picnic: PicnicAPI, product_id: str, keep: bool) -> None:
    print(f"\n[cart] adding {product_id}")
    picnic.add_product(product_id, count=1)

    # Read it back rather than trusting add_product's return value: the point of
    # the spike is proving the item actually landed in the real basket.
    cart = picnic.get_cart()
    print(f"[cart] {cart.total_count} items, total {cart.total_price}")

    if keep:
        print("[cart] --keep given, leaving it in the basket")
        return

    print(f"[cart] removing {product_id} again")
    picnic.remove_product(product_id, count=1)
    print(f"[cart] now {picnic.get_cart().total_count} items")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true",
                        help="leave the test item in the basket instead of removing it")
    parser.add_argument("--no-cart", action="store_true",
                        help="only prove login and search; never touch the basket")
    args = parser.parse_args()

    picnic = connect()

    user = picnic.get_user()
    print(f"[user] {getattr(user, 'firstname', '?')} — "
          f"{getattr(user, 'household_details', None) or 'household details unavailable'}")

    product_id = show_search(picnic)

    if args.no_cart:
        print("\n[cart] skipped (--no-cart)")
    elif product_id:
        show_cart(picnic, product_id, args.keep)
    else:
        print("\n[cart] skipped: search returned nothing to add")

    print("\nspike done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
