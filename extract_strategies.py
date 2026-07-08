"""
Stage 2: AI extraction worker.

Reads raw posts from queue, sends each to OpenRouter AI,
extracts structured trading strategy specs. Stores results in
strategies table and marks posts as processed.

Rotates through multiple API keys to avoid daily rate limits.
Paces calls with a configurable delay between each request.
Tracks usage per key per day in the database so it knows when
all keys are exhausted and can stop gracefully.

Usage:
    python extract_strategies.py          # process one batch
    python extract_strategies.py --watch  # keep running, polling every N seconds
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

DB_PATH = os.getenv("COLLECTOR_DB_PATH", "strategy_bot.db")

# OpenRouter config - multiple API keys for rate-limit rotation
OPENROUTER_KEYS = [k for k in [
    os.getenv("OPENROUTER_API_KEY"),
    os.getenv("OPENROUTER_API_KEY_2"),
    os.getenv("OPENROUTER_API_KEY_3"),
    os.getenv("OPENROUTER_API_KEY_4"),
    os.getenv("OPENROUTER_API_KEY_5"),
] if k]
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Rate-limit management
DAILY_QUOTA_PER_KEY = 10       # conservative: free tier daily cap per key
API_SLEEP = 5                  # seconds between call attempts
MAX_POSTS_PER_RUN = int(os.getenv("EXTRACT_MAX_PER_RUN", "10"))
POLL_INTERVAL = int(os.getenv("EXTRACT_POLL_INTERVAL", "60"))

STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "has_strategy": {
            "type": "boolean",
            "description": "Whether this post describes a specific, implementable trading strategy"
        },
        "strategy_name": {
            "type": "string",
            "description": "Short descriptive name, e.g. 'EMA Crossover on 4H EURUSD'"
        },
        "asset_class": {
            "type": "string",
            "enum": ["forex", "crypto", "metals", "stocks", "options", "futures", "any", "unknown"],
            "description": "The market the strategy targets"
        },
        "entry_rule": {
            "type": "string",
            "description": "Exactly when to enter a trade"
        },
        "exit_rule": {
            "type": "string",
            "description": "Exactly when to exit a trade"
        },
        "indicators": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Technical indicators used, e.g. ['RSI(14)', 'EMA(20,50)']"
        },
        "timeframe": {
            "type": "string",
            "description": "e.g. 1m, 5m, 15m, 30m, 1h, 4h, daily, weekly, or unspecified"
        },
        "stop_loss": {
            "type": "string",
            "description": "Stop loss rule, or empty string if none specified"
        },
        "take_profit": {
            "type": "string",
            "description": "Take profit rule, or empty string if none specified"
        },
        "market_conditions": {
            "type": "string",
            "description": "Ideal market conditions, e.g. trending, ranging, high volatility, or empty"
        },
        "summary": {
            "type": "string",
            "description": "One-paragraph plain-English summary of the strategy"
        }
    },
    "required": ["has_strategy", "strategy_name", "asset_class", "entry_rule", "exit_rule", "summary"]
}

SYSTEM_PROMPT = f"""You are a trading strategy extraction specialist. Given a Reddit post, extract any trading strategy described in it into structured JSON.

Strategy extraction schema:
{json.dumps(STRATEGY_SCHEMA, indent=2)}

Rules:
- Only set has_strategy=true if the post actually describes a specific, implementable trading strategy with clear entry/exit logic.
- If the post is general market discussion, news, memes, or vague advice, set has_strategy=false.
- Be precise with entry/exit rules - capture exact conditions.
- If a field isn't mentioned, use empty string or empty array.
- asset_class must be one of: forex, crypto, metals, stocks, options, futures, any, unknown.
- Respond with valid JSON only, no markdown."""


def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL UNIQUE REFERENCES raw_items(id),
            strategy_name TEXT, asset_class TEXT, entry_rule TEXT, exit_rule TEXT,
            indicators TEXT, timeframe TEXT, stop_loss TEXT, take_profit TEXT,
            market_conditions TEXT, summary TEXT, source_url TEXT, raw_spec TEXT,
            extracted_at TEXT, status TEXT DEFAULT 'extracted'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extraction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL REFERENCES raw_items(id),
            status TEXT NOT NULL, model TEXT, tokens_used INTEGER,
            error TEXT, processed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_keys (
            date TEXT NOT NULL,
            key_index INTEGER NOT NULL,
            call_count INTEGER DEFAULT 0,
            PRIMARY KEY (date, key_index)
        )
    """)
    conn.commit()


