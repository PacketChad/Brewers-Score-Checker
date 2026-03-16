#!/usr/bin/env python3
"""
brewers_score_watcher.py
------------------------
Monitors live Brewers games and fires webhook calls on:
  - Game start
  - Brewers score (any new run by MIL)
  - Game end

Usage:
    python3 brewers_score_watcher.py \
        --webhook-start https://hooks.example.com/start \
        --webhook-score https://hooks.example.com/score \
        --webhook-end   https://hooks.example.com/end

    # Preview payloads without sending anything
    python3 brewers_score_watcher.py \
        --webhook-start https://... --webhook-score https://... --webhook-end https://... \
        --dry-run

    # Faster polling (default 30s)
    python3 brewers_score_watcher.py ... --poll-interval 15

Requirements: Python 3.8+ (stdlib only, no third-party packages)
"""

import argparse
import datetime
import json
import logging
import os
import signal
import sys
import time
import urllib.request
import urllib.error
import ssl

# Bypass SSL certificate verification (macOS python.org install quirk)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOCAL_TZ_NAME    = "America/Chicago"
LOG_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brewers_watcher.log")
DEFAULT_POLL_SEC = 30   # seconds between live score checks
PREGAME_POLL_SEC = 60   # seconds between checks while waiting for first pitch
WEBHOOK_TIMEOUT  = 10   # seconds before a webhook POST gives up
BREWERS_TEAM_ID  = 158
BROADCAST_DELAY_SEC  = 0      # wait before score webhook (broadcast offset)
POST_WEBHOOK_DELAY   = 30     # wait after any webhook fires to let it finish      # extra wait after score detected before webhook fires
BREWERS_ABBREV   = "MIL"

MLB_SCHEDULE_URL = (
    "https://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&teamId=158&gameType=R,S"
    "&hydrate=linescore,teams"
)
MLB_GAME_URL = "https://statsapi.mlb.com/api/v1/game/{game_pk}/linescore"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    log.info("Signal %s received — shutting down.", signum)
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_SSL_CTX) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log.error("GET %s failed: %s", url, exc)
        return None


def send_webhook(url, payload, dry_run=False):
    """POST JSON payload to the webhook URL. Logs success/failure; never raises."""
    if dry_run:
        log.info("[DRY-RUN] Would POST to %s:\n%s", url, json.dumps(payload, indent=2))
        return

    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "BrewersWatcher/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT, context=_SSL_CTX) as resp:
            status = resp.status
        log.info("Webhook sent [HTTP %d] — event: %s  url: %s", status, payload.get("event"), url)
    except urllib.error.HTTPError as exc:
        log.error("Webhook HTTP %d for event '%s': %s  url: %s",
                  exc.code, payload.get("event"), exc.reason, url)
    except Exception as exc:
        log.error("Webhook failed for event '%s': %s  url: %s", payload.get("event"), exc, url)

# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------

def get_todays_game():
    """Return the first Brewers game scheduled for today (local time), or None."""
    local_tz  = ZoneInfo(LOCAL_TZ_NAME)
    today_str = datetime.datetime.now(local_tz).strftime("%Y-%m-%d")
    url       = MLB_SCHEDULE_URL + "&date=" + today_str

    data = http_get(url)
    if not data:
        return None

    dates = data.get("dates", [])
    if not dates:
        return None

    for game in dates[0].get("games", []):
        teams   = game.get("teams", {})
        away_id = teams.get("away", {}).get("team", {}).get("id")
        home_id = teams.get("home", {}).get("team", {}).get("id")
        if BREWERS_TEAM_ID not in (away_id, home_id):
            continue

        raw_dt   = game.get("gameDate", "")
        utc_dt   = datetime.datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        local_dt = utc_dt.astimezone(ZoneInfo(LOCAL_TZ_NAME))

        return {
            "game_pk":     game["gamePk"],
            "home":        teams["home"]["team"]["name"],
            "away":        teams["away"]["team"]["name"],
            "home_abbrev": teams["home"]["team"].get("abbreviation", ""),
            "away_abbrev": teams["away"]["team"].get("abbreviation", ""),
            "start_utc":   utc_dt,
            "start_local": local_dt,
            "status":      game.get("status", {}).get("abstractGameState", "Preview"),
        }

    return None


