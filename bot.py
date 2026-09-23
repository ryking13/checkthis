"""
eBay Alert Bot - Scans eBay for configured items and posts alerts to Discord.
Uses a hybrid discovery strategy: scans two pages of the normal eBay result set on
one run, then two pages sorted by newlyListed on the next run. This preserves the
existing per-run API-call budget while alternating broad/deep discovery with
new-list discovery.

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
import re
import unicodedata
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
TOKEN_CACHE_FILE = Path(__file__).parent / "ebay_token_cache.json"

SEARCH_RESULT_LIMIT = 200   # Maximum allowed by eBay API per request
MAX_PAGES_PER_ITEM = 2      # 200 x 2 = Up to 400 listings checked per discovery mode
MAX_SHIPPING_COST = 15.00

# --- Discovery strategy ---
# We alternate between the normal/default eBay ordering and newlyListed.
# This avoids doubling API usage while ensuring that newly listed bargains are
# checked regularly. Each mode still gets two pages (up to 400 results).
#
# "newlyListed" is supported by the Browse API, but keeping the default search
# as the alternating companion protects against cases where the newly-listed
# result set is incomplete or behaves differently from normal search relevance.
DISCOVERY_MODES = ("default", "newlyListed")
DISCOVERY_MODE_FILE_KEY = "_next_discovery_mode"

# When diagnostics are enabled, print a sample of titles rejected by the local
# keyword filter. This is intentionally sampled rather than printing every
# rejection, so Discord/eBay logs don't become enormous.
FILTER_DIAGNOSTIC_SAMPLE_SIZE = 12

# --- Rate limiting ---
# eBay's Browse API is capped per application per day (5,000 calls/day on the
# default Buy plan, resetting at midnight Pacific). Every item costs at least
# one call per run, so total daily usage is roughly:
#     runs_per_day x len(ITEMS) x pages_actually_fetched
# Blowing past that cap returns HTTP 429 / errorId 2001 for the rest of the
# day, which is what makes every search come back with raw=0.
REQUEST_DELAY = 0.35        # seconds to pause between Browse API calls
MAX_RETRIES = 3             # attempts per page before giving up on it
BACKOFF_BASE = 2.0          # seconds; doubled each retry
TOKEN_REFRESH_MARGIN = 300  # refresh the cached token this many seconds early

# During quiet hours nothing is alerted immediately - matches just go into the
# overnight digest - so scanning every 5 minutes then buys nothing and costs
# ~40% of the daily quota. Only run when the minute-of-day is divisible by
# this, i.e. 15 => :00/:15/:30/:45 instead of all twelve 5-minute slots.
QUIET_HOURS_SCAN_INTERVAL_MIN = 15

# Soft ceiling, deliberately below eBay's hard 5,000/day so retries and second
# pages can't push us over. Usage resets at midnight Pacific, matching eBay.
DAILY_CALL_BUDGET = 4600
USAGE_FILE = Path(__file__).parent / "api_usage.json"
USAGE_TZ = ZoneInfo("America/Los_Angeles")

# Set once a 429 survives all retries. eBay's 2001 error is an app-wide quota
# error, not a per-search one, so once it sticks there is no point burning
# another ~18 calls proving the same thing for every remaining item.
_quota_exhausted = False

# Quiet Hours
QUIET_HOURS_TZ = ZoneInfo("America/Chicago")
QUIET_HOURS_START = _time(22, 0)   # 10:00 PM
QUIET_HOURS_END = _time(6, 30)     # 6:30 AM

# Shared Exclusions
LEGO_EXCLUDE_WORDS = ["minifigure", "minifigures", "only", "pieces", "light kit", "lighting kit", "incomplete", "display"]
RETRO_EXCLUDE_WORDS = ["software","japan", "japanese", "thousand", "untested", "guide", "circular", "poster", "art", "promotion", "promotional", "soundtrack", "fanart", "import", "lot", "comic", "guidebook", "guides"]
BASEBALL_CARD_EXCLUDE_WORDS = ["sgc", "bccg", "bgs", "beckett", "cgc", "csg", "hga", "tag", "reprint", "replica", "reproduction", "custom", "proxy", "fake", "counterfeit", "digital", "lot", "lots"]

ITEMS = [
    {
        "label": "AirPort Express A1392",
        "query": "airport express a1392",
        "max_price": 20,
        "exclude_words": ["a1264", "a1084", "a1143", "a1408", "a1301"],
    },
    #{
    #    "label": "TI-84 Plus",
    #    "query": "ti-84 plus",
    #    "max_price": 20,
    #    "require_words": ["plus"],
    #    "exclude_words": ["school", "case", "silicone"],
    #},
    #{
    #    "label": "TI-Nspire CX",
    #    "query": "ti-nspire cx",
    #    "max_price": 30,
    #    "require_words": ["cx"],
    #    "exclude_words": ["school", "case", "silicone"],
    #},
    # --- Retro N64/SNES games ---
    {
        "label": "Paper Mario (N64)",
        "query": "paper mario n64",
        "max_price": 60,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS,
    },
    #{
    #   "label": "Big Mountain 2000",
    #    "query": "Big Mountain 2000 n64",
    #    "max_price": 100,
    #    "min_price": 39,
    #    "require_words": ["big mountain 2000"],
    #    "exclude_words": RETRO_EXCLUDE_WORDS,
    #},
    {
        "label": "PGA European Tour n64",
        "query": "PGA European Tour n64",
        "max_price": 100,
        "min_price": 39,
        "require_any": ["PGA European Tour"],
        "exclude_words": RETRO_EXCLUDE_WORDS + ["super", "SNES", "sega","photo","lost","boy","gameboy","playstation", "box", "card", "manual"],
    },
    {
        "label": "Castlevania Legacy of Darkness n64",
        "query": "Castlevania Legacy of Darkness",
        "max_price": 150,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS + ["box", "manual"],
    },
    {
        "label": "Ogre Battle 64",
        "query": "Ogre Battle 64",
        "max_price": 155,
        "min_price": 40,
        "exclude_words": RETRO_EXCLUDE_WORDS + ["manual", "booklet", "book", "guidebook", "guides"],
    },
    {
        "label": "Carmageddon n64",
        "query": "Carmageddon",
        "max_price": 125,
        "min_price": 39,
        "exclude_words": RETRO_EXCLUDE_WORDS + ["boy", "gameboy", "playstation", "PC","manual", "booklet", "box", "max", "xbox", "ps4"],
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
    #{
    #    "label": "Zelda Majora's Mask",
    #    "query": "zelda majora's mask",
    #    "max_price": 75,
    #    "min_price": 39,
    #    "exclude_words": RETRO_EXCLUDE_WORDS + ["3ds", "hoodie", "wearable", "figures", "figure", "watch", "amiibo", "collection", "funko", "pin", "plush"],
    #},
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
        "require_any": ["mega man 64", "megaman 64", "megaman64"],
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
        "require_any": ["starcraft 64", "starcraft64"],
        "exclude_words": RETRO_EXCLUDE_WORDS,
    },
    # --- Sports Cards ---
    {
        "label": "Chipper Jones 1991 Topps #333 PSA 10",
        "query": "Chipper Jones 1991 Topps 333",
        "max_price": 135,
        "min_price": 50,
        "require_words": ["chipper", "jones", "333"],
        "require_any": ["psa 10", "psa10", "psa-10"],
        "exclude_words": BASEBALL_CARD_EXCLUDE_WORDS,
        "include_auctions": True,
    },
    {
        "label": "Nolan Ryan 1980 Topps #580 PSA 8",
        "query": "Nolan Ryan 1980 Topps 580",
        "max_price": 130,
        "min_price": 50,
        "require_words": ["nolan", "ryan", "580"],
        "require_any": ["psa 8", "psa8", "psa-8"],
        "exclude_words": BASEBALL_CARD_EXCLUDE_WORDS,
        "include_auctions": True,
    },
    {
        "label": "Luka Doncic 2018 Prizm #280 RC PSA 10",
        "query": "Luka Doncic 2018 Prizm 280",
        "max_price": 180,
        "min_price": 70,
        "require_words": ["luka", "doncic", "280"],
        "require_any": ["psa 10", "psa10", "psa-10"],
        "exclude_words": BASEBALL_CARD_EXCLUDE_WORDS,
        "include_auctions": True,
    },
]


def get_access_token() -> str:
    """
    Returns an application access token, reusing a cached one when it is still
    valid. Client-credentials tokens last ~2 hours, so minting a fresh one on
    every run wastes calls against the identity endpoint's own rate limit for
    no benefit.
    """
    cached = _load_cached_token()
    if cached:
        return cached

    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()
    response = requests.post(
        TOKEN_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded}",
        },
        data={"grant_type": "client_credentials", "scope": OAUTH_SCOPE},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload["access_token"]
    _save_cached_token(token, int(payload.get("expires_in", 7200)))
    return token


def _load_cached_token() -> str | None:
    if not TOKEN_CACHE_FILE.exists():
        return None
    try:
        with open(TOKEN_CACHE_FILE) as f:
            data = json.load(f)
        if time.time() < float(data["expires_at"]) - TOKEN_REFRESH_MARGIN:
            return data["access_token"]
    except (json.JSONDecodeError, IOError, KeyError, TypeError, ValueError):
        pass
    return None


def _save_cached_token(token: str, expires_in: int):
    try:
        with open(TOKEN_CACHE_FILE, "w") as f:
            json.dump({"access_token": token, "expires_at": time.time() + expires_in}, f)
    except IOError as exc:
        print(f"Could not cache eBay token (non-fatal): {exc}")


def _usage_today() -> dict:
    today = datetime.now(USAGE_TZ).strftime("%Y-%m-%d")
    if USAGE_FILE.exists():
        try:
            with open(USAGE_FILE) as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return {"date": today, "calls": 0}


def calls_used_today() -> int:
    return _usage_today().get("calls", 0)


def record_call(n: int = 1):
    data = _usage_today()
    data["calls"] = data.get("calls", 0) + n
    try:
        with open(USAGE_FILE, "w") as f:
            json.dump(data, f)
    except IOError as exc:
        print(f"Could not record API usage (non-fatal): {exc}")


def ebay_get(url: str, headers: dict, params: dict) -> requests.Response | None:
    """
    GETs an eBay API URL, retrying on 429 and 5xx with exponential backoff and
    honoring a Retry-After header when eBay sends one.

    Returns the successful Response, or None if the request could not be
    completed. On a 429 that survives every retry, sets the module-level
    _quota_exhausted flag so the caller can abandon the rest of the run
    instead of generating one more 429 per remaining search term.
    """
    global _quota_exhausted

    for attempt in range(MAX_RETRIES):
        try:
            record_call()
            response = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt + 1}/{MAX_RETRIES}): {exc}")
            if attempt == MAX_RETRIES - 1:
                return None
            time.sleep(BACKOFF_BASE * (2 ** attempt))
            continue

        if response.status_code == 200:
            return response

        if response.status_code == 429 or response.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                if response.status_code == 429:
                    _quota_exhausted = True
                return response

            retry_after = response.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else BACKOFF_BASE * (2 ** attempt)
            except ValueError:
                wait = BACKOFF_BASE * (2 ** attempt)
            wait = min(wait, 30.0)
            print(f"  HTTP {response.status_code} - retrying in {wait:.1f}s "
                  f"(attempt {attempt + 1}/{MAX_RETRIES}).")
            time.sleep(wait)
            continue

        # 4xx other than 429: retrying will not help.
        return response

    return None


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


def search_item(token: str, item: dict, discovery_mode: str) -> tuple[list[dict], dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    if EBAY_ZIP:
        headers["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation=country=US,zip={EBAY_ZIP}"

    min_p = item.get("min_price", "")
    max_p = item["max_price"]
    price_clause = f"price:[{min_p}..{max_p}]"
    # Most items only care about Buy-It-Now listings, but some (e.g. graded
    # sports cards) are commonly sold at auction too - let an item opt in
    # via "include_auctions": True instead of hardcoding fixed-price-only
    # for everything.
    buying_options = "FIXED_PRICE|AUCTION" if item.get("include_auctions") else "FIXED_PRICE"
    filter_value = f"buyingOptions:{{{buying_options}}},{price_clause},priceCurrency:USD"

    all_results = []
    pages_scanned = 0
    ok = True
    mode_label = discovery_mode

    for page in range(MAX_PAGES_PER_ITEM):
        pages_scanned = page + 1
        offset = page * SEARCH_RESULT_LIMIT
        params = {
            "q": item["query"],
            "limit": str(SEARCH_RESULT_LIMIT),
            "offset": str(offset),
            "filter": filter_value,
        }
        if discovery_mode != "default":
            params["sort"] = discovery_mode

        if page > 0:
            time.sleep(REQUEST_DELAY)

        response = ebay_get(SEARCH_URL, headers, params)
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else "no response"
            body = response.text[:300] if response is not None else ""
            print(f"Search failed for {item['label']!r} [{mode_label}]: {status} {body}")
            ok = False
            break

        payload = response.json()
        warnings = payload.get("warnings")
        if warnings:
            print(f"  eBay API warnings for {item['label']!r} [{mode_label}]: {warnings}")

        page_results = payload.get("itemSummaries", [])
        if not page_results:
            break

        all_results.extend(page_results)

        # Only stop early once we've hit the natural end of results (a
        # partially-full page means there's nothing more to fetch).
        if len(page_results) < SEARCH_RESULT_LIMIT:
            break

    stats = {
        "raw": len(all_results),
        "pages": pages_scanned,
        "ok": ok,
        "mode": discovery_mode,
    }
    status_note = "" if ok else "  <-- SEARCH ERRORED, results are incomplete"
    print(f"Search diagnostics [{item['label']}] [{mode_label}]: raw={stats['raw']} "
          f"(pages: {stats['pages']}){status_note}")

    return all_results, stats


def _normalize_text(text: str) -> str:
    """Lowercase and strip accents so e.g. 'Pokémon' matches 'pokemon'."""
    nfkd = unicodedata.normalize("NFKD", text)
    without_accents = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return without_accents.lower()


def matches_required_words(title: str, require_words: list[str] | None) -> bool:
    if not require_words:
        return True
    title_norm = _normalize_text(title)
    return all(_normalize_text(word) in title_norm for word in require_words)


def matches_any_words(title: str, require_any: list[str] | None) -> bool:
    if not require_any:
        return True
    title_norm = _normalize_text(title)
    return any(_normalize_text(phrase) in title_norm for phrase in require_any)


def matches_excluded_words(title: str, exclude_words: list[str] | None) -> bool:
    if not exclude_words:
        return True
    title_norm = _normalize_text(title)
    for word in exclude_words:
        word_norm = _normalize_text(word)
        # Word-boundary match so e.g. "art" doesn't match inside "cartridge".
        if re.search(r"\b" + re.escape(word_norm) + r"\b", title_norm):
            return False
    return True


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
    try:
        with open(PENDING_FILE) as f:
            pending = json.load(f)
    except (json.JSONDecodeError, IOError, TypeError):
        print("WARNING: pending_alerts.json could not be read; starting with an empty queue.")
        return []

    # Defensive de-duplication protects against repeated/overlapping workflow runs.
    deduped = []
    seen_ids = set()
    for entry in pending if isinstance(pending, list) else []:
        item_id = entry.get("item_id")
        if item_id and item_id not in seen_ids:
            seen_ids.add(item_id)
            deduped.append(entry)
    return deduped


def save_pending(pending: list[dict]):
    """Persist a de-duplicated pending queue, or remove it when empty."""
    deduped = []
    seen_ids = set()
    for entry in pending:
        item_id = entry.get("item_id")
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        deduped.append(entry)

    if deduped:
        with open(PENDING_FILE, "w") as f:
            json.dump(deduped, f, indent=2)
    elif PENDING_FILE.exists():
        # Remove the actual file, not just its git index entry.
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


def should_scan_now() -> bool:
    """
    The cron fires every 5 minutes. During quiet hours nothing goes out
    immediately anyway (matches are queued for the morning digest), so most of
    those slots are skipped to stay inside eBay's 5,000 calls/day cap while
    keeping full 5-minute cadence during waking hours.
    """
    if not is_quiet_hours():
        return True
    now = datetime.now(QUIET_HOURS_TZ)
    minute_of_day = now.hour * 60 + now.minute
    return minute_of_day % QUIET_HOURS_SCAN_INTERVAL_MIN == 0


def get_next_discovery_mode(metadata: dict) -> str:
    """Return the mode for this run and persist the opposite mode for next run."""
    current = metadata.get(DISCOVERY_MODE_FILE_KEY, "default")
    if current not in DISCOVERY_MODES:
        current = "default"

    next_mode = "newlyListed" if current == "default" else "default"
    metadata[DISCOVERY_MODE_FILE_KEY] = next_mode
    return current


def run():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set - aborting.")
        return

    if not should_scan_now():
        print(f"Quiet hours - skipping this slot (scanning every "
              f"{QUIET_HOURS_SCAN_INTERVAL_MIN} min until {QUIET_HOURS_END.strftime('%H:%M')}).")
        return

    used = calls_used_today()
    if used >= DAILY_CALL_BUDGET:
        print(f"Daily API budget spent ({used}/{DAILY_CALL_BUDGET}) - skipping this run. "
              f"Usage resets at midnight Pacific.")
        return
    print(f"eBay API calls used today: {used}/{DAILY_CALL_BUDGET}")

    # Bootstrap run: no seen_listings.json yet means this is the first time
    # the bot has ever run (or it was reset), so every currently active
    # matching listing will look "new" at once. Detect this before loading
    # seen_ids so we can route that first big batch through chunked
    # messages instead of firing one Discord alert per listing.
    #
    # A bootstrap run that gets cut short by rate limiting is still a bootstrap
    # run: it creates seen_listings.json having only scanned a few items, so
    # without this flag the next run would look "normal" and fire one Discord
    # message per listing for every item it never got to.
    metadata = load_search_metadata()
    discovery_mode = get_next_discovery_mode(metadata)
    # Persist the discovery rotation immediately. GitHub Actions should commit
    # item_search_metadata.json along with seen_listings.json.
    save_search_metadata(metadata)
    print(f"Discovery strategy this run: {discovery_mode} "
          f"(next run: {metadata[DISCOVERY_MODE_FILE_KEY]})")

    is_bootstrap_run = not SEEN_FILE.exists() or not metadata.get("_bootstrap_complete")
    bootstrap_batch: list[tuple[str, str]] = []  # (item_id, content) pairs
    if is_bootstrap_run:
        print("Bootstrap run in progress (results will be batched).")

    quiet_now = is_quiet_hours()
    if not quiet_now:
        flush_pending_alerts()

    token = get_access_token()
    seen_ids = load_seen()
    pending = load_pending()
    pending_ids = {entry.get("item_id") for entry in pending if entry.get("item_id")}
    now_iso = datetime.now(timezone.utc).isoformat()
    new_alerts = 0
    queued_alerts = 0

    for index, item in enumerate(ITEMS):
        if _quota_exhausted:
            print(f"Skipping {item['label']!r} and all remaining items - eBay request quota exhausted.")
            continue

        if index > 0:
            time.sleep(REQUEST_DELAY)

        results, stats = search_item(token, item, discovery_mode)

        already_seen_count = 0
        price_rejected_count = 0
        keyword_rejected_count = 0
        shipping_rejected_count = 0

        eligible_listings = []
        keyword_rejected_samples = []
        keyword_rejected_by_reason = {
            "required_words": 0,
            "require_any": 0,
            "exclude_words": 0,
        }

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

            required_ok = matches_required_words(title, item.get("require_words"))
            any_ok = matches_any_words(title, item.get("require_any"))
            excluded_ok = matches_excluded_words(title, item.get("exclude_words"))

            if not required_ok or not any_ok or not excluded_ok:
                keyword_rejected_count += 1

                # Break the rejection down so we can tell whether a search is
                # being filtered mostly by required terms, require_any, or
                # explicit exclusions. Keep a small title sample for inspection.
                if not required_ok:
                    keyword_rejected_by_reason["required_words"] += 1
                if not any_ok:
                    keyword_rejected_by_reason["require_any"] += 1
                if not excluded_ok:
                    keyword_rejected_by_reason["exclude_words"] += 1

                if len(keyword_rejected_samples) < FILTER_DIAGNOSTIC_SAMPLE_SIZE:
                    reasons = []
                    if not required_ok:
                        reasons.append("required")
                    if not any_ok:
                        reasons.append("require_any")
                    if not excluded_ok:
                        reasons.append("excluded")
                    keyword_rejected_samples.append(
                        f"    [{', '.join(reasons)}] {title}"
                    )
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

        if keyword_rejected_count:
            print(
                f"  Keyword rejection reasons [{item['label']}]: "
                f"required={keyword_rejected_by_reason['required_words']} | "
                f"require_any={keyword_rejected_by_reason['require_any']} | "
                f"excluded={keyword_rejected_by_reason['exclude_words']}"
            )
            if keyword_rejected_samples:
                print("  Keyword rejection sample:")
                for sample in keyword_rejected_samples:
                    print(sample)

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
                # Do not append the same listing repeatedly if workflow runs
                # overlap or replay a stale checkout.
                if item_id not in pending_ids:
                    pending.append({"item_id": item_id, "content": content})
                    pending_ids.add(item_id)
                    queued_alerts += 1
                seen_ids.add(item_id)
            else:
                delivered = post_to_discord(content)
                if delivered:
                    new_alerts += 1
                    seen_ids.add(item_id)

        if stats.get("ok"):
            metadata[item["label"]] = now_iso

    if is_bootstrap_run and bootstrap_batch:
        delivered_ids = send_chunked_alerts(
            bootstrap_batch,
            header=f"**Initial scan - {len(bootstrap_batch)} matching listing(s) found:**\n\n",
        )
        seen_ids.update(delivered_ids)
        new_alerts += len(delivered_ids)
        print(f"Bootstrap run: sent {len(delivered_ids)}/{len(bootstrap_batch)} listing(s) in chunked messages.")

    if not _quota_exhausted:
        metadata["_bootstrap_complete"] = True

    save_seen(seen_ids)
    save_search_metadata(metadata)
    if quiet_now and not is_bootstrap_run:
        save_pending(pending)

    print(f"\nDone. {new_alerts} alert(s) sent, {queued_alerts} queued for digest. "
          f"API calls used today: {calls_used_today()}/{DAILY_CALL_BUDGET}")
    if _quota_exhausted:
        print(
            "WARNING: this run stopped early because eBay returned 429 (errorId 2001, "
            "request limit reached). That is an application-wide quota, so it will not "
            "clear by retrying sooner - reduce how often this bot runs, or check your "
            "remaining quota with the Developer Analytics getRateLimits API."
        )


if __name__ == "__main__":
    run()
