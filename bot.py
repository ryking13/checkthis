"""
eBay Alert Bot - Runs on GitHub Actions every 5 minutes.
Paginates newly listed items dynamically using item_search_metadata.json timestamps.
"""

import os
import json
import base64
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from datetime import time as _time

# --- API Config ---
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
EBAY_ZIP = os.environ.get("EBAY_ZIP", "")

SEEN_FILE = Path(__file__).parent / "seen_listings.json"
METADATA_FILE = Path(__file__).parent / "item_search_metadata.json"
PENDING_FILE = Path(__file__).parent / "pending_alerts.json"

SEARCH_RESULT_LIMIT = 200
MAX_PAGES_PER_ITEM = 5  # Fetch up to 500 items if a high-volume search has backlogged
MAX_SHIPPING_COST = 15.00

# Quiet Hours
QUIET_HOURS_TZ = ZoneInfo("America/Chicago")
QUIET_HOURS_START = _time(22, 0)   # 10:00 PM
QUIET_HOURS_END = _time(6, 30)     # 6:30 AM

# Shared Exclusions
LEGO_EXCLUDE_WORDS = ["minifigure", "minifigures", "only", "pieces", "light kit", "lighting kit", "incomplete", "display"]
RETRO_EXCLUDE_WORDS = ["japan", "japanese", "thousand", "untested", "guide", "circular", "poster", "art", "promotion", "promotional", "soundtrack", "fanart", "import", "lot"]
BASEBALL_CARD_EXCLUDE_WORDS = ["sgc", "bccg", "bgs", "beckett", "cgc", "csg", "hga", "tag", "reprint", "replica", "reproduction", "custom", "proxy", "fake", "counterfeit", "digital", "lot", "lots"]

