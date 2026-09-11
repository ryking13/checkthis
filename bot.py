"""
eBay Alert Bot - runs on GitHub Actions every 5 minutes (via cron-job.org
trigger), searches eBay for specific items, and posts a Discord alert
when a Buy-It-Now listing is found under that item's price threshold[cite: 1].

This is a separate, independent project from the local Facebook
Marketplace watcher - eBay's API is stateless (no login/session/device
trust needed), so it's safe and appropriate to run in an ephemeral
cloud environment like GitHub Actions, unlike the Facebook scraper[cite: 1].

Dedup is handled with a simple seen_listings.json file, committed back
to the repo after each run (see .github/workflows/ebay-scan.yml) -
same underlying idea as the SQLite store in the Facebook project, just
a format that's easy for a GitHub Actions job to read/write/commit[cite: 1].

Incremental search uses item_search_metadata.json to track the timestamp
of each item's last run[cite: 1]. On every run (including the first), we search
for listings from the last 6 minutes[cite: 1]. This gives a precise, narrow window
without massive scans[cite: 1].

NOTE: eBay's Browse API item_summary/search endpoint does NOT support an
itemStartDate filter clause - it isn't in eBay's documented list of
supported `filter` values. An earlier version of this bot sent
`itemStartDate:[<cutoff>..]` as part of the `filter` param, which eBay's
API silently failed to honor (or rejected), causing zero results across
every single item for hours at a time. Sorting by newlyListed and then
manually filtering the returned page by itemCreationDate / itemOriginDate
(see search_item()) achieves the same narrow-window effect without relying
on a filter key eBay doesn't actually support.

NOTE: the `sort` parameter is case-sensitive - eBay's documented value is
`newlyListed`, not `NEWLY_LISTED`. An earlier version of this bot used the
wrong case, which eBay rejected outright (with an explicit `warnings`
entry, errorId 12008) for some category-restricted searches (e.g. the
PSA-graded trading card items) but was tolerated silently for others. If
result counts look thin for a specific item without any visible eBay
warning in the log, that's a sign eBay may be applying different sort
validation per category - check the raw response, don't assume silence
means the param was accepted cleanly.

The metadata is used for Discord deduplication only - we filter to only
alert on listings that weren't present in the previous run[cite: 1].
"""

import os
import json
import base64
import statistics
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

# --- eBay API config ---
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# US zip code used as the shipping destination when querying eBay, so
# that calculated-shipping listings actually return a shippingOptions
# array[cite: 1]. Without this, eBay has no destination to estimate shipping to
# and silently omits shipping info from search results (see
# X-EBAY-C-ENDUSERCTX / contextualLocation in eBay's docs)[cite: 1].
EBAY_ZIP = os.environ.get("EBAY_ZIP", "")

SEEN_FILE = Path(__file__).parent / "seen_listings.json"
METADATA_FILE = Path(__file__).parent / "item_search_metadata.json"
PENDING_FILE = Path(__file__).parent / "pending_alerts.json"

# Retrieve up to 100 items per API call (eBay's max per page)[cite: 1].
# With a tight 6-minute window, we'll rarely hit 100 results[cite: 1].
SEARCH_RESULT_LIMIT = 100

# Search for listings from the last N minutes (with 1-minute overlap)[cite: 1]
SEARCH_WINDOW_MINUTES = 6
SEARCH_WINDOW_OVERLAP_MINUTES = 1

# One-time recovery mode:
# Set RECOVERY_MODE=true in the GitHub Actions environment for a historical
# catch-up run. Recovery mode paginates through multiple eBay result pages
# instead of assuming the first 100 results contain the whole time window.
RECOVERY_MODE = os.environ.get("RECOVERY_MODE", "").lower() in ("1", "true", "yes")
RECOVERY_MAX_PAGES = int(os.environ.get("RECOVERY_MAX_PAGES", "10"))

