#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 Polymarket 5-Minute Up/Down Outcome Scraper  —  a headless-browser solution
================================================================================

WHAT THIS IS
------------
A small, self-contained, MIT-friendly reference implementation that shows how to
read the **early "Outcome: Up / Down" signal** for Polymarket's 5-minute
"up/down" crypto markets (BTC / ETH / SOL / XRP) *long before* the on-chain
oracle settles the market.

We call this early signal "PTB" (the resolved direction the **P**olymarket
front-end shows you, "**T**op **B**ar"-style, i.e. the rendered outcome label).
Whatever you call it, it is just the string `Up` or `Down` that the website
displays once a 5-minute candle closes.

This file is intentionally standalone: it does NOT depend on any trading bot,
broker SDK, private keys, or accounts. Run it, point it at a market, and it
prints `up` / `down`. That's it. Use it as a teaching example or as the data
layer for your own project.


THE CORE PROBLEM (and why the obvious approaches fail)
------------------------------------------------------
Each 5-minute market resolves to "Up" if the asset's price at candle close is
higher than at candle open, else "Down". You'd think you could read that from
an API. In practice the *timely* answer is surprisingly hard to get:

  1. The on-chain ORACLE is slow.
     Polymarket's authoritative resolution (the `closed` flag and
     `outcomePrices` on the Gamma API, the UMA oracle, etc.) only flips after
     the oracle is fed and finalized. Measured latency is on the order of
     ~hundreds of seconds (we observed a p50 around ~9 minutes). For a strategy
     that must act within the *next* 5-minute candle, that is far too late.

  2. The HTML source does NOT contain the outcome.
     If you `curl` the event page, the "Outcome: Up" text is NOT in the raw
     HTML, and it is NOT in any single XHR/JSON response you can cleanly fetch.
     Polymarket is a Next.js / React app: the outcome label is COMPUTED IN THE
     BROWSER from live price data and only exists in the **rendered DOM**.

  3. But the FRONT-END knows the answer almost immediately.
     The site's JavaScript compares the live price to the open price and renders
     "Outcome: Up / Down" within ~20 seconds of candle close — minutes before
     the oracle. That rendered text is the fastest reliable public signal.

CONCLUSION: to get the answer at front-end speed, we must *be* the front-end.
We load the page in a real (headless) browser, let React render, and read the
outcome straight out of the live DOM with a tiny regex.


HOW IT WORKS (the 5 key ideas)
------------------------------
  (A) URL is deterministic.
      Every market has a slug of the form:
          {asset}-updown-5m-{t_start}
      where `t_start` is the candle's open time as a UNIX timestamp aligned to a
      5-minute boundary (UTC). The page lives at:
          https://polymarket.com/event/{asset}-updown-5m-{t_start}

  (B) Render in a headless browser (Playwright + Chromium).
      `page.goto(url)` loads the SPA; React then computes and paints the
      "Outcome: Up/Down" label into the DOM.

  (C) Poll the DOM *inside* the browser, not from Python.
      We hand the browser a tiny JS function that regex-matches the body text
      and returns `"up"`, `"down"`, or `null`. `page.wait_for_function(...)`
      re-runs it every 100 ms and resolves the instant the text appears. This is
      far faster and cheaper than pulling the whole HTML back to Python and
      re-parsing it on a fixed timer.

  (D) Reuse ONE browser for the whole process; navigate via SPA routing.
      Launching Chromium is expensive (~1s+). We launch once and keep a single
      Browser + Context + Page alive. Subsequent fetches are just
      `page.goto(new_url)`, which Next.js handles as a soft SPA navigation —
      JS/CSS stay cached, so each fetch takes ~1–12s instead of a cold load.

  (E) Block the heavy/irrelevant requests.
      We abort images, fonts, media, and a list of third-party
      analytics/monitoring domains (Sentry, Amplitude, etc.). This cuts first-
      paint time noticeably. CRUCIAL: we DO allow JS and CSS through — block
      those and React never renders, so the outcome never appears.

