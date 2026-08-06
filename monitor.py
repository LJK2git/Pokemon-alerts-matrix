import asyncio
import calendar
import json
import os
import re
import sys
import time
from collections import deque

import feedparser
import requests

# ── Config ───────────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"

REDDIT_POLL_INTERVAL  = 30    # seconds between polls, per feed
REDDIT_STAGGER        = 5     # stagger feed startups so they don't all hit at once
REDDIT_BACKOFF_START  = 30    # seconds, doubles on repeated failures
REDDIT_BACKOFF_MAX    = 300
REDDIT_SEEN_MAX       = 25000 # how many post IDs to remember long-term, per feed
                               # (bumped up from 5000 -- on a hype drop day a hot
                               # sub can generate more than 5000 new posts before
                               # this rolls over, which was evicting drop-day IDs
                               # from seen_ids and letting Reddit's RSS re-serving
                               # them 2-3 days later look "new" again)

# Any feed entry older than this when we first see it is treated as a stale
# repost, not a real new post, and is never alerted on. This is a second,
# independent layer on top of seen_ids/seen_order -- it doesn't matter *why*
# Reddit resurfaced an old post (modqueue approval, RSS cache weirdness,
# seen_ids eviction, etc.), age alone is enough to filter it.
MAX_POST_AGE_SECONDS = 15 * 60  # 15 minutes

# Reddit will happily 429 requests with a generic/blank User-Agent, even on
# the .rss endpoints. Keep this identifiable.
USER_AGENT = "PokemonRestockMonitor/1.0 (personal restock alert bot)"

# tcin.py lives next to this file
TCIN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tcin.py")
TCIN_LOOKUP_TIMEOUT = 25  # seconds
SEARCH_LOOKUP_TIMEOUT = 25  # seconds, for the manual !search command


# ── Config loading ───────────────────────────────────────────────────────────
async def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def build_keyword_pattern(keywords):
    """Returns a compiled regex, or None if the keyword list is empty."""
    if not keywords:
        return None
    sorted_kw = sorted(keywords, key=len, reverse=True)
    escaped = [re.escape(kw) for kw in sorted_kw]
    pattern = r'(?<![a-z])(' + '|'.join(escaped) + r')(?![a-z])'
    return re.compile(pattern, re.IGNORECASE)


def compile_sites(sites_cfg):
    compiled = []
    for site in sites_cfg:
        compiled.append({
            "name": site.get("name", "unnamed site"),
            "url_patterns": [p.lower() for p in site.get("url_patterns", [])],
            "allow_pattern": build_keyword_pattern(site.get("allow_keywords", [])),
            "block_pattern": build_keyword_pattern(site.get("block_keywords", [])),
            "alert_link": site.get("alert_link", ""),
        })
    return compiled


# ── Post evaluation (same logic as before, now fed by RSS entries) ──────────
def extract_haystack_from_entry(entry):
    """Title plus body/summary plus the permalink, so a post that only
    mentions a site in its selftext or a flair-embedded link still matches."""
    parts = [entry.get("title", "") or ""]
    summary = entry.get("summary", "") or ""
    if summary:
        parts.append(summary)
    link = entry.get("link", "") or ""
    if link:
        parts.append(link)
    return " ".join(parts)


def evaluate_post(text, haystack, compiled_sites):
    """Returns a list of (site_name, matched_keyword, alert_link) for every site
    rule this post satisfies: its URL/site is mentioned, an allow keyword hits,
    and no block keyword hits."""
    haystack = haystack.lower()
    fired = []
    for site in compiled_sites:
        if site["url_patterns"] and not any(pat in haystack for pat in site["url_patterns"]):
            continue
        allow_pattern = site["allow_pattern"]
        if not allow_pattern:
            continue
        allow_match = allow_pattern.search(text)
        if not allow_match:
            continue
        block_pattern = site["block_pattern"]
        if block_pattern and block_pattern.search(text):
            continue
        fired.append((site["name"], allow_match.group(), site["alert_link"]))
    return fired