def key_calls_today(conn: sqlite3.Connection) -> dict:
    """Return {key_index: count} for today across all keys."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT key_index, call_count FROM api_usage_keys WHERE date = ?", (today,)
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def record_key_usage(conn: sqlite3.Connection, key_index: int):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn.execute("""
        INSERT INTO api_usage_keys (date, key_index, call_count) VALUES (?, ?, 1)
        ON CONFLICT(date, key_index) DO UPDATE SET call_count = call_count + 1
    """, (today, key_index))
    conn.commit()


def get_next_post(conn: sqlite3.Connection):
    cursor = conn.execute(
        "SELECT id, subreddit, title, body_text, url FROM raw_items WHERE status = 'new' LIMIT 1"
    )
    return cursor.fetchone()


def mark_post(conn: sqlite3.Connection, post_id: str, status: str):
    conn.execute("UPDATE raw_items SET status = ? WHERE id = ?", (status, post_id))
    conn.commit()


def extract_strategy(conn: sqlite3.Connection, title: str, body: str, url: str) -> dict:
    user_prompt = f"""Extract any trading strategy from this Reddit post.

Title: {title}

Body:
{body[:4000]}

URL: {url}

Return valid JSON only."""

    if not OPENROUTER_KEYS:
        return {"error": "No OpenRouter API keys configured"}

    # Find available keys sorted by least-used first (round-robin)
    usage = key_calls_today(conn)
    available = sorted(
        [(i, k) for i, k in enumerate(OPENROUTER_KEYS) if usage.get(i, 0) < DAILY_QUOTA_PER_KEY],
        key=lambda x: usage.get(x[0], 0),
    )

    if not available:
        return {"error": "ALL_KEYS_EXHAUSTED"}

    total_quota = len(OPENROUTER_KEYS) * DAILY_QUOTA_PER_KEY
    used = sum(usage.values())
    print(f"[{used}/{total_quota} quota used]", end=" ", flush=True)

    last_error = None
    for key_idx, api_key in available:
        try:
            response = requests.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENROUTER_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 1000,
                },
                timeout=45,
            )
        except requests.exceptions.Timeout:
            print(f"(key{key_idx+1} timeout)", end=" ", flush=True)
            continue
        except requests.exceptions.ConnectionError as e:
            print(f"(key{key_idx+1} conn err)", end=" ", flush=True)
            continue

        if response.status_code == 429:
            last_error = f"429: {response.text[:100]}"
            record_key_usage(conn, key_idx)
            continue
        if response.status_code != 200:
            last_error = f"API returned {response.status_code}: {response.text[:200]}"
            record_key_usage(conn, key_idx)
            continue

        # Count this call attempt (only valid/non-timeout calls)
        record_key_usage(conn, key_idx)

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        usage_data = data.get("usage", {})
        tokens = {
            "prompt": usage_data.get("prompt_tokens", 0),
            "completion": usage_data.get("completion_tokens", 0),
            "total": usage_data.get("total_tokens", 0),
        }

        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            if content.startswith("json"):
                content = content[4:].strip()

        try:
            spec = json.loads(content)
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON: {content[:500]}", "tokens": tokens}

        spec["tokens"] = tokens
        return spec

    return {"error": last_error or "All available keys exhausted"}


def process_post(conn: sqlite3.Connection, post) -> dict:
    post_id, subreddit, title, body, url = post
    print(f"  {title[:55]:55s}...", end=" ", flush=True)

    result = extract_strategy(conn, title, body or "", url)

    if "error" in result:
        if result["error"] == "ALL_KEYS_EXHAUSTED":
            print("DAILY QUOTA EXHAUSTED - all keys used up")
            return {"status": "quota_exhausted"}
        print(f"ERROR: {result['error'][:80]}")
        mark_post(conn, post_id, "error")
        conn.execute(
            "INSERT INTO extraction_log (post_id, status, model, error, processed_at) VALUES (?, ?, ?, ?, ?)",
            (post_id, "error", OPENROUTER_MODEL, result["error"], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return {"status": "error", "error": result["error"]}

    tokens = result.pop("tokens", {})

    if not result.get("has_strategy"):
        print("no strategy")
        mark_post(conn, post_id, "no_strategy")
        conn.execute(
            "INSERT INTO extraction_log (post_id, status, model, tokens_used, processed_at) VALUES (?, ?, ?, ?, ?)",
            (post_id, "no_strategy", OPENROUTER_MODEL, tokens.get("total", 0),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return {"status": "no_strategy"}

    conn.execute(
        """INSERT INTO strategies (
            post_id, strategy_name, asset_class, entry_rule, exit_rule,
            indicators, timeframe, stop_loss, take_profit, market_conditions,
            summary, source_url, raw_spec, extracted_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'extracted')""",
        (
            post_id,
            result.get("strategy_name", ""),
            result.get("asset_class", "unknown"),
            result.get("entry_rule", ""),
            result.get("exit_rule", ""),
            json.dumps(result.get("indicators", [])),
            result.get("timeframe", ""),
            result.get("stop_loss", ""),
            result.get("take_profit", ""),
            result.get("market_conditions", ""),
            result.get("summary", ""),
            url,
            json.dumps(result),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    mark_post(conn, post_id, "extracted")
    conn.execute(
        "INSERT INTO extraction_log (post_id, status, model, tokens_used, processed_at) VALUES (?, ?, ?, ?, ?)",
        (post_id, "extracted", OPENROUTER_MODEL, tokens.get("total", 0),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    print(f"-> {result.get('strategy_name', 'unnamed')} ({result.get('asset_class', '?')})")
    return {"status": "extracted", "strategy_name": result.get("strategy_name")}


def main():
    if not OPENROUTER_KEYS:
        print("No OpenRouter API keys set in .env (OPENROUTER_API_KEY)")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Extract trading strategies from posts using AI")
    parser.add_argument("--watch", action="store_true", help="Keep running, polling for new posts")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    cursor = conn.execute("SELECT COUNT(*) FROM raw_items WHERE status = 'new'")
    pending = cursor.fetchone()[0]
    print(f"Queue: {pending} unprocessed  |  Keys: {len(OPENROUTER_KEYS)} x {DAILY_QUOTA_PER_KEY}/day = {len(OPENROUTER_KEYS) * DAILY_QUOTA_PER_KEY} daily capacity")

    if pending == 0 and not args.watch:
        print("Nothing to do. Run a collector first or use --watch.")
        conn.close()
        return

    def do_batch():
        processed = 0
        while processed < MAX_POSTS_PER_RUN:
            post = get_next_post(conn)
            if not post:
                break
            result = process_post(conn, post)
            processed += 1
            if result.get("status") == "quota_exhausted":
                return -1  # signal all keys exhausted
            if processed < MAX_POSTS_PER_RUN:
                time.sleep(API_SLEEP)
        return processed

    if args.watch:
        while True:
            result = do_batch()
            if result == -1:
                print(f"\nDaily quota exhausted. Sleeping {POLL_INTERVAL}s...")
            elif result == 0:
                print(".", end="", flush=True)
            time.sleep(POLL_INTERVAL)
    else:
        do_batch()

    conn.close()


if __name__ == "__main__":
    main()
