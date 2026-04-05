#!/usr/bin/env python3
"""
brewers.py
----------
Single combined script: a long-running agent that manages the full Brewers
season schedule and launches the score watcher only during active games.

Architecture:
  - Agent  : fetches the season schedule, sleeps until each game, then
             launches the watcher as a subprocess at game time.
  - Watcher: polls the live linescore every N seconds and fires webhooks
             for game_start, brewers_score, and game_end events.
  - At game end the watcher exits and the agent sleeps until the next game.

Nothing runs between games — the agent is a lightweight sleeping process
that wakes up just before first pitch.

Usage:
    python3 brewers.py \
        --webhook-start https://hooks.example.com/start \
        --webhook-score https://hooks.example.com/score \
        --webhook-end   https://hooks.example.com/end

Flags:
    --webhook-start    URL   (required) POST when game starts
    --webhook-score    URL   (required) POST when Brewers score
    --webhook-end      URL   (required) POST when game ends
    --poll-interval    SECS  live score check interval      (default: 30)
    --pregame-interval SECS  pregame state check interval   (default: 60)
    --dry-run                print payloads, do not send webhooks
    --test-webhooks          fire sample payloads to all three URLs then exit
    --schedule               print upcoming games and exit

Run persistently:
    See systemd / launchd service templates at the bottom of this file.

Requirements: Python 3.9+ (stdlib only)
"""

import argparse
import datetime
import json
import logging
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOCAL_TZ_NAME    = "America/Chicago"
LOG_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brewers.log")
DEFAULT_POLL_SEC = 30
PREGAME_POLL_SEC = 60
WEBHOOK_TIMEOUT  = 10
BREWERS_TEAM_ID  = 158
BREWERS_ABBREV   = "MIL"
SCHEDULE_REFRESH_HOURS = 24
PREGAME_TIMEOUT_MINS   = 60     # bail out if game hasn't gone live this many minutes after scheduled start
BROADCAST_DELAY_SEC    = 0       # extra wait after score detected before webhook fires
POST_WEBHOOK_DELAY     = 30      # wait after any webhook fires to let it finish


# Hardcoded MLB team ID -> abbreviation lookup as fallback
# when the schedule API doesn't return abbreviations
MLB_TEAM_ABBREVS = {
    108: "LAA", 109: "AZ",  110: "BAL", 111: "BOS", 112: "CHC",
    113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
    118: "KC",  119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH",
    134: "PIT", 135: "SD",  136: "SEA", 137: "SF",  138: "STL",
    139: "TB",  140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
    144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}

def get_team_abbrev(team_dict):
    """Get team abbreviation from API response, falling back to lookup table."""
    abbrev = team_dict.get("abbreviation", "")
    if abbrev:
        return abbrev
    team_id = team_dict.get("id")
    return MLB_TEAM_ABBREVS.get(team_id, "OPP")

MLB_SCHEDULE_URL = (
    "https://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&teamId=158&gameType=R,S"
    "&hydrate=linescore,teams"
)
MLB_GAME_URL = "https://statsapi.mlb.com/api/v1/game/{game_pk}/linescore"

# Bypass SSL certificate verification (macOS python.org install quirk)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

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
_shutdown      = False
_watcher_proc  = None   # currently running watcher subprocess

def _handle_signal(signum, frame):
    global _shutdown
    log.info("Signal %s received — shutting down.", signum)
    _shutdown = True
    if _watcher_proc and _watcher_proc.poll() is None:
        log.info("Terminating watcher subprocess (PID %d).", _watcher_proc.pid)
        _watcher_proc.terminate()

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
    """POST JSON payload to a webhook URL. Logs result; never raises."""
    if dry_run:
        log.info("[DRY-RUN] Would POST to %s:\n%s", url, json.dumps(payload, indent=2))
        return
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "BrewersAgent/1.0"},
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

