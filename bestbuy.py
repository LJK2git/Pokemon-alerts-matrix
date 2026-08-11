#!/usr/bin/env python3
"""
Best Buy invite-button checker.

This module owns ONLY the "does this Best Buy product page currently show
a clickable invite/raffle request button" check. It has no scheduling,
no Matrix, no config-loading opinions beyond accepting a URL string.

Called from monitor.py's daily scheduler (see run_daily_bestbuy_check /
BESTBUY_CHECK_HOUR_ET in monitor.py), and still runnable standalone:

    python3 bestbuy.py [url] [--headed]

Debug artifacts: a screenshot is saved to debug_runs/ on every run (same
as before). HTML source is NOT saved anymore -- we still read the page
source in-memory to look for the driverSku buttonState field, we just
don't write it to disk.
"""
import re
import signal
import sys
from datetime import datetime
from pathlib import Path
from seleniumbase import SB

DEFAULT_URL = (
    "https://www.bestbuy.com/site/pokemon-trading-card-game-scarlet-violet-"
    "prismatic-evolutions-super-premium-collection/6621081.p?skuId=6621081"
)

INVITE_PHRASES = [
    "request an invitation",
    "request invitation",
    "request an invite",
    "request invite",
    "send invite",
    "invitation",
    "invite",
]

# If any of these show up instead, you didn't land on the real product page --
# you hit a bot-check / interruption page, and the result below is meaningless.
# NOTE: this list only catches interstitials whose *wording* you already know.
# It will always be incomplete -- see _page_looks_suspicious() below for a
# wording-independent backstop.
BLOCK_PHRASES = [
    "pardon our interruption",
    "are you a human",
    "verify you are a human",
    "access denied",
    "reference #",
    "captcha",
    "unusual traffic",
    "checking your browser",
]

# A real product page on this site has consistently rendered ~12,000 chars
# of visible body text with a title ending in "- Best Buy". An interstitial/
# challenge page we hit rendered only 287 chars with a title that was just
# the bare domain. This threshold is deliberately conservative (well below
# a real PDP, comfortably above a typical challenge page) so it flags
# "something's off" even when the interstitial's exact wording is new.
MIN_EXPECTED_VISIBLE_CHARS = 1500

# Elements a shopper could actually click. A phrase only "counts" if it shows
# up as the visible label of one of these -- not just anywhere in the DOM.
CLICKABLE_SELECTOR = "a, button, input[type='submit'], input[type='button'], [role='button']"

# Best Buy's own structured signal for the primary ("driver") SKU on a PDP,
# pulled straight out of the embedded GraphQL/JSON payload, e.g.:
#   {"buttonState":"ADD_TO_CART","driverSku":true,"rank":1,"skuId":"12780709", ...
# This is the literal field Best Buy's frontend uses to decide which button
# to render -- far more reliable than scraping visible button text. Handles
# both the normal unescaped form and the escaped form that shows up when the
# same payload is re-serialized elsewhere on the page (e.g. a __NEXT_DATA__
# blob), so the same skuId may match twice; we dedupe those.
DRIVER_SKU_BUTTON_STATE_PATTERN = re.compile(
    r'\\?"buttonState\\?"\s*:\s*\\?"([A-Z_]+)\\?"\s*,\s*'
    r'\\?"driverSku\\?"\s*:\s*true\s*,\s*'
    r'\\?"rank\\?"\s*:\s*1\s*,\s*'
    r'\\?"skuId\\?"\s*:\s*\\?"(\d+)\\?"'
)

# States we've actually observed on normal (non-gated) product pages.
# Anything outside this set is unrecognized and worth flagging -- it may be
# the invite-gated state, we just haven't captured a sample of it yet. If
# you ever see a genuine hit below, add the exact string here so future
# runs treat it as known-normal or add it to a dedicated "gated" set.
KNOWN_NORMAL_BUTTON_STATES = {
    "ADD_TO_CART",
    "SEE_DETAILS",
    "PRE_ORDER",
    "NOT_AVAILABLE",
    "SOLD_OUT",
    "COMING_SOON",
}

DEBUG_DIR = Path("debug_runs")
DEBUG_DIR.mkdir(exist_ok=True)


def _handle_sigterm(signum, frame):
    raise SystemExit(f"Terminated by signal {signum}")