ITEMS = [
    {
        "label": "AirPort Express A1392",
        "query": "airport express a1392",
        "max_price": 20,
        "exclude_words": ["a1264", "a1084", "a1143", "a1408", "a1301"],
    },
    {
        "label": "Codenames Deep Undercover",
        "query": "codenames deep undercover",
        "max_price": 20,
        "exclude_words": ["man", "woman", "pieces"],
    },
    {
        "label": "TI-84 Plus",
        "query": "ti-parser plus",
        "max_price": 20,
        "require_words": ["plus"],
        "exclude_words": ["school", "case", "silicone"],
    },
    {
        "label": "TI-Nspire CX",
        "query": "ti-nspire cx",
        "max_price": 30,
        "require_words": ["cx"],
        "exclude_words": ["school", "case", "silicone"],
    },
    # --- LEGO sets ---
    {
        "label": "LEGO Central Perk (21319)",
        "query": "lego 21319",
        "max_price": 75,
        "require_any": ["21319", "central perk"],
        "exclude_words": LEGO_EXCLUDE_WORDS,
    },
    {
        "label": "LEGO DeLorean Time Machine (21103)",
        "query": "lego 21103",
        "max_price": 35,
        "require_any": ["21103", "delorean"],
        "exclude_words": LEGO_EXCLUDE_WORDS + ["77256"],
    },
    {
        "label": "LEGO Ship in a Bottle (21313)",
        "query": "lego 21313",
        "max_price": 60,
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
        "require_any": ["pokemon stadium 2"],
        "exclude_words": RETRO_EXCLUDE_WORDS + ["card", "cards", "deck", "3ds"],
    },
    {
        "label": "Pokemon Stadium 2 manual",
        "query": "pokemon stadium 2 manual",
        "max_price": 25,
        "min_price": 1,
        "require_any": ["pokemon stadium 2", "manual"],
        "exclude_words": RETRO_EXCLUDE_WORDS + ["card", "cards", "deck", "3ds"],
    },
    {
        "label": "Pokemon Stadium 2 box",
        "query": "pokemon stadium 2 box",
        "max_price": 155,
        "min_price": 25,
        "require_any": ["pokemon stadium 2", "box"],
        "exclude_words": RETRO_EXCLUDE_WORDS + ["card", "cards", "deck", "3ds"],
    },
    {
        "label": "Snowboard Kids 2",
        "query": "Snowboard Kids 2",
        "max_price": 100,
        "min_price": 39,
        "require_any": ["snowboard kids 2"],
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
        "exclude_words": RETRO_EXCLUDE_WORDS + ["3ds", "hoodie", "wearable", "figures", "figure", "watch", "amiibo", "collection", "funko", "pin", "plush"],
    },
    {
        "label": "Super Metroid",
        "query": "super metroid",
        "max_price": 85,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS,
    },
    {
        "label": "Indiana Jones Infernal Machine",
        "query": "Indiana Jones Infernal Machine n64",
        "max_price": 85,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS,
    },
    {
        "label": "Mega Man 64",
        "query": "Mega Man 64",
        "max_price": 85,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS,
    },
    {
        "label": "Space Station Silicon Valley",
        "query": "Space Station Silicon Valley n64",
        "max_price": 66,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS,
    },
    {
        "label": "StarCraft 64",
        "query": "StarCraft 64",
        "max_price": 110,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS,
    },
    {
        "label": "Secret of Mana",
        "query": "secret of mana",
        "max_price": 45,
        "require_any": ["secret of mana"],
        "exclude_words": RETRO_EXCLUDE_WORDS + ["playstation", "ps4", "vinyl", "record", "records", "figure"],
    },

    # --- Baseball / Basketball cards ---
    {
        "label": "Chipper Jones 1991 Topps #333 PSA 10",
        "query": "Chipper Jones 1991 Topps 333 PSA 10",
        "max_price": 125,
        "min_price": 50,
        "require_words": ["chipper", "jones", "333"],
        "require_any": ["psa 10", "psa10", "psa-10"],
        "exclude_words": BASEBALL_CARD_EXCLUDE_WORDS,
    },
    {
        "label": "Nolan Ryan 1980 Topps #580 PSA 8",
        "query": "Nolan Ryan 1980 Topps 580 PSA 8",
        "max_price": 120,
        "min_price": 50,
        "require_words": ["nolan", "ryan", "580"],
        "require_any": ["psa 8", "psa8", "psa-8"],
        "exclude_words": BASEBALL_CARD_EXCLUDE_WORDS,
    },
    {
        "label": "Luka Doncic 2018 Prizm #280 RC PSA 10",
        "query": "Luka Doncic 2018 Prizm 280 RC PSA 10",
        "max_price": 180,
        "min_price": 70,
        "require_words": ["luka", "doncic", "280"],
        "require_any": ["psa 10", "psa10", "psa-10"],
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


def load_search_metadata() -> dict:
    if not METADATA_FILE.exists():
        return {}
    try:
        with open(METADATA_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_search_metadata(metadata: dict):
    with open(METADATA_FILE, "w") as f:
        json.dump(metadata, f, indent=2)


def search_item(token: str, item: dict, last_run_iso: str | None, seen_ids: set[str]) -> list[dict]:
    """
    Paginates through eBay results sorted by newlyListed until we cross
    listings older than the last run time OR encounter seen items.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    if EBAY_ZIP:
        headers["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation=country=US,zip={EBAY_ZIP}"

    # Build valid eBay API filter format (without 'None')
    min_p = item.get("min_price", "")
    max_p = item["max_price"]
    price_clause = f"price:[{min_p}..{max_p}]"
    filter_value = f"buyingOptions:{{FIXED_PRICE}},{price_clause},priceCurrency:USD"

    # Calculate cutoff time (1-minute overlap buffer)
    now = datetime.now(timezone.utc)
    if last_run_iso:
        try:
            cutoff_time = datetime.fromisoformat(last_run_iso) - timedelta(minutes=1)
        except ValueError:
            cutoff_time = now - timedelta(minutes=10)
    else:
        cutoff_time = now - timedelta(minutes=10)

    unseen_new_results = []
    stop_paginating = False

    for page in range(MAX_PAGES_PER_ITEM):
        offset = page * SEARCH_RESULT_LIMIT
        params = {
            "q": item["query"],
            "limit": str(SEARCH_RESULT_LIMIT),
            "offset": str(offset),
            "sort": "newlyListed",
            "filter": filter_value,
        }

        response = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
        if response.status_code != 200:
            print(f"Search failed for {item['label']!r}: {response.status_code} {response.text}")
            break

        payload = response.json()
        warnings = payload.get("warnings")
        if warnings:
            print(f"  eBay API warnings for {item['label']!r}: {warnings}")

        page_results = payload.get("itemSummaries", [])
        if not page_results:
            break

        for listing in page_results:
            item_id = listing.get("itemId")
            
            # If we hit an item we've already processed, we've caught up
            if item_id in seen_ids:
                stop_paginating = True
                break

            creation_str = listing.get("itemCreationDate") or listing.get("itemOriginDate")
            if creation_str:
                try:
                    creation_dt = datetime.fromisoformat(creation_str.replace("Z", "+00:00"))
                    if creation_dt < cutoff_time:
                        stop_paginating = True
                        break
                except (ValueError, TypeError):
                    pass

            unseen_new_results.append(listing)

        if stop_paginating:
            break

    print(f"  Search [{item['label']}]: collected {len(unseen_new_results)} new candidate listings across {page + 1} page(s).")
    return unseen_new_results


def matches_required_words(title: str, require_words: list[str] | None) -> bool:
    if not require_words:
        return True
    title_lower = title.lower()
    return all(word.lower() in title_lower for word in require_words)


def matches_any_words(title: str, require_any: list[str] | None) -> bool:
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


def is_quiet_hours(now=None) -> bool:
    if now is None:
        now = datetime.now(QUIET_HOURS_TZ)
    else:
        now = now.astimezone(QUIET_HOURS_TZ)

    current_time = now.time()
    if QUIET_HOURS_START <= QUIET_HOURS_END:
        return QUIET_HOURS_START <= current_time < QUIET_HOURS_END
    else:
        return current_time >= QUIET_HOURS_START or current_time < QUIET_HOURS_END


def load_pending() -> list[dict]:
    if not PENDING_FILE.exists():
        return []
    with open(PENDING_FILE) as f:
        return json.load(f)


def save_pending(pending: list[dict]):
    if pending:
        with open(PENDING_FILE, "w") as f:
            json.dump(pending, f, indent=2)
    elif PENDING_FILE.exists():
        PENDING_FILE.unlink()


def get_shipping_cost(listing: dict) -> float | None:
    shipping_options = listing.get("shippingOptions")
    if not shipping_options:
        return None

    costs = [
        float(opt.get("shippingCost", {}).get("value"))
        for opt in shipping_options
        if opt.get("shippingCost", {}).get("value") is not None
    ]
    return min(costs) if costs else None


def build_alert_content(item: dict, listing: dict) -> str:
    title = listing.get("title", "Untitled")
    price_str = listing.get("price", {}).get("value", "?")
    url = listing.get("itemWebUrl", "")

    min_price = item.get("min_price")
    threshold_str = f"${min_price}-${item['max_price']}" if min_price is not None else f"≤ ${item['max_price']}"
    shipping_cost = get_shipping_cost(listing)

    try:
        price = float(price_str)
    except (TypeError, ValueError):
        price = None

    if shipping_cost is None:
        price_line = f"**${price_str} - {title}**\n(shipping cost unavailable)"
    elif shipping_cost == 0:
        price_line = f"**${price_str} (free shipping) - {title}**"
    elif price is not None:
        total = price + shipping_cost
        price_line = f"**${price_str} + ${shipping_cost:.2f} shipping = ${total:.2f} total - {title}**"
    else:
        price_line = f"**${price_str} + ${shipping_cost:.2f} shipping - {title}**"

    return f"{price_line}\nMatched: *{item['label']}* (threshold: {threshold_str})\n<{url}>"


def post_to_discord(content: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set - skipping notification.")
        return False
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=30)
        return response.status_code in (200, 204)
    except requests.RequestException as exc:
        print(f"Discord post error: {exc}")
        return False


def flush_pending_alerts():
    pending = load_pending()
    if not pending:
        return

    header = f"**Overnight digest - {len(pending)} listing(s) found during quiet hours:**\n\n"
    current_chunk = header
    chunks = []
    for entry in pending:
        block = entry["content"] + "\n\n"
        if len(current_chunk) + len(block) > 1900:
            chunks.append(current_chunk)
            current_chunk = block
        else:
            current_chunk += block
    chunks.append(current_chunk)

    for chunk in chunks:
        post_to_discord(chunk)

    save_pending([])
    print(f"Flushed {len(pending)} queued overnight alert(s).")


def run():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set - aborting.")
        return

    quiet_now = is_quiet_hours()
    if not quiet_now:
        flush_pending_alerts()

    token = get_access_token()
    seen_ids = load_seen()
    pending = load_pending()
    metadata = load_search_metadata()
    now_iso = datetime.now(timezone.utc).isoformat()
    new_alerts = 0
    queued_alerts = 0

    for item in ITEMS:
        last_run = metadata.get(item["label"])
        results = search_item(token, item, last_run, seen_ids)

        eligible_listings = []
        for listing in results:
            item_id = listing.get("itemId")
            if not item_id or item_id in seen_ids:
                continue

            title = listing.get("title", "")
            try:
                listing_price = float(listing.get("price", {}).get("value"))
            except (TypeError, ValueError):
                continue

            max_price = float(item["max_price"])
            min_price = item.get("min_price")

            if listing_price > max_price or (min_price is not None and listing_price < float(min_price)):
                continue

            if not matches_required_words(title, item.get("require_words")):
                continue
            if not matches_any_words(title, item.get("require_any")):
                continue
            if not matches_excluded_words(title, item.get("exclude_words")):
                continue

            shipping_cost = get_shipping_cost(listing)
            if shipping_cost is not None and shipping_cost > MAX_SHIPPING_COST:
                continue

            eligible_listings.append(listing)

        print(f"{item['label']}: {len(results)} candidate(s) | {len(eligible_listings)} NEW eligible")

        for listing in eligible_listings:
            item_id = listing["itemId"]
            content = build_alert_content(item, listing)

            if quiet_now:
                pending.append({"item_id": item_id, "content": content})
                queued_alerts += 1
                seen_ids.add(item_id)
            else:
                delivered = post_to_discord(content)
                if delivered:
                    new_alerts += 1
                    seen_ids.add(item_id)

        metadata[item["label"]] = now_iso

    save_seen(seen_ids)
    save_search_metadata(metadata)
    if quiet_now:
        save_pending(pending)

    print(f"\nDone. {new_alerts} alert(s) sent, {queued_alerts} queued for digest.")


if __name__ == "__main__":
    run()
