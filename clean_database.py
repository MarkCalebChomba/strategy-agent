"""
Clean existing strategy_bot.db by removing junk posts that don't match
the updated filters. Run after adjusting filters in reddit_collector.py.
"""

import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

DB_PATH = "strategy_bot.db"

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

BLOCKED_TITLE_KEYWORDS = {
    "lpt:", "ysk:", "aita", "how many", "what would you do",
    "eli5", "cmv", "tifu", "ama", "update:", "spoilers",
}

MIN_SCORE = 1000
MIN_COMMENTS = 10


def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT id, subreddit, title, score, num_comments FROM raw_items")
    rows = cursor.fetchall()
    
    to_delete = []
    for row in rows:
        post_id, subreddit, title, score, num_comments = row
        
        # Check filters
        if score < MIN_SCORE or num_comments < MIN_COMMENTS:
            to_delete.append(post_id)
            continue
        if subreddit.lower() in {s.lower() for s in BLOCKED_SUBREDDITS}:
            to_delete.append(post_id)
            continue
        title_lower = title.lower()
        if any(keyword in title_lower for keyword in BLOCKED_TITLE_KEYWORDS):
            to_delete.append(post_id)
            continue
    
    if to_delete:
        placeholders = ','.join('?' * len(to_delete))
        conn.execute(f"DELETE FROM raw_items WHERE id IN ({placeholders})", to_delete)
        conn.commit()
        print(f"Removed {len(to_delete)} junk posts")
        print(f"Remaining: {len(rows) - len(to_delete)} posts")
    else:
        print("No junk posts found")
    
    conn.close()


if __name__ == "__main__":
    main()
