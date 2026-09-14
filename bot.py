"""
eBay Alert Bot - Scans eBay for configured items and posts alerts to Discord.
Paginates through up to 1,000 items per search term (200 limit x 5 pages).

No date/lookback filtering is applied - any currently active listing that
matches an item's price/keyword rules is eligible. Dedup against repeat
alerts is handled entirely via seen_listings.json (an item is only ever
alerted once, the first time it's seen).

On the very first run (no seen_listings.json yet), every currently active
matching listing across all items counts as "new" at once, which can be a
large batch. That initial batch is sent as chunked Discord messages (same
mechanism as the quiet-hours digest) instead of one alert per listing, to
avoid spamming the channel. Every run after that returns to normal
one-alert-per-new-listing behavior.
"""

import os
import json
import base64
import time
import requests
from pathlib import Path
from datetime import datetime, timezone
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

SEARCH_RESULT_LIMIT = 200   # Maximum allowed by eBay API per request
MAX_PAGES_PER_ITEM = 5      # 200 x 5 = Up to 1,000 listings checked per search term
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
        "query": "ti-84 plus",
        "max_price": 20,
        "require_words": ["plus"],
        "exclude_words": ["school", "case", "silicone", "yellow", "parts", "battery"],
    },
    {
        "label": "TI-Nspire CX",
        "query": "ti-nspire cx",
        "max_price": 30,
        "require_words": ["cx"],
        "exclude_words": ["school", "case", "silicone", "yellow", "parts", "battery"],
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
        "require_any": ["mega man"],
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
    # --- Sports Cards ---
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


def search_item(token: str, item: dict) -> tuple[list[dict], dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    if EBAY_ZIP:
        headers["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation=country=US,zip={EBAY_ZIP}"

    min_p = item.get("min_price", "")
    max_p = item["max_price"]
    price_clause = f"price:[{min_p}..{max_p}]"
    filter_value = f"buyingOptions:{{FIXED_PRICE}},{price_clause},priceCurrency:USD"

    all_results = []
    pages_scanned = 0

    for page in range(MAX_PAGES_PER_ITEM):
        pages_scanned = page + 1
        offset = page * SEARCH_RESULT_LIMIT
        params = {
            "q": item["query"],
            "limit": str(SEARCH_RESULT_LIMIT),
            "offset": str(offset),
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

        all_results.extend(page_results)

        # Only stop early once we've hit the natural end of results (a
        # partially-full page means there's nothing more to fetch).
        if len(page_results) < SEARCH_RESULT_LIMIT:
            break

    stats = {"raw": len(all_results), "pages": pages_scanned}
    print(f"Search diagnostics [{item['label']}]: raw={stats['raw']} (pages: {stats['pages']})")

    return all_results, stats


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
        if response.status_code == 429:
            # Rate limited - Discord tells us how long to wait (seconds,
            # sometimes fractional) via the response body/header. Sleep
            # and retry once rather than silently dropping the message.
            try:
                retry_after = float(response.json().get("retry_after", 1.0))
            except (ValueError, TypeError, json.JSONDecodeError):
                retry_after = 1.0
            print(f"Discord rate limited - waiting {retry_after:.2f}s and retrying once.")
            time.sleep(retry_after + 0.25)
            response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=30)
        if response.status_code not in (200, 204):
            print(f"Discord post failed: {response.status_code} {response.text}")
        return response.status_code in (200, 204)
    except requests.RequestException as exc:
        print(f"Discord post error: {exc}")
        return False


def send_chunked_alerts(entries: list[tuple[str, str]], header: str) -> set[str]:
    """
    Posts a list of (item_id, content) alert entries as one or more Discord
    messages, staying under Discord's ~2000 character limit per message by
    starting a new chunk whenever the next entry would overflow it.

    A small delay is added between sends and each result is checked -
    Discord webhooks are rate-limited (roughly 5 requests / 2 seconds), and
    firing chunks back-to-back with no pacing or failure check can cause
    later chunks to be silently dropped, which is exactly what happened
    without this: some items partway through a big bootstrap batch never
    made it to Discord even though they were correctly identified as
    eligible.

    Returns the set of item_ids whose chunk was successfully delivered, so
    the caller only marks those as seen - a failed chunk's items stay
    un-seen and will be retried on the next run.
    """
    current_chunk_text = header
    current_chunk_ids: list[str] = []
    chunks: list[tuple[str, list[str]]] = []

    for item_id, content in entries:
        block = content + "\n\n"
        if len(current_chunk_text) + len(block) > 1900:
            chunks.append((current_chunk_text, current_chunk_ids))
            current_chunk_text = block
            current_chunk_ids = [item_id]
        else:
            current_chunk_text += block
            current_chunk_ids.append(item_id)
    chunks.append((current_chunk_text, current_chunk_ids))

    delivered_ids: set[str] = set()
    failed = 0
    for i, (chunk_text, chunk_ids) in enumerate(chunks):
        if post_to_discord(chunk_text):
            delivered_ids.update(chunk_ids)
        else:
            failed += 1
            print(f"  Chunk {i + 1}/{len(chunks)} failed to send ({len(chunk_ids)} listing(s) will be retried next run).")
        if i < len(chunks) - 1:
            time.sleep(0.5)  # stay comfortably under Discord's rate limit

    if failed:
        print(f"WARNING: {failed}/{len(chunks)} alert chunk(s) failed to send to Discord.")

    return delivered_ids


def flush_pending_alerts():
    pending = load_pending()
    if not pending:
        return

    header = f"**Overnight digest - {len(pending)} listing(s) found during quiet hours:**\n\n"
    entries = [(entry["item_id"], entry["content"]) for entry in pending]
    delivered_ids = send_chunked_alerts(entries, header)

    failed_entries = [entry for entry in pending if entry["item_id"] not in delivered_ids]
    save_pending(failed_entries)
    print(f"Flushed {len(pending) - len(failed_entries)}/{len(pending)} queued overnight alert(s).")


def run():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set - aborting.")
        return

    # Bootstrap run: no seen_listings.json yet means this is the first time
    # the bot has ever run (or it was reset), so every currently active
    # matching listing will look "new" at once. Detect this before loading
    # seen_ids so we can route that first big batch through chunked
    # messages instead of firing one Discord alert per listing.
    is_bootstrap_run = not SEEN_FILE.exists()
    bootstrap_batch: list[tuple[str, str]] = []  # (item_id, content) pairs
    if is_bootstrap_run:
        print("No seen_listings.json found - treating this as a bootstrap run (results will be batched).")

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
        results, stats = search_item(token, item)

        already_seen_count = 0
        price_rejected_count = 0
        keyword_rejected_count = 0
        shipping_rejected_count = 0

        eligible_listings = []
        for listing in results:
            item_id = listing.get("itemId")
            if not item_id or item_id in seen_ids:
                already_seen_count += 1
                continue

            title = listing.get("title", "")
            try:
                listing_price = float(listing.get("price", {}).get("value"))
            except (TypeError, ValueError):
                price_rejected_count += 1
                continue

            max_price = float(item["max_price"])
            min_price = item.get("min_price")

            if listing_price > max_price or (min_price is not None and listing_price < float(min_price)):
                price_rejected_count += 1
                continue

            if (not matches_required_words(title, item.get("require_words"))) or \
               (not matches_any_words(title, item.get("require_any"))) or \
               (not matches_excluded_words(title, item.get("exclude_words"))):
                keyword_rejected_count += 1
                continue

            shipping_cost = get_shipping_cost(listing)
            if shipping_cost is not None and shipping_cost > MAX_SHIPPING_COST:
                shipping_rejected_count += 1
                continue

            eligible_listings.append(listing)

        print(
            f"Filtering breakdown [{item['label']}]: "
            f"raw_results={len(results)} | already_seen={already_seen_count} | "
            f"price_rejected={price_rejected_count} | keyword_rejected={keyword_rejected_count} | "
            f"shipping_rejected={shipping_rejected_count} | NEW_ELIGIBLE={len(eligible_listings)}"
        )

        for listing in eligible_listings:
            item_id = listing["itemId"]
            content = build_alert_content(item, listing)

            if is_bootstrap_run:
                # First-ever run: every current match counts as "new" at
                # once, so don't fire off one Discord message per listing.
                # Queue them all and send as chunked batch messages below.
                # Don't mark as seen yet - only do that for chunks that
                # actually succeed (see below), so a failed send doesn't
                # get silently treated as "already alerted."
                bootstrap_batch.append((item_id, content))
            elif quiet_now:
                pending.append({"item_id": item_id, "content": content})
                queued_alerts += 1
                seen_ids.add(item_id)
            else:
                delivered = post_to_discord(content)
                if delivered:
                    new_alerts += 1
                    seen_ids.add(item_id)

        metadata[item["label"]] = now_iso

    if is_bootstrap_run and bootstrap_batch:
        delivered_ids = send_chunked_alerts(
            bootstrap_batch,
            header=f"**Initial scan - {len(bootstrap_batch)} matching listing(s) found:**\n\n",
        )
        seen_ids.update(delivered_ids)
        new_alerts += len(delivered_ids)
        print(f"Bootstrap run: sent {len(delivered_ids)}/{len(bootstrap_batch)} listing(s) in chunked messages.")

    save_seen(seen_ids)
    save_search_metadata(metadata)
    if quiet_now and not is_bootstrap_run:
        save_pending(pending)

    print(f"\nDone. {new_alerts} alert(s) sent, {queued_alerts} queued for digest.")


if __name__ == "__main__":
    run()