def _save_debug_artifacts(sb, tag: str):
    """Save a screenshot every run, so you can inspect exactly what headless
    saw even though there's no visible window. HTML is intentionally NOT
    written to disk -- we only need it in-memory for the buttonState scan."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot_path = DEBUG_DIR / f"{tag}_{ts}.png"
    try:
        sb.save_screenshot(str(shot_path))
        print(f"[i] Debug artifact: {shot_path.name}")
    except Exception as e:
        print(f"[!] Could not save screenshot: {e}")


def _get_visible_text(sb) -> str:
    """Rendered, visible body text only -- excludes <script>/<style> content,
    hidden elements, and attributes like href/aria-label/JSON-LD blobs that
    a real shopper never sees."""
    try:
        return sb.get_text("body").lower()
    except Exception as e:
        print(f"[!] Could not read visible text: {e}")
        return ""


def _page_looks_blocked(visible_text: str) -> bool:
    return any(p in visible_text for p in BLOCK_PHRASES)


def _page_looks_suspicious(title: str, visible_text: str) -> bool:
    """Wording-independent backstop for 'this probably isn't the real PDP'.
    BLOCK_PHRASES only catches interstitials whose exact text you've already
    seen; this catches the shape of the problem instead -- a real product
    page reliably has a long body and a title ending in '- Best Buy'. Don't
    hardcode new interstitial wording every time Best Buy changes it -- this
    check doesn't need to know the wording at all."""
    too_short = len(visible_text) < MIN_EXPECTED_VISIBLE_CHARS
    title_looks_wrong = bool(title) and "- best buy" not in title.lower()
    return too_short or title_looks_wrong


def _find_clickable_invite_element(sb):
    """Return (phrase, label) for the first visible, clickable element whose
    label matches an invite phrase, or None. This is the only thing that
    should count as a real "FOUND" -- a word appearing somewhere in the page
    (e.g. inside a help-center href) does not."""
    try:
        elements = sb.find_elements(CLICKABLE_SELECTOR)
    except Exception as e:
        print(f"[!] Could not query clickable elements: {e}")
        return None

    for el in elements:
        try:
            if not el.is_displayed():
                continue
            label = (el.text or "").strip()
            if not label:
                # Buttons sometimes carry their visible label in `value`
                # (e.g. <input type="submit" value="Request Invite">) rather
                # than inner text -- that's still visible to the shopper,
                # unlike href/aria-label/JSON-LD.
                label = (el.get_attribute("value") or "").strip()
            if not label:
                continue
            label_lower = label.lower()
            for phrase in INVITE_PHRASES:
                if phrase in label_lower:
                    return phrase, label
        except Exception:
            continue
    return None


def _find_driver_sku_button_states(page_source: str):
    """Pull Best Buy's own structured buttonState field for the primary
    (driverSku:true) listing straight out of the embedded JSON payload.
    Returns {skuId: buttonState} for whatever it finds (usually one entry).
    Returns {} if the pattern isn't present -- Best Buy could restructure
    this payload at any time, so this is a bonus signal, not a dependency."""
    matches = DRIVER_SKU_BUTTON_STATE_PATTERN.findall(page_source)
    states = {}
    for state, sku in matches:
        states[sku] = state  # dedupe escaped/unescaped duplicates of the same JSON
    return states


