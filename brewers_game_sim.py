#!/usr/bin/env python3
"""
brewers_test.py
---------------
Simulates a game score progression to test webhook firing logic without
needing a live game. Injects fake linescore responses directly into the
watch_game loop.

Usage:
    python3 brewers_test.py \
        --webhook-start https://hooks.example.com/start \
        --webhook-score https://hooks.example.com/score \
        --webhook-end   https://hooks.example.com/end

    # Dry run — print payloads without sending
    python3 brewers_test.py --webhook-start https://... --webhook-score https://... --webhook-end https://... --dry-run

    # Start mid-game (tests first_poll baseline logic)
    python3 brewers_test.py ... --start-score 2

    # Control delay between each simulated poll (default: 2s)
    python3 brewers_test.py ... --tick 1
"""

import argparse
import datetime
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
import ssl
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Minimal copies of shared config and helpers from brewers.py
# ---------------------------------------------------------------------------
LOCAL_TZ_NAME       = "America/Chicago"
LOG_FILE            = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brewers_test.log")
WEBHOOK_TIMEOUT     = 10
BREWERS_ABBREV      = "MIL"
BROADCAST_DELAY_SEC  = 0      # wait before score webhook (broadcast offset)
POST_WEBHOOK_DELAY   = 30     # wait after any webhook fires to let it finish

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

class FlushHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