Plus a couple of production niceties:
  * Exponential-ish backoff retry until a per-call deadline (a freshly closed
    candle may take a few seconds to show its outcome).
  * Periodic browser restart to bound Chromium's long-run memory growth.
  * In-memory cache so the same candle is never scraped twice.


DEPENDENCIES
------------
    pip install playwright
    playwright install chromium      # one-time, downloads the browser (~150MB+)


QUICK START
-----------
    # Scrape the most-recently-closed BTC 5m candle:
    python polymarket_outcome_scraper.py --asset btc

    # Scrape a specific candle by its UNIX open-time:
    python polymarket_outcome_scraper.py --asset eth --t-start 1716900000

    # Watch the browser work (debugging):
    python polymarket_outcome_scraper.py --asset btc --no-headless

Programmatic use:

    import asyncio
    from polymarket_outcome_scraper import OutcomeScraper

    async def main():
        async with OutcomeScraper(asset="btc") as scraper:
            outcome, status, attempts = await scraper.get_outcome_with_retry(
                t_start=1716900000,
            )
            print(outcome)   # 'up' / 'down' / None

    asyncio.run(main())


IMPORTANT CAVEATS / ETHICS
--------------------------
  * This reads PUBLIC, already-rendered information from a public website. Be a
    good citizen: keep request rates modest, identify a realistic user agent,
    and respect Polymarket's Terms of Service. Do not hammer the site.
  * The front-end outcome is an *early indicator*, not the on-chain settlement.
    In rare edge cases (e.g. a price that hovers exactly at the open) the
    displayed label could differ from final oracle resolution. Treat it as a
    fast signal, and reconcile against authoritative data if correctness is
    critical.
  * Front-end markup changes over time. If Polymarket restructures the page,
    the "Outcome: Up/Down" text or its location may change. The regex in
    `_OUTCOME_DOM_PROBE_JS` is the single place you'd update.

License: do whatever you like with this file. Attribution appreciated, not
required.
================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import time
from typing import List, Optional, Tuple

# Playwright is imported lazily inside OutcomeScraper so that simply importing
# this module (e.g. for the constants / URL helpers) does not require Chromium
# to be installed. The actual import happens in `_lazy_import_playwright()`.
async_playwright = None
PlaywrightTimeoutError = None


# ──────────────────────────────────────────────────────────────────────────────
# Logging — plain stdlib logger so this file has zero non-Playwright deps.
# ──────────────────────────────────────────────────────────────────────────────
logger = logging.getLogger("outcome_scraper")


# ──────────────────────────────────────────────────────────────────────────────
# Constants & configuration
# ──────────────────────────────────────────────────────────────────────────────

POLYMARKET_WEB = "https://polymarket.com"

# 5-minute candle duration, in seconds. Candle open times (`t_start`) are always
# aligned to a multiple of this value in UTC.
CANDLE_SECONDS = 300

# Slug prefixes for the supported 5-minute up/down markets. The full event slug
# is "{slug_prefix}-5m-{t_start}", e.g. "btc-updown-5m-1716900000".
ASSET_SLUG_PREFIX = {
    "btc": "btc-updown",
    "eth": "eth-updown",
    "sol": "sol-updown",
    "xrp": "xrp-updown",
}

# This JavaScript runs *inside the page* (via page.wait_for_function). It reads
# the live, rendered body text and tries to pull out the outcome label.
#
# Returning a truthy value ('up'/'down') tells Playwright "done, stop polling".
# Returning null tells Playwright "not yet, poll again in `polling` ms".
#
# THE REGEX IS THE HEART OF THE WHOLE SOLUTION. If Polymarket changes its
# wording, this is the one line you adjust. We match case-insensitively and
# normalize to lowercase so callers always get 'up' / 'down'.
_OUTCOME_DOM_PROBE_JS = r"""
() => {
    if (!document.body) return null;
    const text = document.body.innerText || "";
    const m = text.match(/Outcome:\s*(Up|Down)/i);
    return m ? m[1].toLowerCase() : null;
}
"""

