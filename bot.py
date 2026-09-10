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

# US zip code used as the shipping destination when querying eBay, so
# that calculated-shipping listings actually return a shippingOptions
# array. Without this, eBay has no destination to estimate shipping to
# and silently omits shipping info from search results (see
# X-EBAY-C-ENDUSERCTX / contextualLocation in eBay's docs).
EBAY_ZIP = os.environ.get("EBAY_ZIP", "")

SEEN_FILE = Path(__file__).parent / "seen_listings.json"

# Retrieve more listings per search while keeping one API call per item.
SEARCH_RESULT_LIMIT = 100
PENDING_FILE = Path(__file__).parent / "pending_alerts.json"

# --- Quiet hours ---
# No Discord notifications are sent between QUIET_HOURS_START and
# QUIET_HOURS_END (in QUIET_HOURS_TZ). Matches found during that window
# are still detected and saved to disk (see PENDING_FILE) - they're
# just queued instead of posted immediately. As soon as a run happens
# at or after QUIET_HOURS_END, any queued alerts are flushed as a
# single batch dump before that run's own new alerts are sent.
#
# NOTE: GitHub Actions runs in UTC. QUIET_HOURS_TZ tells the bot what
# "10pm" and "6:30am" mean in wall-clock time - update this if you
# move to a different timezone. This does NOT auto-adjust in a way
# that requires code changes for DST; zoneinfo handles that.
from datetime import time as _time
from zoneinfo import ZoneInfo

QUIET_HOURS_TZ = ZoneInfo("America/Chicago")
QUIET_HOURS_START = _time(22, 0)   # 10:00 PM
QUIET_HOURS_END = _time(6, 30)     # 6:30 AM

