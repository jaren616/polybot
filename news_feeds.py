"""Pulls news items from RSS (free) and optionally X (paid)."""
import calendar
import html
import re
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import feedparser
import requests

import config


@dataclass
class NewsItem:
    id: str
    source: str
    text: str
    url: str
    published_ts: float  # unix seconds


def _clean(s: str) -> str:
    """Strip HTML tags like <p> that some feeds put in summaries."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


_failed: dict[str, int] = {}


def _fetch_one(feed_url: str) -> list[NewsItem]:
    """Download one feed with a timeout (so a slow site can't stall the bot), then parse it."""
    try:
        r = requests.get(feed_url, timeout=10, headers={"User-Agent": "Mozilla/5.0 polybot/2"})
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:  # one broken feed shouldn't stop the bot
        _failed[feed_url] = _failed.get(feed_url, 0) + 1
        if _failed[feed_url] in (1, 10, 100):  # don't spam the log every minute
            print(f"[feeds] {feed_url} failed ({_failed[feed_url]}x): {e}")
        return []
    src = feed.feed.get("title", feed_url)
    out = []
    for e in feed.entries:
        parsed = e.get("published_parsed") or e.get("updated_parsed")
        ts = calendar.timegm(parsed) if parsed else time.time()
        title = _clean(e.get("title", ""))
        summary = _clean(e.get("summary", ""))
        text = f"{title}. {summary}" if summary else title
        link = e.get("link", "")
        out.append(NewsItem(_hash(link or title), src, text[:1000], link, ts))
    return out


def fetch_rss() -> list[NewsItem]:
    """All feeds at once, 12 at a time, so 30+ sources still take only a few seconds."""
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = pool.map(_fetch_one, config.RSS_FEEDS)
    seen, items = set(), []
    for batch in results:
        for it in batch:
            if it.id not in seen:  # the same story can appear in several feeds
                seen.add(it.id)
                items.append(it)
    return items


_x_user_ids: dict[str, str] = {}


def _x_get(path: str, params=None) -> dict:
    r = requests.get(
        f"https://api.x.com/2{path}",
        headers={"Authorization": f"Bearer {config.X_BEARER_TOKEN}"},
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_x() -> list[NewsItem]:
    if not config.X_BEARER_TOKEN:
        return []
    items = []
    for name in config.X_ACCOUNTS:
        try:
            if name not in _x_user_ids:
                _x_user_ids[name] = _x_get(f"/users/by/username/{name}")["data"]["id"]
            data = _x_get(
                f"/users/{_x_user_ids[name]}/tweets",
                {
                    "max_results": max(5, config.X_MAX_RESULTS_PER_ACCOUNT),
                    "tweet.fields": "created_at",
                    "exclude": "retweets,replies",
                },
            )
        except Exception as e:
            print(f"[feeds] X @{name} failed: {e}")
            continue
        for t in data.get("data", []):
            ts = calendar.timegm(time.strptime(t["created_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            items.append(NewsItem(
                f"x{t['id']}", f"X @{name}", t["text"],
                f"https://x.com/{name}/status/{t['id']}", ts,
            ))
    return items


def fetch_all() -> list[NewsItem]:
    return fetch_rss() + fetch_x()