def fetch_season_schedule():
    """
    Fetch all upcoming Brewers games for the current season.
    Returns list of game dicts sorted by start time, nearest first.
    """
    local_tz = ZoneInfo(LOCAL_TZ_NAME)
    year     = datetime.datetime.now(local_tz).year
    url      = MLB_SCHEDULE_URL + "&season={}".format(year)

    log.info("Fetching season schedule from MLB Stats API (%d)...", year)
    data = http_get(url, timeout=15)
    if not data:
        return []

    games = []
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            teams   = game.get("teams", {})
            away_id = teams.get("away", {}).get("team", {}).get("id")
            home_id = teams.get("home", {}).get("team", {}).get("id")
            if BREWERS_TEAM_ID not in (away_id, home_id):
                continue

            raw_dt = game.get("gameDate", "")
            if not raw_dt:
                continue
            utc_dt = datetime.datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            if utc_dt <= now_utc:
                continue  # already started or finished

            local_dt = utc_dt.astimezone(ZoneInfo(LOCAL_TZ_NAME))
            games.append({
                "game_pk":     game["gamePk"],
                "home":        teams["home"]["team"]["name"],
                "away":        teams["away"]["team"]["name"],
                "home_abbrev":     get_team_abbrev(teams["home"]["team"]),
                "away_abbrev":     get_team_abbrev(teams["away"]["team"]),
                "brewers_are_home": teams["home"]["team"].get("id") == BREWERS_TEAM_ID,
                "utc_dt":          utc_dt,
                "local_dt":        local_dt,
                "label":           "{} @ {}  —  {}".format(
                    teams["away"]["team"]["name"],
                    teams["home"]["team"]["name"],
                    local_dt.strftime("%a %b %d %I:%M %p %Z"),
                ),
            })

    games.sort(key=lambda g: g["utc_dt"])
    log.info("Found %d upcoming game(s).", len(games))
    for g in games[:5]:
        log.info("  • %s", g["label"])
    if len(games) > 5:
        log.info("  ... and %d more.", len(games) - 5)
    return games



def is_game_finished(game_pk):
    """
    Check if a game is finished by querying the schedule API status field.
    Returns (is_finished, state, code, detailed).
    """
    url  = ("https://statsapi.mlb.com/api/v1/schedule"
            "?sportId=1&gamePks={}&hydrate=game(status)".format(game_pk))
    data = http_get(url)
    if not data:
        return False, False, "", "", ""

    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            if game.get("gamePk") != game_pk:
                continue
            status   = game.get("status", {})
            state    = status.get("abstractGameState", "").lower()
            code     = status.get("statusCode", "")
            detailed = status.get("detailedState", "").lower()
            # Exclude postponed/rescheduled/cancelled codes — these are not
            # legitimate game completions and should not trigger game_end webhook
            not_finished_codes = ("DR", "DI", "DC", "RR", "CR", "UR")
            is_postponed = (code in not_finished_codes
                            or "postponed" in detailed
                            or "rescheduled" in detailed
                            or "cancelled" in detailed)
            finished = (not is_postponed
                        and (state == "final"
                             or code in ("F", "O")
                             or "final" in detailed
                             or "completed" in detailed))
            return finished, is_postponed, state, code, detailed
    return False, False, "", "", ""

def get_linescore(game_pk):
    """Fetch the current linescore for a live game."""
    return http_get(MLB_GAME_URL.format(game_pk=game_pk))


def parse_score(linescore, game):
    """Return (mil_score, opp_score, state, inning)."""
    teams     = linescore.get("teams", {})
    home_runs = teams.get("home", {}).get("runs", 0) or 0
    away_runs = teams.get("away", {}).get("runs", 0) or 0

    if game.get("brewers_are_home", game["home_abbrev"] == BREWERS_ABBREV):
        mil_score, opp_score = home_runs, away_runs
    else:
        mil_score, opp_score = away_runs, home_runs

    inning       = linescore.get("currentInning", 0) or 0
    inning_half  = linescore.get("inningHalf", "").strip()
    outs         = linescore.get("outs", 0) or 0
    is_game_over = linescore.get("isGameOver", False)
    state_raw    = linescore.get("abstractGameState", "Preview").lower()

    # Use isGameOver or state=final as the game-over signal
    if is_game_over or state_raw == "final":
        state = "final"
    elif state_raw in ("live", "in progress") or inning > 0:
        state = "live"
    else:
        state = "preview"

    return mil_score, opp_score, state, inning, inning_half, outs, home_runs, away_runs

# ---------------------------------------------------------------------------
# Interruptible sleeps
# ---------------------------------------------------------------------------

def sleep_until(target_utc):
    """Sleep until target_utc. Returns True on arrival, False on shutdown."""
    while not _shutdown:
        secs = (target_utc - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if secs <= 0:
            return True
        time.sleep(min(30, secs))
    return False


def short_sleep(seconds):
    """Sleep in small chunks. Returns True normally, False on shutdown."""
    end = time.monotonic() + seconds
    while not _shutdown:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(5, remaining))
    return False