def check_for_invite(url: str, headless: bool = True, debug_tag: str = "run") -> bool:
    """Returns True if a real, clickable invite/raffle-request element (or
    an unrecognized driverSku buttonState) was found on `url`. False
    otherwise -- including when the page looked blocked/suspicious."""
    with SB(uc=True, headless=headless) as sb:
        sb.uc_open_with_reconnect(url, reconnect_time=4)
        # Give any JS challenge (Cloudflare/PerimeterX-style) a moment to
        # resolve, then wait for the DOM to actually finish loading.
        sb.sleep(2)
        try:
            sb.wait_for_ready_state_complete(timeout=15)
        except Exception:
            print("[!] Page never reached 'complete' ready state -- may be stuck on a challenge.")
        try:
            sb.wait_for_element("body", timeout=10)
            sb.sleep(1.5)  # let late-rendering JS (price, buttons) finish
        except Exception:
            pass

        title = sb.get_title()
        visible_text = _get_visible_text(sb)
        try:
            page_source = sb.get_page_source()
        except Exception as e:
            print(f"[!] Could not read page source: {e}")
            page_source = ""

        print(f"[i] Page title: {title!r}")
        print(f"[i] Visible text length: {len(visible_text)} chars")
        _save_debug_artifacts(sb, debug_tag)

        blocked = _page_looks_blocked(visible_text)
        suspicious = _page_looks_suspicious(title, visible_text)
        if blocked or suspicious:
            reason = "matched a known block phrase" if blocked else "failed length/title sanity check"
            print(f"[!] This doesn't look like the real product page ({reason}).")
            print("[!] Headless run was likely detected, redirected, or interrupted "
                  "-- don't trust the result below.")
            # It's short enough to just dump straight to the terminal so you
            # don't have to go open the screenshot to see what happened.
            if len(visible_text) < MIN_EXPECTED_VISIBLE_CHARS:
                print(f"[i] Full visible text of this page:\n{'-'*40}\n{visible_text}\n{'-'*40}")

        # --- Signal 1: a real, clickable, visible "Request Invite"-style element ---
        match = _find_clickable_invite_element(sb)
        if match:
            phrase, label = match
            print(f'[+] FOUND: clickable element matching "{phrase}" -- label: {label!r}')
            return True

        # Informational only -- a phrase floating in visible text (e.g. a
        # sentence mentioning "invitation") without a matching clickable
        # element is NOT a hit, but it's worth knowing about when debugging.
        loose_hits = [p for p in INVITE_PHRASES if p in visible_text]
        if loose_hits:
            print(f"[i] Phrase(s) appear in visible text but not as a clickable element: {loose_hits}")

        # --- Signal 2: Best Buy's own structured buttonState field ---
        # This is the field that actually drives which button gets rendered,
        # straight from their GraphQL/JSON payload -- independent of whether
        # the text-matching logic above is even looking in the right place.
        # If it ever reports something other than a known-normal state,
        # that's a strong sign the product just entered (or left) a gated /
        # invite-only state, even if the visible button text hasn't been
        # captured in INVITE_PHRASES yet.
        driver_states = _find_driver_sku_button_states(page_source)
        if driver_states:
            for sku, state in driver_states.items():
                print(f"[i] Driver SKU {sku} buttonState: {state!r}")
                if state not in KNOWN_NORMAL_BUTTON_STATES:
                    print(
                        f"[!!] UNRECOGNIZED buttonState {state!r} for SKU {sku} -- "
                        f"doesn't match any known-normal state. This may be the "
                        f"invite-gated state. If you confirm it is, add {state!r} "
                        f"to KNOWN_NORMAL_BUTTON_STATES (if it's benign) or track "
                        f"it as the real gated-state signal going forward."
                    )
                    return True
        elif not suspicious:
            # Only worth flagging as a payload-structure surprise on pages
            # that otherwise looked like the real PDP -- on a suspicious/
            # blocked page, of course there's no product JSON to find.
            print(
                "[i] Could not locate a driverSku buttonState field in the page "
                "source (Best Buy may have changed their payload structure)."
            )

    return False


def check_urls_for_invite(urls, headless: bool = True):
    """Runs check_for_invite() for each URL in `urls`, in order.

    Returns a list of (url, found: bool) tuples -- one entry per URL, in
    the same order they were given. A single bad/erroring URL does not
    stop the rest of the batch: the error is printed and that URL is
    recorded as found=False.

    This is the entry point monitor.py's daily scheduler calls.
    """
    results = []
    for i, url in enumerate(urls):
        tag = f"daily_{i}"
        print(f"[i] Checking Best Buy URL {i + 1}/{len(urls)}: {url}")
        try:
            found = check_for_invite(url, headless=headless, debug_tag=tag)
        except Exception as e:
            print(f"[!] check_for_invite crashed for {url}: {e}")
            found = False
        results.append((url, found))
    return results


def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    url = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else DEFAULT_URL
    headless = "--headed" not in sys.argv

    found = False
    try:
        found = check_for_invite(url, headless=headless, debug_tag="headless" if headless else "headed")
    finally:
        if not found:
            print("[-] No invitation request button found right now.")

    sys.exit(0 if found else 1)


if __name__ == "__main__":
    main()