# ── Post age check (filters stale reposts) ────────────────────────────────────
def entry_age_seconds(entry):
    """Seconds since the entry's published time, or None if we can't tell.
    Returns None (rather than 0) on missing/bad data so we fail OPEN --
    better to occasionally alert on something borderline than silently
    eat a legit new post because parsing failed."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    try:
        return time.time() - calendar.timegm(struct)
    except Exception:
        return None


# ── Product-name extraction (for the Target tcin.py lookup) ─────────────────
TRAILING_FILLER_WORDS = {
    "is", "in", "stock", "back", "now", "at", "for", "the", "a", "an",
    "just", "dropped", "available", "live", "restocked", "add", "to", "cart",
}


def extract_product_name(title, site_name, matched_kw):
    """Best-effort guess at the product-name portion of a reddit restock
    title. Cuts the title at whichever comes first: the site name (e.g.
    "Target") or the matched allow-keyword (e.g. "in stock"), then trims
    leftover filler words/punctuation off the end.

    "Pokemon Pitch Black Booster Bundle is in stock at Target for $31.99"
    -> "Pokemon Pitch Black Booster Bundle"

    This is heuristic and will misfire on unusual title phrasing -- if
    lookups start failing a lot, check the logged product_name against
    the raw title and adjust this function.
    """
    lower = title.lower()
    cut_points = []

    kw_idx = lower.find(matched_kw.lower())
    if kw_idx > 0:
        cut_points.append(kw_idx)

    site_idx = lower.find(site_name.lower())
    if site_idx > 0:
        cut_points.append(site_idx)

    cut = min(cut_points) if cut_points else len(title)
    name = title[:cut]

    name = re.sub(r'[\s\-|:,]+$', '', name)  # trailing punctuation/dashes

    words = name.split()
    while words and words[-1].lower() in TRAILING_FILLER_WORDS:
        words.pop()
    name = " ".join(words).strip()

    # If we trimmed it down to nothing (or almost nothing) useful, fall
    # back to the full title rather than sending an empty/junk query.
    if len(name) < 3:
        name = title.strip()

    return name


# ── tcin.py lookup (Target only) ─────────────────────────────────────────────
async def run_tcin_lookup(product_name, official_only=True, timeout=TCIN_LOOKUP_TIMEOUT):
    """Runs tcin.py --auto --json for product_name and returns the parsed
    dict, or None if the lookup failed, timed out, or found nothing."""
    cmd = [sys.executable, TCIN_SCRIPT, product_name, "--auto", "--json"]
    if official_only:
        cmd.append("--official-only")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"[tcin] lookup for {product_name!r} timed out after {timeout}s")
        try:
            proc.kill()
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[tcin] failed to launch lookup for {product_name!r}: {e}")
        return None

    if proc.returncode != 0:
        err = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        print(f"[tcin] lookup for {product_name!r} exited {proc.returncode}: {err[:300]}")
        return None

    try:
        return json.loads(stdout.decode(errors="replace").strip())
    except json.JSONDecodeError as e:
        print(f"[tcin] couldn't parse output for {product_name!r}: {e} -- raw: {stdout[:300]!r}")
        return None


# ── tcin.py manual search (for the !search Matrix command) ──────────────────
async def run_tcin_search(query, count=5, official_only=False, timeout=SEARCH_LOOKUP_TIMEOUT):
    """Runs tcin.py for `query` WITHOUT --auto, so it returns the top
    `count` candidates (title/tcin/price/stock/match score) instead of
    just guessing the single best one.

    This is for manual use via the Matrix !search command -- when the
    automatic reddit-title-derived query didn't find anything (or found
    the wrong thing) and a person wants to reword the product name by
    hand and try again.

    Returns the raw text tcin.py would print to stdout, or a short
    human-readable error string if the lookup failed/timed out.
    """
    cmd = [sys.executable, TCIN_SCRIPT, query, "--count", str(count)]
    if official_only:
        cmd.append("--official-only")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"[search] lookup for {query!r} timed out after {timeout}s")
        try:
            proc.kill()
        except Exception:
            pass
        return f"Search for {query!r} timed out after {timeout}s. Try a shorter/simpler query."
    except Exception as e:
        print(f"[search] failed to launch lookup for {query!r}: {e}")
        return f"Failed to launch search for {query!r}: {e}"

    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()

    if proc.returncode != 0:
        print(f"[search] lookup for {query!r} exited {proc.returncode}: {(err or out)[:300]}")
        return out or err or f"Search for {query!r} failed (exit {proc.returncode})."

    return out or f"No output for {query!r}."


def format_tcin_result(result):
    price = result.get("price")
    price_str = f"${price}" if price is not None else "price unknown"
    stock_str = "IN STOCK" if result.get("in_stock") else "OUT OF STOCK"
    return (
        f"{result['title']} — {price_str} [{stock_str}]\n"
        f"{result['url']}\n"
        f"Sold by: {result.get('sold_by', '?')}"
    )


# ── Alert firing ──────────────────────────────────────────────────────────────
async def _fire_target_alert(say, matched_kw, author, post_url, title, site_name):
    product_name = extract_product_name(title, site_name, matched_kw)
    print(f"[tcin] looking up {product_name!r} (from title: {title!r})")
    result = await run_tcin_lookup(product_name)

    if result:
        say(format_tcin_result(result))
        say(post_url)
    else:
        # tcin.py failed or found nothing -- fall back to the plain alert
        # so we never silently drop a hit.
        say(f"[{site_name}] Queue started {author} — '{matched_kw}' "
            f"(tcin lookup failed for {product_name!r}, check it manually)")
        say(post_url)


async def _fire_alerts(say, site_hits, author, post_url, title):
    for site_name, matched_kw, alert_link in site_hits:
        if site_name == "Target":
            await _fire_target_alert(say, matched_kw, author, post_url, title, site_name)
        else:
            if alert_link:
                say(alert_link)
            say(f"[{site_name}] Queue started {author} — '{matched_kw}'")
            say(post_url)


# ── Reddit RSS fetching ──────────────────────────────────────────────────────
def _fetch_feed(feed_url):
    """Blocking fetch+parse. Run via asyncio.to_thread so it doesn't stall
    the event loop or the other feed watchers."""
    resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


async def watch_reddit_feed(feed_url, compiled_sites, say, offset, blacklist_pattern=None):
    await asyncio.sleep(offset)
    print(f"[reddit] Watching feed: {feed_url}")

    # `seen_ids` now accumulates across the whole run instead of being
    # replaced with each poll's snapshot. Previously it was overwritten
    # every 30s with just the IDs currently in the feed, so a post that
    # scrolled out of the RSS window (e.g. pushed out by newer posts, or
    # held in modqueue and then approved later) and later reappeared in
    # the feed looked "new" again and re-fired the alert. `seen_order`
    # is a FIFO so memory stays bounded (REDDIT_SEEN_MAX) without wiping
    # everything at once.
    seen_ids = set()
    seen_order = deque()
    initialized = False
    backoff = REDDIT_BACKOFF_START

    while True:
        try:
            parsed = await asyncio.to_thread(_fetch_feed, feed_url)
            if parsed.bozo and not parsed.entries:
                raise parsed.bozo_exception or RuntimeError("empty/invalid feed")

            entries = parsed.entries

            if not initialized:
                # Baseline pass: remember everything currently in the feed,
                # but don't alert on it.
                for e in entries:
                    eid = e.get("id") or e.get("link", "")
                    if eid not in seen_ids:
                        seen_ids.add(eid)
                        seen_order.append(eid)
                print(f"[reddit] Init {feed_url}: {len(entries)} entr{'y' if len(entries)==1 else 'ies'} loaded")
                initialized = True
            else:
                new_entries = [e for e in entries if (e.get("id") or e.get("link", "")) not in seen_ids]
                print(f"[reddit] Checked {feed_url}: {len(new_entries)} new post(s)")
                for e in reversed(new_entries):  # oldest-first, alerts fire in order
                    eid = e.get("id") or e.get("link", "")
                    seen_ids.add(eid)
                    seen_order.append(eid)

                    title = e.get("title", "") or ""
                    if not title:
                        continue

                    # Skip stale reposts: something Reddit is re-serving in
                    # the feed that's actually days old, even though its ID
                    # wasn't in seen_ids (e.g. it fell out of REDDIT_SEEN_MAX,
                    # or Reddit re-surfaced it after a modqueue approval).
                    age = entry_age_seconds(e)
                    if age is not None and age > MAX_POST_AGE_SECONDS:
                        print(f"[reddit] Skipping stale repost ({feed_url}) — "
                              f"{title[:80]!r} (age {age/60:.1f}m)")
                        continue

                    author = e.get("author", "unknown")
                    post_url = e.get("link", "")
                    print(f"[reddit] New post ({feed_url}) — {author}: {title[:80]!r}")
                    haystack = extract_haystack_from_entry(e)
                    hits = evaluate_post(title, haystack, compiled_sites)

                    # Global blacklist: applies across every site, checked only
                    # after a post already matched a site + allow keyword (no
                    # point blacklist-checking a post that wouldn't have alerted
                    # anyway). Unlike per-site block_keywords, this is for
                    # specific sets/products you never want notified about no
                    # matter where they show up (e.g. a set you don't care for).
                    if hits and blacklist_pattern:
                        bad_match = blacklist_pattern.search(title)
                        if bad_match:
                            print(f"[reddit] Blacklisted ({feed_url}) — "
                                  f"'{bad_match.group()}' in title, skipping: {title[:80]!r}")
                            continue

                    await _fire_alerts(say, hits, author, post_url, title)

                # Evict oldest IDs once we're over the cap, so seen_ids
                # doesn't grow forever but still remembers thousands of
                # posts back -- comfortably more than a feed refresh cycle
                # or a modqueue delay.
                while len(seen_order) > REDDIT_SEEN_MAX:
                    old = seen_order.popleft()
                    seen_ids.discard(old)

            backoff = REDDIT_BACKOFF_START
            await asyncio.sleep(REDDIT_POLL_INTERVAL)
        except Exception as e:
            print(f"[reddit] Error checking {feed_url}: {e} — backing off {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, REDDIT_BACKOFF_MAX)


# ── Entry point ──────────────────────────────────────────────────────────────
async def run_monitor(say):
    """
    Main entry point for the monitor.
    Pass any callable as `say` — it receives alert strings as they fire.
    Called by the Matrix bot, but also works standalone (see __main__ below).
    """
    try:
        cfg = await load_config()
    except FileNotFoundError:
        print(f"{CONFIG_FILE} not found. Create it (see the example) and restart.")
        return
    except json.JSONDecodeError as e:
        print(f"{CONFIG_FILE} is not valid JSON: {e}")
        return

    feeds = cfg.get("reddit_feeds", [])
    compiled_sites = compile_sites(cfg.get("sites", []))
    blacklist_pattern = build_keyword_pattern(cfg.get("blacklist_keywords", []))

    if not compiled_sites:
        print(f"No sites configured in {CONFIG_FILE} — nothing would ever match. Exiting.")
        return
    if not feeds:
        print(f"No reddit_feeds configured in {CONFIG_FILE} — nothing to watch. Exiting.")
        return

    print(f"Loaded {len(feeds)} reddit feed(s), {len(compiled_sites)} site rule(s)")
    for s in compiled_sites:
        print(f"  Site '{s['name']}': url_patterns={s['url_patterns']}")
    if blacklist_pattern:
        print(f"  Blacklist keywords: {cfg.get('blacklist_keywords', [])}")

    tasks = [
        asyncio.create_task(
            watch_reddit_feed(feed_url, compiled_sites, say, offset=i * REDDIT_STAGGER,
                               blacklist_pattern=blacklist_pattern)
        )
        for i, feed_url in enumerate(feeds)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            print(f"  [monitor] a watcher task ended with exception: {r}")


# ── Standalone mode: python monitor.py ────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(run_monitor(say=print))
