#!/usr/bin/env python3
"""
find_tcin.py

Type a product name, get back the tcin.

Why not just use Target's own search? Per test_target_search.py: Target's
in-site search (both the old recommendations feed and plp_search_v2) will
silently backfill with unrelated "you might also like" products instead of
admitting it found nothing, when the literal keyword doesn't match well.
There's no reliable way to tell "real match" from "fallback junk" from
inside that response alone.

This tool sidesteps that by using a site-restricted web search
(site:target.com/p) via DuckDuckGo's no-API-key HTML endpoint. DDG indexes
the actual page title/content, so it doesn't have Target in-site search's
ad/category-fallback behavior -- if it can't find your product, it just
returns nothing, rather than guessing.

Every candidate tcin found this way is then hydrated against RedSky's
fulfillment endpoint (the part of the original script that was already
confirmed reliable) so you see real title/price/stock/official-status
before trusting a match, and results are ranked by title similarity to
your query.

Usage:
    python find_tcin.py "prismatic evolutions spc"
    python find_tcin.py "prismatic evolutions spc" --auto      # print tcin/price/link/stock for the best match
    python find_tcin.py "prismatic evolutions spc" --auto --json   # same, as one JSON object (for bots/scripts)
    python find_tcin.py "prismatic evolutions spc" --count 8   # check more candidates

Known limitation: this scrapes a search engine results page rather than
using an official API, so if DuckDuckGo changes their HTML markup this
will need a regex tweak (look at RESULT_RE below). If it starts returning
zero results across many different queries, that's the likely cause --
run once with a query you know has results and inspect resp.text.
"""

import re
import sys
import html
import json
import uuid
import difflib
import argparse
import requests
from urllib.parse import urlparse, parse_qs, unquote

REDSKY_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"
FULFILLMENT_URL = "https://redsky.target.com/redsky_aggregations/v1/web/product_summary_with_fulfillment_v1"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"

TARGET_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Origin": "https://www.target.com",
    "Referer": "https://www.target.com/",
}

DDG_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://duckduckgo.com/",
}

DEFAULT_PRICING_STORE_ID = "2006"

TCIN_RE = re.compile(r"/A-(\d+)(?:[/?]|$)")
RESULT_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def new_visitor_id() -> str:
    return uuid.uuid4().hex.upper()


def strip_tags(s: str) -> str:
    return html.unescape(TAG_RE.sub("", s)).strip()