# Applies to every item in ITEMS - listings with a known shipping cost
# above this are skipped entirely (not even queued during quiet hours).
# Listings where shipping cost couldn't be determined (e.g. local
# pickup only, or eBay didn't return shippingOptions) are NOT excluded
# by this filter, since we have no evidence they're actually expensive
# to ship - they still go through the normal price/title filters.
MAX_SHIPPING_COST = 15.00

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
    "display",
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
        "require_words": ["plus"],  # must specifically say "Plus", not just any TI-84
        "exclude_words": ["school"],
    },
    {
        "label": "TI-Nspire CX",
        "query": "ti-nspire cx",
        "max_price": 30,
        "require_words": ["cx"],  # must specifically say "CX" - plain TI-Nspire isn't as valuable
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
        "require_any": ["pokemon stadium 2"],  # must be the exact phrase, not just "pokemon stadium" (was matching the original game)
        "exclude_words": RETRO_EXCLUDE_WORDS + ["card", "cards", "deck", "3ds"],
    },
    {
        "label": "Snowboard Kids 2",
        "query": "Snowboard Kids 2",
        "max_price": 100,
        "min_price": 39,
        "require_any": ["snowboard kids 2"],  # must be the exact phrase, not scattered words (was matching snowboarding gear)
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
        "label": "Secret of Mana",
        "query": "secret of mana",
        "max_price": 45,
        "require_any": ["secret of mana"],  # must be the exact phrase, not scattered words
        "exclude_words": RETRO_EXCLUDE_WORDS + ["playstation", "ps4", "vinyl", "record", "records", "figure"],
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
        "require_words": ["chipper", "jones", "333"],
        "require_any": ["psa 10", "psa10", "psa-10"],  # cover common spacing/formatting variants sellers use
        "exclude_words": BASEBALL_CARD_EXCLUDE_WORDS,
    },
    {
        "label": "Nolan Ryan 1980 Topps #580 PSA 8",
        "query": "Nolan Ryan 1980 Topps 580 PSA 8",
        "max_price": 120,
        "min_price": 50,
        "require_words": ["nolan", "ryan", "580"],
        "require_any": ["psa 8", "psa8", "psa-8"],  # cover common spacing/formatting variants sellers use
        "exclude_words": BASEBALL_CARD_EXCLUDE_WORDS,
    },

    # --- Basketball cards ---
    {
        "label": "Luka Doncic 2018 Prizm #280 RC PSA 10",
        "query": "Luka Doncic 2018 Prizm 280 RC PSA 10",
        "max_price": 180,
        "min_price": 70,
        "require_words": ["luka", "doncic", "280"],
        "require_any": ["psa 10", "psa10", "psa-10"],  # cover common spacing/formatting variants sellers use
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

    Results are sorted by newly listed and expanded to 100. We deliberately
    keep this to ONE API request per item per run because the bot runs
    frequently and API usage matters.
    """
    min_price = item.get("min_price", "")
    price_range = f"price:[{min_price}..{item['max_price']}]"

    params = {
        "q": item["query"],
        "limit": str(SEARCH_RESULT_LIMIT),
        "sort": "NEWLY_LISTED",
        "filter": f"buyingOptions:{{FIXED_PRICE}},{price_range},priceCurrency:USD",
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    # Calculated-shipping listings only return a shippingOptions array
    # if eBay knows a destination to estimate shipping to. Without this
    # header, shipping info is silently omitted from results even
    # though the listing itself has a real shipping cost.
    if EBAY_ZIP:
        headers["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation=country=US,zip={EBAY_ZIP}"

    response = requests.get(
        SEARCH_URL,
        headers=headers,
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


def is_quiet_hours(now=None) -> bool:
    """
    Returns True if the current time (in QUIET_HOURS_TZ) falls within
    the quiet-hours window. Handles the overnight wraparound (start
    time is later in the day than end time).
    """
    from datetime import datetime

    if now is None:
        now = datetime.now(QUIET_HOURS_TZ)
    else:
        now = now.astimezone(QUIET_HOURS_TZ)

    current_time = now.time()

    if QUIET_HOURS_START <= QUIET_HOURS_END:
        # Normal same-day window, e.g. 1pm-5pm
        return QUIET_HOURS_START <= current_time < QUIET_HOURS_END
    else:
        # Overnight window, e.g. 10pm-6:30am - true if it's after
        # start OR before end
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
        # Nothing queued - remove the file rather than leave an empty
        # array committed to the repo indefinitely.
        PENDING_FILE.unlink()


def get_shipping_cost(listing: dict) -> float | None:
    """
    Returns the cheapest shipping cost for a listing, or 0.0 if free
    shipping, or None if shipping info isn't available (e.g. local
    pickup only, or the field wasn't returned for some reason).
    """
    shipping_options = listing.get("shippingOptions")
    if not shipping_options:
        return None

    costs = []
    for option in shipping_options:
        cost = option.get("shippingCost", {}).get("value")
        if cost is not None:
            costs.append(float(cost))

    if not costs:
        return None

    return min(costs)


def build_alert_content(item: dict, listing: dict) -> str:
    title = listing.get("title", "Untitled")
    price_str = listing.get("price", {}).get("value", "?")
    url = listing.get("itemWebUrl", "")

    min_price = item.get("min_price")
    if min_price is not None:
        threshold_str = f"${min_price}-${item['max_price']}"
    else:
        threshold_str = f"≤ ${item['max_price']}"

    shipping_cost = get_shipping_cost(listing)

    try:
        price = float(price_str)
    except (TypeError, ValueError):
        price = None

    if shipping_cost is None:
        # Shipping info unavailable (e.g. local pickup only) - don't
        # claim a total we can't actually back up.
        price_line = f"**${price_str} - {title}**\n(shipping cost unavailable)"
    elif shipping_cost == 0:
        price_line = f"**${price_str} (free shipping) - {title}**"
    elif price is not None:
        total = price + shipping_cost
        price_line = f"**${price_str} + ${shipping_cost:.2f} shipping = ${total:.2f} total - {title}**"
    else:
        price_line = f"**${price_str} + ${shipping_cost:.2f} shipping - {title}**"

    return (
        f"{price_line}\n"
        f"Matched: *{item['label']}* (threshold: {threshold_str})\n"
        f"<{url}>"
    )


def post_to_discord(content: str):
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set - skipping notification.")
        return

    response = requests.post(DISCORD_WEBHOOK_URL, json={"content": content})
    if response.status_code not in (200, 204):
        print(f"Discord post failed: {response.status_code} {response.text}")


def send_discord_alert(item: dict, listing: dict):
    post_to_discord(build_alert_content(item, listing))


def flush_pending_alerts():
    """
    Posts all queued off-hours alerts as a single batch dump, then
    clears the queue. Discord has a ~2000 character message limit, so
    alerts are grouped into chunks rather than sent as one giant post.
    """
    pending = load_pending()
    if not pending:
        return

    header = f"**Overnight digest - {len(pending)} listing(s) found during quiet hours:**\n\n"
    chunks = []
    current_chunk = header
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

    if not EBAY_ZIP:
        print(
            "WARNING: EBAY_ZIP not set - shipping costs will be unavailable "
            "for calculated-shipping listings in Discord alerts."
        )

    quiet_now = is_quiet_hours()

    # If we're no longer in quiet hours, flush anything queued from
    # overnight before processing this run's own results. This means
    # whichever run happens at/after QUIET_HOURS_END delivers the batch
    # dump - typically the ~6:30am run, given the 5-minute cron cadence.
    if not quiet_now:
        flush_pending_alerts()

    token = get_access_token()
    seen_ids = load_seen()
    pending = load_pending()
    new_alerts = 0
    queued_alerts = 0

    for item in ITEMS:
        results = search_item(token, item)

        stats = {
            "api_results": len(results),
            "already_seen": 0,
            "missing_id": 0,
            "required_words": 0,
            "required_any": 0,
            "excluded_words": 0,
            "shipping": 0,
            "eligible": 0,
        }

        eligible_listings = []

        for listing in results:
            item_id = listing.get("itemId")

            if not item_id:
                stats["missing_id"] += 1
                continue

            if item_id in seen_ids:
                stats["already_seen"] += 1
                continue

            title = listing.get("title", "")

            if not matches_required_words(title, item.get("require_words")):
                stats["required_words"] += 1
                continue

            if not matches_any_words(title, item.get("require_any")):
                stats["required_any"] += 1
                continue

            if not matches_excluded_words(title, item.get("exclude_words")):
                stats["excluded_words"] += 1
                continue

            shipping_cost = get_shipping_cost(listing)
            if shipping_cost is not None and shipping_cost > MAX_SHIPPING_COST:
                stats["shipping"] += 1
                continue

            stats["eligible"] += 1
            eligible_listings.append(listing)

        print(
            f"{item['label']}: "
            f"{stats['api_results']} found | "
            f"{stats['already_seen']} seen | "
            f"{stats['required_words']} req-word rejects | "
            f"{stats['required_any']} req-any rejects | "
            f"{stats['excluded_words']} excluded | "
            f"{stats['shipping']} shipping rejects | "
            f"{stats['eligible']} NEW eligible"
        )

        for listing in eligible_listings:
            item_id = listing["itemId"]
            content = build_alert_content(item, listing)

            if quiet_now:
                pending.append({"content": content})
                queued_alerts += 1
            else:
                post_to_discord(content)
                new_alerts += 1

            seen_ids.add(item_id)

    save_seen(seen_ids)
    if quiet_now:
        save_pending(pending)

    print(f"\nDone. {new_alerts} alert(s) sent, {queued_alerts} queued for the morning digest.")


if __name__ == "__main__":
    run()