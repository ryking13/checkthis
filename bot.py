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
#   require_words  - (optional) title must contain ALL of these words
#                     (case-insensitive) in addition to matching the query
#   exclude_words  - (optional) title must NOT contain any of these words
#                     (case-insensitive)
#   label          - friendly name shown in Discord alerts
ITEMS = [
    {
        "label": "AirPort Express A1392",
        "query": "airport express a1392",
        "max_price": 20,
        "exclude_words": ["a1264", "a1084", "a1143", "a1408", "base station"],
    },
    {
        "label": "Codenames Deep Undercover",
        "query": "codenames deep undercover",
        "max_price": 20,
        "exclude_words": ["man", "woman"],
    },
    {
        "label": "TI-84 Plus",
        "query": "ti-84 plus",
        "max_price": 20,
        "require_words": ["plus"],  # must specifically say "Plus", not just any TI-84
    },
    {
        "label": "TI-Nspire CX",
        "query": "ti-nspire cx",
        "max_price": 30,
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
    params = {
        "q": item["query"],
        "limit": "30",
        "filter": f"buyingOptions:{{FIXED_PRICE}},price:[..{item['max_price']}],priceCurrency:USD",
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

    content = (
        f"**${price} - {title}**\n"
        f"Matched: *{item['label']}* (threshold: ${item['max_price']})\n"
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

            if not matches_excluded_words(title, item.get("exclude_words")):
                continue

            send_discord_alert(item, listing)
            seen_ids.add(item_id)
            new_alerts += 1

    save_seen(seen_ids)
    print(f"\nDone. {new_alerts} new alert(s) sent.")


if __name__ == "__main__":
    run()