# Third-party analytics / monitoring / wallet domains. Aborting these speeds up
# first paint and avoids unnecessary traffic. They have nothing to do with the
# outcome label, so blocking them is safe.
#
# NOTE: We deliberately do NOT block scripts or stylesheets from Polymarket's
# own origin — React needs them to render, and without rendering there is no
# outcome text to read.
_BLOCKED_DOMAINS = (
    "sentry.io", "amplitude.com", "google-analytics.com", "googletagmanager.com",
    "intercom.io", "intercomcdn.com", "walletconnect.com", "walletconnect.org",
    "segment.com", "privy.io", "moonpay.com", "magic.link", "transak.com",
    "google.com/recaptcha", "cdn.cookielaw.org", "datadoghq-browser-agent",
    "datadoghq.com", "hotjar.com", "mixpanel.com", "fullstory.com",
    "newrelic.com",
)

# Resource *types* we always abort (we never need them to read text).
_BLOCKED_RESOURCE_TYPES = ("image", "media", "font")


# ──────────────────────────────────────────────────────────────────────────────
# Time / URL helpers — pure functions, trivially unit-testable.
# ──────────────────────────────────────────────────────────────────────────────

def align_to_candle(unix_ts: float) -> int:
    """Round a UNIX timestamp DOWN to the open time of its 5-minute candle (UTC).

    Candle boundaries are simply multiples of CANDLE_SECONDS since the epoch,
    which is already UTC-aligned, so no timezone math is required.
    """
    return (int(unix_ts) // CANDLE_SECONDS) * CANDLE_SECONDS


def latest_closed_candle(now: Optional[float] = None) -> int:
    """Return the open time of the most-recently *closed* 5-minute candle.

    The candle currently in progress has not closed yet, so we step back one
    full candle from "now".
    """
    if now is None:
        now = time.time()
    return align_to_candle(now) - CANDLE_SECONDS


def build_event_url(asset: str, t_start: int) -> str:
    """Construct the public Polymarket event URL for a given asset + candle.

    Example:
        build_event_url("btc", 1716900000)
        -> "https://polymarket.com/event/btc-updown-5m-1716900000"
    """
    asset = asset.lower()
    if asset not in ASSET_SLUG_PREFIX:
        raise ValueError(
            f"Unsupported asset '{asset}'. Supported: {sorted(ASSET_SLUG_PREFIX)}"
        )
    slug_prefix = ASSET_SLUG_PREFIX[asset]
    return f"{POLYMARKET_WEB}/event/{slug_prefix}-5m-{t_start}"


# ──────────────────────────────────────────────────────────────────────────────
# Lazy Playwright import
# ──────────────────────────────────────────────────────────────────────────────

def _lazy_import_playwright():
    """Import Playwright on first use, with a friendly error if it's missing.

    Keeping this lazy means you can `import` this module (to reuse the URL/time
    helpers, for example) without having Chromium installed.
    """
    global async_playwright, PlaywrightTimeoutError
    if async_playwright is None:
        try:
            from playwright.async_api import (
                async_playwright as _ap,
                TimeoutError as _pte,
            )
            async_playwright = _ap
            PlaywrightTimeoutError = _pte
        except ImportError as e:
            raise ImportError(
                "Playwright is required. Install it with:\n"
                "    pip install playwright\n"
                "    playwright install chromium"
            ) from e


# ──────────────────────────────────────────────────────────────────────────────
# Network interception
# ──────────────────────────────────────────────────────────────────────────────

async def _route_handler(route):
    """Playwright request interceptor.

    Strategy: abort the expensive/irrelevant stuff (images, fonts, media, and
    known third-party analytics), and let everything else through — crucially
    including the first-party JS/CSS that React needs to render.

    A route handler must never raise; if it does, the page can hang. So we
    swallow exceptions and fall back to continuing the request.
    """
    try:
        request = route.request
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            return await route.abort()
        url = request.url
        for domain in _BLOCKED_DOMAINS:
            if domain in url:
                return await route.abort()
        return await route.continue_()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# The scraper
# ──────────────────────────────────────────────────────────────────────────────

class OutcomeScraper:
    """Headless-browser scraper for Polymarket 5-minute up/down outcomes.

    Lifecycle:
        scraper = OutcomeScraper(asset="btc")
        await scraper.start()                 # launch Chromium (idempotent)
        out, status, attempts = await scraper.get_outcome_with_retry(t_start)
        await scraper.close()                 # tear everything down

    Or as an async context manager:
        async with OutcomeScraper(asset="btc") as scraper:
            out, status, attempts = await scraper.get_outcome_with_retry(t_start)

    Status codes returned by the scrape methods:
        'resolved'    -> outcome found ('up' / 'down')
        'unresolved'  -> page loaded but no Outcome text yet (candle just closed)
        'load_error'  -> navigation / browser error
        'timeout'     -> deadline reached without ever resolving (retry method)
    """

    def __init__(
        self,
        asset: str,
        *,
        max_wait_sec: float = 25.0,
        restart_every: int = 50,
        headless: bool = True,
        user_agent: Optional[str] = None,
    ):
        """
        Args:
            asset:         one of 'btc' / 'eth' / 'sol' / 'xrp'.
            max_wait_sec:  per-attempt ceiling for page load + DOM polling.
            restart_every: relaunch Chromium after this many fetches to bound
                           long-run memory growth. Set very high to disable.
            headless:      False shows a real browser window (handy to debug).
            user_agent:    override the UA string if you like.
        """
        _lazy_import_playwright()

        asset = asset.lower()
        if asset not in ASSET_SLUG_PREFIX:
            raise ValueError(
                f"Unsupported asset '{asset}'. Supported: {sorted(ASSET_SLUG_PREFIX)}"
            )

        self.asset = asset
        self.max_wait_sec = max_wait_sec
        self.restart_every = restart_every
        self.headless = headless
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

        # Long-lived Playwright objects. We keep exactly one of each alive for
        # the whole process and navigate via page.goto().
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

        self._fetches_since_launch = 0
        self._launched_at = 0.0

        # Resolved-outcome cache: a closed candle never changes, so once we know
        # its outcome we never need to scrape it again.
        self._cache: dict = {}

        # Guards concurrent start()/restart() from racing each other.
        self._lifecycle_lock = asyncio.Lock()

    # ── async context-manager sugar ──────────────────────────────────────────
    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    # ── browser lifecycle ─────────────────────────────────────────────────────
    async def start(self):
        """Launch the browser. Idempotent: a no-op if already running."""
        async with self._lifecycle_lock:
            if self._browser is not None:
                return
            await self._launch_locked()

    async def _launch_locked(self):
        """Actually launch Chromium. Caller MUST already hold the lock."""
        try:
            self._pw = await async_playwright().start()
            # These Chromium flags reduce flakiness in containers/CI and shave a
            # little startup cost. --no-sandbox is commonly required in Docker.
            self._browser = await self._pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-features=TranslateUI",
                    "--disable-extensions",
                    "--mute-audio",
                ],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=self.user_agent,
            )
            self._page = await self._context.new_page()
            # Install the request interceptor for every request on this page.
            await self._page.route("**/*", _route_handler)
            self._fetches_since_launch = 0
            self._launched_at = time.time()
            logger.info(
                "browser launched (headless=%s, restart_every=%d)",
                self.headless, self.restart_every,
            )
        except Exception:
            logger.exception("failed to launch browser")
            await self._teardown_locked()
            raise

    async def close(self):
        """Tear down the page, context, browser and Playwright. Idempotent."""
        async with self._lifecycle_lock:
            await self._teardown_locked()

    async def _teardown_locked(self):
        """Best-effort cleanup. Caller MUST already hold the lock.

        Every close is wrapped in try/except because, by the time we're tearing
        down, some objects may already be dead (e.g. after a browser crash).
        """
        for obj, closer in (
            (self._page, lambda o: o.close()),
            (self._context, lambda o: o.close()),
            (self._browser, lambda o: o.close()),
            (self._pw, lambda o: o.stop()),
        ):
            if obj is not None:
                try:
                    await closer(obj)
                except Exception:
                    pass
        self._page = self._context = self._browser = self._pw = None

    async def _restart_if_needed(self):
        """Relaunch the browser once we've done `restart_every` fetches.

        Chromium's memory creeps up over thousands of navigations; a periodic
        clean restart keeps a long-running process healthy. Must be called
        OUTSIDE the lock (it acquires the lock itself).
        """
        if self._fetches_since_launch < self.restart_every:
            return
        async with self._lifecycle_lock:
            if self._fetches_since_launch < self.restart_every:  # double-check
                return
            uptime = int(time.time() - self._launched_at)
            logger.info(
                "recycling browser after %d fetches (uptime %ds)",
                self._fetches_since_launch, uptime,
            )
            await self._teardown_locked()
            await self._launch_locked()

    # ── single fetch ───────────────────────────────────────────────────────────
    async def get_outcome(self, t_start: int) -> Tuple[Optional[str], str, int]:
        """Scrape the outcome for one candle, exactly once (no retry loop).

        Returns (outcome, status, latency_ms):
            outcome:    'up' / 'down' / None
            status:     'resolved' / 'unresolved' / 'load_error'
            latency_ms: how long this attempt took, in milliseconds.

        The flow:
            1. Fast path: return from cache if we've already resolved this candle.
            2. Maybe recycle the browser; ensure it's started.
            3. page.goto(url) with wait_until='domcontentloaded' (we don't need
               'networkidle' — an SPA keeps chattering, and we only care that the
               document exists so our JS probe can start running).
            4. page.wait_for_function(probe, polling=100ms) — the browser itself
               re-checks the DOM every 100ms and resolves the moment the regex
               matches. This is the key to low latency.
        """
        # 1) Cache hit — a closed candle's outcome is immutable.
        if t_start in self._cache:
            return (self._cache[t_start], "resolved", 0)

        await self._restart_if_needed()
        await self.start()  # ensure browser is up (idempotent)

        url = build_event_url(self.asset, t_start)
        started = time.time()
        try:
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self.max_wait_sec * 1000),
            )

            # Poll the DOM *inside the browser* until the outcome appears or we
            # hit max_wait_sec. `json_value()` pulls the resolved JS value back
            # into Python.
            handle = await self._page.wait_for_function(
                _OUTCOME_DOM_PROBE_JS,
                polling=100,
                timeout=int(self.max_wait_sec * 1000),
            )
            outcome = await handle.json_value()

            self._fetches_since_launch += 1
            latency_ms = int((time.time() - started) * 1000)

            if outcome in ("up", "down"):
                self._cache[t_start] = outcome
                return (outcome, "resolved", latency_ms)

            # wait_for_function only resolves on a truthy return, so reaching
            # here is unexpected — treat as not-yet-resolved.
            return (None, "unresolved", latency_ms)

        except PlaywrightTimeoutError:
            # The page loaded but no "Outcome:" text showed up within the window.
            # Almost always means the candle only just closed; a retry will work.
            self._fetches_since_launch += 1
            latency_ms = int((time.time() - started) * 1000)
            return (None, "unresolved", latency_ms)

        except Exception as e:
            # Navigation / network / browser-crash error.
            latency_ms = int((time.time() - started) * 1000)
            logger.debug("scrape error for t_start=%s: %s: %s",
                         t_start, type(e).__name__, e)
            # Force a browser recycle on the next call — the page may be wedged.
            self._fetches_since_launch = max(self._fetches_since_launch,
                                             self.restart_every)
            return (None, "load_error", latency_ms)

    # ── fetch with retry ─────────────────────────────────────────────────────
    async def get_outcome_with_retry(
        self,
        t_start: int,
        *,
        deadline_ts: Optional[float] = None,
        backoff_seq: Optional[List[float]] = None,
        max_attempts: int = 10,
    ) -> Tuple[Optional[str], str, int]:
        """Scrape with backoff retry until success, deadline, or attempt cap.

        A freshly-closed candle may need a few seconds before the front-end
        paints its outcome, so we retry with growing waits.

        Args:
            t_start:      candle open time (UNIX, 5-min aligned).
            deadline_ts:  absolute UNIX time to stop trying. Defaults to
                          now + 270s (leaves headroom inside a 5-min window).
            backoff_seq:  per-attempt wait schedule (seconds). We index into it
                          by attempt number and clamp at the last element.
            max_attempts: hard cap on attempts.

        Returns (outcome, status, attempts):
            status is 'resolved' on success, else the last failure status, or
            'timeout' if the deadline was hit first.
        """
        if deadline_ts is None:
            deadline_ts = time.time() + 270.0
        if backoff_seq is None:
            backoff_seq = [0, 3, 5, 10, 20, 30, 45, 60, 90, 120]

        tag = f"{self.asset}@{t_start}"
        last_status = "unresolved"

        for attempt in range(1, max_attempts + 1):
            if time.time() >= deadline_ts:
                logger.warning("[%s] deadline reached after %d attempts (last=%s)",
                               tag, attempt - 1, last_status)
                return (None, "timeout", attempt - 1)

            outcome, status, latency_ms = await self.get_outcome(t_start)
            last_status = status

            if status == "resolved":
                logger.info("[%s] resolved outcome=%s (attempt %d, %dms)",
                            tag, outcome, attempt, latency_ms)
                return (outcome, status, attempt)

            if attempt >= max_attempts:
                logger.warning("[%s] giving up after %d attempts (last=%s)",
                               tag, max_attempts, status)
                return (None, status, attempt)

            # Pick the backoff for this attempt and add ±20% jitter so many
            # clients don't retry in lockstep.
            wait = backoff_seq[min(attempt - 1, len(backoff_seq) - 1)]
            wait = wait * (0.8 + 0.4 * random.random())
            # Never sleep past the deadline.
            wait = min(wait, max(0.5, deadline_ts - time.time() - 0.5))

            logger.info("[%s] attempt %d -> %s (%dms); retrying in %.1fs",
                        tag, attempt, status, latency_ms, wait)
            if wait > 0:
                await asyncio.sleep(wait)

        return (None, last_status, max_attempts)


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point — a runnable demo / smoke test.
# ──────────────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Scrape the early 'Outcome: Up/Down' label for a Polymarket "
            "5-minute up/down market, straight from the rendered front-end."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--asset", default="btc",
                   choices=sorted(ASSET_SLUG_PREFIX),
                   help="market asset (default: btc)")
    p.add_argument("--t-start", type=int, default=None,
                   help="candle open time as a UNIX timestamp (5-min aligned). "
                        "Default: the most recently closed candle.")
    p.add_argument("--max-wait-sec", type=float, default=25.0,
                   help="per-attempt timeout for load + DOM polling (default 25)")
    p.add_argument("--max-attempts", type=int, default=10,
                   help="retry attempts before giving up (default 10)")
    p.add_argument("--no-headless", action="store_true",
                   help="show the browser window (useful for debugging)")
    p.add_argument("--debug", action="store_true",
                   help="verbose logging")
    return p


async def _run_cli(args) -> int:
    t_start = args.t_start if args.t_start is not None else latest_closed_candle()
    # Normalize to a candle boundary in case a non-aligned timestamp was passed.
    t_start = align_to_candle(t_start)

    url = build_event_url(args.asset, t_start)
    logger.info("scraping %s", url)

    async with OutcomeScraper(
        asset=args.asset,
        max_wait_sec=args.max_wait_sec,
        headless=not args.no_headless,
    ) as scraper:
        outcome, status, attempts = await scraper.get_outcome_with_retry(
            t_start, max_attempts=args.max_attempts,
        )

    print("─" * 60)
    print(f"asset    : {args.asset}")
    print(f"t_start  : {t_start}  ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(t_start))})")
    print(f"url      : {url}")
    print(f"outcome  : {outcome}")
    print(f"status   : {status}")
    print(f"attempts : {attempts}")
    print("─" * 60)

    # Exit code 0 on success, 1 otherwise — friendly for shell scripting.
    return 0 if status == "resolved" else 1


def main():
    args = _build_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        exit_code = asyncio.run(_run_cli(args))
    except KeyboardInterrupt:
        exit_code = 130
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