def get_linescore(game_pk):
    """Fetch the current linescore for a game."""
    return http_get(MLB_GAME_URL.format(game_pk=game_pk))


def parse_score(linescore, game):
    """Return (mil_score, opp_score, game_state, inning)."""
    teams     = linescore.get("teams", {})
    home_runs = teams.get("home", {}).get("runs", 0) or 0
    away_runs = teams.get("away", {}).get("runs", 0) or 0

    if game["home_abbrev"] == BREWERS_ABBREV:
        mil_score, opp_score = home_runs, away_runs
    else:
        mil_score, opp_score = away_runs, home_runs

    inning       = linescore.get("currentInning", 0) or 0
    inning_half  = linescore.get("currentInningHalf", "").lower()
    is_game_over = linescore.get("isGameOver", False)
    state_raw    = linescore.get("abstractGameState", "Preview").lower()

    # Use isGameOver or state=final as the game-over signal
    if is_game_over or state_raw == "final":
        state = "final"
    elif state_raw in ("live", "in progress") or inning > 0:
        state = "live"
    else:
        state = "preview"

    return mil_score, opp_score, state, inning

# ---------------------------------------------------------------------------
# Interruptible sleeps
# ---------------------------------------------------------------------------

def sleep_until(target_utc):
    """Sleep until target_utc. Returns True on arrival, False if shutdown."""
    while not _shutdown:
        secs = (target_utc - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if secs <= 0:
            return True
        time.sleep(min(30, secs))
    return False


def short_sleep(seconds):
    """Sleep for `seconds` in small chunks. Returns True normally, False on shutdown."""
    end = time.monotonic() + seconds
    while not _shutdown:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(5, remaining))
    return False

# ---------------------------------------------------------------------------
# Core watcher
# ---------------------------------------------------------------------------

def watch_game(game, webhooks, poll_sec, pregame_sec, broadcast_delay, post_webhook_delay, dry_run):
    """
    Monitor a single game from pregame through final.

    webhooks is a dict with keys:
      'game_start'    -> URL to POST when game starts
      'brewers_score' -> URL to POST when Brewers score
      'game_end'      -> URL to POST when game ends
    """
    game_pk = game["game_pk"]
    matchup = "{} @ {}".format(game["away"], game["home"])
    log.info("Watching: %s  (gamePk %d)", matchup, game_pk)

    prev_mil_score = 0
    prev_state     = "preview"
    game_started   = False
    game_ended     = False
    first_poll     = True   # used to establish score baseline without triggering webhook

    def _base_payload(event, mil, opp, inning):
        return {
            "event":     event,
            "matchup":   matchup,
            "game_pk":   game_pk,
            "brewers":   mil,
            "opponent":  opp,
            "inning":    inning,
            "timestamp": datetime.datetime.now(ZoneInfo(LOCAL_TZ_NAME)).isoformat(),
        }

    while not _shutdown and not game_ended:
        linescore = get_linescore(game_pk)
        if linescore is None:
            log.warning("Could not fetch linescore — retrying in %ds.", poll_sec)
            if not short_sleep(poll_sec):
                break
            continue

        mil_score, opp_score, state, inning = parse_score(linescore, game)

        log.info("Score check — inning: %s  |  MIL %d (prev %d)  OPP %d  |  state: %s",
                 inning, mil_score, prev_mil_score, opp_score, state)

        # ── Game start ────────────────────────────────────────────────────
        # Also trigger on inning > 0 in case API is slow to flip state to live
        if (state == "live" or inning > 0) and prev_state == "preview" and not game_started:
            game_started = True
            log.info("GAME START: %s  |  MIL %d – OPP %d  (inning %d)",
                     matchup, mil_score, opp_score, inning)
            send_webhook(
                webhooks["game_start"],
                _base_payload("game_start", mil_score, opp_score, inning),
                dry_run,
            )
            if post_webhook_delay > 0:
                log.info("Post-webhook delay — waiting %ds...", post_webhook_delay)
                time.sleep(post_webhook_delay)


        # ── Brewers score ─────────────────────────────────────────────────
        if first_poll and mil_score > 0:
            log.info("First poll baseline — MIL score is already %d, skipping score webhook.", mil_score)
        elif (state in ("live", "final") or inning > 0) and mil_score > prev_mil_score:
            runs_added = mil_score - prev_mil_score
            log.info("BREWERS SCORE! +%d run(s) — MIL %d -> %d, OPP %d  (inning %d)",
                     runs_added, prev_mil_score, mil_score, opp_score, inning)
            if broadcast_delay > 0:
                log.info("Broadcast delay — waiting %ds before firing webhook...", broadcast_delay)
                short_sleep(broadcast_delay)
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

        interval = poll_sec if state == "live" else pregame_sec
        if not short_sleep(interval):
            break

    log.info("Done watching game %d.", game_pk)


