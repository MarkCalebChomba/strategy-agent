"""
Reddit strategy collector.

Stage 1 of the trading-strategy pipeline: searches Reddit for candidate
strategy posts (forex, crypto, gold/silver) and stores them in a local
SQLite queue (raw_items table) for the AI extraction stage to process
later. No AI calls happen here - just collection.

Backend: RapidAPI's Reddit34 "Search Posts" endpoint, sitewide keyword
search (no fixed subreddit list). Free-tier accounts on this API get a
hard MONTHLY quota (commonly 50 requests) - this script tracks usage
in the same SQLite database and will refuse to run past budget rather
than silently burning your whole month on one run. Each run spends a
configurable slice of the remaining budget, rotating through a fixed
list of search queries so repeated runs cover new ground instead of
repeating the same search.

Usage:
    python reddit_collector.py
"""

import os
import sqlite3
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("COLLECTOR_DB_PATH", "strategy_bot.db")

# Rotated through over time - each run uses a slice of these, persisted
# across runs so we don't keep re-searching the same handful of terms.
# Tailored to forex / crypto / gold-silver per your target markets.
SEARCH_QUERIES = [
    "forex trading strategy",
    "gold trading strategy",
    "silver trading strategy",
    "crypto trading strategy",
    "backtested forex strategy",
    "backtested crypto strategy",
    "scalping strategy forex",
    "swing trading strategy crypto",
    "XAUUSD strategy",
    "moving average crossover strategy",
    "RSI strategy forex",
    "breakout strategy crypto",
    "price action trading strategy",
    "support and resistance strategy",
    "trend following strategy forex",
    "mean reversion strategy crypto",
    "Fibonacci retracement strategy",
    "win rate trading strategy",
    "algorithmic trading strategy forex",
    "Bitcoin trading strategy",
]

MIN_SCORE = 1000  # high bar - search results are already keyword-relevant
MIN_COMMENTS = 10

# Subreddits to exclude - non-trading/junk content
BLOCKED_SUBREDDITS = {
    "funny", "politics", "gameofthrones", "teenagers", "AskMen", "AskWomen",
    "comics", "prorevenge", "iamverysmart", "pics", "videos", "movies",
    "music", "gaming", "books", "television", "netflix", "todayilearned",
    "mildlyinfuriating", "oddlysatisfying", "wholesomememes", "memes",
    "dankmemes", "bonehurtingjuice", "surrealmemes", "antimeme",
    "AntiMLM", "entitledparents", "AmItheAsshole", "relationships",
    "dating_advice", "tifu", "confession", "offmychest", "rant",
    "LifeProTips", "YouShouldKnow", "UnethicalLifeProTips",
    "subredditoftheday", "announcements", "news", "worldnews",
    "nottheonion", "theonion", "OutOfTheLoop", "NoStupidQuestions",
    "explainlikeimfive", "AskScience", "AskHistory", "AskPhilosophy",
    "changemyview", "unpopularopinion", "trueoffmychest",
    "antiwork", "HFY", "pokemon", "pettyrevenge", "pathofexile",
    "nosleep", "cats", "WritingPrompts", "MaliciousCompliance",
    "BeAmazed", "apolloapp", "complaints", "science", "youtube",
}

# Title keywords that indicate non-strategy content
BLOCKED_TITLE_KEYWORDS = {
    "lpt:", "ysk:", "aita", "how many", "what would you do",
    "eli5", "cmv", "tifu", "ama", "update:", "spoilers",
}

# --- RapidAPI (Reddit34 Search Posts) config --------------------------
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = os.getenv("RAPIDAPI_REDDIT_HOST", "reddit34.p.rapidapi.com")
RAPIDAPI_SEARCH_PATH = os.getenv("RAPIDAPI_SEARCH_PATH", "/getSearchPosts")
RAPIDAPI_QUERY_PARAM = os.getenv("RAPIDAPI_QUERY_PARAM", "query")
# Best-effort extras - harmless if this API ignores unknown params,
# helpful if it respects them. Adjust via .env if a response error
# mentions an invalid param name.
RAPIDAPI_SEARCH_SORT = os.getenv("RAPIDAPI_SEARCH_SORT", "top")
RAPIDAPI_SEARCH_TIME = os.getenv("RAPIDAPI_SEARCH_TIME", "all")

