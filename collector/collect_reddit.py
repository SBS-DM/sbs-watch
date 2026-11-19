"""Collect Reddit threads that mention our brand and competitors.

Reddit closed its free feeds and gates its own API behind approval and a
commercial contract, so this pilot finds threads through the Brave Search API
(site:reddit.com queries from the "reddit" section of collector/brands.json).
A thread counts for a brand if its title or search snippet passes the same
brand, banking-context and exclude rules as YouTube. Results are merged into
docs/data/reddit.json, which the dashboard reads. Standard library only.

Needs a Brave Search API key in the BRAVE_API_KEY environment variable (a
GitHub Actions secret, or a line BRAVE_API_KEY=... in a local .env file).
Without one it exits quietly, so the rest of the daily workflow still runs.

Stored per thread: title, subreddit, date, link. No usernames, no post text.
Run with --backfill once to search the past year instead of the past month.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collect_news import geography, geography_rules
from collect_youtube import relevance_rules, video_language

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "collector" / "brands.json"
OUTPUT = ROOT / "docs" / "data" / "reddit.json"
API = "https://api.search.brave.com/res/v1/web/search"
THREAD = re.compile(r"reddit\.com/r/([^/]+)/comments/([a-z0-9]+)", re.IGNORECASE)


def api_key():
    key = os.environ.get("BRAVE_API_KEY", "").strip()
    env_file = ROOT / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("BRAVE_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"')
    return key


def search(query, key, freshness):
    url = API + "?" + urllib.parse.urlencode({"q": query, "count": 20, "freshness": freshness, "safesearch": "off"})
    request = urllib.request.Request(url, headers={"Accept": "application/json", "X-Subscription-Token": key})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get("web", {}).get("results", [])
    except urllib.error.HTTPError as error:
        # Never echo request headers: they contain the key
        raise RuntimeError(f"Brave Search returned {error.code}: {error.read()[:200].decode('utf-8', 'replace')}") from None


def clean_title(title):
    """Strip HTML and Brave's 'r/sub on Reddit:' / ': r/sub' decorations from a thread title."""
    title = re.sub(r"<[^>]+>", "", title)
    title = re.sub(r"^r/\w+ on Reddit:\s*", "", title)
    return re.sub(r"\s*:\s*r/\w+\s*$", "", title).strip()


def published(result, fallback):
    """Best-effort post date: Brave's page_age when present, else when we first saw it."""
    for field in ("page_age", "age"):
        value = result.get(field, "")
        if re.match(r"\d{4}-\d{2}-\d{2}", value):
            return value[:10] + "T00:00:00Z"
    return fallback


def main():
    key = api_key()
    if not key:
        print("No BRAVE_API_KEY set; skipping Reddit collection")
        return

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    settings = config["reddit"]
    rules = relevance_rules(config)
    geo_rules = geography_rules()
    freshness = "py" if "--backfill" in sys.argv else settings["freshness"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    stored = {}
    if OUTPUT.exists():
        stored = {t["id"]: t for t in json.loads(OUTPUT.read_text(encoding="utf-8"))["articles"]}

    found, requests, dropped = {}, 0, 0
    for brand, queries in settings["queries"].items():
        for query in queries:
            results = search(query, key, freshness)
            requests += 1
            time.sleep(1.1)  # free plan allows about one request per second
            for result in results:
                thread = THREAD.search(result.get("url", ""))
                if not thread:  # user profiles, wiki pages, subreddit front pages
                    continue
                subreddit, post_id = thread.group(1), thread.group(2).lower()
                title = clean_title(result.get("title", ""))
                text = title + "\n" + re.sub(r"<[^>]+>", "", result.get("description", ""))
                if not rules[brand](text):
                    dropped += 1
                    continue
                entry = found.setdefault(post_id, {
                    "id": post_id,
                    "title": title,
                    "source": f"r/{subreddit}",
                    "published": stored.get(post_id, {}).get("published") or published(result, now),
                    "lang": video_language({"title": text}, "en"),
                    "url": f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/",
                    "brands": [],
                    "first_seen": stored.get(post_id, {}).get("first_seen", now),
                })
                if brand not in entry["brands"]:
                    entry["brands"].append(brand)

    threads = {**stored, **found}
    languages = {e["lang"] for e in config["editions"]}
    for thread in threads.values():
        thread["title"] = clean_title(thread["title"])
        thread["countries"], thread["regions"] = geography(thread["title"], geo_rules)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings["retention_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    kept = sorted((t for t in threads.values() if t["brands"] and t["lang"] in languages and t["published"] >= cutoff),
                  key=lambda t: t["published"], reverse=True)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({
        "updated": now,
        "brands": [b["name"] for b in config["brands"]],
        "articles": kept,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    for name in settings["queries"]:
        print(f"{name}: {sum(name in t['brands'] for t in kept)} threads")
    print(f"Saved {len(kept)} threads ({dropped} off-topic results dropped, {requests} searches)")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        sys.exit(str(error))