# ---------------------------------------------------------------------------
# Watcher  (runs in-process during an active game)
# ---------------------------------------------------------------------------

def watch_game(game, webhooks, poll_sec, pregame_sec, broadcast_delay, post_webhook_delay, dry_run):
    """
    Poll the linescore every poll_sec seconds for the duration of a game.
    Fires webhooks for: game_start, brewers_score, game_end.
    Returns when the game reaches Final state or shutdown is requested.
    """
    game_pk = game["game_pk"]
    matchup = "{} @ {}".format(game["away"], game["home"])
    log.info("Watcher active: %s  (gamePk %d)", matchup, game_pk)

    prev_mil_score = 0
    prev_state     = "preview"
    game_started   = False
    game_ended     = False
    first_poll     = True   # used to establish score baseline without triggering webhook
    pregame_start  = datetime.datetime.now(datetime.timezone.utc)  # track how long we've been in preview

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

        mil_score, opp_score, state, inning, inning_half, outs, home_runs, away_runs = parse_score(linescore, game)


        # ── Check schedule API for definitive game over status ───────────────
        finished, is_postponed, sched_state, sched_code, sched_detail = is_game_finished(game_pk)
        opp_abbrev = (game["away_abbrev"] or "OPP") if game.get("brewers_are_home") else (game["home_abbrev"] or "OPP")
        if game.get("brewers_are_home"):
            score_str = "MIL(home): {} (prev {})  {}(away): {}".format(mil_score, prev_mil_score, opp_abbrev, opp_score)
        else:
            score_str = "{}(home): {}  MIL(away): {} (prev {})".format(opp_abbrev, opp_score, mil_score, prev_mil_score)
        half_str = inning_half[:3].lower() if inning_half else ""
        inning_str = "{} {}".format(half_str, inning).strip() if half_str else str(inning)
        log.info("inning: %s  outs: %s  |  %s  |  %s (%s) [%s]",
                 inning_str, outs, score_str, sched_detail or sched_state, sched_code, sched_state)
        # ── Postponed / rescheduled — only exit if game hasn't actually started ──
        if is_postponed and not game_started and inning == 0:
            log.warning("Game %d appears postponed/rescheduled (%s — %s). Exiting watcher without firing game_end webhook.",
                        game_pk, sched_code, sched_detail)
            break
        elif is_postponed and (game_started or inning > 0):
            log.info("Schedule API shows postponed (%s) but game is active (inning %s) — ignoring postponed status.",
                     sched_code, inning)
            # If we're in the 9th or later with 3 outs, declare game over
            scheduled_innings = 9
            if game_started and inning >= scheduled_innings and outs >= 3:
                log.warning("Linescore shows inning %d with %d outs — declaring game over despite postponed status.",
                            inning, outs)
                game_ended = True
                result     = "win" if mil_score > opp_score else ("loss" if mil_score < opp_score else "tie")
                log.info("GAME FINAL (linescore): %s  |  MIL %d – %s %d  (%s)",
                         matchup, mil_score, opp_abbrev, opp_score, result.upper())
                payload = _base_payload("game_end", mil_score, opp_score, inning)
                payload["result"] = result
                send_webhook(webhooks["game_end"], payload, dry_run)
                if post_webhook_delay > 0:
                    log.info("Post-webhook delay — waiting %ds...", post_webhook_delay)
                    short_sleep(post_webhook_delay)
                break

        if game_started and finished:
            game_ended = True
            result     = "win" if mil_score > opp_score else ("loss" if mil_score < opp_score else "tie")
            log.info("GAME FINAL (schedule API): %s  |  MIL %d – %s %d  (%s)",
                     matchup, mil_score, opp_abbrev, opp_score, result.upper())
            payload = _base_payload("game_end", mil_score, opp_score, inning)
            payload["result"] = result
            send_webhook(webhooks["game_end"], payload, dry_run)
            if post_webhook_delay > 0:
                log.info("Post-webhook delay — waiting %ds...", post_webhook_delay)
                short_sleep(post_webhook_delay)
            break

        # ── Pregame timeout — bail out if stuck in preview too long ────────
        if not game_started and inning == 0:
            mins_waiting = (datetime.datetime.now(datetime.timezone.utc) - pregame_start).total_seconds() / 60
            if mins_waiting >= PREGAME_TIMEOUT_MINS:
                log.warning("Game has not started after %.0f minutes — possible postponement. Exiting watcher.", mins_waiting)
                break
            else:
                log.info("Pregame — waiting for game to start (%.0f/%d min timeout).", mins_waiting, PREGAME_TIMEOUT_MINS)

        # ── Game start ────────────────────────────────────────────────────
        # Also trigger on inning > 0 in case API is slow to flip state to live
        if (state == "live" or inning > 0) and prev_state == "preview" and not game_started:
            game_started = True
            log.info("GAME START: %s  |  MIL %d – %s %d  (inning %d)",
                     matchup, mil_score, opp_abbrev, opp_score, inning)
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
            log.info("BREWERS SCORE! +%d run(s) — MIL %d -> %d, %s %d  (inning %d)",
                     runs_added, prev_mil_score, mil_score, opp_abbrev, opp_score, inning)
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
            log.info("GAME FINAL: %s  |  MIL %d – %s %d  (%s)",
                     matchup, mil_score, opp_abbrev, opp_score, result.upper())
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

    log.info("Watcher done: game %d.", game_pk)

