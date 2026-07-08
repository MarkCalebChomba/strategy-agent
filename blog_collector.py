"""
Stage 1b: Blog / RSS strategy collector.

Fetches trading strategy articles from blogs, Substacks, and news feeds
with zero API cost and no quotas. Drops into the same raw_items queue
that the AI extraction worker processes.

Usage:
    python blog_collector.py
"""

import os
import sqlite3
import time
import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

DB_PATH = os.getenv("COLLECTOR_DB_PATH", "strategy_bot.db")

# RSS feeds from trading blogs / Substacks / news sources.
# These are free, public, no API key needed.
RSS_FEEDS = [
    # Trading blogs that frequently publish strategy content
    "https://pepperstone.com/feeds/forex-trading-strategies/feed.xml",
    "https://www.dailyfx.com/feeds/forex/",
    "https://www.babypips.com/feed",
    "https://www.forexfactory.com/rss/news.xml",
    # MQL5 articles (rich source of strategy code + descriptions)
    "https://www.mql5.com/en/rss/articles",
    "https://www.mql5.com/en/rss/experts",
    # Substack trading newsletters
    "https://tradingstrategycourse.com/feed.xml",
    # General financial / markets (likely to discuss strategies)
    "https://www.investopedia.com/feed-builder/feed/feed.xml",
    "https://seekingalpha.com/feed.xml",
    "https://www.tradingview.com/feed/",
]

MAX_ARTICLES_PER_FEED = 20
REQUEST_DELAY = 1.5


def init_db(conn: sqlite3.Connection):
    conn.execute("""
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
    """)
    conn.commit()


def fetch_feed(url: str) -> list:
    try:
        r = requests.get(url, headers={"User-Agent": "strategy-bot/1.0"}, timeout=15)
        if r.status_code != 200:
            return []
        return parse_rss(r.text, r.url)
    except Exception as e:
        print(f"    fetch error: {e}")
        return []


def parse_rss(xml_text: str, feed_url: str) -> list:
    """Parse RSS or Atom XML into our normalized format."""
    articles = []
    text = xml_text

    if "<item>" in text:
        items = text.split("<item>")[1:]
    elif "<entry>" in text:
        items = text.split("<entry>")[1:]
    else:
        return []

    for item in items[:MAX_ARTICLES_PER_FEED]:
        try:
            title = extract_tag(item, "title")
            link = extract_tag(item, "link")
            # link might be <link>text</link> or <link href="url"/>
            if link and not link.startswith("http"):
                link = extract_attr(item, "link", "href") or link

            pub_date_str = extract_tag(item, "pubDate") or extract_tag(item, "published") or extract_tag(item, "updated")
            pub_date = None
            if pub_date_str:
                try:
                    pub_date = parsedate_to_datetime(pub_date_str).isoformat()
                except Exception:
                    pub_date = datetime.now(timezone.utc).isoformat()
            else:
                pub_date = datetime.now(timezone.utc).isoformat()

            content = extract_tag(item, "content:encoded") or extract_tag(item, "content") or extract_tag(item, "description")
            author = extract_tag(item, "author") or extract_tag(item, "creator") or ""

            if not title or not link:
                continue

            article_id = hashlib.sha256(link.encode()).hexdigest()[:16]

            articles.append({
                "id": article_id,
                "source": "blog",
                "subreddit": feed_url.split("/")[2] if "//" in feed_url else feed_url[:20],
                "title": title.strip(),
                "body_text": content.strip() if content else "",
                "url": link,
                "author": author.strip(),
                "score": 0,
                "num_comments": 0,
                "created_utc": pub_date,
            })
        except Exception:
            continue

    return articles


def extract_tag(html: str, tag: str) -> str:
    """Extract content between <tag> and </tag> (simple, no parser)."""
    start = html.find(f"<{tag}>")
    if start == -1:
        start = html.find(f"<{tag} ")
    if start == -1:
        return ""
    end = html.find(">", start)
    if end == -1:
        return ""
    close = html.find(f"</{tag}>", end)
    if close == -1:
        return ""
    return html[end + 1 : close]


def extract_attr(html: str, tag: str, attr: str) -> str:
    """Extract attribute value from <tag attr="value">."""
    start = html.find(f"<{tag}")
    if start == -1:
        return ""
    end = html.find(">", start)
    snippet = html[start:end]
    key = f'{attr}="'
    pos = snippet.find(key)
    if pos == -1:
        key = f"{attr}='"
        pos = snippet.find(key)
    if pos == -1:
        return ""
    pos += len(key)
    close = snippet.find('"' if snippet[pos - 1] != "'" else "'", pos)
    if close == -1:
        return ""
    return snippet[pos:close]


def store_article(conn: sqlite3.Connection, article: dict) -> bool:
    cursor = conn.execute("SELECT 1 FROM raw_items WHERE id = ?", (article["id"],))
    if cursor.fetchone():
        return False
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO raw_items (id, source, subreddit, title, body_text, url, author, score, num_comments, created_utc, collected_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')""",
        (article["id"], article["source"], article["subreddit"],
         article["title"], article["body_text"], article["url"],
         article["author"], article["score"], article["num_comments"],
         article["created_utc"], now),
    )
    return True


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    stats = {"feeds": 0, "fetched": 0, "new": 0, "duplicates": 0}
    print(f"Scanning {len(RSS_FEEDS)} RSS feeds...\n")

    for i, feed_url in enumerate(RSS_FEEDS):
        print(f"  [{i+1}/{len(RSS_FEEDS)}] {feed_url[:60]}...", end=" ", flush=True)
        articles = fetch_feed(feed_url)
        stats["feeds"] += 1
        stats["fetched"] += len(articles)

        for article in articles:
            if store_article(conn, article):
                stats["new"] += 1
            else:
                stats["duplicates"] += 1

        print(f"{len(articles)} articles ({stats['new']} new)")
        time.sleep(REQUEST_DELAY)

    conn.commit()
    conn.close()

    print(f"\nDone. {stats['fetched']} articles from {stats['feeds']} feeds, "
          f"{stats['new']} new, {stats['duplicates']} duplicates")


if __name__ == "__main__":
    main()
