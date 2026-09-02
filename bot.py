"""
eBay Alert Bot - runs on GitHub Actions every 5 minutes (via cron-job.org
trigger), searches eBay for specific items, and posts a Discord alert
when a Buy-It-Now listing is found under that item's price threshold.

This is a separate, independent project from the local Facebook
Marketplace watcher - eBay's API is stateless (no login/session/device
trust needed), so it's safe and appropriate to run in an ephemeral
cloud environment like GitHub Actions, unlike the Facebook scraper.

Dedup is handled with a simple seen_listings.json file, committed back
to the repo after each run (see .github/workflows/ebay-scan.yml) -
same underlying idea as the SQLite store in the Facebook project, just
a format that's easy for a GitHub Actions job to read/write/commit.
"""

import os
import json
import base64
import statistics
import requests
from pathlib import Path

# --- eBay API config ---
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

SEEN_FILE = Path(__file__).parent / "seen_listings.json"

# --- Item config ---
# Each item defines:
#   query          - what to search eBay for
#   max_price      - alert only if price is at or below this
#   min_price      - (optional) alert only if price is at or above this -
#                     useful for collectible/retro items where a
#                     suspiciously low price often means broken,
#                     incomplete, or a reproduction/bootleg
#   require_words  - (optional) title must contain ALL of these words
#                     (case-insensitive) in addition to matching the query
#   require_any    - (optional) title must contain AT LEAST ONE of these
#                     words/phrases (case-insensitive) - used for "set
#                     number OR set name" style matching
#   exclude_words  - (optional) title must NOT contain any of these words
#                     (case-insensitive)
#   label          - friendly name shown in Discord alerts

# Shared exclusion list applied to all LEGO set searches - filters out
# standalone minifigures, box/bag/manual-only listings, parts lots,
# incomplete sets, and third-party lighting kits (not the actual set).
LEGO_EXCLUDE_WORDS = [
    "minifigure",
    "minifigures",
    "only",
    "pieces",
    "light kit",
    "lighting kit",
    "incomplete",
]

# Shared exclusion list applied to the retro N64/SNES game searches -
# filters out Japanese imports (different region/cart) and suspicious
# "untested" listings, which are common ways for bad-condition or
# non-working carts to slip through.
RETRO_EXCLUDE_WORDS = ["japan", "japanese", "thousand", "untested", "guide", "circular", "poster", "art", "promotion", "promotional", "soundtrack", "fanart", "import", "lot"]

# Shared exclusion list applied to baseball-card searches.
# These are intended to keep the scanner focused on PSA-graded cards
# and eliminate common non-card / non-original-card noise.
BASEBALL_CARD_EXCLUDE_WORDS = [
    "sgc",
    "bccg",
    "bgs",
    "beckett",
    "cgc",
    "csg",
    "hga",
    "tag",
    "reprint",
    "replica",
    "reproduction",
    "custom",
    "proxy",
    "fake",
    "counterfeit",
    "digital",
    "lot",
    "lots",
]

ITEMS = [
    {
        "label": "AirPort Express A1392",
        "query": "airport express a1392",
        "max_price": 20,
        "exclude_words": ["a1264", "a1084", "a1143", "a1408"],
    },
    {
        "label": "Codenames Deep Undercover",
        "query": "codenames deep undercover",
        "max_price": 20,
        "exclude_words": ["man", "woman", "pieces"],
    },
    {
        "label": "TI-84 Plus",
        "query": "ti-84 plus",
        "max_price": 20,
        "require_words": ["plus"],  # must specifically say "Plus", not just any TI-84
        "exclude_words": ["school"],
    },
    {
        "label": "TI-Nspire CX",
        "query": "ti-nspire cx",
        "max_price": 30,
        "exclude_words": ["school"],
    },

    # --- LEGO sets ---
    # query uses the set number (most reliable - sellers almost always
    # include it), require_any lets either the set number or set name
    # count as a match, in case a listing only has one or the other.
    # LEGO_EXCLUDE_WORDS filters out common junk matches: standalone
    # minifigures, incomplete/parts-only listings, and third-party
    # lighting kits that aren't the actual set.
    {
        "label": "LEGO Central Perk (21319)",
        "query": "lego 21319",
        "max_price": 40,
        "require_any": ["21319", "central perk"],
        "exclude_words": LEGO_EXCLUDE_WORDS,
    },
    {
        "label": "LEGO DeLorean Time Machine (21103)",
        "query": "lego 21103",
        "max_price": 35,
        "require_any": ["21103", "delorean"],
        "exclude_words": LEGO_EXCLUDE_WORDS,
    },
    {
        "label": "LEGO Ship in a Bottle (21313)",
        "query": "lego 21313",
        "max_price": 40,
        "require_any": ["21313", "ship in a bottle"],
        "exclude_words": LEGO_EXCLUDE_WORDS,
    },
    {
        "label": "LEGO Medieval Blacksmith (21325)",
        "query": "lego 21325",
        "max_price": 50,
        "require_any": ["21325", "medieval blacksmith"],
        "exclude_words": LEGO_EXCLUDE_WORDS,
    },
    {
        "label": "LEGO Gingerbread House (10267)",
        "query": "lego 10267",
        "max_price": 50,
        "require_any": ["10267", "gingerbread house"],
        "exclude_words": LEGO_EXCLUDE_WORDS + ["40337"],
    },

    # --- Retro N64/SNES games ---
    # min_price filters out suspiciously-cheap listings, which for
    # valuable carts like these are usually reproductions, loose
    # carts with issues, or bait-and-switch listings.
    {
        "label": "Paper Mario (N64)",
        "query": "paper mario n64",
        "max_price": 60,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS,
    },
    {
        "label": "Pokemon Stadium 2",
        "query": "pokemon stadium 2",
        "max_price": 75,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS + ["card", "cards"],
    },
    {
        "label": "Snowboard Kids 2",
        "query": "Snowboard Kids 2",
        "max_price": 95,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS + ["boots"],
    },
    {
        "label": "Goemon's Great Adventure",
        "query": "Goemon's Great Adventure",
        "max_price": 125,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS + ["ganbare", "ps5", "gameboy", "boy", "mystical"],
    },
    {
        "label": "Zelda Majora's Mask",
        "query": "zelda majora's mask",
        "max_price": 75,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS + ["3ds"],
    },
    {
        "label": "Super Metroid",
        "query": "super metroid",
        "max_price": 85,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS,
    },
    {
        "label": "Secret of Mana",
        "query": "secret of mana",
        "max_price": 45,
        "exclude_words": RETRO_EXCLUDE_WORDS + ["playstation", "ps4"],
    },

    # --- Baseball cards ---
    # These searches intentionally require PSA + the exact grade/card
    # identifiers to reduce noise from raw cards, other grading companies,
    # lots, and unrelated listings.
    {
        "label": "Chipper Jones 1991 Topps #333 PSA 10",
        "query": "Chipper Jones 1991 Topps 333 PSA 10",
        "max_price": 125,
        "min_price": 50,
        "require_words": ["chipper", "jones", "333", "psa", "10"],
        "exclude_words": BASEBALL_CARD_EXCLUDE_WORDS,
    },
    {
        "label": "Nolan Ryan 1980 Topps #580 PSA 8",
        "query": "Nolan Ryan 1980 Topps 580 PSA 8",
        "max_price": 120,
        "min_price": 50,
        "require_words": ["nolan", "ryan", "580", "psa", "8"],
        "exclude_words": BASEBALL_CARD_EXCLUDE_WORDS,
    },
]