# ---------------------------------------------------------------------------
# Webhook tester
# ---------------------------------------------------------------------------

def test_webhooks(webhooks, dry_run=False):
    """Fire a sample payload to each webhook URL with a 30s gap between each."""
    log.info("=" * 60)
    log.info("Testing all three webhooks...")
    log.info("=" * 60)

    now            = datetime.datetime.now(ZoneInfo(LOCAL_TZ_NAME)).isoformat()
    sample_matchup = "Chicago Cubs @ Milwaukee Brewers"
    sample_pk      = 999999

    tests = [
        ("game_start", webhooks["game_start"], {
            "event": "game_start", "matchup": sample_matchup, "game_pk": sample_pk,
            "brewers": 0, "opponent": 0, "inning": 1, "timestamp": now,
        }),
        ("brewers_score", webhooks["brewers_score"], {
            "event": "brewers_score", "matchup": sample_matchup, "game_pk": sample_pk,
            "brewers": 1, "opponent": 0, "inning": 3, "runs_added": 1, "timestamp": now,
        }),
        ("game_end", webhooks["game_end"], {
            "event": "game_end", "matchup": sample_matchup, "game_pk": sample_pk,
            "brewers": 4, "opponent": 2, "inning": 9, "result": "win", "timestamp": now,
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
# Agent loop  (season-level scheduler)
# ---------------------------------------------------------------------------


def get_live_game():
    """
    Check if there is a Brewers game currently in progress right now.
    Returns a game dict if found, None otherwise.
    """
    local_tz  = ZoneInfo(LOCAL_TZ_NAME)
    today_str = datetime.datetime.now(local_tz).strftime("%Y-%m-%d")
    url       = MLB_SCHEDULE_URL + "&date=" + today_str

    log.info("Checking for a game currently in progress...")
    data = http_get(url, timeout=15)
    if not data:
        return None

    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            teams   = game.get("teams", {})
            away_id = teams.get("away", {}).get("team", {}).get("id")
            home_id = teams.get("home", {}).get("team", {}).get("id")
            if BREWERS_TEAM_ID not in (away_id, home_id):
                continue

            state = game.get("status", {}).get("abstractGameState", "")
            if state not in ("Live", "In Progress"):
                continue

            raw_dt   = game.get("gameDate", "")
            utc_dt   = datetime.datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            local_dt = utc_dt.astimezone(ZoneInfo(LOCAL_TZ_NAME))

            log.info("Found game in progress: %s @ %s",
                     teams["away"]["team"]["name"], teams["home"]["team"]["name"])

            return {
                "game_pk":     game["gamePk"],
                "home":        teams["home"]["team"]["name"],
                "away":        teams["away"]["team"]["name"],
                "home_abbrev":     get_team_abbrev(teams["home"]["team"]),
                "away_abbrev":     get_team_abbrev(teams["away"]["team"]),
                "brewers_are_home": teams["home"]["team"].get("id") == BREWERS_TEAM_ID,
                "utc_dt":          utc_dt,
                "local_dt":        local_dt,
                "label":           "{} @ {}  —  {}".format(
                    teams["away"]["team"]["name"],
                    teams["home"]["team"]["name"],
                    local_dt.strftime("%a %b %d %I:%M %p %Z"),
                ),
            }
    return None

def run_agent(webhooks, poll_sec, pregame_sec, broadcast_delay, post_webhook_delay, dry_run):
    """
    Main agent loop:
      1. Fetch the full season schedule.
      2. Sleep until 5 minutes before the next game's start time.
      3. Hand off to watch_game() for the duration of the game.
      4. Advance to the next game. Refresh schedule every 24 hours.
    """
    log.info("Agent active. Checking for game in progress...")
    live_game = get_live_game()
    if live_game:
        log.info("Resuming mid-game: %s", live_game["label"])
        log.info("=" * 60)
        log.info("GAME TIME: %s", live_game["label"])
        log.info("=" * 60)
        watch_game(live_game, webhooks, poll_sec, pregame_sec, broadcast_delay, post_webhook_delay, dry_run)
        if _shutdown:
            log.info("Agent stopped.")
            return
        log.info("Game over. Continuing to season schedule.")

    log.info("Fetching season schedule...")
    games        = fetch_season_schedule()
    last_refresh = datetime.datetime.now(datetime.timezone.utc)
    game_index   = 0

    while not _shutdown:

        # Periodic schedule refresh to catch postponements / additions
        now_utc  = datetime.datetime.now(datetime.timezone.utc)
        age_hrs  = (now_utc - last_refresh).total_seconds() / 3600
        if age_hrs >= SCHEDULE_REFRESH_HOURS:
            log.info("Refreshing schedule (%.1f hours since last fetch)...", age_hrs)
            fresh = fetch_season_schedule()
            if fresh:
                games      = fresh
                game_index = 0
            last_refresh = now_utc

        # Skip any games that have already started or passed
        while game_index < len(games) and \
              (games[game_index]["utc_dt"] - datetime.datetime.now(datetime.timezone.utc)).total_seconds() < -300:
            log.info("Skipping past game: %s", games[game_index]["label"])
            game_index += 1

        if game_index >= len(games):
            log.info("No more games in schedule. Sleeping 24h then re-checking...")
            if not short_sleep(86400):
                break
            fresh = fetch_season_schedule()
            if fresh:
                games      = fresh
                game_index = 0
            last_refresh = datetime.datetime.now(datetime.timezone.utc)
            continue

        next_game = games[game_index]
        secs      = (next_game["utc_dt"] - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        h, rem    = divmod(max(0, int(secs)), 3600)
        m         = rem // 60

        log.info("Next game : %s", next_game["label"])

        # Sleep until 5 minutes before first pitch
        if secs > 300:
            wake_at = next_game["utc_dt"] - datetime.timedelta(minutes=5)
            log.info("Sleeping  : %dh %dm until 5 min before first pitch.", h, m)
            if not sleep_until(wake_at):
                break
            log.info("Waking up — game starts in ~5 minutes.")

        # ── Active game window ────────────────────────────────────────────
        log.info("=" * 60)
        log.info("GAME TIME: %s", next_game["label"])
        log.info("=" * 60)

        watch_game(next_game, webhooks, poll_sec, pregame_sec, broadcast_delay, post_webhook_delay, dry_run)

        if _shutdown:
            break

        log.info("Game over. Advancing to next game.")
        game_index += 1

        # Brief pause before looping back so any final API state settles
        if not short_sleep(60):
            break

    log.info("Agent stopped.")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Brewers season agent — schedules and monitors every game.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 brewers.py \\\n"
            "      --webhook-start https://hooks.example.com/start \\\n"
            "      --webhook-score https://hooks.example.com/score \\\n"
            "      --webhook-end   https://hooks.example.com/end\n"
        ),
    )
    parser.add_argument("--webhook-start",    required=True,  metavar="URL",
                        help="Webhook URL to POST when the game starts.")
    parser.add_argument("--webhook-score",    required=True,  metavar="URL",
                        help="Webhook URL to POST when the Brewers score.")
    parser.add_argument("--webhook-end",      required=True,  metavar="URL",
                        help="Webhook URL to POST when the game ends.")
    parser.add_argument("--poll-interval",    type=int, default=DEFAULT_POLL_SEC, metavar="SECS",
                        help="Live score check interval in seconds (default: {}).".format(DEFAULT_POLL_SEC))
    parser.add_argument("--pregame-interval", type=int, default=PREGAME_POLL_SEC, metavar="SECS",
                        help="Pregame state check interval in seconds (default: {}).".format(PREGAME_POLL_SEC))
    parser.add_argument("--broadcast-delay",  type=int, default=BROADCAST_DELAY_SEC, metavar="SECS",
                        help="Seconds to wait after a Brewers score before firing the webhook (default: {}).".format(BROADCAST_DELAY_SEC))
    parser.add_argument("--post-webhook-delay", type=int, default=POST_WEBHOOK_DELAY, metavar="SECS",
                        help="Seconds to wait after each webhook fires (default: {}).".format(POST_WEBHOOK_DELAY))
    parser.add_argument("--dry-run",          action="store_true",
                        help="Print webhook payloads without sending them.")
    parser.add_argument("--test-webhooks",    action="store_true",
                        help="Fire sample payloads to all three URLs then exit.")
    parser.add_argument("--schedule",         action="store_true",
                        help="Print upcoming games with countdowns and exit.")
    args = parser.parse_args()

    webhooks = {
        "game_start":    args.webhook_start,
        "brewers_score": args.webhook_score,
        "game_end":      args.webhook_end,
    }

    log.info("=" * 60)
    log.info("Brewers Agent started — PID %d", os.getpid())
    if args.dry_run:
        log.info("Mode          : DRY-RUN (no webhooks will be sent)")
    else:
        log.info("Webhook start : %s", args.webhook_start)
        log.info("Webhook score : %s", args.webhook_score)
        log.info("Webhook end   : %s", args.webhook_end)
    log.info("Poll interval : %ds live / %ds pregame", args.poll_interval, args.pregame_interval)
    log.info("Broadcast delay: %ds", args.broadcast_delay)
    log.info("Post-webhook delay: %ds", args.post_webhook_delay)
    log.info("Log file      : %s", LOG_FILE)
    log.info("=" * 60)

    # ── One-shot modes ────────────────────────────────────────────────────
    if args.test_webhooks:
        test_webhooks(webhooks, dry_run=args.dry_run)
        return

    if args.schedule:
        games = fetch_season_schedule()
        if not games:
            log.info("No upcoming games found.")
            return
        log.info("Upcoming Brewers games:")
        for g in games:
            secs   = max(0, (g["utc_dt"] - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
            h, rem = divmod(int(secs), 3600)
            m      = rem // 60
            log.info("  %s  (in %dh %dm)", g["label"], h, m)
        return

    # ── Main agent loop ───────────────────────────────────────────────────
    run_agent(webhooks, args.poll_interval, args.pregame_interval, args.broadcast_delay, args.post_webhook_delay, args.dry_run)


if __name__ == "__main__":
    main()


# =============================================================================
# SERVICE FILE TEMPLATES
# =============================================================================
#
# ── Linux (systemd) ──────────────────────────────────────────────────────────
# Save as: /etc/systemd/system/brewers.service
# Then run:
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now brewers
#
# [Unit]
# Description=Brewers Season Agent
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# Type=simple
# User=YOUR_USERNAME
# ExecStart=/usr/bin/python3 /path/to/brewers.py \
#     --webhook-start https://hooks.example.com/start \
#     --webhook-score https://hooks.example.com/score \
#     --webhook-end   https://hooks.example.com/end
# Restart=on-failure
# RestartSec=30
# StandardOutput=append:/path/to/brewers.log
# StandardError=append:/path/to/brewers.log
#
# [Install]
# WantedBy=multi-user.target
#
# =============================================================================
#
# ── macOS (launchd) ──────────────────────────────────────────────────────────
# Save as: ~/Library/LaunchAgents/com.brewers.agent.plist
# Then run:
#   launchctl load ~/Library/LaunchAgents/com.brewers.agent.plist
#
# <?xml version="1.0" encoding="UTF-8"?>
# <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#   "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
# <plist version="1.0">
# <dict>
#   <key>Label</key>             <string>com.brewers.agent</string>
#   <key>ProgramArguments</key>
#   <array>
#     <string>/usr/bin/python3</string>
#     <string>/path/to/brewers.py</string>
#     <string>--webhook-start</string> <string>https://hooks.example.com/start</string>
#     <string>--webhook-score</string> <string>https://hooks.example.com/score</string>
#     <string>--webhook-end</string>   <string>https://hooks.example.com/end</string>
#   </array>
#   <key>RunAtLoad</key>         <true/>
#   <key>KeepAlive</key>         <true/>
#   <key>StandardOutPath</key>   <string>/path/to/brewers.log</string>
#   <key>StandardErrorPath</key> <string>/path/to/brewers.log</string>
# </dict>
# </plist>
# =============================================================================