# In recovery mode, if the normal price-filtered search returns no raw results,
# retry once without the price filter. The normal price check is still applied
# locally later in run(), so this is a safety net against an overly restrictive
# eBay search response rather than a change to alert criteria.
RECOVERY_FALLBACK_NO_PRICE = os.environ.get(
    "RECOVERY_FALLBACK_NO_PRICE", "true"
).lower() in ("1", "true", "yes")

# --- Quiet hours ---
# No Discord notifications are sent between QUIET_HOURS_START and
# QUIET_HOURS_END (in QUIET_HOURS_TZ)[cite: 1]. Matches found during that window
# are still detected and saved to disk (see PENDING_FILE) - they're
# just queued instead of posted immediately[cite: 1]. As soon as a run happens
# at or after QUIET_HOURS_END, any queued alerts are flushed as a
# single batch dump before that run's own new alerts are sent[cite: 1].
#
# NOTE: GitHub Actions runs in UTC[cite: 1]. QUIET_HOURS_TZ tells the bot what
# "10pm" and "6:30am" mean in wall-clock time - update this if you
# move to a different timezone[cite: 1]. This does NOT auto-adjust in a way
# that requires code changes for DST; zoneinfo handles that[cite: 1].
from datetime import time as _time
from zoneinfo import ZoneInfo

QUIET_HOURS_TZ = ZoneInfo("America/Chicago")
QUIET_HOURS_START = _time(22, 0)   # 10:00 PM
QUIET_HOURS_END = _time(6, 30)     # 6:30 AM

# Applies to every item in ITEMS - listings with a known shipping cost
# above this are skipped entirely (not even queued during quiet hours)[cite: 1].
# Listings where shipping cost couldn't be determined (e.g. local
# pickup only, or eBay didn't return shippingOptions) are NOT excluded
# by this filter, since we have no evidence they're actually expensive
# to ship - they still go through the normal price/title filters[cite: 1].
MAX_SHIPPING_COST = 15.00

# Shared exclusion list applied to all LEGO set searches - filters out
# standalone minifigures, box/bag/manual-only listings, parts lots,
# incomplete sets, and third-party lighting kits (not the actual set)[cite: 1].
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
# non-working carts to slip through[cite: 1].
RETRO_EXCLUDE_WORDS = ["japan", "japanese", "thousand", "untested", "guide", "circular", "poster", "art", "promotion", "promotional", "soundtrack", "fanart", "import", "lot"]