# How many of the 50ish monthly requests a single run is allowed to spend.
RAPIDAPI_MAX_REQUESTS_PER_RUN = int(os.getenv("RAPIDAPI_MAX_REQUESTS_PER_RUN", "5"))
# Your plan's monthly cap - leave a little headroom below the real limit.
RAPIDAPI_MONTHLY_QUOTA = int(os.getenv("RAPIDAPI_MONTHLY_QUOTA", "45"))
RAPIDAPI_SLEEP_SECONDS = 1


def has_rapidapi_credentials() -> bool:
    return bool(RAPIDAPI_KEY)


# --- Budget tracking (persisted in the same SQLite db) -----------------

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_items (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            subreddit TEXT,
            title TEXT,
            body_text TEXT,
            url TEXT,
            author TEXT,
            score INTEGER,
            num_comments INTEGER,
            created_utc TEXT,
            collected_at TEXT,
            status TEXT DEFAULT 'new'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_usage (
            month TEXT PRIMARY KEY,
            request_count INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collector_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()
    return conn


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def requests_used_this_month(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT request_count FROM api_usage WHERE month = ?", (_current_month(),)
    ).fetchone()
    return row[0] if row else 0


def record_request_used(conn: sqlite3.Connection):
    month = _current_month()
    conn.execute(
        """
        INSERT INTO api_usage (month, request_count) VALUES (?, 1)
        ON CONFLICT(month) DO UPDATE SET request_count = request_count + 1
        """,
        (month,),
    )
    conn.commit()


def get_next_query_index(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM collector_state WHERE key = 'next_query_index'"
    ).fetchone()
    return int(row[0]) if row else 0


def set_next_query_index(conn: sqlite3.Connection, idx: int):
    conn.execute(
        """
        INSERT INTO collector_state (key, value) VALUES ('next_query_index', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(idx),),
    )
    conn.commit()


# --- RapidAPI search + flexible response parsing ------------------------

def _extract_post_list(payload):
    """Reddit34-style responses wrap posts differently across endpoints
    and have changed shape on us before - try the shapes we've seen."""
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "children" in data:
            return [c.get("data", c) for c in data["children"]]
        if "posts" in data:
            return data["posts"]
    return None


def _normalize_rapidapi_post(post: dict):
    inner = post.get("data", post)
    post_id = inner.get("id") or inner.get("post_id")
    title = inner.get("title")
    if not post_id or not title:
        return None

    permalink = inner.get("permalink", "")
    url = (
        f"https://reddit.com{permalink}"
        if permalink.startswith("/")
        else inner.get("url", "")
    )
    created = inner.get("created_utc") or inner.get("created") or 0
    try:
        created_iso = datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        created_iso = datetime.now(timezone.utc).isoformat()

    return {
        "id": str(post_id),
        "subreddit": inner.get("subreddit", ""),
        "title": title,
        "body_text": inner.get("selftext") or inner.get("body") or "",
        "url": url,
        "author": inner.get("author", ""),
        "score": int(inner.get("score") or inner.get("ups") or 0),
        "num_comments": int(inner.get("num_comments") or inner.get("comment_count") or 0),
        "created_utc": created_iso,
    }


def search_posts(query: str):
    """Spend exactly one API request searching for `query`. Returns
    (list_of_normalized_posts, error_message_or_None)."""
    url = f"https://{RAPIDAPI_HOST}{RAPIDAPI_SEARCH_PATH}"
    params = {
        RAPIDAPI_QUERY_PARAM: query,
        "sort": RAPIDAPI_SEARCH_SORT,
        "time": RAPIDAPI_SEARCH_TIME,
    }
    headers = {
        "content-type": "application/json",
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }

    response = requests.get(url, params=params, headers=headers, timeout=20)
    time.sleep(RAPIDAPI_SLEEP_SECONDS)

    if response.status_code == 429:
        return [], "quota_exceeded"
    if response.status_code != 200:
        return [], f"http_{response.status_code}: {response.text[:300]}"

    try:
        payload = response.json()
    except ValueError:
        return [], f"non_json: {response.text[:300]}"

    candidates = _extract_post_list(payload)
    if candidates is None:
        return [], f"unrecognized_shape: {str(payload)[:500]}"

    posts = [p for p in (_normalize_rapidapi_post(c) for c in candidates) if p]
    return posts, None


def passes_filters(post: dict) -> bool:
    # Check score and comments
    if post["score"] < MIN_SCORE or post["num_comments"] < MIN_COMMENTS:
        return False
    # Check blocked subreddits
    if post["subreddit"].lower() in {s.lower() for s in BLOCKED_SUBREDDITS}:
        return False
    # Check blocked title keywords
    title_lower = post["title"].lower()
    if any(keyword in title_lower for keyword in BLOCKED_TITLE_KEYWORDS):
        return False
    return True


def store_post(conn: sqlite3.Connection, post: dict) -> bool:
    """Returns True if newly inserted, False if duplicate or filtered out."""
    if not passes_filters(post):
        return False
    cursor = conn.execute("SELECT 1 FROM raw_items WHERE id = ?", (post["id"],))
    if cursor.fetchone():
        return False
    conn.execute(
        """
        INSERT INTO raw_items (
            id, source, subreddit, title, body_text, url,
            author, score, num_comments, created_utc, collected_at, status
        ) VALUES (?, 'reddit', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
        """,
        (
            post["id"], post["subreddit"], post["title"], post["body_text"],
            post["url"], post["author"], post["score"], post["num_comments"],
            post["created_utc"], datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True


def collect_via_search(conn: sqlite3.Connection) -> dict:
    stats = {"queries_run": 0, "scanned": 0, "kept": 0, "duplicates": 0}

    used = requests_used_this_month(conn)
    remaining = RAPIDAPI_MONTHLY_QUOTA - used
    if remaining <= 0:
        print(
            f"Monthly budget exhausted ({used}/{RAPIDAPI_MONTHLY_QUOTA} requests "
            f"used) - skipping this run. Resets next calendar month, or raise "
            f"RAPIDAPI_MONTHLY_QUOTA in .env if your plan reset already."
        )
        return stats

    n_this_run = min(RAPIDAPI_MAX_REQUESTS_PER_RUN, remaining, len(SEARCH_QUERIES))
    start_idx = get_next_query_index(conn)

    for i in range(n_this_run):
        query = SEARCH_QUERIES[(start_idx + i) % len(SEARCH_QUERIES)]
        posts, error = search_posts(query)
        record_request_used(conn)  # count it even on error - it still cost a request
        stats["queries_run"] += 1

        if error == "quota_exceeded":
            print(f"  [\"{query}\"] hit quota mid-run - stopping here for this run.")
            set_next_query_index(conn, (start_idx + i) % len(SEARCH_QUERIES))
            conn.commit()
            return stats
        if error:
            print(f"  [\"{query}\"] error: {error}")
            continue

        for post in posts:
            stats["scanned"] += 1
            if store_post(conn, post):
                stats["kept"] += 1
            else:
                stats["duplicates"] += 1

    set_next_query_index(conn, (start_idx + n_this_run) % len(SEARCH_QUERIES))
    conn.commit()
    return stats


def main():
    if not has_rapidapi_credentials():
        print("No RAPIDAPI_KEY set in .env - nothing to do. Set it and re-run.")
        return

    conn = init_db(DB_PATH)
    used_before = requests_used_this_month(conn)
    stats = collect_via_search(conn)
    conn.close()

    print(
        f"Ran {stats['queries_run']} search queries this run "
        f"({used_before + stats['queries_run']}/{RAPIDAPI_MONTHLY_QUOTA} of monthly "
        f"budget used) | scanned {stats['scanned']} posts | "
        f"kept {stats['kept']} new candidates | "
        f"skipped {stats['duplicates']} duplicates/below quality bar"
    )
    print(f"Queue stored at: {DB_PATH}")


if __name__ == "__main__":
    main()