# Configure a single logger with no propagation to avoid duplicate entries
fmt = logging.Formatter(fmt="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("brewers")
log.setLevel(logging.INFO)
log.propagate = False
log.handlers.clear()
_fh = FlushHandler(LOG_FILE, mode='a')
_fh.setFormatter(fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(fmt)
log.addHandler(_fh)
log.addHandler(_sh)


def send_webhook(url, payload, dry_run=False):
    if dry_run:
        log.info("[DRY-RUN] Would POST to %s:\n%s", url, json.dumps(payload, indent=2))
        return
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "BrewersTest/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT, context=_SSL_CTX) as resp:
            log.info("Webhook sent [HTTP %d] — event: %s", resp.status, payload.get("event"))
    except urllib.error.HTTPError as exc:
        log.error("Webhook HTTP %d for '%s': %s", exc.code, payload.get("event"), exc.reason)
    except Exception as exc:
        log.error("Webhook failed for '%s': %s", payload.get("event"), exc)

# ---------------------------------------------------------------------------
# Simulated game progression
# ---------------------------------------------------------------------------

def build_game_sequence(start_score=0):
    """
    Returns a list of (mil_score, opp_score, state, inning) tuples that
    simulate a full game from first pitch through final.

    start_score > 0 simulates joining mid-game — the first poll will
    already show a non-zero score, testing the first_poll baseline logic.
    """
    sequence = [
        # (mil, opp, state, inning)  — description
        (start_score, 0, "preview", 0),   # pregame / just before first pitch
        (start_score, 0, "live",    1),   # game starts — should fire game_start webhook
        (start_score, 0, "live",    1),   # top 1st, no score
        (start_score, 0, "live",    2),   # bottom 1st, no score
        (start_score, 1, "live",    3),   # opponent scores — no webhook expected
        (start_score, 1, "live",    3),   # still 3rd
        (start_score + 1, 1, "live", 4),  # BREWERS SCORE — should fire score webhook
        (start_score + 1, 1, "live", 5),  # no change
        (start_score + 1, 2, "live", 6),  # opponent scores again — no webhook
        (start_score + 2, 2, "live", 7),  # BREWERS SCORE again — should fire score webhook
        (start_score + 4, 2, "live", 8),  # BREWERS score 2 runs — should fire with runs_added=2
        (start_score + 4, 2, "live", 9),  # top 9th
        (start_score + 4, 2, "final", 9), # GAME OVER — should fire game_end webhook (win)
    ]
    return sequence


def run_test(webhooks, broadcast_delay, post_webhook_delay, tick, dry_run, start_score):
    """
    Iterates through the fake game sequence, running the same webhook logic
    as watch_game() in brewers.py.
    """
    game = {
        "game_pk":     999999,
        "home":        "Milwaukee Brewers",
        "away":        "Chicago Cubs",
        "home_abbrev": "MIL",
        "away_abbrev": "CHC",
    }
    matchup   = "{} @ {}".format(game["away"], game["home"])
    sequence  = build_game_sequence(start_score)

    prev_mil_score = 0
    prev_state     = "preview"
    game_started   = False
    game_ended     = False
    first_poll     = True

    def _base_payload(event, mil, opp, inning):
        return {
            "event":     event,
            "matchup":   matchup,
            "game_pk":   game["game_pk"],
            "brewers":   mil,
            "opponent":  opp,
            "inning":    inning,
            "timestamp": datetime.datetime.now(ZoneInfo(LOCAL_TZ_NAME)).isoformat(),
        }

    log.info("=" * 60)
    log.info("Brewers Test — simulating %d polling ticks", len(sequence))
    log.info("Matchup      : %s", matchup)
    log.info("Start score  : MIL %d (tests first_poll baseline: %s)",
             start_score, "YES" if start_score > 0 else "NO")
    log.info("Tick interval: %ds", tick)
    log.info("Broadcast delay: %ds", broadcast_delay)
    log.info("=" * 60)

    for i, (mil_score, opp_score, state, inning) in enumerate(sequence):
        log.info("--- Tick %d/%d ---", i + 1, len(sequence))

        log.info("Score check — inning: %s  |  MIL %d (prev %d)  OPP %d  |  state: %s",
                 inning, mil_score, prev_mil_score, opp_score, state)

        # ── Game start ────────────────────────────────────────────────────
        if state == "live" and prev_state == "preview" and not game_started:
            game_started = True
            log.info("GAME START: %s  |  MIL %d – OPP %d  (inning %d)",
                     matchup, mil_score, opp_score, inning)
            send_webhook(webhooks["game_start"],
                         _base_payload("game_start", mil_score, opp_score, inning), dry_run)
            if post_webhook_delay > 0:
                log.info("Post-webhook delay — waiting %ds...", post_webhook_delay)
                time.sleep(post_webhook_delay)


        # ── Brewers score ─────────────────────────────────────────────────
        if first_poll and mil_score > 0:
            log.info("First poll baseline — MIL score is already %d, skipping score webhook.", mil_score)
        elif state in ("live", "final") and mil_score > prev_mil_score:
            runs_added = mil_score - prev_mil_score
            log.info("BREWERS SCORE! +%d run(s) — MIL %d -> %d, OPP %d  (inning %d)",
                     runs_added, prev_mil_score, mil_score, opp_score, inning)
            if broadcast_delay > 0:
                log.info("Broadcast delay — waiting %ds before firing webhook...", broadcast_delay)
                time.sleep(broadcast_delay)
            payload = _base_payload("brewers_score", mil_score, opp_score, inning)
            payload["runs_added"] = runs_added
            send_webhook(webhooks["brewers_score"], payload, dry_run)
            if post_webhook_delay > 0:
                log.info("Post-webhook delay — waiting %ds...", post_webhook_delay)
                time.sleep(post_webhook_delay)

        # ── Game end ──────────────────────────────────────────────────────
        if state == "final" and not game_ended:
            game_ended = True
            result     = "win" if mil_score > opp_score else ("loss" if mil_score < opp_score else "tie")
            log.info("GAME FINAL: %s  |  MIL %d – OPP %d  (%s)",
                     matchup, mil_score, opp_score, result.upper())
            payload = _base_payload("game_end", mil_score, opp_score, inning)
            payload["result"] = result
            send_webhook(webhooks["game_end"], payload, dry_run)
            if post_webhook_delay > 0:
                log.info("Post-webhook delay — waiting %ds...", post_webhook_delay)
                time.sleep(post_webhook_delay)
            break

        first_poll     = False
        prev_mil_score = mil_score
        prev_state     = state

        if i < len(sequence) - 1:
            time.sleep(tick)

    log.info("=" * 60)
    log.info("Test complete.")
    log.info("=" * 60)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Simulate a Brewers game to test webhook logic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 brewers_test.py \\\n"
            "      --webhook-start https://hooks.example.com/start \\\n"
            "      --webhook-score https://hooks.example.com/score \\\n"
            "      --webhook-end   https://hooks.example.com/end\n"
        ),
    )
    parser.add_argument("--webhook-start",    required=True,  metavar="URL",
                        help="Webhook URL for game start.")
    parser.add_argument("--webhook-score",    required=True,  metavar="URL",
                        help="Webhook URL for Brewers score.")
    parser.add_argument("--webhook-end",      required=True,  metavar="URL",
                        help="Webhook URL for game end.")
    parser.add_argument("--broadcast-delay",  type=int, default=BROADCAST_DELAY_SEC, metavar="SECS",
                        help="Seconds to wait after score before firing webhook (default: 0).")
    parser.add_argument("--post-webhook-delay", type=int, default=POST_WEBHOOK_DELAY,   metavar="SECS",
                        help="Seconds to wait after each webhook fires (default: {}).".format(POST_WEBHOOK_DELAY))
    parser.add_argument("--tick",             type=int, default=2, metavar="SECS",
                        help="Seconds between simulated polls (default: 2).")
    parser.add_argument("--start-score",      type=int, default=0, metavar="N",
                        help="Simulate joining mid-game with MIL already at N runs (default: 0).")
    parser.add_argument("--dry-run",          action="store_true",
                        help="Print webhook payloads without sending them.")
    args = parser.parse_args()

    webhooks = {
        "game_start":    args.webhook_start,
        "brewers_score": args.webhook_score,
        "game_end":      args.webhook_end,
    }

    run_test(
        webhooks           = webhooks,
        broadcast_delay    = args.broadcast_delay,
        post_webhook_delay = args.post_webhook_delay,
        tick               = args.tick,
        dry_run            = args.dry_run,
        start_score        = args.start_score,
    )

if __name__ == "__main__":
    main()