def get_access_token() -> str:
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()

    response = requests.post(
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded}",
        },
        data={"grant_type": "client_credentials", "scope": OAUTH_SCOPE},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def search_item(token: str, item: dict) -> list[dict]:
    """
    Searches eBay for one configured item, filtered to Buy It Now
    (fixed price) listings only - auctions are always excluded per the
    price thresholds being "buy it now" prices, not bid prices.
    """
    min_price = item.get("min_price", "")
    price_range = f"price:[{min_price}..{item['max_price']}]"

    params = {
        "q": item["query"],
        "limit": "30",
        "filter": f"buyingOptions:{{FIXED_PRICE}},{price_range},priceCurrency:USD",
    }

    response = requests.get(
        SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        params=params,
    )

    if response.status_code != 200:
        print(f"Search failed for {item['label']!r}: {response.status_code} {response.text}")
        return []

    return response.json().get("itemSummaries", [])


def matches_required_words(title: str, require_words: list[str] | None) -> bool:
    if not require_words:
        return True
    title_lower = title.lower()
    return all(word.lower() in title_lower for word in require_words)


def matches_any_words(title: str, require_any: list[str] | None) -> bool:
    """At least one of these words/phrases must appear in the title -
    used for "set number OR set name" style matching (e.g. a LEGO
    listing counts if it mentions either "21319" or "central perk")."""
    if not require_any:
        return True
    title_lower = title.lower()
    return any(phrase.lower() in title_lower for phrase in require_any)


def matches_excluded_words(title: str, exclude_words: list[str] | None) -> bool:
    if not exclude_words:
        return True
    title_lower = title.lower()
    return not any(word.lower() in title_lower for word in exclude_words)


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    with open(SEEN_FILE) as f:
        return set(json.load(f))


def save_seen(seen_ids: set[str]):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen_ids), f, indent=2)


def send_discord_alert(item: dict, listing: dict):
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set - skipping notification.")
        return

    title = listing.get("title", "Untitled")
    price = listing.get("price", {}).get("value", "?")
    url = listing.get("itemWebUrl", "")

    min_price = item.get("min_price")
    if min_price is not None:
        threshold_str = f"${min_price}-${item['max_price']}"
    else:
        threshold_str = f"≤ ${item['max_price']}"

    content = (
        f"**${price} - {title}**\n"
        f"Matched: *{item['label']}* (threshold: {threshold_str})\n"
        f"<{url}>"
    )

    response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
    if response.status_code not in (200, 204):
        print(f"Discord post failed: {response.status_code} {response.text}")


def run():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set - aborting.")
        return

    token = get_access_token()
    seen_ids = load_seen()
    new_alerts = 0

    for item in ITEMS:
        results = search_item(token, item)
        print(f"{item['label']}: {len(results)} candidate listings under ${item['max_price']}")

        for listing in results:
            item_id = listing.get("itemId")
            if not item_id or item_id in seen_ids:
                continue

            title = listing.get("title", "")
            if not matches_required_words(title, item.get("require_words")):
                continue

            if not matches_any_words(title, item.get("require_any")):
                continue

            if not matches_excluded_words(title, item.get("exclude_words")):
                continue

            send_discord_alert(item, listing)
            seen_ids.add(item_id)
            new_alerts += 1

    save_seen(seen_ids)
    print(f"\nDone. {new_alerts} new alert(s) sent.")


if __name__ == "__main__":
    run()