# ---------------------------------------------------------------------------
# Webhook tester
# ---------------------------------------------------------------------------

def test_webhooks(webhooks, dry_run=False):
    """Fire a sample payload to each webhook URL and report the result."""
    log.info("=" * 60)
    log.info("Testing all three webhooks...")
    log.info("=" * 60)

    now = datetime.datetime.now(ZoneInfo(LOCAL_TZ_NAME)).isoformat()
    sample_matchup = "Chicago Cubs @ Milwaukee Brewers"
    sample_pk      = 999999

    tests = [
        ("game_start", webhooks["game_start"], {
            "event":     "game_start",
            "matchup":   sample_matchup,
            "game_pk":   sample_pk,
            "brewers":   0,
            "opponent":  0,
            "inning":    1,
            "timestamp": now,
        }),
        ("brewers_score", webhooks["brewers_score"], {
            "event":      "brewers_score",
            "matchup":    sample_matchup,
            "game_pk":    sample_pk,
            "brewers":    1,
            "opponent":   0,
            "inning":     3,
            "runs_added": 1,
            "timestamp":  now,
        }),
        ("game_end", webhooks["game_end"], {
            "event":     "game_end",
            "matchup":   sample_matchup,
            "game_pk":   sample_pk,
            "brewers":   4,
            "opponent":  2,
            "inning":    9,
            "result":    "win",
            "timestamp": now,
        }),
    ]

    for i, (name, url, payload) in enumerate(tests):
        log.info("--- Testing: %s ---", name)
        log.info("URL: %s", url)
        send_webhook(url, payload, dry_run)
        if i < len(tests) - 1:
            log.info("Waiting 30 seconds before next webhook...")
            time.sleep(30)

    log.info("=" * 60)
    log.info("Webhook test complete.")
    log.info("=" * 60)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Watch live Brewers games and fire webhooks on key events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 brewers_score_watcher.py \\\n"
            "      --webhook-start https://hooks.example.com/start \\\n"
            "      --webhook-score https://hooks.example.com/score \\\n"
            "      --webhook-end   https://hooks.example.com/end\n"
        ),
    )
    parser.add_argument(
        "--webhook-start",
        required=True,
        metavar="URL",
        help="Webhook URL to POST when the game starts.",
    )
    parser.add_argument(
        "--webhook-score",
        required=True,
        metavar="URL",
        help="Webhook URL to POST when the Brewers score.",
    )
    parser.add_argument(
        "--webhook-end",
        required=True,
        metavar="URL",
        help="Webhook URL to POST when the game ends.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=DEFAULT_POLL_SEC,
        metavar="SECS",
        help="Seconds between live score checks (default: {}).".format(DEFAULT_POLL_SEC),
    )
    parser.add_argument(
        "--pregame-interval",
        type=int,
        default=PREGAME_POLL_SEC,
        metavar="SECS",
        help="Seconds between checks while waiting for first pitch (default: {}).".format(PREGAME_POLL_SEC),
    )
    parser.add_argument(
        "--broadcast-delay",
        type=int,
        default=BROADCAST_DELAY_SEC,
        metavar="SECS",
        help="Seconds to wait after a Brewers score before firing the webhook (default: {}).".format(BROADCAST_DELAY_SEC),
    )
    parser.add_argument(
        "--post-webhook-delay",
        type=int,
        default=POST_WEBHOOK_DELAY,
        metavar="SECS",
        help="Seconds to wait after each webhook fires (default: {}).".format(POST_WEBHOOK_DELAY),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print webhook payloads without sending them.",
    )
    parser.add_argument(
        "--test-webhooks",
        action="store_true",
        help="Send a sample payload to each webhook URL and exit.",
    )
    args = parser.parse_args()

    webhooks = {
        "game_start":    args.webhook_start,
        "brewers_score": args.webhook_score,
        "game_end":      args.webhook_end,
    }

    log.info("=" * 60)
    log.info("Brewers Score Watcher started — PID %d", os.getpid())
    if args.dry_run:
        log.info("Mode         : DRY-RUN (no webhooks will be sent)")
    else:
        log.info("Webhook start: %s", args.webhook_start)
        log.info("Webhook score: %s", args.webhook_score)
        log.info("Webhook end  : %s", args.webhook_end)
    log.info("Poll interval: %ds live / %ds pregame", args.poll_interval, args.pregame_interval)
    log.info("Broadcast delay: %ds", args.broadcast_delay)
    log.info("Post-webhook delay: %ds", args.post_webhook_delay)
    log.info("Log file     : %s", LOG_FILE)
    log.info("=" * 60)

    if args.test_webhooks:
        test_webhooks(webhooks, dry_run=args.dry_run)
        return

    while not _shutdown:
        local_tz  = ZoneInfo(LOCAL_TZ_NAME)
        today_str = datetime.datetime.now(local_tz).strftime("%A %b %d")

        log.info("Checking for a Brewers game today (%s)...", today_str)
        game = get_todays_game()

        if game is None:
            log.info("No Brewers game today. Checking again in 1 hour.")
            if not short_sleep(3600):
                break
            continue

        log.info("Game found: %s @ %s — starts %s",
                 game["away"], game["home"],
                 game["start_local"].strftime("%I:%M %p %Z"))

        # Sleep until 5 min before first pitch if game hasn't started yet
        now_utc       = datetime.datetime.now(datetime.timezone.utc)
        secs_to_start = (game["start_utc"] - now_utc).total_seconds()

        if secs_to_start > 300:
            wake_at = game["start_utc"] - datetime.timedelta(minutes=5)
            h, rem  = divmod(int(secs_to_start), 3600)
            m       = rem // 60
            log.info("Game starts in %dh %dm — sleeping until 5 min before first pitch.", h, m)
            if not sleep_until(wake_at):
                break

        watch_game(game, webhooks, args.poll_interval, args.pregame_interval, args.broadcast_delay, args.post_webhook_delay, args.dry_run)

        if _shutdown:
            break

        # Sleep until just after midnight, then check for tomorrow's game
        local_tz         = ZoneInfo(LOCAL_TZ_NAME)
        now_local        = datetime.datetime.now(local_tz)
        midnight         = (now_local + datetime.timedelta(days=1)).replace(
            hour=0, minute=5, second=0, microsecond=0
        )
        secs_to_midnight = (midnight - now_local).total_seconds()
        log.info("Game over. Sleeping %.0f minutes until tomorrow's check.", secs_to_midnight / 60)
        if not short_sleep(secs_to_midnight):
            break

    log.info("Brewers Score Watcher stopped.")


if __name__ == "__main__":
    main()