# Shared exclusion list applied to baseball-card searches[cite: 1].
# These are intended to keep the scanner focused on PSA-graded cards
# and eliminate common non-card / non-original-card noise[cite: 1].
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
        "exclude_words": ["school", "case", "silicone"],
    },
    {
        "label": "TI-Nspire CX",
        "query": "ti-nspire cx",
        "max_price": 30,
        "require_words": ["cx"],  # must specifically say "CX" - plain TI-Nspire isn't as valuable
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

    # --- Baseball cards ---
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

    # --- Basketball cards ---
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


def search_item(token: str, item: dict) -> list[dict]:
    """
    Searches eBay for one configured item.

    Normal mode:
      - one page of newly-listed results
      - locally keeps only listings inside the narrow time window

    Recovery mode:
      - paginates through multiple result pages
      - continues until results are older than the cutoff or the configured
        recovery page limit is reached
      - prints detailed diagnostics so a zero-result run can be distinguished
        from an eBay/API result, date-filter, or pagination problem
      - optionally retries once without the eBay price filter if the first
        request returns no raw results
    """
    min_price = item.get("min_price")
    price_range = f"price:[{min_price}..{item['max_price']}]"

    now = datetime.now(timezone.utc)
    cutoff_time = now - timedelta(
        minutes=SEARCH_WINDOW_MINUTES + SEARCH_WINDOW_OVERLAP_MINUTES
    )
    cutoff_iso = cutoff_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    if EBAY_ZIP:
        headers["X-EBAY-C-ENDUSERCTX"] = (
            f"contextualLocation=country=US,zip={EBAY_ZIP}"
        )

    base_filter = "buyingOptions:{FIXED_PRICE},priceCurrency:USD"
    price_filter = f",{price_range}"
    normal_filter = base_filter + price_filter

    def request_page(offset: int, filter_value: str):
        params = {
            "q": item["query"],
            "limit": str(SEARCH_RESULT_LIMIT),
            "offset": str(offset),
            "sort": "newlyListed",
            "filter": filter_value,
        }

        response = requests.get(
            SEARCH_URL,
            headers=headers,
            params=params,
            timeout=30,
        )

        if response.status_code != 200:
            print(
                f"Search failed for {item['label']!r}: "
                f"{response.status_code} {response.text}"
            )
            return None

        payload = response.json()
        warnings = payload.get("warnings")
        if warnings:
            print(f"  eBay API warnings for {item['label']!r}: {warnings}")

        return payload

    def collect_pages(filter_value: str):
        all_results = []
        page = 0
        total_reported = None
        hit_old_result = False
        missing_dates = 0
        invalid_dates = 0

        while True:
            offset = page * SEARCH_RESULT_LIMIT
            payload = request_page(offset, filter_value)
            if payload is None:
                break

            page_results = payload.get("itemSummaries", [])
            all_results.extend(page_results)

            if total_reported is None:
                total_reported = payload.get("total")

            for listing in page_results:
                creation_str = (
                    listing.get("itemCreationDate")
                    or listing.get("itemOriginDate")
                )
                if not creation_str:
                    missing_dates += 1
                    continue
                try:
                    creation_dt = datetime.fromisoformat(
                        creation_str.replace("Z", "+00:00")
                    )
                    if creation_dt < cutoff_time:
                        hit_old_result = True
                except (ValueError, TypeError):
                    invalid_dates += 1

            page += 1

            if not page_results:
                break

            if not RECOVERY_MODE:
                break

            if hit_old_result:
                break

            if page >= RECOVERY_MAX_PAGES:
                break

            if (
                total_reported is not None
                and offset + len(page_results) >= total_reported
            ):
                break

        return (
            all_results,
            total_reported,
            page,
            missing_dates,
            invalid_dates,
            hit_old_result,
        )

    (
        raw_results,
        total_reported,
        pages,
        missing_dates,
        invalid_dates,
        hit_old_result,
    ) = collect_pages(normal_filter)

    # Recovery-only safety net. If eBay returns no raw matches at all, retry
    # without the price filter. run() enforces the configured price bounds
    # locally before an alert is sent.
    fallback_used = False
    if RECOVERY_MODE and RECOVERY_FALLBACK_NO_PRICE and not raw_results:
        fallback_used = True
        print(
            f"  Recovery fallback for {item['label']!r}: "
            "retrying without eBay price filter."
        )
        (
            raw_results,
            total_reported,
            pages,
            missing_dates,
            invalid_dates,
            hit_old_result,
        ) = collect_pages(base_filter)

    filtered_results = []
    older_than_cutoff = 0

    for listing in raw_results:
        creation_str = (
            listing.get("itemCreationDate")
            or listing.get("itemOriginDate")
        )

        if not creation_str:
            filtered_results.append(listing)
            continue

        try:
            creation_dt = datetime.fromisoformat(
                creation_str.replace("Z", "+00:00")
            )
            if creation_dt >= cutoff_time:
                filtered_results.append(listing)
            else:
                older_than_cutoff += 1
        except (ValueError, TypeError):
            # Keep malformed-date results visible rather than silently losing
            # a potentially valid listing.
            filtered_results.append(listing)

    print(
        f"  Search diagnostics [{item['label']}]: "
        f"window={SEARCH_WINDOW_MINUTES}m + overlap="
        f"{SEARCH_WINDOW_OVERLAP_MINUTES}m | "
        f"cutoff={cutoff_iso} | "
        f"raw={len(raw_results)} | "
        f"eBay_total={total_reported} | "
        f"pages={pages} | "
        f"date_filtered={len(filtered_results)} | "
        f"older={older_than_cutoff} | "
        f"missing_date={missing_dates} | "
        f"invalid_date={invalid_dates} | "
        f"hit_old={hit_old_result} | "
        f"fallback_no_price={fallback_used}"
    )

    dated = []
    for listing in raw_results:
        creation_str = (
            listing.get("itemCreationDate")
            or listing.get("itemOriginDate")
        )
        if creation_str:
            dated.append(creation_str)

    if dated:
        print(
            f"  Date range returned [{item['label']}]: "
            f"newest={max(dated)} | oldest={min(dated)}"
        )

    return filtered_results


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
    from datetime import datetime

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


def post_to_discord(content: str, max_retries: int = 4) -> bool:
    """
    Post to Discord and retry rate limits/transient failures.

    Returns True only when Discord accepted the message. Listings are only
    marked as seen after successful delivery (or safe persistence to pending).
    """
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set - skipping notification.")
        return False

    import time

    for attempt in range(max_retries + 1):
        try:
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                json={"content": content},
                timeout=30,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                print(f"Discord post failed after retries: {exc}")
                return False
            wait_seconds = 2 ** attempt
            print(
                f"Discord request error; retrying in {wait_seconds}s: {exc}"
            )
            time.sleep(wait_seconds)
            continue

        if response.status_code in (200, 204):
            return True

        if response.status_code == 429:
            retry_after = 2.0
            try:
                retry_after = float(
                    response.json().get("retry_after", retry_after)
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                pass

            if attempt >= max_retries:
                print(
                    f"Discord post failed after {max_retries + 1} attempts: "
                    f"429 {response.text}"
                )
                return False

            print(
                f"Discord rate limited (429); retrying in "
                f"{retry_after:.2f}s "
                f"(attempt {attempt + 1}/{max_retries + 1})"
            )
            time.sleep(max(0.1, retry_after))
            continue

        if response.status_code >= 500 and attempt < max_retries:
            wait_seconds = 2 ** attempt
            print(
                f"Discord server error {response.status_code}; "
                f"retrying in {wait_seconds}s"
            )
            time.sleep(wait_seconds)
            continue

        print(f"Discord post failed: {response.status_code} {response.text}")
        return False

    return False


def send_discord_alert(item: dict, listing: dict) -> bool:
    return post_to_discord(build_alert_content(item, listing))


def flush_pending_alerts():
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

    print(
        f"Scan mode: {'RECOVERY' if RECOVERY_MODE else 'NORMAL'} | "
        f"window={SEARCH_WINDOW_MINUTES}m | "
        f"overlap={SEARCH_WINDOW_OVERLAP_MINUTES}m"
    )
    if RECOVERY_MODE:
        print(
            f"Recovery pagination: up to {RECOVERY_MAX_PAGES} pages "
            f"({SEARCH_RESULT_LIMIT} results/page)"
        )

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
        results = search_item(token, item)

        stats = {
            "api_results": len(results),
            "already_seen": 0,
            "missing_id": 0,
            "price": 0,
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

            # Recovery fallback searches without the eBay price filter, so
            # enforce the configured price bounds locally for every listing.
            try:
                listing_price = float(
                    listing.get("price", {}).get("value")
                )
            except (TypeError, ValueError):
                listing_price = None

            max_price = float(item["max_price"])
            min_price = item.get("min_price")

            if listing_price is None:
                stats["price"] += 1
                continue

            if listing_price > max_price or (
                min_price is not None and listing_price < float(min_price)
            ):
                stats["price"] += 1
                continue

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
            f"{stats['price']} price rejects | "
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
                pending.append({"item_id": item_id, "content": content})
                queued_alerts += 1
                # The alert is durably persisted, so it is safe to mark it
                # seen and avoid queueing it repeatedly.
                seen_ids.add(item_id)
            else:
                delivered = post_to_discord(content)
                if delivered:
                    new_alerts += 1
                    seen_ids.add(item_id)
                else:
                    print(
                        f"  NOT marking {item_id} as seen because Discord "
                        "delivery failed."
                    )

        metadata[item["label"]] = now_iso

    save_seen(seen_ids)
    save_search_metadata(metadata)
    if quiet_now:
        save_pending(pending)

    print(f"\nDone. {new_alerts} alert(s) sent, {queued_alerts} queued for the morning digest.")


if __name__ == "__main__":
    run()