def resolve_ddg_href(href: str) -> str:
    """DDG's HTML results wrap outbound links in a redirect; unwrap it to
    get the real target.com URL."""
    if "duckduckgo.com/l/" in href:
        parsed = urlparse(href if href.startswith("http") else "https:" + href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def search_target_product_pages(query: str, count: int) -> list[dict]:
    """Site-restricted DDG search for target.com product pages.
    Returns [{tcin, title, url}, ...] in DDG's ranked order, deduped by tcin."""
    params = {"q": f"site:target.com/p {query}"}
    resp = requests.get(DDG_HTML_URL, params=params, headers=DDG_HEADERS, timeout=15)
    resp.raise_for_status()

    results = []
    seen_tcins = set()
    for href, raw_title in RESULT_RE.findall(resp.text):
        url = resolve_ddg_href(href)
        if "target.com/p/" not in url:
            continue
        m = TCIN_RE.search(url)
        if not m:
            continue
        tcin = m.group(1)
        if tcin in seen_tcins:
            continue
        seen_tcins.add(tcin)
        results.append({"tcin": tcin, "title": strip_tags(raw_title), "url": url})
        if len(results) >= count:
            break
    return results


def fetch_fulfillment(tcins: list[str], store_id: str) -> dict:
    params = {
        "key": REDSKY_KEY,
        "channel": "WEB",
        "tcins": ",".join(tcins),
        "store_id": store_id,
        "required_store_id": store_id,
        "visitor_id": new_visitor_id(),
        "paid_membership": "false",
        "base_membership": "false",
        "card_membership": "false",
    }
    resp = requests.get(FULFILLMENT_URL, params=params, headers=TARGET_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def find_by_tcin(node, found=None, seen=None) -> list:
    if found is None:
        found = []
    if seen is None:
        seen = set()
    if isinstance(node, dict):
        if "tcin" in node and node.get("tcin") is not None:
            t = str(node["tcin"])
            if t not in seen:
                seen.add(t)
                found.append(node)
        for v in node.values():
            find_by_tcin(v, found, seen)
    elif isinstance(node, list):
        for item in node:
            find_by_tcin(item, found, seen)
    return found


def dig(d, *path, default=None):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def hydrate(tcin_candidates: list[dict], store_id: str, query: str) -> list[dict]:
    tcins = [c["tcin"] for c in tcin_candidates]
    if not tcins:
        return []
    data = fetch_fulfillment(tcins, store_id)
    products = {str(p.get("tcin") or dig(p, "item", "tcin")): p for p in find_by_tcin(data)}

    out = []
    for rank, cand in enumerate(tcin_candidates):
        p = products.get(cand["tcin"])
        if not p:
            continue
        item = p.get("item", {}) or {}
        price = p.get("price", {}) or {}
        title = dig(item, "product_description", "title") or item.get("title") or cand["title"]
        is_marketplace = dig(item, "fulfillment", "is_marketplace")
        sold_by = ("MARKETPLACE (third-party seller)" if is_marketplace is True
                   else "OFFICIAL (sold by Target)")
        out.append({
            "rank": rank + 1,
            "tcin": cand["tcin"],
            "title": title,
            "url": dig(item, "enrichment", "buy_url") or cand["url"],
            "price": price.get("current_retail") or price.get("formatted_current_price"),
            "sold_by": sold_by,
            "shipping_status": dig(p, "fulfillment", "shipping_options", "availability_status"),
            "sold_out": dig(p, "fulfillment", "sold_out"),
            "match_score": round(difflib.SequenceMatcher(None, query.lower(), title.lower()).ratio(), 3),
        })
    return out


def main():
    parser = argparse.ArgumentParser(description="Type a Target product name, get its tcin.")
    parser.add_argument("query", help='Product name, e.g. "prismatic evolutions spc"')
    parser.add_argument("--count", type=int, default=8,
                         help="How many candidates to check before ranking/filtering (default 8; "
                              "the final printed list is always capped at top 3)")
    parser.add_argument("--store-id", default=DEFAULT_PRICING_STORE_ID)
    parser.add_argument("--auto", action="store_true",
                         help="Print tcin/price/link/stock for the best match only, nothing else")
    parser.add_argument("--json", action="store_true",
                         help="With --auto, print a single JSON object instead of plain-text lines "
                              "(easier to parse from a bot)")
    parser.add_argument("--official-only", action="store_true",
                         help="Drop marketplace/third-party-seller results, keep only items Target sells itself")
    args = parser.parse_args()

    try:
        candidates = search_target_product_pages(args.query, args.count)
    except requests.exceptions.HTTPError as e:
        print(f"Search request failed: {e}")
        sys.exit(1)

    if not candidates:
        print("No target.com product pages found for that query. Try trimming it down "
              "(drop words like 'box'/'collection', or brand prefixes) and re-run.")
        sys.exit(1)

    try:
        results = hydrate(candidates, args.store_id, args.query)
    except requests.exceptions.HTTPError as e:
        print(f"Fulfillment lookup failed: {e}")
        print("Raw candidates found (unverified):")
        for c in candidates:
            print(f"  {c['tcin']}: {c['title']} ({c['url']})")
        sys.exit(1)

    if not results:
        print("Found product page(s) but couldn't hydrate fulfillment data. Raw candidates:")
        for c in candidates:
            print(f"  {c['tcin']}: {c['title']} ({c['url']})")
        sys.exit(1)

    results.sort(key=lambda r: r["match_score"], reverse=True)

    if args.official_only:
        results = [r for r in results if r["sold_by"].startswith("OFFICIAL")]
        if not results:
            print("No OFFICIAL (Target-sold) matches found -- every candidate for this "
                  "query was a marketplace/third-party listing. Re-run without "
                  "--official-only to see them.")
            sys.exit(1)

    if args.auto:
        best = results[0]
        in_stock = best["shipping_status"] == "IN_STOCK"
        if args.json:
            print(json.dumps({
                "tcin": best["tcin"],
                "title": best["title"],
                "price": best["price"],
                "url": best["url"],
                "in_stock": in_stock,
                "shipping_status": best["shipping_status"],
                "sold_by": best["sold_by"],
                "match_score": best["match_score"],
            }))
        else:
            print(f"TCIN:  {best['tcin']}")
            print(f"Price: {best['price']}")
            print(f"Link:  {best['url']}")
            print(f"Stock: {'IN STOCK' if in_stock else 'OUT OF STOCK'} ({best['shipping_status']})")
        return

    results = results[:3]

    print(f"Candidates for {args.query!r}:\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. {r['title']}")
        print(f"   TCIN:      {r['tcin']}")
        print(f"   URL:       {r['url']}")
        print(f"   Price:     {r['price']}")
        print(f"   Sold by:   {r['sold_by']}")
        print(f"   Shipping:  {r['shipping_status']}  (sold_out={r['sold_out']})")
        print(f"   Match:     {r['match_score']}  (search rank #{r['rank']})")
        print()


if __name__ == "__main__":
    main()
