"""SOL-Watch notification bridge: polls the SOL API and posts changes to Discord.

Standalone: needs only Python 3.9+, the `requests` package and a .env file.
See README.md beside this file for setup on Linux.

Files it writes next to itself (none of them with --once):
    logs/bridge.log        everything it prints (rotated, 5 x 5 MB)
    logs/errors.log        only problems, with tracebacks (rotated, 3 x 2 MB)
    logs/new-fields.log    field names the API returned for the first time
    logs/known-fields.json the field names seen so far, per endpoint
    logs/spec/             each new version of the API spec, and changes.log
    state.json             what has been announced, so a restart repeats nothing
    sol_watch.lock         stops a second copy running (Linux only)
    captures/              raw JSON of item types never seen before

Your adjustable settings go in settings.ini next to this script (optional; copy settings.example.ini).
Secrets come from a .env file next to this script (or one folder up), or from
real environment variables, which win:
    SOL_BRIDGE_API_KEY   SOL API key (SOL_API_KEY is accepted too)
    WEBHOOK_CORP         Discord webhook for research, fleet and mining
    WEBHOOK_GENERAL      Discord webhook for contracts, defense and global chat
    WEBHOOK_ALLIANCE     Discord webhook for alliance chat
    IRL_TIMEZONE         optional, e.g. Europe/London; default is the machine's zone

Usage:
    python3 sol_watch.py              # asks whether to broadcast startup baselines
    python3 sol_watch.py --silent     # startup baselines to the log only (use for services)
    python3 sol_watch.py --broadcast  # startup baselines to Discord too
    python3 sol_watch.py --once       # baselines plus one poll of everything, then exit
                                      # (never posts to Discord unless --broadcast)

API behaviour this script relies on (checked against the live API, Nov 2026):
    - Read-only API, Bearer key auth, 60 reads/min per key with bursts of 30.
      Going over returns 429 with Retry-After (seconds).
    - Invalid or revoked keys get 429 "Too many bad keys" for ~5 min, not 401.
    - ETags are weak (W/"...") and only produce 304 when sent back WITHOUT "W/".
    - Money is in $M. Game time is in days since J2000 (2000-01-01 12:00 UTC);
      /clock's timeScale is game days per real day (24).
    - /craft missions[].status: transit, active, complete, failed, derelict
      (plus preparing/decaying per the spec). missions[].rangeQueued is true while a
      launch waits for its spaceport's range (never seen true yet, so unverified).
    - /craft safeMode[] items are {eventId, missionId, auto}; eventId is the
      /defense hazardIncidents[].id that caused it (a flare safes dozens at once).
    - /defense hazardIncidents[] items are {id, hazardId, forecastDay, startDay,
      endDay, hits}. Flares are forecast 3 game days (3 real hours) before startDay.
    - /contracts offers[] have tier newcomer/mid/leader, or no tier key at all.
      won[] lists {contractId, companyId, day} for offers won by anyone.
      siegeContracts[] items are {id, kind, siteId, targetId, reward, postedDay,
      expiresDay, factionId}; targetId is the company under siege.
    - /research shows only the current job. The research queue is /company queues[]
      with list "research"; other queues there carry a "reason" when blocked.
    - /industry/flows sites[].heldBy: input, staffing, upkeep, keepLimit, paused.
    - /fleets/map fleetMoves[]: {id, companyId, fromId, toId, units, departDay, arriveDay}.
      Battle lists (battles, engagements, sieges, engagementOrders, duels, ourSieges)
      had never been non-empty by Nov 2026: the first item of each is saved to captures/.
    - /factions standing{} is by faction id; a faction can have standing before it
      has emerged (mars_republic). There is no UEG standing and no bloodmark field yet.
    - Chat, loss and hazard-damage ids are integers that only grow.
"""

import bisect
import hashlib
import json
import logging
import math
import os
import re
import socket
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

import requests

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore[misc,assignment]

VERSION = "1.07.13"
HERE = Path(__file__).resolve().parent
SETTINGS_FILE = "settings.ini"  # your own settings (optional); see settings.example.ini
MIN_POLL_SECONDS = 10           # settings.ini can't poll anything more often than this
READS_PER_MINUTE_WARNING = 40   # warn when the intervals add up to more API reads a minute than this
LOG_DIR = HERE / "logs"
API_BASE = "https://solthegame.com/api/v1"

# ================= SETTINGS =================
# These are the defaults. Change them in settings.ini (see settings.example.ini), not here: an upgrade
# replaces this file. Settings with no settings.ini key describe the game itself and change with new versions.
# "newcomer" shows every tier, "mid" hides newcomer offers, "leader" shows only
# leader offers. Untiered (faction/special) offers are always shown.
CONTRACT_FILTER_TIER = "mid"

# Seconds between polls of each watcher.
INTERVAL_CHAT = 15
INTERVAL_CRAFT = 20
INTERVAL_MINING = 45
INTERVAL_CONTRACTS = 60
INTERVAL_HISTORY = 120
INTERVAL_RESEARCH = 60
INTERVAL_DEFENSE = 60
INTERVAL_COMPANY = 60     # queues, crew supplies, construction
INTERVAL_INDUSTRY = 300   # storage, stocks running out, production stalls
INTERVAL_FLEET_MAP = 30   # rival fleets heading our way, battles anywhere
INTERVAL_FLEETS = 60      # our battles, duels and sieges
INTERVAL_SLOW = 300       # factions, alliance, account, discoveries
INTERVAL_DIPLOMACY = 60   # deals and agreements (offers expire, so check often)

# Warn this many real minutes before an offer waiting on us expires.
DEAL_EXPIRY_WARNING_MINUTES = 60
INTERVAL_RECORDS = 600    # badges and firsts
INTERVAL_NEWS = 300       # Solar News: pirate raids and thefts, salvages of our derelicts
INTERVAL_PAD_GOALS = 300  # spaceports asking every company for a new launch pad's materials (/ueg)
INTERVAL_PLACES = 21600   # the game's list of places, which can grow without a game update (every 6 hours)
# Valuable derelicts of other companies: each check downloads the whole craft map (~1.3 MB), so hourly.
# DERELICT_CHECK_MINUTES and DERELICT_MIN_VALUE in .env override these two (and survive upgrades).
INTERVAL_DERELICTS = 3600
DERELICT_MIN_INTERVAL = 600  # never more often than every 10 minutes, whatever .env says
# Post another company's derelict when its salvage value (lootValue, $M) is at least this (0: every one).
# None turns it off.
DERELICT_ALERT_MIN_VALUE: Optional[float] = 0
INTERVAL_API_SPEC = 3600  # watch the API itself for new fields
INTERVAL_GAME_SITE = 300  # the game's web page, to spot game updates
# Post each new game build to Discord. False: game updates are only logged (and counted in the daily status);
# a change to the API they bring is still posted, by the API spec check.
GAME_UPDATE_POSTS = False
# Post new places (landing sites, stations) the game adds. False: they're only logged; names refresh either way.
NEW_PLACE_POSTS = False
GAME_SITE_URL = "https://solthegame.com/"
# The web client's code files carry a fingerprint that changes with each build: assets/index-LPebQ6Gw.js
GAME_BUNDLE = re.compile(r"assets/[\w.-]+\.(?:js|css)")
# After an update, wait this long (the server restarts) before re-checking the API spec.
SPEC_RECHECK_AFTER_UPDATE_SECONDS = 120

# Bridge updates: at startup and once a day, read the VERSION line of sol_watch.py on GitHub (one small
# request to GitHub, no key or game data sent) and post to the Corp channel while a newer version exists.
# False turns it off. A fork can point BRIDGE_SOURCE_URL and BRIDGE_PROJECT_URL at its own project.
UPDATE_CHECK = True
INTERVAL_UPDATE_CHECK = 86400
BRIDGE_PROJECT_URL = "https://github.com/Jomangork/sol-watch-bridge"
BRIDGE_SOURCE_URL = "https://raw.githubusercontent.com/Jomangork/sol-watch-bridge/main/sol_watch.py"
# A restart within this many seconds of the last notice doesn't repeat it (e.g. several restarts in a row).
UPDATE_NOTICE_MIN_GAP_SECONDS = 3600

# Warn when something will run out or fill up within these many real hours.
# (1 game day = 1 real hour, so these match the game's own supply warnings.)
COUNTDOWN_WARNINGS_HOURS = (72, 48, 24, 12, 4, 1)
BALLOT_WARNINGS_HOURS = (24, 12, 1)  # real hours before Earth's ballot closes, in both modes
# Earth's ballot (glossary "Ballot"): opens each game year on 1 January, nominations for 120 game days, then
# voting for 168. Lite mode can't read /factions, so it works the close out from these.
BALLOT_NOMINATION_DAYS, BALLOT_VOTING_DAYS = 120, 168
# An Ambition done (build HML7qYMP): the company with the most stages names its colony, within a time limit
# (nameUntil). When that's us, it's posted at once and again this many real hours before the limit.
NAMING_WARNINGS_HOURS = (24,)
# A stock's warnings are given again only after a refill: at least this many times the amount it had
# when last warned. A stock made and used at the same rate wobbles, and would otherwise be warned again and again.
COUNTDOWN_REFILL_FACTOR = 1.25
# A stock running out within this many real hours is already empty for practical purposes
# (e.g. "runs out in 5m" with 0.5 t left): its warning is left to the production-stall alert.
STOCK_ALREADY_EMPTY_HOURS = 0.25
# Nor when what's left shows as 0.0: a crumb used a crumb at a time can still last hours ("0.0 left, using 0.00/day").
STOCK_ALREADY_EMPTY_QTY = 0.05

# Queue reasons that only mean "waiting for the level before this one" - not problems.
# Queue reasons that only report progress, not a block.
IGNORED_QUEUE_REASONS = re.compile(r"^(Level \d+ is being built|Your construction crews are building .*)$")
# An upgrade bill that disappears within this many game days of its readyDay finished
# (one that disappears earlier was cancelled).
UPGRADE_DONE_SLACK_DAYS = 0.1

# Production stalls that are our own choice, not problems.
IGNORED_STALL_REASONS = {"paused", "keepLimit"}
KEEP_LIMIT_SLACK = 0.01  # a site "held by staffing" whose own good is within this of our keep limit is full, not a stall
# Off Earth, a place out of either of these stops all its production; a crew shortage only slows
# refineries and factories, and mines run without full crew (player, 2026-11-05).
UPKEEP_GOODS = ("consumables", "equipment")
CREWED_SITE_KINDS = ("refinery", "factory")
# Don't re-announce the same site stalling within this many real hours.
STALL_REPEAT_HOURS = 6
# A site still producing is posted as slowed only below this share of its full output (nominalPerDay), and
# once: again only if it falls this many points further, or after running above it for this many real hours.
# A refinery that uses more ore than its own mine digs runs short for good, and drops on and off the game's
# list as a little ore piles up; at full output the game may still name an upkeep good (build Ll3R8T5j).
SLOWED_POST_BELOW_PERCENT = 90
SLOWED_WORSE_BY_POINTS = 10
SLOWED_RECOVERY_HOURS = 24

# Rival warships heading for our places. Earth and Moon space (LEO station, Lunar orbit station, the Moon,
# the Earth–Moon Lagrange points: places whose body is Earth or the Moon) is UEG space, where no fighting is
# allowed, so arrivals there are routine traffic and only logged. While we're marked hostile to an alliance
# (a spy of ours caught) or branded a terrorist, every arrival alerts, wherever it is. False alerts there too.
INCOMING_IGNORE_EARTH_MOON = True
INCOMING_IGNORE_PLACES = set()  # type: set  # more place ids to ignore, e.g. {"pallas/b"}
INCOMING_IGNORE_COMPANIES = {"UEG Navy"}  # the government's NPC navy
INCOMING_THREAT_ROLES = {"combat", "capital"}  # unit roles from /reference/military

# Report all faction standings when any one moves at least this much.
STANDING_ALERT_STEP = 1.0
# Display names for faction ids. Unknown ids are shown as they are.
FACTION_NAMES = {
    "concord": "C-7 (Concord of Seven)",
    "morningstar": "Morningstar Aerospace",
    "wsa": "WSA (Western Space Alliance)",
    "zvezda": "Zvezda Space Directorate",
    "belt_compact": "UBN (Unified Belter Nation)",
    "mars_republic": "Mars Republic",
    "clear_sky": "Clear Sky",
}
# Watch faction and company data for these words appearing (Clear Sky's bloodmark
# isn't in the API yet; this announces it the moment it is).
WATCH_WORDS = re.compile(r"blood|clear.?sky", re.IGNORECASE)

# Standing advisor: what a contract does to our standing. The API has none of this;
# it comes from the game's own standing logs (Nov 2026).
# Each faction's rivals, as the game's Factions table lists them.
FACTION_RIVALS = {
    "belt_compact": {"wsa", "zvezda", "morningstar", "concord"},
    "concord": {"morningstar", "belt_compact"},
    "morningstar": {"concord", "belt_compact"},
    "wsa": {"zvezda", "belt_compact"},
    "zvezda": {"wsa", "belt_compact"},
}
# Standing the client gives: alliance jobs by level, and a full contract (a board contract
# delivered, a ship order filled, or surrendering to its siege).
STANDING_GAIN = {"easy": 2, "medium": 4, "hard": 10, "contract": 10}
# What each of the client's rivals takes away, for hard jobs and full contracts only
# (easy and medium alliance jobs cost rivals nothing).
RIVAL_PENALTY = 5
# A faction at or below this refuses to let us complete its contracts.
REFUSES_CONTRACTS_AT = -50
# Standing pace (game build CJPZkdZn): a gain is worth 1 − standing/120 of itself, and each faction (and the UEG)
# gives at most a set amount a real day before that, enough to climb from 0 to its top tier in 30 real days
# (faction Paragon 90, UEG Guardian 75). Losses are never limited. /factions standingPaceToday is what we've used.
STANDING_PACE_FALLOFF = 120
STANDING_PACE_REAL_DAYS = 30
STANDING_PACE_TOP = {"faction": 90, "ueg": 75}

# New faction alliance jobs: the easiest level worth announcing ("easy", "medium" or "hard"),
# and how many arriving at once become one digest instead of separate posts.
ALLIANCE_JOB_MIN_LEVEL = "easy"
ALLIANCE_JOB_DIGEST_AT = 3
JOB_LEVELS = {"easy": 0, "medium": 1, "hard": 2}

# Real seconds of warning before research completes.
RESEARCH_WARNING_SECONDS = 300
# The game can take a few minutes to start the next queued research; the lab only counts as idle after this.
RESEARCH_START_GRACE_SECONDS = 600

# Thieves take idle, uncrewed ships at Earth and the Moon; the news says only "A ship slipped its moorings around
# Earth and the Moon" (seen 2026-11-07 and 11-08), never whose. After one, our ship totals from before it are compared
# with ours after it for up to this long. Snapshots older than SHIP_HISTORY_SECONDS are dropped.
THEFT_CHECK_SECONDS = 1800
SHIP_HISTORY_SECONDS = 7200
THEFT_NEWS = "slipped its moorings"
# "Crews at <company>’s <place> outpost walked off the job in support of Clear Sky. Its output stops until 2032-09-21."
WALKOUT_RE = re.compile(r"Crews at (?P<company>.+?)[’']s (?P<place>.+?) outpost walked off the job.*?"
                        r"until (?P<until>\d{4}-\d{2}-\d{2})", re.S)
# Market events (shortages and gluts in one region, from the news) are only logged for now, as a test; True posts them.
MARKET_EVENT_POSTS = False

# Lite mode (since 1.07.00): only what every player sees alike, for a channel anyone may read. It runs from a key
# with just News and records and Public chat, and reads nothing else. None: lite when the key can't read Company;
# settings.ini [bridge] mode = full or lite decides it instead.
BRIDGE_MODE: Optional[str] = None
LITE_SECTIONS = {"news", "chat"}
# The only API paths lite mode reads ("" is the key's index); any other read is refused.
LITE_PATHS = ("", "/news", "/chat", "/companies", "/records", "/memorial", "/clock", "/reference")
INTERVAL_LITE_COMPANIES = 600  # name changes, bully marks and the earnings leaderboard
# Solar News kinds lite mode posts, with their icon. A kind not listed in either table is new: posted with 📰.
LITE_NEWS_ICONS = {
    "raid": "🏴‍☠️", "hazard": "☀️", "faction": "🛒", "segment": "🚩", "politics": "🗳️", "holiday": "🎉",
    "strike": "💣", "clear_sky": "✊", "founded": "🆕", "inactive": "🌑", "alliance": "🤝", "colony": "🏘️",
    "station": "🛰️", "outpost": "📍", "range_expansion": "🚀", "ueg_built": "🏗️", "ueg_navy": "⚓",
    "ueg_elevator": "🏛️", "badge_first": "🏅", "project_honor": "🏆", "market_event": "📈", "salvage": "♻️",
    "abandoned": "🛸", "launch_failure": "💥", "outpost_surrendered": "🏳️", "return": "🔙", "terrorist_hunt": "⚔️",
    "frontier": "🏺", "battle_begun": "⚔️", "attack_repelled": "🛡️",
    "siege_laid": "🏰"}
# Kinds only counted in the digest: frequent, or posted from elsewhere (crew lost comes from the memorial).
# Hazard items that hit one craft count here too; flare forecasts are posted.
LITE_NEWS_NOISE = {  # kind: (one, several)
    "contract": ("contract won", "contracts won"), "hazard": ("hazard hit", "hazard hits"),
    "price_move": ("price move", "price moves"), "launch": ("launch", "launches"), "arrival": ("arrival", "arrivals"),
    "ueg_supplies": ("UEG supply run", "UEG supply runs"), "first": ("world first", "world firsts"),
    "ore_body": ("asteroid move", "asteroid moves"), "discovery": ("discovery", "discoveries"),
    "crew_lost": ("crew-lost item (posted from the memorial)", "crew-lost items (posted from the memorial)")}
# Kinds cut to their first sentence: the rest (what a failed launch carried) is detail to look up in the game.
LITE_FIRST_SENTENCE_ONLY = {"launch_failure"}
# Faction news about an Ambition (build HML7qYMP: "The Concord of Seven announced its next Ambition: Titan Waystation.")
# gets its own icon, not the faction kind's 🛒.
# Its end names no Ambition: "The Western Space Alliance finished Europa Ice Works, built with Some Company and …".
LITE_AMBITION_ICON = "🎯"
LITE_AMBITION_RE = re.compile(r"\bAmbition\b| finished .+, built with ")
# A faction's colony on its way (1.07.08): "Pwyll has its habitat. WSA now wants 20 settlers there.", "… opened its
# colony at Pwyll, Europa." Posted with the colony kind's icon, not 🛒.
LITE_FACTION_COLONY_RE = re.compile(r"\bcolony\b|\bsettlers\b|\bhas its habitat\b")
# The game announces a new UEG Navy ship more than once ("handed UES Amazon over …", later the same again, or
# "took delivery of" / "commissioned" in other words). Only the first naming a ship is posted; the names are kept.
LITE_NAVY_NEW_SHIP_RE = re.compile(r"\b(handed|took delivery|commissioned|joined the UEG Navy)\b")
LITE_NAVY_SHIP_NAME_RE = re.compile(r"\bUES ([A-Z][\w'’-]*(?: [A-Z][\w'’-]*)*)")
LITE_NAVY_SHIPS_KEPT = 300
# "Some Company was first to recover a tally sheet at Ceres orbit." Several from one company in a poll: one line.
LITE_CODEX_FIRST_RE = re.compile(r"^(?P<company>.+?) was first to recover (?P<what>.+) at (?P<where>[^.]+?)\.?$")
LITE_BALLOT_OPEN_RE = re.compile(r"Earth[’']s ballot is open: (?P<options>.+)")
LITE_FORECAST_RE = re.compile(r"Space Weather Office|forecasts? a solar flare", re.IGNORECASE)

# The read limit is 60/min per key (burst 30); spacing every request keeps us
# far below it even when several watchers fall due at once.
REQUEST_SPACING_SECONDS = 1.0
CLOCK_REFRESH_SECONDS = 300
DISCORD_MAX_LENGTH = 1900  # Discord rejects messages over 2000 characters
DISCORD_RETRY_SECONDS = 60  # while Discord is unreachable, try the queue again this often
DISCORD_QUEUE_MAX = 200     # messages kept while Discord is unreachable (oldest dropped first)
STARTUP_RETRY_MAX_SECONDS = 300

# The API key sections the bridge reads (ids from the /api/v1 index), what each feeds, and its endpoints.
# Tested 2026-11-04 with one-section keys: each section unlocks exactly its endpoints. "required": the bridge
# can't start without it. Market and Segments aren't read.
KEY_SECTIONS = (
    ("company", "Company", "required", ("/me", "/company"),
     "the bridge can't start without it; also blocked queues, crew supplies, construction"),
    ("news", "News and records", "needed", ("/companies", "/news", "/records"),
     "company names in every post, pirate raids and thefts, salvage and faction-purchase news, badges and firsts"),
    ("craft", "Launches and craft", "needed", ("/craft", "/craft/map"),
     "launches, arrivals, losses, safe mode, derelicts"),
    ("fleets", "Fleets", "needed", ("/fleets", "/fleets/map"), "incoming fleets, battles, our sieges and duels"),
    ("contracts", "Contracts", "needed", ("/contracts", "/contracts/history"),
     "contract board, alliance jobs, ship orders, sieges against us, closed contracts"),
    ("defense", "Defense", "needed", ("/defense",), "hazards, strikes, debris fields, asteroid threats"),
    ("segments", "Segments", "needed", ("/segments",),
     "tax and permit rises where our craft are, rivals taking over our claims"),
    ("factions", "Factions", "needed", ("/factions",), "standings, Halcyon Arms Exchange, Earth's government, diplomacy"),
    ("industry", "Industry", "needed", ("/industry", "/industry/flows"),
     "stocks running out, storage filling, production stalls"),
    ("research", "Research", "needed", ("/research",), "research alerts"),
    ("mining", "Mining", "needed", ("/mining", "/mining/bodies"), "mining and ore running out"),
    ("ueg", "UEG", "needed", ("/ueg",), "pad goals"),
    ("alliance", "Alliance", "needed", ("/alliance",), "spies caught"),
    ("discoveries", "Discoveries", "needed", ("/discoveries",), "alliances marking us hostile"),
    ("chat", "Public chat", "needed", ("/chat",), "global chat relay"),
    # Since 2026-11-05 /chat includes alliance messages only when the key may read Alliance chat, which is
    # unticked on new keys. /chat/alliance itself isn't read: /chat carries both channels.
    ("alliance_chat", "Alliance chat", "optional", ("/chat/alliance",), "alliance chat relay"),
    ("account", "Account", "optional", ("/account",), "the daily gift reminder"),
)
# A short "still running" post to the corp channel once a day at this IRL hour (IRL_TIMEZONE), or None for none.
# If it doesn't arrive, the bridge or its machine is down.
DAILY_STATUS_HOUR: Optional[int] = 8
# Times of day for the status post, as (hour, minute) in IRL_TIMEZONE, e.g. ((6, 0), (16, 0)); () for none.
# None: once a day at DAILY_STATUS_HOUR (the older setting). settings.ini: status_times = 06:00, 16:00
STATUS_TIMES: Optional[tuple] = None

J2000_EPOCH = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Timestamps: game time in a grey box ("`23 Apr 2032, 06:22 AM`"). A time still ahead also gets the real
# time as a Discord timestamp, which each viewer sees in their own time zone, with a live countdown. The
# log files get that real time written out (in IRL_TIMEZONE) instead of Discord's codes.
# GAME_TIME_ICON goes inside the grey box before the game time ("" for none).
GAME_TIME_ICON, REAL_TIME_ICON = "", "🕰️"
GAME_TIME_FORMAT = "%b %Y, %I:%M %p"   # after the day of the month: "23 Apr 2032, 06:22 AM"
LOG_REAL_TIME_FORMAT = "%b %Y, %I:%M %p"
FUTURE_STAMP_AFTER_SECONDS = 60        # a time at least this far ahead also shows real time and a countdown
DISCORD_TIME = re.compile(r"<t:(\d+):[tTdDfF]>(?: \(<t:\d+:R>\))?|<t:(\d+):R>")
LOG_TZ: Optional[tzinfo] = None  # set from IRL_TIMEZONE by GameClock

LOG = logging.getLogger("sol_watch")
LOG.addHandler(logging.NullHandler())  # no log files until setup_logs() (and never with --once)
LOG.propagate = False
# Lines with these tags are problems: they also go to errors.log.
PROBLEM_TAGS = re.compile(r"^\[(Network error|API error|Rate limited|Discord error|Discord exception|Discord queue|"
                          r"Watcher error|Recovered|FATAL ERROR|Warning|Retry|Lock)\]")


PROBLEMS = deque()  # (time, tag, message) of each problem logged, for the daily status (pruned to 24 h)
# The server going briefly away. Near a game update's go-live time these are the update, not a problem.
OUTAGE = re.compile(r"^\[Network error\]|^\[API error\] .*HTTP (50[234]|5[23]\d)\b")  # 52x/53x: the CDN in front
UPDATE_OUTAGE_WINDOW_SECONDS = 600  # outages this close to a game update count as part of it
# Outage errors with no more than this gap between them are one outage; one lasting at least OUTAGE_MIN_SECONDS
# is a server outage in the status post ("Game server down 1:20–5:00 AM (3h 40m)"), not thousands of problems.
OUTAGE_GAP_SECONDS = 600
OUTAGE_MIN_SECONDS = 300
# When the game's server last answered (200 or 304), a few seconds apart at most, for 24 h. A read that got through
# ends an outage spell: scattered dropped connections whose retries succeed aren't the server being down (1.07.08).
READS_OK = deque()
READS_OK_STEP_SECONDS = 5


def read_ok(now=None):
    now = time.time() if now is None else now
    if not READS_OK or now - READS_OK[-1] >= READS_OK_STEP_SECONDS:
        READS_OK.append(now)
        while READS_OK and READS_OK[0] < now - 86400:
            READS_OK.popleft()


def read_ok_between(start, end):
    """True if the server answered after start and before end."""
    i = bisect.bisect_right(READS_OK, start)
    return i < len(READS_OK) and READS_OK[i] < end


def log(message, detail=None):
    """Print a line (flushed, so it reaches journalctl at once) and write it to the log files."""
    message = DISCORD_TIME.sub(readable_time, message)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)
    problem = PROBLEM_TAGS.match(message)
    if problem:
        LOG.warning(message + (f"\n{detail.rstrip()}" if detail else ""))
        if problem.group(1) != "Recovered":
            PROBLEMS.append((time.time(), problem.group(1), message))
            while PROBLEMS and PROBLEMS[0][0] < time.time() - 86400:
                PROBLEMS.popleft()
    else:
        LOG.info(message)


def readable_time(match):
    """A Discord timestamp code written out for the logs: '<t:1793750000:t> (<t:1793750000:R>)' -> '4 Nov 2026, 12:57 AM'."""
    when = datetime.fromtimestamp(int(match.group(1) or match.group(2)), tz=LOG_TZ)
    return f"{when.day} {when.strftime(LOG_REAL_TIME_FORMAT)}"


def setup_logs():
    LOG_DIR.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for name, level, size_mb, copies in (("bridge.log", logging.INFO, 5, 5), ("errors.log", logging.WARNING, 2, 3)):
        handler = RotatingFileHandler(LOG_DIR / name, maxBytes=size_mb * 1024 * 1024, backupCount=copies, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def hide_webhooks(text):
    """Webhook URLs contain their secret token; keep them out of the logs."""
    return re.sub(r"/api/webhooks/\S+", "/api/webhooks/…", str(text))


class State:
    """A small JSON file of what has been announced, so a restart doesn't repeat alerts."""

    def __init__(self, path, writable):
        self.path, self.writable, self.dirty = path, writable, False
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError) as err:
            log(f"[Warning] Couldn't read {path.name} ({err}); starting with no saved state.")
            self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        if self.data.get(key) != value:
            self.data[key] = value
            self.dirty = True

    def save(self):
        if not (self.dirty and self.writable):
            return
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)  # atomic, so a crash mid-write never leaves half a file
            self.dirty = False
        except OSError as err:
            log(f"[Warning] Couldn't save {self.path.name}: {err}")


def field_example(value):
    if isinstance(value, dict):
        return "{…}"
    if isinstance(value, list):
        return "[…]"
    return json.dumps(value, ensure_ascii=False)[:80]


def collect_fields(value, path, found, ids=frozenset()):
    """Every field path in a response, e.g. '.factions[].status'.

    Keys that are data rather than field names become {id}: numbers, place ids ('mars/d'),
    rock ids ('rock:x'), tech levels ('missile_guidance__6') and any id from the reference
    tables (goods, techs, ships, places, facilities), so a new tech or ship type isn't a "new field".
    Faction ids are not in the reference tables, so a new faction (Clear Sky) still shows up.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            is_id = key.isdigit() or "/" in key or ":" in key or key in ids or re.search(r"__\d+$", key)
            child_path = f"{path}.{'{id}' if is_id else key}"
            found.setdefault(child_path, field_example(child))
            collect_fields(child, child_path, found, ids)
    elif isinstance(value, list):
        for child in value:
            collect_fields(child, path + "[]", found, ids)


class FieldTracker:
    """Logs every field name an endpoint returns for the first time, at any depth.

    The spec only lists top-level fields, so this is how new fields inside list
    items (new combat data, Clear Sky, ...) get noticed. Each is logged once.
    """
    MAX_PER_RESPONSE = 40
    FORMAT = 2  # bumped when field paths are built differently: older files are re-recorded silently

    def __init__(self, writable):
        self.path, self.writable = LOG_DIR / "known-fields.json", writable
        self.ids = frozenset()  # reference-table ids, set by main() once they're loaded
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        endpoints = saved.get("endpoints", {}) if saved.get("format") == self.FORMAT else {}
        self.known = {k: set(v) for k, v in endpoints.items()}

    def observe(self, endpoint, body):
        found = {}
        collect_fields(body, "", found, self.ids)
        known = self.known.get(endpoint)
        if known is None:  # first time we see this endpoint: record it, nothing to report
            self.known[endpoint] = set(found)
            self.save()
            return
        new = [p for p in found if p not in known]
        if not new:
            return
        known.update(new)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report = [f"{stamp} {endpoint} {p} = {found[p]}" for p in new[:self.MAX_PER_RESPONSE]]
        if len(new) > self.MAX_PER_RESPONSE:
            report.append(f"{stamp} {endpoint} ... and {len(new) - self.MAX_PER_RESPONSE} more")
        log(f"[New fields] {endpoint}: {', '.join(new[:5])}" + (f" (+{len(new) - 5} more)" if len(new) > 5 else ""))
        if self.writable:
            with open(LOG_DIR / "new-fields.log", "a", encoding="utf-8") as handle:
                handle.write("\n".join(report) + "\n")
        self.save()

    def save(self):
        if self.writable:
            try:
                self.path.write_text(json.dumps({"format": self.FORMAT, "endpoints": {k: sorted(v) for k, v in self.known.items()}},
                                                indent=0), encoding="utf-8")
            except OSError as err:
                log(f"[Warning] Couldn't save {self.path.name}: {err}")


def load_env():
    for env_path in (HERE / ".env", HERE.parent / ".env"):
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
            break
    load_settings(HERE / SETTINGS_FILE)

    config = {
        "api_key": os.environ.get("SOL_BRIDGE_API_KEY") or os.environ.get("SOL_API_KEY"),
        "corp": os.environ.get("WEBHOOK_CORP"),
        "general": os.environ.get("WEBHOOK_GENERAL"),
        "alliance": os.environ.get("WEBHOOK_ALLIANCE"),
        # Lite mode posts everything to one channel: WEBHOOK_LITE, or else the general channel.
        "lite": os.environ.get("WEBHOOK_LITE") or os.environ.get("WEBHOOK_GENERAL"),
    }
    if not config["api_key"]:
        sys.exit(f"Missing settings: SOL_BRIDGE_API_KEY. Put it in {HERE / '.env'} (see .env.example).")
    config["irl_tz"] = load_timezone(os.environ.get("IRL_TIMEZONE", "").strip())
    apply_derelict_settings(os.environ.get("DERELICT_CHECK_MINUTES", "").strip(),
                            os.environ.get("DERELICT_MIN_VALUE", "").strip())
    check_read_rate()
    return config


def apply_derelict_settings(minutes, min_value):
    """DERELICT_CHECK_MINUTES and DERELICT_MIN_VALUE from .env, which win over the settings at the top
    (so they survive an upgrade). "off" for either turns the derelict check off, as does 0 minutes;
    a value of 0 posts every derelict."""
    global INTERVAL_DERELICTS, DERELICT_ALERT_MIN_VALUE
    if minutes:
        if minutes.lower() in ("off", "0", "none"):
            DERELICT_ALERT_MIN_VALUE = None
        else:
            try:
                INTERVAL_DERELICTS = max(int(float(minutes) * 60), DERELICT_MIN_INTERVAL)
            except ValueError:
                log(f"[Warning] DERELICT_CHECK_MINUTES '{minutes}' isn't a number; "
                    f"checking every {INTERVAL_DERELICTS // 60} minutes.")
    if min_value:
        if min_value.lower() in ("off", "none"):
            DERELICT_ALERT_MIN_VALUE = None
        else:
            try:
                DERELICT_ALERT_MIN_VALUE = float(min_value.replace("$", "").replace("M", "").replace(",", ""))
            except ValueError:
                log(f"[Warning] DERELICT_MIN_VALUE '{min_value}' isn't a number; using ${DERELICT_ALERT_MIN_VALUE}M.")
    DerelictWatcher.interval = INTERVAL_DERELICTS


# ================= SETTINGS FILE =================
# settings.ini next to the script (optional): the user's own settings, which an upgrade never replaces.
# Every key is optional; a missing file or key keeps the default set at the top of this file.
# (section, key, setting, kind, limits). Kinds: int and float with (min, max); choice with its options;
# bool; set (comma-separated words); hours (comma-separated whole hours); hour (0-23 or off); text.
# "env" settings are passed on as environment variables, which a real variable or .env overrides.
SETTINGS_SPEC = (
    ("contracts", "tier_filter", "CONTRACT_FILTER_TIER", "choice", ("newcomer", "mid", "leader")),
    ("contracts", "alliance_job_min_level", "ALLIANCE_JOB_MIN_LEVEL", "choice", ("easy", "medium", "hard")),
    ("contracts", "alliance_job_digest_at", "ALLIANCE_JOB_DIGEST_AT", "int", (2, 100)),
    ("alerts", "countdown_warnings_hours", "COUNTDOWN_WARNINGS_HOURS", "hours", None),
    ("alerts", "ballot_warnings_hours", "BALLOT_WARNINGS_HOURS", "hours", None),
    ("alerts", "research_warning_minutes", "RESEARCH_WARNING_SECONDS", "minutes", (1, 120)),
    ("alerts", "deal_expiry_warning_minutes", "DEAL_EXPIRY_WARNING_MINUTES", "int", (1, 1440)),
    ("alerts", "stall_repeat_hours", "STALL_REPEAT_HOURS", "float", (0, 168)),
    ("alerts", "ignored_stall_reasons", "IGNORED_STALL_REASONS", "set", None),
    ("alerts", "slowed_post_below_percent", "SLOWED_POST_BELOW_PERCENT", "float", (1, 100)),
    ("alerts", "slowed_worse_by_points", "SLOWED_WORSE_BY_POINTS", "float", (1, 100)),
    ("alerts", "slowed_recovery_hours", "SLOWED_RECOVERY_HOURS", "float", (0, 720)),
    ("alerts", "standing_alert_step", "STANDING_ALERT_STEP", "float", (0.1, 100)),
    ("alerts", "status_times", "STATUS_TIMES", "times", None),
    ("alerts", "daily_status_hour", "DAILY_STATUS_HOUR", "hour", None),  # older, one whole hour
    ("alerts", "game_update_posts", "GAME_UPDATE_POSTS", "bool", None),
    ("alerts", "new_place_posts", "NEW_PLACE_POSTS", "bool", None),
    ("incoming", "ignore_earth_moon", "INCOMING_IGNORE_EARTH_MOON", "bool", None),
    ("incoming", "ignore_places", "INCOMING_IGNORE_PLACES", "set", None),
    ("incoming", "ignore_companies", "INCOMING_IGNORE_COMPANIES", "set", None),
    ("derelicts", "check_minutes", "DERELICT_CHECK_MINUTES", "env", None),
    ("derelicts", "min_value", "DERELICT_MIN_VALUE", "env", None),
    ("time", "irl_timezone", "IRL_TIMEZONE", "env", None),
    ("bridge", "mode", "BRIDGE_MODE", "choice", ("full", "lite")),
    ("updates", "update_check", "UPDATE_CHECK", "bool", None),
    ("updates", "project_url", "BRIDGE_PROJECT_URL", "text", None),
    ("updates", "source_url", "BRIDGE_SOURCE_URL", "text", None),
) + tuple(("intervals", name.lower(), f"INTERVAL_{name}", "int", (MIN_POLL_SECONDS, 86400)) for name in (
    "CHAT", "CRAFT", "MINING", "CONTRACTS", "HISTORY", "RESEARCH", "DEFENSE", "COMPANY", "INDUSTRY",
    "FLEET_MAP", "FLEETS", "SLOW", "DIPLOMACY", "RECORDS", "NEWS", "PAD_GOALS", "PLACES", "API_SPEC", "GAME_SITE"))
OFF_WORDS = {"off", "none", "no", "false", "0"}


def parse_setting(kind, limits, raw):
    """The value of one setting from its text, or raises ValueError with what's wrong."""
    text = raw.strip()
    if kind in ("int", "float", "minutes"):
        value = float(text.replace(",", ""))
        low, high = limits
        if not low <= value <= high:
            raise ValueError(f"must be from {low:g} to {high:g}")
        if kind == "minutes":
            return int(value * 60)
        return int(value) if kind == "int" else value
    if kind == "choice":
        if text.lower() not in limits:
            raise ValueError("must be one of " + ", ".join(limits))
        return text.lower()
    if kind == "bool":
        if text.lower() in ("yes", "on", "true", "1"):
            return True
        if text.lower() in OFF_WORDS:
            return False
        raise ValueError("must be yes or no")
    if kind == "set":
        return {part.strip() for part in text.split(",") if part.strip()}
    if kind == "hours":
        hours = sorted({int(part) for part in text.split(",") if part.strip()}, reverse=True)
        if not hours or hours[-1] < 1:
            raise ValueError("must be whole hours of 1 or more, e.g. 72, 24, 4, 1")
        return tuple(hours)
    if kind == "times":  # "06:00, 16:00" or "8" or off
        if text.lower() in OFF_WORDS - {"0"}:
            return ()
        times = set()
        for part in (p.strip() for p in text.split(",") if p.strip()):
            hour, _, minute = part.partition(":")
            h, m = int(hour), int(minute or 0)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("must be times of day like 06:00, 16:30, or off")
            times.add((h, m))
        if not times:
            raise ValueError("must be times of day like 06:00, 16:30, or off")
        return tuple(sorted(times))
    if kind == "hour":
        if text.lower() in OFF_WORDS - {"0"}:
            return None
        hour = int(text)
        if not 0 <= hour <= 23:
            raise ValueError("must be an hour from 0 to 23, or off")
        return hour
    if not text.startswith("https://"):  # text: the update check's addresses
        raise ValueError("must be an https:// address")
    return text


def load_settings(path):
    """Apply settings.ini (if there is one). A bad value is reported and its default kept."""
    import configparser
    if not path.exists():
        log(f"[Settings] No {path.name}: using the defaults (see settings.example.ini).")
        return
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as err:
        log(f"[Warning] {path.name} couldn't be read, so the defaults are used: {err}")
        return
    known = {(section, key): (name, kind, limits) for section, key, name, kind, limits in SETTINGS_SPEC}
    applied = []
    for section in parser.sections():
        for key, raw in parser.items(section):
            spec = known.get((section.lower(), key.lower()))
            if spec is None:
                log(f"[Warning] {path.name}: unknown setting '{key}' in [{section}], ignored (a typo?).")
                continue
            name, kind, limits = spec
            if not raw.strip():
                continue  # left empty: the default
            if kind == "env":
                os.environ.setdefault(name, raw.strip())  # a real variable or .env still wins
                applied.append(key)
                continue
            try:
                globals()[name] = parse_setting(kind, limits, raw)
            except ValueError as err:
                log(f"[Warning] {path.name}: [{section}] {key} = {raw.strip()!r} {err}; the default is kept.")
                continue
            applied.append(key)
    apply_poll_intervals()
    log(f"[Settings] {path.name}: " + (", ".join(applied) if applied else "nothing set, defaults used"))


def check_read_rate():
    reads = estimated_reads_per_minute()
    if reads > READS_PER_MINUTE_WARNING:
        log(f"[Warning] These intervals make about {reads:.0f} API reads a minute; the API allows 60 a minute "
            f"per key, and other tools on the same key share it. Longer intervals are kinder to the server.")
    return reads


def apply_poll_intervals():
    """Each watcher class takes its poll interval from its INTERVAL_* setting (fixed when the class was made)."""
    for cls_name, setting in WATCHER_INTERVALS.items():
        globals()[cls_name].interval = globals()[setting]


def estimated_reads_per_minute():
    """API reads a minute at the current intervals (the website, spec and GitHub checks aren't API reads)."""
    return sum(60 / globals()[setting] for cls_name, setting in WATCHER_INTERVALS.items()
               if cls_name not in ("ApiSpecWatcher", "GameUpdateWatcher", "DerelictWatcher")) \
        + (60 / INTERVAL_DERELICTS if DERELICT_ALERT_MIN_VALUE is not None else 0)


def load_timezone(name):
    """The zone for IRL times; None means the machine's local zone."""
    if not name:
        return None
    if ZoneInfo is None:
        log("[Warning] IRL_TIMEZONE needs Python 3.9+; using the machine's local time.")
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log(f"[Warning] Unknown IRL_TIMEZONE '{name}' (install tzdata?); using the machine's local time.")
        return None


# ================= SYSTEMD =================

_last_heartbeat = 0.0


def sd_notify(message):
    """Send a status line (READY=1, WATCHDOG=1, STATUS=...) to systemd. Does nothing outside systemd."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return
    if address.startswith("@"):  # an abstract socket
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError as err:
        LOG.debug(f"sd_notify failed: {err}")


def heartbeat():
    """Tell systemd's watchdog we're alive (at most every 10 s). Called from every loop and wait,
    so only a real freeze stops it, and the service restarts after WatchdogSec."""
    global _last_heartbeat
    if time.monotonic() - _last_heartbeat >= 10:
        _last_heartbeat = time.monotonic()
        sd_notify("WATCHDOG=1")


def nap(seconds):
    """time.sleep that keeps the watchdog fed through long waits."""
    end = time.monotonic() + seconds
    while True:
        heartbeat()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 10))


# ================= SOL API =================

class SolApi:
    def __init__(self, api_key):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.etags = {}
        self.bodies = {}  # last body per request, for get_latest()
        self.last_request = 0.0
        self.fields = None  # a FieldTracker, set by main()
        self.rejected = False      # the game is rejecting our key (alerted once until it works again)
        self.paused_until = 0.0    # no reads before this (monotonic), during a bad-key lockout
        self.forbidden = set()     # paths that answered 403: this key may not read them
        self.on_alert: Optional[Callable[[str], None]] = None
        self.expected_forbidden: set = set()  # paths whose section the startup check reported missing
        self.allowed: Optional[tuple] = None  # lite mode: the only API paths it may read (LITE_PATHS)

    def get_latest(self, path, params=None):
        """Like get(), but returns the previous body instead of None when unchanged (304)."""
        body = self.get(path, params)
        key = (path, json.dumps(params, sort_keys=True))
        if body is not None:
            self.bodies[key] = body
        return self.bodies.get(key)

    def get(self, path, params=None, use_etag=True, reader=""):
        """Return the parsed body, or None when unchanged (304) or failed.

        path is relative to /api/v1, or a full URL (e.g. the OpenAPI spec). reader names who is asking: each
        reader keeps its own ETag, so when two watchers poll one path (factions and diplomacy both read
        /factions), the first to see a change doesn't make the other get "unchanged" and miss it."""
        cache_key = (path, json.dumps(params, sort_keys=True), reader)
        if self.allowed is not None and not path.startswith("http") and path not in self.allowed and \
                not any(p and path.startswith(p + "/") for p in self.allowed):
            log(f"[Lite] Not reading {path}: lite mode reads only public data ({', '.join(p for p in self.allowed if p)}).")
            return None
        if self.paused_until > time.monotonic() and not path.startswith("http"):
            return None  # our key is locked out: reading now would only extend the lockout
        headers = {}
        if use_etag and cache_key in self.etags:
            headers["If-None-Match"] = self.etags[cache_key]

        for attempt in range(3):
            heartbeat()
            wait = self.last_request + REQUEST_SPACING_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            try:
                url = path if path.startswith("http") else API_BASE + path
                response = self.session.get(url, params=params, headers=headers, timeout=20)
            except requests.RequestException as err:
                log(f"[Network error] {path}: {err}")
                time.sleep(2)
                continue

            if response.status_code in (200, 304) and not path.startswith("http"):
                read_ok()
                if self.rejected:
                    self.rejected = False
                    self.alert("🔑 The game accepts our API key again. The bridge is reading as usual.")
                if path in self.forbidden:
                    self.forbidden.discard(path)
                    self.alert(f"🔑 Our API key can read `{path}` again.")
            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    log(f"[API error] {path}: HTTP 200 but not JSON: {response.text[:200]!r}")
                    return None
                if response.headers.get("ETag"):
                    # The server only answers 304 when the weak "W/" prefix is stripped.
                    self.etags[cache_key] = response.headers["ETag"].removeprefix("W/")
                if self.fields and not path.startswith("http"):
                    try:
                        self.fields.observe(path, body)
                    except Exception as err:  # field tracking must never break polling
                        log(f"[Warning] Field tracking failed for {path}: {err!r}")
                return body
            if response.status_code == 304:
                return None
            text = response.text.lstrip()
            if self.key_trouble(path, response, text):
                return None
            if response.status_code == 429:
                retry = float(response.headers.get("Retry-After", 2 ** (attempt + 1)))
                log(f"[Rate limited] {path}: waiting {retry:.0f}s")
                nap(retry)
                continue
            text = "(HTML error page)" if text[:15].lower().startswith(("<!doctype", "<html")) else text[:200]
            log(f"[API error] {path}: HTTP {response.status_code} {text}")
            return None
        return None

    def key_trouble(self, path, response, text):
        """True when the game rejected our key (401, or 429 "Too many bad keys") or the key may not read
        this path (403). Alerts once through self.alert, and pauses all reads while the key is rejected:
        more reads with a bad key only extend the game's 5-minute lockout."""
        status = response.status_code
        if status == 403 and not path.startswith("http"):
            if path not in self.forbidden:
                self.forbidden.add(path)
                log(f"[API key] {path}: HTTP 403, this key may not read it: {text[:200]}")
                if path in self.expected_forbidden:
                    return True  # its section was reported missing at startup
                self.alert(f"🔑 **Our API key may not read `{path}`.** Part of the bridge stays quiet until the "
                           f"key is allowed that section (in the game: Settings › Account › API keys).")
            return True
        if status == 401 or (status == 429 and "bad key" in text.lower()):
            wait = float(response.headers.get("Retry-After") or 300)
            self.paused_until = time.monotonic() + wait
            if not self.rejected:
                self.rejected = True
                log(f"[API key] Rejected (HTTP {status}): {text[:200]}")
                self.alert("🔑 **The game is rejecting our API key.** Every read fails until it's replaced. "
                           "Changing the account password revokes every key, and so does 90 days unused. Make a "
                           "new key in the game (Settings › Account › API keys), put it in `.env` as "
                           "`SOL_BRIDGE_API_KEY` and restart the bridge.")
            return True
        return False

    def alert(self, text):
        """Log a key problem and post it to Discord (main() sets on_alert to post to the corp channel)."""
        log(text)
        if self.on_alert:
            self.on_alert(text)


class GameClock:
    """Converts between game days and real time using /clock (timeScale = game days per real day)."""

    def __init__(self, api, irl_tz=None):
        global LOG_TZ
        self.api = api
        self.irl_tz = LOG_TZ = irl_tz
        self.epoch_real_ms = 1790724770179
        self.epoch_game_day = 10957.5
        self.time_scale = 24.0
        self.fetched_at = 0.0

    def refresh(self, force=False):
        if not force and time.monotonic() - self.fetched_at < CLOCK_REFRESH_SECONDS:
            return
        data = self.api.get("/clock", use_etag=False)
        if data and data.get("clock"):
            clock = data["clock"]
            self.epoch_real_ms = clock["epochRealMs"]
            self.epoch_game_day = clock["epochGameDay"]
            self.time_scale = clock["timeScale"]
            self.fetched_at = time.monotonic()
        else:  # keep the last values and try again in a minute, not on every loop
            self.fetched_at = time.monotonic() - CLOCK_REFRESH_SECONDS + 60

    def game_day_for(self, real_ms):
        return self.epoch_game_day + (real_ms - self.epoch_real_ms) / 86_400_000 * self.time_scale

    def real_ms_for(self, game_day):
        return self.epoch_real_ms + (game_day - self.epoch_game_day) / self.time_scale * 86_400_000

    def now_game_day(self):
        return self.game_day_for(time.time() * 1000)

    def stamp(self, game_day=None, real_ms=None):
        """'`23 Apr 2032, 06:22 AM`' for a game day or real ms (default: now). A time still ahead also gets
        the real time and a live countdown as Discord timestamps:
        '`27 Apr 2032, 09:49 AM` · 🕰️ <t:1793750000:t> (<t:1793750000:R>)'."""
        if game_day is None and real_ms is None:
            real_ms = time.time() * 1000
        if game_day is None:
            game_day = self.game_day_for(real_ms)
        if real_ms is None:
            real_ms = self.real_ms_for(game_day)
        sol = J2000_EPOCH + timedelta(days=game_day)
        icon = f"{GAME_TIME_ICON} " if GAME_TIME_ICON else ""
        text = f"`{icon}{sol.day} {sol.strftime(GAME_TIME_FORMAT)}`"
        if real_ms >= (time.time() + FUTURE_STAMP_AFTER_SECONDS) * 1000:
            unix = int(real_ms // 1000)
            text += f" · {REAL_TIME_ICON} <t:{unix}:t> (<t:{unix}:R>)"
        return text

    def game_days_to_hours(self, game_days):
        return game_days / self.time_scale * 24

    def hours_until(self, game_day):
        return self.game_days_to_hours(game_day - self.now_game_day())

    def until(self, game_day):
        """Real time left until a game day, e.g. 'in 2h 58m' (or 'now')."""
        minutes = round((self.real_ms_for(game_day) - time.time() * 1000) / 60_000)
        if minutes <= 0:
            return "now"
        hours, minutes = divmod(minutes, 60)
        if hours >= 48:
            return f"in {hours / 24:.1f} days"
        return f"in {hours}h {minutes}m" if hours else f"in {minutes}m"


# ================= DISCORD =================

class Discord:
    def __init__(self, enabled):
        self.enabled = enabled
        self.session = requests.Session()
        self.queue = deque()  # (webhook, text) waiting while Discord is unreachable
        self.failed_at = None

    def deliver(self, webhook_url, text):
        """True once Discord has the message (or rejected it for good); False when worth retrying later."""
        # Never ping anyone, even if a chat message contains @everyone.
        payload = {"content": text, "allowed_mentions": {"parse": []}}
        for attempt in range(3):
            heartbeat()
            try:
                response = self.session.post(webhook_url, json=payload, timeout=10)
            except requests.RequestException as err:
                log(f"[Discord exception] {type(err).__name__}: {hide_webhooks(err)[:200]}")
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code == 429:
                nap(retry_after(response))
                continue
            if response.status_code >= 500:
                log(f"[Discord error] HTTP {response.status_code}; retrying")
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code not in (200, 204):
                # 4xx: a deleted webhook or a bad message. Retrying won't help.
                log(f"[Discord error] HTTP {response.status_code}: {hide_webhooks(response.text[:200])}")
            time.sleep(0.3)
            return True
        return False

    def send(self, webhook_url, text):
        if (self.queue and not self.flush()) or not self.deliver(webhook_url, text):
            self.hold(webhook_url, text)

    def hold(self, webhook_url, text):
        if len(self.queue) >= DISCORD_QUEUE_MAX:
            self.queue.popleft()
            log("[Discord queue] Queue full; dropped the oldest waiting message.")
        self.queue.append((webhook_url, text))
        self.failed_at = time.monotonic()
        log(f"[Discord queue] Couldn't reach Discord; {len(self.queue)} message(s) waiting. "
            f"Retrying every {DISCORD_RETRY_SECONDS}s.")

    def flush(self):
        """Send queued messages, oldest first. True when nothing is left waiting."""
        if not self.queue:
            return True
        if time.monotonic() - (self.failed_at or 0) < DISCORD_RETRY_SECONDS:
            return False
        sent = 0
        while self.queue:
            if not self.deliver(*self.queue[0]):
                self.failed_at = time.monotonic()
                return False
            self.queue.popleft()
            sent += 1
        log(f"[Recovered] Discord is reachable again; sent {sent} queued message(s).")
        return True

    def post(self, webhook_url, header, lines):
        lines = merge_repeats([stamp_after_headline(line) for line in lines])
        for line in lines:
            log(line)
        if not self.enabled or not lines:
            return
        # Several updates from one poll share a message, one after another with no heading: each
        # line has its own emoji. They split into more messages only at Discord's length limit.
        chunks, current = [], ""
        for line in lines:
            if current and len(current) + len(line) + 1 > DISCORD_MAX_LENGTH:
                chunks.append(current)
                current = ""
            current = f"{current}\n{line}" if current else line
        chunks.append(current)
        for chunk in chunks:
            self.send(webhook_url, chunk[:2000])


LEADING_STAMP_RE = re.compile(r"^((?:[^\w\s`*\[(]\S*\s+){1,3})(`[^`\n]*\d{4}[^`\n]*`(?: · [^\n<]*<t:\d+:t> \(<t:\d+:R>\))?) (?=\S)")


def stamp_after_headline(text):
    """Watchers write a post's game time right after its emoji; it's moved after the post's main information, on
    each line that starts that way: before the first " | " (outside [brackets] and **bold**) when there is one,
    after a headline ending in a colon ("**5 sites at Tros stopped:** `…`", its list below), else at the end."""
    out = []
    for line in text.split("\n"):
        m = LEADING_STAMP_RE.match(line)
        if m:
            icons, stamp, rest = m.group(1), m.group(2), line[m.end():]
            split = next((i.start() for i in re.finditer(r" \| ", rest)
                          if rest.count("[", 0, i.start()) == rest.count("]", 0, i.start())
                          and rest.count("**", 0, i.start()) % 2 == 0 and rest.count("`", 0, i.start()) % 2 == 0), None)
            if split is not None:
                line = f"{icons}{rest[:split]} · {stamp}{rest[split:]}"
            elif rest.rstrip().endswith((":", ":**")):
                line = f"{icons}{rest.rstrip()} {stamp}"
            else:
                line = f"{icons}{rest.rstrip()} · {stamp}"
        out.append(line)
    return "\n".join(out)


def merge_repeats(lines):
    """The same line twice in one post becomes one, marked ×2 at the end of its first line: two docking rings
    upgraded at once, or two contracts of one title won together, read as one thing posted twice."""
    counts = {}  # type: dict
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    merged = []
    for line, n in counts.items():
        if n > 1:
            first, _, rest = line.partition("\n")
            line = f"{first} (×{n})" + (f"\n{rest}" if rest else "")
        merged.append(line)
    return merged


def retry_after(response):
    """Seconds Discord asks us to wait after a 429."""
    try:
        return min(float(response.json().get("retry_after", 1)), 60)
    except (ValueError, AttributeError):
        return min(float(response.headers.get("Retry-After", 2)), 60)


class Reminders:
    """Alerts due at a real time (research about to finish, a flare landing).

    Watchers call set() on every poll, so a changed time simply moves the
    reminder. Each key fires at most once, even if set() is called again later.
    """

    def __init__(self, discord):
        self.discord = discord
        self.pending = {}  # key -> (due real ms, webhook, header, line or callable)
        self.fired = {}    # key -> real ms it fired; forgotten after a week

    def set(self, key, due_real_ms, webhook, header, line):
        if key in self.fired or due_real_ms <= time.time() * 1000:
            return
        self.pending[key] = (due_real_ms, webhook, header, line)

    def cancel(self, key):
        self.pending.pop(key, None)

    def fire_due(self):
        now = time.time() * 1000
        for key, (due, webhook, header, line) in list(self.pending.items()):
            if due <= now:
                del self.pending[key]
                self.fired[key] = now
                self.discord.post(webhook, header, [line() if callable(line) else line])
        for key in [k for k, fired in self.fired.items() if now - fired > 7 * 86_400_000]:
            del self.fired[key]


# ================= WATCHERS =================
# Each watcher polls one endpoint. update() receives the body and returns
# lines to post; on the first call it seeds its state and returns startup lines.

def money(value):
    return f"${value:,.1f}M" if isinstance(value, (int, float)) else "$?M"


COMPANY_KINDS: dict = {}  # company id -> "navy", "exchange" or "pirate" (players have none), from /companies


def load_company_names(api):
    data = api.get("/companies", use_etag=False) or {}
    companies = [c for c in data.get("companies", []) if isinstance(c, dict) and "id" in c]
    COMPANY_KINDS.update({c["id"]: c["kind"] for c in companies if c.get("kind")})
    return {c["id"]: c.get("name", f"Company #{c['id']}") for c in companies}


def newest_id(items):
    return max((item["id"] for item in items if isinstance(item.get("id"), int)), default=0)


POLL_COUNTS = {"polls": 0, "failed": 0}  # since startup, for the daily status


def status_times():
    """The (hour, minute) times of day for the status post: STATUS_TIMES, or the older DAILY_STATUS_HOUR."""
    if STATUS_TIMES is not None:
        return STATUS_TIMES
    return () if DAILY_STATUS_HOUR is None else ((DAILY_STATUS_HOUR, 0),)


class DailyStatus:
    """A short "still running" post at each of status_times() (IRL time). Its absence means trouble.
    Each post covers the time since the previous one (at most 24 h). Times missed while the bridge was down
    give one post when it's back, not one each."""
    header = "BRIDGE STATUS"

    def __init__(self, ctx, webhook):
        self.ctx, self.webhook = ctx, webhook
        self.started = time.time()
        # The very first run, or the first after the times were changed, counts the latest time as done:
        # neither a deploy nor a new schedule posts at once.
        times = [list(t) for t in status_times()]
        if ctx["state"].get("status_last_slot") is None or ctx["state"].get("status_times") != times:
            slot = self.latest_slot()
            ctx["state"].set("status_last_slot", slot.isoformat() if slot else "")
            ctx["state"].set("status_times", times)

    def now(self):
        return datetime.now(tz=self.ctx["clock"].irl_tz)

    def today(self):
        return self.now().date()

    def latest_slot(self, now=None):
        """The most recent scheduled time at or before now (looking back up to a day), or None if none are set."""
        now = now or self.now()
        times = status_times()
        if not times:
            return None
        for day in (now.date(), now.date() - timedelta(days=1)):
            due = [datetime(day.year, day.month, day.day, h, m, tzinfo=now.tzinfo) for h, m in times]
            due = [d for d in due if d <= now]
            if due:
                return max(due).replace(tzinfo=None)
        return None

    def check(self):
        slot = self.latest_slot()
        if slot is None or slot.isoformat() <= (self.ctx["state"].get("status_last_slot") or ""):
            return
        self.ctx["state"].set("status_last_slot", slot.isoformat())
        text = self.text()
        self.ctx["state"].set("status_last_at", time.time())
        self.ctx["discord"].post(self.webhook, self.header, [text])

    def text(self):
        up = time.time() - self.started
        uptime = f"{up / 86400:.1f} days" if up >= 86400 else f"{up / 3600:.0f}h"
        # Since the previous post (problems and game updates are only kept for 24 h anyway).
        span = min(time.time() - (self.ctx["state"].get("status_last_at") or 0), 86400)
        cutoff = time.time() - span
        window = f"{span / 3600:.0f}h"
        updates = [u for u in (self.ctx["state"].get("game_updates") or []) if u >= cutoff]
        tags, excused, spells = {}, 0, []  # spells: [start, end, [tags]] of outage errors close together
        for when, tag, message in PROBLEMS:
            if when < cutoff:
                continue
            if OUTAGE.match(message):
                if any(abs(when - u) <= UPDATE_OUTAGE_WINDOW_SECONDS for u in updates):
                    excused += 1  # the server restarting for a game update
                elif spells and when - spells[-1][1] <= OUTAGE_GAP_SECONDS \
                        and not read_ok_between(spells[-1][1], when):
                    spells[-1][1] = when
                    spells[-1][2].append(tag)
                else:
                    spells.append([when, when, [tag]])
                continue
            tags[tag] = tags.get(tag, 0) + 1
        downs = []
        for start, end, spell_tags in spells:
            if end - start >= OUTAGE_MIN_SECONDS:
                downs.append(f"<t:{int(start)}:t>–<t:{int(end)}:t> ({duration_text(end - start)})")
            else:  # a blip: counted as the errors it was
                for tag in spell_tags:
                    tags[tag] = tags.get(tag, 0) + 1
        problems = (f"{sum(tags.values())} in {window} (" + ", ".join(f"{t} ×{n}" for t, n in sorted(tags.items(), key=lambda kv: -kv[1]))
                    + ")") if tags else f"none in {window}"
        game_updates = f"{len(updates)} in {window}" if updates else f"none in {window}"
        if excused:
            game_updates += f" ({excused} brief outage{'s' if excused != 1 else ''} during them)"
        build = ", ".join(b.split("/")[-1] for b in (self.ctx["state"].get("game_site") or {}).get("bundles", []) if b.endswith(".js"))
        queued = len(getattr(self.ctx["discord"], "queue", []) or [])
        return (f"🟢 **[BRIDGE STATUS]** Sol-Watch v{VERSION} running for {uptime} | "
                f"{POLL_COUNTS['polls']:,} polls, {POLL_COUNTS['failed']:,} failed | Game updates: {game_updates} | "
                + (f"Game server down: {', '.join(downs)} | " if downs else "")
                + f"Problems logged: {problems}"
                + (f" | {queued} Discord message(s) waiting to send" if queued else "")
                + (f" | Game build: {build}" if build else ""))


class Watcher:
    path = ""
    params: Optional[dict] = None
    interval = 60
    header = ""

    def __init__(self, ctx, webhook):
        self.ctx = ctx
        self.webhook = webhook
        self.next_due = 0.0
        self.seeded = False
        self.last_error, self.error_count = None, 0

    def safe_poll(self, when=""):
        """poll(), logging a repeated error once (then every 60th time) instead of on every poll."""
        name = type(self).__name__
        heartbeat()
        POLL_COUNTS["polls"] += 1
        try:
            self.poll()
        except Exception as err:
            POLL_COUNTS["failed"] += 1
            text = repr(err)
            self.error_count = self.error_count + 1 if text == self.last_error else 1
            if self.error_count == 1:
                log(f"[Watcher error] {name}{when}: {text}", traceback.format_exc())
            elif self.error_count % 60 == 0:
                log(f"[Watcher error] {name}: the same error again, {self.error_count} times in a row: {text}")
            self.last_error = text
            return
        if self.last_error:
            log(f"[Recovered] {name} is working again after {self.error_count} failed poll(s).")
            self.last_error, self.error_count = None, 0

    def poll(self):
        data = self.ctx["api"].get(self.path, self.params, reader=type(self).__name__)
        if data is None:
            return
        first = not self.seeded
        lines = self.update(data, first)
        self.seeded = True
        self.publish(self.webhook, self.header, lines, first)

    def publish(self, webhook, header, lines, first):
        if first and not self.ctx["broadcast_startup"]:
            for line in lines:
                log(line)
        else:
            self.ctx["discord"].post(webhook, header, lines)

    def update(self, data, first):
        raise NotImplementedError


class ResearchWatcher(Watcher):
    path, interval, header = "/research", INTERVAL_RESEARCH, "RESEARCH UPDATE"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.known, self.tech_id = set(), None
        self.waiting = None  # (names finished, time.time()) while the game hasn't yet started the queued next one

    def update(self, data, first):
        clock, names = self.ctx["clock"], self.ctx["tech_names"]
        known = set(data.get("techs", []))
        job = data.get("research") or {}
        tech_id = job.get("techId")
        lines = []

        if first:
            if tech_id:
                lines.append(f"🔬 **[RESEARCH ACTIVE]** **{names.get(tech_id, tech_id)}** (pace: {job.get('pace')})\n"
                             f"Expected completion: {clock.stamp(game_day=job.get('completeDay'))}")
            else:
                lines.append("💤 **[RESEARCH IDLE]** Nothing is being researched.")
        else:
            finished = [names.get(t, t) for t in sorted(known - self.known)]
            started = tech_id and tech_id != self.tech_id
            for name in finished:
                lines.append(f"✅ {clock.stamp()} **{name}** research finished!")
            if started:
                lines.append(f"🔬 {clock.stamp(game_day=job.get('startDay'))} Started **{names.get(tech_id, tech_id)}**\n"
                             f"Expected completion: {clock.stamp(game_day=job.get('completeDay'))}")
            queue = self.ctx.get("research_queue")
            if tech_id:
                self.waiting = None
            elif finished and queue:
                # The game can take minutes to move the next queued item into the lab (seen: about 3 minutes).
                self.waiting = (finished, time.time())
            elif finished:
                # The company watcher warns earlier, when the queue empties; this is the lab actually stopping.
                lines = [f"🏁 {clock.stamp()} **{', '.join(finished)}** research finished - "
                         f"⚠️ **nothing else started, the lab is now idle.** Queue new research!"]
            elif self.tech_id:
                lines.append(f"💤 {clock.stamp()} Research on **{names.get(self.tech_id, self.tech_id)}** stopped - "
                             f"the lab is idle.")
            if self.waiting and not tech_id and (not queue or time.time() - self.waiting[1] >= RESEARCH_START_GRACE_SECONDS):
                done, since = self.waiting
                minutes = max(1, round((time.time() - since) / 60))
                if queue:
                    head = queue[0]
                    head_id = (head.get("action") or {}).get("techId")
                    reason = f": {head['reason']}" if head.get("reason") else ""
                    held = (f" **{names.get(head_id, head_id)}** is next in the queue ({len(queue)} queued) "
                            f"but hasn't started{reason}")
                else:
                    held = " The queue has emptied since."
                lines.append(f"🏁 {clock.stamp()} ⚠️ **The lab is idle**, {minutes} min after **{', '.join(done)}** "
                             f"finished.{held}")
                self.waiting = None

        # Five real minutes before the current job completes.
        reminders = self.ctx["reminders"]
        if self.tech_id and self.tech_id != tech_id:
            reminders.cancel(f"research:{self.tech_id}")
        if tech_id and job.get("completeDay"):
            name = names.get(tech_id, tech_id)
            reminders.set(f"research:{tech_id}", clock.real_ms_for(job["completeDay"]) - RESEARCH_WARNING_SECONDS * 1000,
                          self.webhook, self.header,
                          f"⏰ **{name}** research completes in {RESEARCH_WARNING_SECONDS // 60} minutes "
                          f"{clock.stamp(game_day=job['completeDay'])}")

        self.known, self.tech_id = known, tech_id
        self.ctx["research_job"] = job if tech_id else None  # the company watcher's queue alert uses it
        return lines


class CraftWatcher(Watcher):
    path, interval, header = "/craft", INTERVAL_CRAFT, "FLEET TELEMETRY UPDATE"

    def update(self, data, first):
        clock = self.ctx["clock"]
        missions = {m["id"]: m for m in data.get("missions", []) if "id" in m}
        losses = data.get("losses", [])
        # safeMode items: {eventId, missionId, auto}. eventId is the hazardIncidents id that
        # caused it (a flare puts dozens of craft into safe mode at once), or null if manual.
        safe_mode = {s.get("missionId"): s for s in data.get("safeMode", [])}
        by_event = {}
        for entry in safe_mode.values():
            if entry.get("eventId") is not None:
                by_event[entry["eventId"]] = by_event.get(entry["eventId"], 0) + 1
        self.ctx["safe_mode_by_event"] = by_event  # the defense watcher reports these with the hazard
        self.ctx["missions"] = missions  # the company watcher times blocked queues from these
        # Names of our craft ever seen as derelicts this run, so NewsWatcher can spot them in the news.
        self.ctx.setdefault("our_derelicts", set()).update(
            m.get("name") for m in missions.values() if m.get("status") == "derelict" and m.get("name"))
        # Derelicts our salvage teams are heading for (name -> team), so NewsWatcher can tell when a rival takes one.
        # The target's name is in the team's statement ("Salvage Pueo II-1") or, failing that, the derelict watcher's list.
        # Rebuilt every poll from teams still on their way, so a team that failed or finished no longer counts.
        targets = {}
        for m in missions.values():
            if m.get("payloadId") == "salvage_team" and m.get("targetMissionId") \
                    and m.get("status") in ("preparing", "transit", "active"):
                statement = m.get("statement") or ""
                target = (self.ctx.get("derelict_names") or {}).get(m["targetMissionId"]) or \
                    (statement[len("Salvage "):] if statement.startswith("Salvage ") else None)
                if target:
                    targets[target] = m.get("name")
        self.ctx["salvage_targets"] = targets
        lines = []

        if first:
            latest = max(missions.values(), key=lambda m: m.get("launchDay") or 0, default=None)
            if latest:
                lines.append(f"🚀 **[FLEET - LATEST LAUNCH]** {clock.stamp(game_day=latest.get('launchDay'))} "
                             f"**{latest.get('name')}** → **{body_name(self.ctx, latest.get('destinationId'))}** | "
                             f"Status: **{latest.get('status')}**")
        else:
            for mission_id, m in missions.items():
                name, dest, status = m.get("name"), body_name(self.ctx, m.get("destinationId")), m.get("status")
                old = self.statuses.get(mission_id)
                if old is None:
                    # A launch we just made: say nothing, whether it went to the pad or into the queue.
                    queue = " (queued for the range)" if m.get("rangeQueued") else ""
                    log(f"[Fleet] New mission {name} → {dest}{queue}")
                    continue
                if self.queued.get(mission_id) and not m.get("rangeQueued") and status not in ("transit", "failed"):
                    lines.append(f"🛫 {clock.stamp()} **{name}** left the queue and is on the pad at "
                                 f"**{place_name(self.ctx, m.get('launchRegion'))}** | Liftoff {clock.stamp(game_day=m.get('launchDay'))}")
                if old == status:
                    continue
                elif old == "derelict":
                    # We abandoned it: drifting out of service (or being salvaged) isn't news to post.
                    # Salvages of ours are posted from the news by NewsWatcher.
                    log(f"[Fleet] Derelict {name} is now {status}: {m.get('outcome') or ''}".rstrip(": "))
                elif status == "transit":
                    lines.append(f"🚀 {clock.stamp(game_day=m.get('launchDay'))} **{name}** launched for **{dest}**!")
                elif status == "active":
                    lines.append(f"✨ {clock.stamp(game_day=m.get('arriveDay'))} **{name}** arrived at **{dest}** and is operating.")
                elif status == "complete" and "decommissioned" in (m.get("outcome") or "").lower():
                    log(f"[Fleet] {name} decommissioned")  # our own choice: not posted
                elif status == "complete":
                    lines.append(f"🏁 {clock.stamp(game_day=m.get('endDay'))} **{name}** completed. {m.get('outcome') or ''}".rstrip())
                elif status == "failed":
                    lines.append(f"🚨 {clock.stamp(game_day=m.get('endDay'))} **{name}** failed! {m.get('outcome') or ''}".rstrip())
                else:
                    where = orbit_text(m.get("segmentId"), m.get("destinationId"))
                    where = f" in **{where}**" if where else ""
                    lines.append(f"⚠️ {clock.stamp()} **{name}**{where} is now **{status}** (was {old}).")

            for mission_id in safe_mode.keys() - self.safe_mode.keys():
                if safe_mode[mission_id].get("eventId") is not None:
                    continue  # hazard-driven: summarised in the defense alerts instead
                name = missions.get(mission_id, {}).get("name") or f"Craft #{mission_id}"
                lines.append(f"⚠️ {clock.stamp()} **{name}** entered safe mode.")

            for loss in losses:
                if isinstance(loss.get("id"), int) and loss["id"] > self.newest_loss:
                    lines.append(f"💀 {clock.stamp(game_day=loss.get('day'))} **{loss.get('name')}** ({loss.get('type')}) lost: "
                                 f"{loss.get('cause')} | Crew lost {loss.get('crewLost', 0)}, saved {loss.get('crewSaved', 0)} "
                                 f"| Value lost {money(loss.get('valueLost'))}")

        self.statuses = {mission_id: m.get("status") for mission_id, m in missions.items()}
        self.queued = {mission_id: bool(m.get("rangeQueued")) for mission_id, m in missions.items()}
        self.safe_mode = safe_mode
        self.newest_loss = max(self.newest_loss if not first else 0, newest_id(losses))
        lines += self.capsules(data.get("capsules") or [])
        return lines

    def capsules(self, items):
        """/craft capsules[]: "Our crews' escape capsules still out, and crews we hold" (spec). Never sampled yet:
        the first is saved to captures/ and every one is posted in plain words, a new one and one no longer listed.
        Remembered in state.json, so a restart doesn't post them again, and one already out at a first start is."""
        ctx, clock = self.ctx, self.ctx["clock"]
        state = ctx["state"]
        known = state.get("capsules_seen") or {}
        now = {capsule_key(item): item for item in items if isinstance(item, dict)}
        lines = []
        for key in now.keys() - known.keys():
            if "capsules" not in ctx["captured"]:
                ctx["captured"].add("capsules")
                capture("craft-capsules", now[key])
            lines.append(f"🆘 **[ESCAPE CAPSULES / CAPTIVES]** {clock.stamp()} {capsule_text(ctx, now[key])}")
        for key in known.keys() - now.keys():
            lines.append(f"🛟 {clock.stamp()} No longer listed (picked up, handed over or lost): "
                         f"{known[key].get('text', key)}")
        state.set("capsules_seen", {key: {"text": capsule_text(ctx, item)} for key, item in now.items()})
        return lines


class MiningWatcher(Watcher):
    path, interval, header = "/mining", INTERVAL_MINING, "MINING UPDATE"

    def update(self, data, first):
        clock = self.ctx["clock"]
        orders = {}
        for fleet in data.get("mining", []):
            for order in fleet.get("orders", []):
                orders[(order.get("placeId"), order.get("goodId"))] = order
        lines = []

        for (place, good), order in orders.items():
            status = order.get("status")
            where, what = place_name(self.ctx, place), good_name(self.ctx, good)
            if first:
                lines.append(f"⛏️ **[MINING ORDER]** **{where}** ({what}) | {status}")
            elif self.statuses.get((place, good)) != status:
                lines.append(f"⛏️ {clock.stamp()} **{where}** ({what}) | {status} | Dug: {order.get('dug') or 0:,.0f} t")

        self.statuses = {key: order.get("status") for key, order in orders.items()}
        return lines


class OreBodiesWatcher(Watcher):
    path, interval, header = "/mining/bodies", INTERVAL_MINING, "RESOURCE DEPLETION"

    def update(self, data, first):
        clock = self.ctx["clock"]
        # Captured asteroids are places like "rock:pd-9000-2"; their names ("2030 AL-B") are only here.
        for body in data.get("oreBodies", []):
            if body.get("placeId") and body.get("name"):
                self.ctx["place_names"].setdefault(body["placeId"], body["name"])
        depleted = {}
        for body in data.get("oreBodies", []):
            for reserve in body.get("reserves", []):
                if reserve.get("initial", 0) > 0 and reserve.get("remaining", 0) <= 0:
                    depleted[(body.get("placeId"), reserve.get("goodId"))] = body.get("name") or body.get("placeId")
        lines = []

        if first:
            log(f"[Startup] {len(depleted)} ore reserves already exhausted.")
        else:
            for (place, good), name in depleted.items():
                if (place, good) not in self.depleted:
                    lines.append(f"⚠️ {clock.stamp()} **{name}** (`{place}`) has run out of **{good}**.")

        self.depleted = depleted
        return lines


def passes_tier_filter(offer):
    tier = (offer.get("tier") or "").lower()
    if not tier:
        return True
    allowed = {
        "newcomer": {"newcomer", "mid", "leader"},
        "mid": {"mid", "leader"},
        "leader": {"leader"},
    }.get(CONTRACT_FILTER_TIER.lower())
    return allowed is None or tier in allowed


def standing_tier(value):
    """The game's tier names (from its standing logs; exact edges unconfirmed)."""
    if value <= REFUSES_CONTRACTS_AT:
        return "Hostile"
    if value <= -10:
        return "Unfriendly"
    if value < 10:
        return "Neutral"
    if value < 50:
        return "Friendly"
    return "Trusted"


FACTION_SHORT_NAMES = {"morningstar": "Morningstar", "zvezda": "Zvezda"}


def faction_short(faction_id):
    """'C-7', 'WSA', 'UBN', 'Morningstar', 'Zvezda': short enough for the advisor's one line."""
    return FACTION_SHORT_NAMES.get(faction_id) or FACTION_NAMES.get(faction_id, faction_id or "?").split(" (")[0]


# Earth's ballot phases. Only "voting" has been seen; the others are guesses and fall back to the raw phase.
BALLOT_PHASES = {"voting": "voting open", "nominating": "nominations open", "nominations": "nominations open",
                 "closed": "closed", "done": "closed"}


def policy_name(policy):
    """'traffic_control' -> 'Traffic control'. The API gives policy ids only, no names or effects.
    Nominations and votes may be ids or items; an item is named by its id or policyId."""
    if isinstance(policy, dict):
        pid = policy.get("id") or policy.get("policyId")
        return policy_name(pid) if isinstance(pid, str) else describe(policy)
    text = str(policy or "?").replace("_", " ")
    return text[:1].upper() + text[1:]


STANDING_IF_DONE = "If done: "


def standing_day_cap(party):
    """Raw standing a faction ("faction") or the UEG ("ueg") gives at most in a real day: 5.5 and 3.9."""
    top = STANDING_PACE_TOP[party]
    return -STANDING_PACE_FALLOFF / STANDING_PACE_REAL_DAYS * math.log(1 - top / STANDING_PACE_FALLOFF)


def standing_room(ctx, party_id):
    """Raw standing still to earn today with a faction id or "ueg" (what's left of the day's allowance)."""
    used = (ctx.get("standing_pace") or {}).get(party_id) or 0
    return max(0.0, standing_day_cap("ueg" if party_id == "ueg" else "faction") - used)


def standing_gain_today(ctx, faction_id, gain):
    """What a raw gain is worth now: cut to today's room, then by the returns at our standing (the game's VKe)."""
    current = (ctx.get("standing") or {}).get(faction_id) or 0
    return min(gain, standing_room(ctx, faction_id)) * max(0.0, 1 - max(0.0, current) / STANDING_PACE_FALLOFF)


def standing_advice(ctx, faction_id, gain, hits_rivals):
    """One line on what a job would do to our standing, e.g.
    'If done: +1.3 Morningstar (+92 → +93), −5 C-7 (+6 → +1), −5 UBN (−54 → −59)'
    plus tier changes and a ⛔ if the client refuses our contracts. '' for non-faction clients.
    The gain is what the standing pace lets through today; rivals' losses are never limited.
    No 🏛️: that's kept for standing that has actually changed."""
    if not faction_id or faction_id not in FACTION_RIVALS:
        return ""
    standing = ctx.get("standing") or {}
    parts, changes = [], []

    def move(fid, delta, text):
        old = standing.get(fid)
        if old is None:
            return f"{text} {faction_short(fid)}"
        new = max(-100.0, min(100.0, old + delta))
        if standing_tier(new) != standing_tier(old):
            changes.append(f"{faction_short(fid)} {'drops' if new < old else 'rises'} to **{standing_tier(new)}**")
        return f"{text} {faction_short(fid)} ({signed(old)} → {signed(new)})"

    earned = standing_gain_today(ctx, faction_id, gain) if gain > 0 else gain
    if gain > 0 and earned < 0.05:
        parts.append(f"no standing with {faction_short(faction_id)} (capped today)")
    else:
        parts.append(move(faction_id, earned, signed_tenths(earned)))
    # hits_rivals: True takes RIVAL_PENALTY from each rival; a number (an offer's own standing.rival) takes that.
    penalty = RIVAL_PENALTY if hits_rivals is True else float(hits_rivals or 0)
    if penalty:
        parts += [move(rival, -penalty, signed_tenths(-penalty)) for rival in sorted(FACTION_RIVALS[faction_id])]
    text = STANDING_IF_DONE + ", ".join(parts)
    if changes:
        text += " | ⚠️ " + "; ".join(changes)
    current = standing.get(faction_id)
    if current is not None and current <= REFUSES_CONTRACTS_AT:
        text += (f"\n⛔ **{faction_short(faction_id)} refuses our contracts** at {signed(current)} "
                 f"({signed(REFUSES_CONTRACTS_AT)} or below)")
    return text


def signed(value):
    """'+4', '−54' (a real minus sign, as the game shows it)."""
    return f"{value:+.0f}".replace("-", "−")


def signed_tenths(value):
    """'+2.3', or '+4' when it's a whole number."""
    return signed(value) if abs(value - round(value)) < 0.05 else f"{value:+.1f}".replace("-", "−")


def offer_label(offer, ambitions=None):
    """The tag in a board post: the tier, or AMBITION · <its name> / CODEX for the offers that have none."""
    if offer.get("ambitionId") is not None:
        name = (ambitions or {}).get(offer["ambitionId"])
        return f"AMBITION · {name}" if name else "AMBITION"
    if offer.get("kind") == "codex_recover":
        return "CODEX"
    return (offer.get("tier") or "special").upper()


def offer_standing(offer):
    """(gain, what each rival loses) if the offer is done. Board offers carry their own standing {gain, rival} since
    build HML7qYMP (a survey for an Ambition: +4 and nothing off rivals). A Codex recovery lists none: the glossary
    puts it in the band of a faction's medium job. Otherwise a full contract: +10, −5 each rival."""
    standing = offer.get("standing")
    if isinstance(standing, dict) and isinstance(standing.get("gain"), (int, float)):
        return standing["gain"], standing.get("rival") or 0
    if offer.get("kind") == "codex_recover":
        return STANDING_GAIN["medium"], 0
    return STANDING_GAIN["contract"], True


def describe_offer(offer, clock, ambitions=None):
    tier = offer_label(offer, ambitions)
    reward = offer.get("reward") or {}
    rp = f" + {reward['rp']} RP" if reward.get("rp") else ""
    return (f"{clock.stamp(game_day=offer.get('postedDay'))} **[{tier}] {offer.get('title')}** | "
            f"Client: {offer.get('client')} | Reward: `{money(reward.get('money'))}{rp}`")


class ContractsWatcher(Watcher):
    path, interval, header = "/contracts", INTERVAL_CONTRACTS, "CONTRACT BOARD"

    # An offer that vanishes before it expires was won by someone. The board's
    # won[] list names the company; give it a couple of polls to show up there.
    WON_LOOKUP_POLLS = 2

    # Lists whose items we've never seen: announced field by field, the first of each saved to captures/.
    OPEN_LISTS = (("bounties", "💰", "BOUNTY"), ("fuelRequests", "🛟", "RESCUE CONTRACT"),
                  ("rescues", "🛟", "RESCUE UNDER WAY"))

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.vanished = {}  # offer id -> (offer, polls waited)
        self.jobs, self.orders, self.lists = {}, {}, {}

    def with_advice(self, text, faction_id, gain, hits_rivals):
        advice = standing_advice(self.ctx, faction_id, gain, hits_rivals)
        return f"{text}\n{advice}" if advice else text

    def offer_text(self, offer):
        # A board contract delivered counts in full: +10 with the client, −5 with each of its rivals.
        return self.with_advice(describe_offer(offer, self.ctx["clock"], self.ctx.get("ambition_names")),
                                offer.get("factionId"), *offer_standing(offer))

    def update(self, data, first):
        clock = self.ctx["clock"]
        offers = {o["id"]: o for o in data.get("offers", []) if "id" in o}
        won = {w.get("contractId"): w for w in data.get("won", [])}
        lines = []

        if first:
            shown = sorted((o for o in offers.values() if passes_tier_filter(o)),
                           key=lambda o: o.get("postedDay", 0), reverse=True)
            if shown:
                lines.append(f"📜 **[CONTRACT BOARD - LATEST]** {self.offer_text(shown[0])}")
        else:
            for offer_id in offers.keys() - self.offers.keys():
                offer, repair = offers[offer_id], self.repair_for(offers[offer_id])
                if repair:  # shown whatever the tier filter says
                    lines.append(f"🔧 **[REPAIR | {repair}]** {self.offer_text(offer)} | "
                                 f"Craft: {offer.get('craft')} | Cause: {offer.get('cause')}")
                elif passes_tier_filter(offer):
                    lines.append(f"📜 {self.offer_text(offer)}")
            for offer_id in self.offers.keys() - offers.keys():
                offer = self.offers[offer_id]
                if passes_tier_filter(offer) and clock.now_game_day() < offer.get("expiresDay", 0):
                    self.vanished[offer_id] = (offer, 0)
            lines += self.report_taken(won)
        lines += self.check_alliance_jobs(data, first)
        lines += self.check_ship_orders(data, first)
        lines += self.check_open_lists(data, first)

        # Sieges against us always go to Discord, even as a startup baseline.
        self.ctx["discord"].post(self.webhook, "SIEGE ALERT", self.check_sieges(data, first))
        self.offers = offers
        return lines

    def check_alliance_jobs(self, data, first):
        """New alliance contracts: from factions (with a level, and they move our standing) and from other
        clients (Deep Field Institute, UEG Procurement Office, range offices, ...: no level, no standing).

        The game posts a day's jobs all at once (14 at 4 AM on Nov 3), so ALLIANCE_JOB_DIGEST_AT
        or more arrive as one digest, grouped by faction (other clients last), hardest first.
        ALLIANCE_JOB_MIN_LEVEL applies only to faction jobs.
        """
        jobs = {c["id"]: c for c in data.get("allianceContracts", []) if "id" in c}
        new = [] if first else [
            jobs[job_id] for job_id in jobs.keys() - self.jobs.keys()
            if jobs[job_id].get("wonBy") is None
            and (not jobs[job_id].get("factionId")
                 or JOB_LEVELS.get(jobs[job_id].get("level"), 0) >= JOB_LEVELS.get(ALLIANCE_JOB_MIN_LEVEL, 0))]
        self.jobs = jobs
        if len(new) < ALLIANCE_JOB_DIGEST_AT:
            return [self.job_text(job) for job in new]

        clock, by_faction = self.ctx["clock"], {}
        for job in new:
            by_faction.setdefault(job.get("factionId") or "", []).append(job)
        lines = [f"🤝 **[ALLIANCE JOBS]** {clock.stamp()} **{len(new)} new alliance jobs**, rewards shared by the work done:"]
        for faction_id, group in sorted(by_faction.items(), key=lambda g: (not g[0], faction_short(g[0]) if g[0] else "")):
            rows = []
            for job in sorted(group, key=lambda j: (-JOB_LEVELS.get(j.get("level"), 0), -(j.get("reward") or {}).get("money", 0))):
                level, reward = job.get("level"), job.get("reward") or {}
                rp = f" + {reward['rp']} RP" if reward.get("rp") else ""
                if faction_id:
                    advice = standing_advice(self.ctx, faction_id, STANDING_GAIN.get(level, 0), level == "hard")
                    advice = advice.replace("\n", " ")
                    rows.append(f"• **{(level or '?').upper()}** {job.get('title')} | `{money(reward.get('money'))}{rp}` | {advice}")
                else:
                    rows.append(f"• {job.get('title')} | {job.get('client')} | `{money(reward.get('money'))}{rp}`")
            lines.append(f"**{faction_short(faction_id) if faction_id else 'Other clients'}**\n" + "\n".join(rows))
        return lines

    def job_text(self, job):
        clock = self.ctx["clock"]
        level, reward = job.get("level"), job.get("reward") or {}
        rp = f" + {reward['rp']} RP" if reward.get("rp") else ""
        label = f"ALLIANCE JOB | {level.upper()}" if level else "ALLIANCE JOB"
        text = (f"🤝 **[{label}]** {clock.stamp(game_day=job.get('postedDay'))} "
                f"**{job.get('title')}** | Client: {job.get('client')} | Reward: `{money(reward.get('money'))}{rp}`, "
                f"shared by the work done | Open until {clock.stamp(game_day=job.get('expiresDay'))}")
        if not job.get("factionId"):
            return text  # not a faction: no standing to gain or lose
        # Easy and medium jobs cost the client's rivals nothing; hard ones do.
        return self.with_advice(text, job["factionId"], STANDING_GAIN.get(level, 0), level == "hard")

    def repair_for(self, offer):
        """'OUR CRAFT' or 'ALLY <name>' for a repair contract on our or an ally's damaged craft, else None.
        Repair offers carry {hazardId, cause, targetCompanyId, craft} (seen Nov 3: a debris cascade in LEO)."""
        target = offer.get("targetCompanyId")
        if target is None:
            return None
        if target == self.ctx["company_id"]:
            return "OUR CRAFT"
        if target in self.ctx.get("ally_ids", set()):
            return f"ALLY {self.company_name(target)}"
        return None

    def check_ship_orders(self, data, first):
        """Companies' and faction navies' orders for craft. A faction's order filled counts as a full contract."""
        clock, lines = self.ctx["clock"], []
        orders = {o["id"]: o for o in data.get("shipOrders", []) if "id" in o}
        if not first:
            for order_id in orders.keys() - self.orders.keys():
                o = orders[order_id]
                text = (f"🛳️ **[SHIP ORDER]** {clock.stamp(game_day=o.get('postedDay'))} **{o.get('buyerName')}** wants "
                        f"**{o.get('count')}× {o.get('craft')}** at **{place_name(self.ctx, o.get('placeId'))}** | "
                        f"Price `{money(o.get('price'))}` | Open until {clock.stamp(game_day=o.get('expiresDay'))}")
                lines.append(self.with_advice(text, o.get("factionId"), STANDING_GAIN["contract"], True))
            for order_id in self.orders.keys() - orders.keys():
                o = self.orders[order_id]
                how = "expired" if clock.now_game_day() >= (o.get("expiresDay") or 0) else "was filled or withdrawn"
                lines.append(f"🛳️ {clock.stamp()} {o.get('buyerName')}'s order for {o.get('count')}× {o.get('craft')} {how}.")
        self.orders = orders
        return lines

    def check_open_lists(self, data, first):
        clock, lines = self.ctx["clock"], []
        for name, icon, label in self.OPEN_LISTS:
            items = {item_key(i): i for i in data.get(name) or []}
            if not first:
                for key in items.keys() - self.lists.get(name, {}).keys():
                    if name not in self.ctx["captured"]:
                        self.ctx["captured"].add(name)
                        capture(f"contracts-{name}", items[key])
                    lines.append(f"{icon} **[{label}]** {clock.stamp()} {describe(items[key])}")
            self.lists[name] = items
        return lines

    def report_taken(self, won):
        clock, lines = self.ctx["clock"], []
        for offer_id, (offer, waited) in list(self.vanished.items()):
            winner = won.get(offer_id)
            if winner is None and waited < self.WON_LOOKUP_POLLS:
                self.vanished[offer_id] = (offer, waited + 1)
                continue
            del self.vanished[offer_id]
            if winner and winner.get("companyId") == self.ctx["company_id"]:
                continue  # ours: the contract history watcher reports it
            tier = (offer.get("tier") or "special").upper()
            who = f"**{self.company_name(winner.get('companyId'))}**" if winner else "another company (probably)"
            when = clock.stamp(game_day=winner.get("day")) if winner else clock.stamp()
            left = clock.until(offer["expiresDay"]).replace("in ", "")
            early = f" ({left} before it would have expired)" if left != "now" else ""
            lines.append(f"🏴 {when} **[{tier}] {offer.get('title')}** was taken by {who}{early}.")
        return lines

    def check_sieges(self, data, first):
        """Siege contracts posted against us: when new (even at startup) and when they close.

        The ones already announced are saved in state.json, so a restart doesn't repeat them.
        """
        clock, our_id = self.ctx["clock"], self.ctx["company_id"]
        sieges = {str(s.get("id")): s for s in data.get("siegeContracts", []) if s.get("targetId") == our_id}
        previous = self.ctx["state"].get("sieges", {}) if first else self.sieges
        lines = []
        for siege_id in sieges.keys() - previous.keys():
            s = sieges[siege_id]
            lines.append(f"🚨⚔️ **[SIEGE CONTRACT AGAINST US]** {clock.stamp(game_day=s.get('postedDay'))} "
                         f"Faction **{FACTION_NAMES.get(s.get('factionId'), s.get('factionId') or 'unknown')}** is paying **{money(s.get('reward'))}** "
                         f"to **{s.get('kind')}** our site {site_text(self.ctx, s.get('siteId'))} | Open until "
                         f"{clock.stamp(game_day=s.get('expiresDay'))}")
            # Surrendering counts as delivering the faction's contract: the one way seen to win back a
            # faction that refuses our contracts, at a cost with its rivals.
            advice = standing_advice(self.ctx, s.get("factionId"), STANDING_GAIN["contract"], True)
            if advice:
                lines[-1] += "\nIf we surrender: " + advice.replace(STANDING_IF_DONE, "", 1).split("\n⛔")[0]
        for siege_id in previous.keys() - sieges.keys():
            s = previous[siege_id]
            ended = "expired" if clock.now_game_day() >= s.get("expiresDay", 0) else "was fulfilled or withdrawn"
            lines.append(f"🏰 {clock.stamp()} The siege contract on our site {site_text(self.ctx, s.get('siteId'))} {ended}.")
        self.sieges = sieges
        self.ctx["state"].set("sieges", sieges)
        return lines

    def company_name(self, company_id):
        names = self.ctx["company_names"]
        if company_id not in names and time.monotonic() - self.ctx.get("companies_fetched", 0) > 1800:
            self.ctx["companies_fetched"] = time.monotonic()
            names.update(load_company_names(self.ctx["api"]))
        return names.get(company_id, f"Company #{company_id}")


class ContractHistoryWatcher(Watcher):
    path, params, interval, header = "/contracts/history", {"scope": "mine"}, INTERVAL_HISTORY, "CONTRACTS CLOSED"

    def update(self, data, first):
        clock = self.ctx["clock"]
        items = {i["key"]: i for i in data.get("items", []) if "key" in i}

        def describe(item):
            return (f"{clock.stamp(game_day=item.get('day'))} **{(item.get('outcome') or 'closed').upper()}**: "
                    f"{item.get('title')} | Client: {item.get('client')} | Reward: `{money((item.get('reward') or {}).get('money'))}`")

        lines = []
        if first:
            if data.get("items"):
                lines.append(f"🏆 **[CONTRACT HISTORY - LATEST]** {describe(data['items'][0])}")
        else:
            lines = [f"🏆 {describe(items[key])}" for key in items.keys() - self.keys]

        self.keys = set(items)
        return lines


HAZARD_LABELS = {"flare": ("☀️", "SOLAR FLARE")}
# A flare's proton storm (since 2026-11-05): /defense hazardIncidents[].size, the UEG Space Weather Office's
# rating. While it lasts, the sensors of craft that can't take it are blinded: they drop out of our detection
# net, and rivals can slip past. Radiation shields (each doubles what a craft can take) shorten it; ships
# docked at our station are sheltered. Only "strong" seen so far; the others are from the game's glossary.
PROTON_STORMS = {"minor": "S1 minor", "strong": "S3 strong", "severe": "S4 severe"}


def proton_storm_text(size):
    """The flare forecast's proton storm line, or "" if the forecast has no rating."""
    if not size:
        return ""
    rating = PROTON_STORMS.get(str(size).lower(), str(size))
    return (f" Proton storm **{rating}**: sensors of craft without enough radiation shielding are blinded "
            f"for a while, so rivals can slip past where they watch (ships docked at our station are sheltered).")


def hazard_label(incident):
    hazard = str(incident.get("hazardId") or "hazard")
    return HAZARD_LABELS.get(hazard, ("⚠️", hazard.upper()))


class DefenseWatcher(Watcher):
    path, interval, header = "/defense", INTERVAL_DEFENSE, "DEFENSE & HAZARD ALERT"

    def victims(self, hits):
        """'**us**, **Example Ally Co (ally)** ×2, Some Rival Inc': whose craft a hazard hit, us and allies in bold."""
        ctx, counts = self.ctx, {}
        for d in hits:
            counts[d.get("companyId")] = counts.get(d.get("companyId"), 0) + 1
        names = []
        for company_id, n in sorted(counts.items(), key=lambda c: (c[0] != ctx["company_id"],
                                                                   c[0] not in ctx.get("ally_ids", set()), -c[1])):
            if company_id == ctx["company_id"]:
                name, bold = "us", True
            elif company_id in ctx.get("ally_ids", set()):
                name, bold = f"{ctx['company_names'].get(company_id, f'Company #{company_id}')} (ally)", True
            else:
                name, bold = ctx["company_names"].get(company_id, f"Company #{company_id}") if company_id else "unowned", False
            name += f" ×{n}" if n > 1 else ""
            names.append(f"**{name}**" if bold else name)
        return ", ".join(names[:8]) + (f" +{len(names) - 8} more" if len(names) > 8 else "")

    def hit_place(self, d):
        """Where a hazard hit: a craft's placeId is a body ('nemesis'), an outpost's or station's a place."""
        if d.get("kind", "mission") == "mission":
            return body_name(self.ctx, d.get("placeId"))
        return place_name(self.ctx, d.get("placeId"))

    def damaged_text(self, d):
        """'our station' or 'our craft **Footman-3**' for one of our hazardDamage items (outposts have their own line)."""
        if d.get("kind") == "station":
            return "our station"
        mission = self.ctx.get("missions", {}).get(d.get("missionId")) or {}
        return f"our craft **{mission['name']}**" if mission.get("name") else f"our mission #{d.get('missionId')}"

    def deadline_text(self, d):
        if not d.get("deadlineDay"):
            return ""
        return f" Repair it by {self.ctx['clock'].stamp(game_day=d['deadlineDay'])} or lose it."

    def schedule_landing(self, incident):
        """Remind when a forecast hazard actually arrives, with how many of our craft are safed for it."""
        if not incident.get("startDay"):
            return
        clock = self.ctx["clock"]
        icon, label = hazard_label(incident)
        end = f" | Lasts until {clock.stamp(game_day=incident['endDay'])}" if incident.get("endDay") else ""

        def line():
            safed = self.ctx.get("safe_mode_by_event", {}).get(incident["id"], 0)
            return (f"{icon} **[{label} ARRIVING NOW]** {clock.stamp(game_day=incident['startDay'])}{end} | "
                    f"{safed} of our craft in safe mode for it")

        self.ctx["reminders"].set(f"incident:{incident['id']}", clock.real_ms_for(incident["startDay"]),
                                  self.webhook, self.header, line)

    def update(self, data, first):
        clock, our_id = self.ctx["clock"], self.ctx["company_id"]
        # Our tugs out on a debris clear leave /industry until they're back: the theft check doesn't count them.
        self.ctx["clearing_tugs"] = {t.get("id") for c in data.get("debrisClears", [])
                                     if c.get("companyId") == our_id for t in c.get("tugs") or []}
        threats = {t["id"]: t for t in data.get("threats", []) if "id" in t}
        damage = data.get("hazardDamage", [])
        # hazardIncidents items: {id, hazardId, forecastDay, startDay, endDay, hits}. A flare
        # is forecast 3 game days (3 real hours) ahead and lasts one game day.
        incidents = {i["id"]: i for i in data.get("hazardIncidents", []) if "id" in i}
        lines = []

        for incident in incidents.values():
            self.schedule_landing(incident)

        if first:
            ours = [d for d in damage if d.get("companyId") == our_id]
            upcoming = [i for i in incidents.values() if clock.now_game_day() < i.get("startDay", 0)]
            next_up = ""
            if upcoming:
                i = min(upcoming, key=lambda i: i["startDay"])
                next_up = f" | Next: **{hazard_label(i)[1]}** lands {clock.stamp(game_day=i['startDay'])}"
            debris = len(data.get("debrisFields") or [])
            outposts = len([d for d in ours if d.get("kind") == "outpost"])
            lines.append(f"🛡️ **[DEFENSE STATUS]** {len(threats)} asteroid threats tracked | "
                         f"{debris} debris field(s) active | {len(ours) - outposts} of our craft awaiting hazard repairs"
                         + (f" | {outposts} outpost(s) recovering from a hazard hit" if outposts else "") + next_up)
        else:
            for threat_id, t in threats.items():
                old = self.threats.get(threat_id)
                if old is None:
                    lines.append(f"☄️ {clock.stamp(game_day=t.get('discoveredDay'))} **New asteroid threat {t.get('name')}** "
                                 f"({t.get('diameterM')} m) | Arrives {clock.stamp(game_day=t.get('arrivalDay'))} "
                                 f"| Impactors needed: {t.get('impactorsNeeded')}")
                elif t.get("outcome") and t.get("outcome") != old.get("outcome"):
                    lines.append(f"☄️ {clock.stamp()} Asteroid **{t.get('name')}**: outcome **{t.get('outcome')}**")

            for incident_id in incidents.keys() - self.incidents.keys():
                i = incidents[incident_id]
                icon, label = hazard_label(i)
                when = f"Lands {clock.stamp(game_day=i.get('startDay'))}" \
                    if i.get("startDay") else "Landing time unknown"
                until = f" and lasts until {clock.stamp(game_day=i['endDay'])}" if i.get("endDay") else ""
                lines.append(f"{icon} **[{label} FORECAST]** {clock.stamp(game_day=i.get('forecastDay'))} "
                             f"{when}{until}." + proton_storm_text(i.get("size")))

            new_damage = [d for d in damage if isinstance(d.get("id"), int) and d["id"] > self.newest_damage]
            events = {}
            for d in new_damage:
                events.setdefault((d.get("hazardId"), d.get("hitDay")), []).append(d)
            for (hazard, hit_day), hits in events.items():
                ours = [d for d in hits if d.get("companyId") == our_id]
                if len(hits) == 1:
                    where = f"at **{self.hit_place(hits[0])}**"
                else:
                    kinds = {}  # hazardDamage kind: "mission" (a craft), "station" or "outpost"
                    for d in hits:
                        kind = "craft" if d.get("kind", "mission") == "mission" else d["kind"]
                        kinds[kind] = kinds.get(kind, 0) + 1
                    where = "across **" + " and ".join(
                        f"{n} {k}" + ("s" if n > 1 and k != "craft" else "") for k, n in kinds.items()) + "**"
                lines.append(f"🚨 {clock.stamp(game_day=hit_day)} **[HAZARD STRIKE | {str(hazard).upper()}]** "
                             f"struck {where}: {self.victims(hits)}.")
                # hazardDamage items carry deadlineDay: whatever was damaged is lost unless repaired by then.
                for d in ours:
                    place = self.hit_place(d)
                    if d.get("kind") == "outpost":
                        # Outposts aren't lost: the game says their sites make nothing until deadlineDay,
                        # "while its machines dig out", and then work again on their own.
                        back = (f" until {clock.stamp(game_day=d['deadlineDay'])}" if d.get("deadlineDay") else "")
                        lines.append(f"🔧 Our outpost at **{place}** was hit: its mines and plants make nothing{back}. "
                                     f"It works again on its own.")
                    else:
                        lines.append(f"🔧 {first_upper(self.damaged_text(d))} at {place} was damaged.{self.deadline_text(d)}")
        # Our damaged outposts, so production stalls there are blamed on the damage, not on crew.
        self.ctx["damaged_outposts"] = {d.get("placeId"): d.get("deadlineDay") for d in damage
                                        if d.get("companyId") == our_id and d.get("kind") == "outpost"}

        lines += self.debris_changes(data.get("debrisFields") or [], first)
        self.threats = threats
        self.incidents = incidents
        self.newest_damage = max(0 if first else self.newest_damage, newest_id(damage))
        return lines

    def debris_changes(self, fields, first):
        """New debris fields (e.g. from a failed launch), remembered in state.json so a restart misses none.

        debrisFields items: {id, cascadeId, generation, startDay, endDay, strength, companyId, cause, strikes, fined}.
        Strikes from them already come through hazardDamage, so only the field itself is announced here."""
        clock, state = self.ctx["clock"], self.ctx["state"]
        fields = {str(f["id"]): f for f in fields if "id" in f}
        seen = state.get("debris_fields")
        lines = []
        if seen is None:  # first run ever: record what's out there without announcing it
            if fields:
                log(f"[Debris] {len(fields)} debris field(s) already active: "
                    + "; ".join(f.get("cause") or "?" for f in fields.values()))
        else:
            for field_id in sorted(fields.keys() - set(seen), key=int):
                f = fields[field_id]
                cascade = (f" | Cascade generation {f['generation']} (from field #{f.get('cascadeId')})"
                           if (f.get("generation") or 1) > 1 else "")
                ends = (f" | Lasts until {clock.stamp(game_day=f['endDay'])}"
                        if f.get("endDay") else "")
                lines.append(f"💫 {clock.stamp(game_day=f.get('startDay'))} **[DEBRIS FIELD]** {f.get('cause') or 'New debris'} | "
                             f"Caused by {self.victims([f])} | Strength {f.get('strength')}{cascade}{ends}")
            for field_id in set(seen) - fields.keys():
                log(f"[Debris] Debris field #{field_id} has cleared")
        state.set("debris_fields", sorted(fields.keys(), key=int))
        return lines


class ChatWatcher(Watcher):
    path, interval = "/chat", INTERVAL_CHAT
    channels = (("global", "general", "💬", "GLOBAL CHAT"), ("alliance", "alliance", "🤝", "ALLIANCE CHAT"))

    def __init__(self, ctx):
        super().__init__(ctx, None)
        self.newest = {}  # channel -> highest message id seen (ids only grow)

    def poll(self):
        data = self.ctx["api"].get(self.path)
        if data is None:
            return
        first = not self.seeded
        for channel, webhook_key, icon, header in self.channels:
            messages = sorted(data.get(channel, []), key=lambda m: m.get("id", 0))
            if first:
                fresh = messages[-1:]
            else:
                fresh = [m for m in messages if m.get("id", 0) > self.newest.get(channel, 0)]
            self.newest[channel] = max(self.newest.get(channel, 0), newest_id(messages))

            label = f" **[{header} - LATEST]**" if first else ""
            lines = [f"{icon}{label} {self.ctx['clock'].stamp(real_ms=m.get('sentAt'))} "
                     f"**{m.get('companyName')}**: {m.get('text', '')}" for m in fresh]
            self.publish(self.ctx["webhooks"][webhook_key], header + " UPDATE", lines, first)
        self.seeded = True


class Countdown:
    """Announces each COUNTDOWN_WARNINGS_HOURS mark once as something counts down.

    check() returns the mark just crossed (e.g. 48), or None. Recovering above
    the highest mark resets it. The marks reached are kept in state.json under
    `name`, so a restart announces a mark crossed while we were down and doesn't
    repeat one already given; something seen for the first time is only recorded.
    """

    def __init__(self, state=None, name=None, marks=None):
        self.state, self.name, self.marks = state, name, marks
        saved = state.get(name, {}) if state is not None and name else {}
        self.levels = dict(saved)  # "|"-joined key -> lowest mark reached (None: above them all)
        # key -> the amount when its level was last set. With an amount given, a level only goes back
        # up (re-arming its warnings) on a real refill, not because the use rate wobbled.
        self.amounts = dict(state.get(f"{name}_amounts", {})) if state is not None and name else {}
        self.checked = set()       # keys checked since the last prune()

    @staticmethod
    def _key(key):
        return "|".join(str(part) for part in key) if isinstance(key, tuple) else str(key)

    def check(self, key, hours, first, amount=None):
        crossed = [h for h in (self.marks or COUNTDOWN_WARNINGS_HOURS) if hours is not None and hours <= h]
        level = min(crossed) if crossed else None
        key = self._key(key)
        known, old = key in self.levels, self.levels.get(key)
        self.checked.add(key)
        rising = old is not None and (level is None or level > old)
        if rising and amount is not None and self.amounts.get(key) is not None \
                and amount < self.amounts[key] * COUNTDOWN_REFILL_FACTOR:
            # Looks further from running out only because the use rate dipped: keep the warnings given.
            return None
        if level != old or key not in self.amounts:
            self.amounts[key] = amount
        self.levels[key] = level
        self.save()
        if level is None or (old is not None and level >= old) or (first and not known):
            return None
        return level

    def hold(self, key):
        """Not counting down right now (a stock not falling this poll): keep its warnings as they are."""
        key = self._key(key)
        if key in self.levels:
            self.checked.add(key)

    def prune(self):
        """Forget things not checked this poll (a stock that's gone, a site removed), so the saved list stays small."""
        self.levels = {k: v for k, v in self.levels.items() if k in self.checked}
        self.amounts = {k: v for k, v in self.amounts.items() if k in self.levels}
        self.checked = set()
        self.save()

    def save(self):
        if self.state is not None and self.name:
            self.state.set(self.name, dict(self.levels))
            self.state.set(f"{self.name}_amounts", dict(self.amounts))


def hours_text(hours):
    return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.0f}h" if hours >= 1 else f"{hours * 60:.0f}m"


def units_text(units, names=None):
    names = names or {}
    return ", ".join(f"{count}× {names.get(unit, unit)}" for unit, count in sorted((units or {}).items(), key=lambda kv: -kv[1]))


def tidy_id(text):
    """'north_queensland' -> 'North queensland': a readable fallback for an id the reference tables don't name."""
    return first_upper(str(text).replace("_", " "))


def first_upper(text):
    """Capitalise only the first letter (str.capitalize() lowercases the rest, names included)."""
    return text[:1].upper() + text[1:]


def place_name(ctx, place_id):
    """A surface place, region or station ('pallas/a' -> 'Pallas north 179°E'); a body name if it's a body."""
    if place_id is None:
        return "an unknown place"
    return ((ctx.get("place_names") or {}).get(place_id) or (ctx.get("body_names") or {}).get(place_id)
            or tidy_id(place_id))


def site_text(ctx, place_id):
    """'**Tros** on Ganymede': a place in bold with its world, unless the name already says it ('Pallas north 179°E')."""
    name = place_name(ctx, place_id)
    body_id = (ctx.get("place_bodies") or {}).get(place_id)
    body = body_name(ctx, body_id) if body_id else ""
    return f"**{name}** on {body}" if body and body.lower() not in name.lower() else f"**{name}**"


def body_name(ctx, body_id):
    """A planet, moon or asteroid ('moon' -> 'Moon', where place_name would give the Shackleton rim outpost)."""
    if body_id is None:
        return "an unknown place"
    return (ctx.get("body_names") or {}).get(body_id) or (ctx.get("place_names") or {}).get(body_id) or tidy_id(body_id)


def game_day_of_date(text):
    """The game day a calendar date ('2032-09-21') starts on: game days count from noon, 1 Jan 2000."""
    return (datetime.strptime(text, "%Y-%m-%d") - datetime(2000, 1, 1)).days - 0.5


def ballot_closing_text(clock, closes_day, mark, ballot_line="", vote_line=""):
    """'🗳️ Earth's ballot closes in 24h' with when, what's on it and, in full mode, our vote."""
    rows = [f"Closes: {clock.stamp(game_day=closes_day)}"] + [r for r in (ballot_line, vote_line) if r]
    return f"🗳️ {clock.stamp()} **Earth's ballot closes in {mark}h** ({mark}h warning)\n" + \
        "\n".join(f"• {r}" for r in rows)


def stall_text(stamp, place, reasons, state="stopped", note=None):
    """One message for the sites that stopped (or slowed) at one place: reasons is {why: ["factory (Steel)", ...]}.
    A single site keeps the one-line form; several get a count and a line per reason. A note is the cause
    shared by every site (missing upkeep goods); then a why only adds to it, and may be empty."""
    count = sum(len(sites) for sites in reasons.values())
    icon = "⏸️" if state == "stopped" else "🐢"
    if count == 1:
        why, sites = next(iter(reasons.items()))
        if note:
            why = f"{note}. {first_upper(why)}." if why and why != "mines" else note
        return f"{icon} {stamp} **{sites[0]} at {place} {state}:** {why}"
    head = f"{icon} {stamp} **{count} sites at {place} {state}:**" + (f" {note}" if note else "")
    return head + "".join("\n• " + (f"**{why}:** " if why else "") + ", ".join(sites) for why, sites in reasons.items())


def duration_text(seconds):
    """A real span: '45m', '3h 40m', '2h'."""
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours and minutes else f"{hours}h" if hours else f"{minutes}m"


def qty_text(qty):
    """A stock amount: '0', '0.75', '2', '1,072'."""
    return f"{qty:,.0f}" if abs(qty) >= 100 else f"{qty:,.2f}".rstrip("0").rstrip(".")


EARTH_ORBITS = {"leo": "LEO", "meo": "MEO", "geo": "GEO"}


def orbit_text(segment_id, destination_id=None):
    """A craft's orbit from its segmentId: "ganymede:low" -> "low Ganymede orbit", "earth:geo" -> "GEO".
    Without a segment, the destination ("ganymede" -> "Ganymede"); "" if neither is known."""
    body, _, band = (segment_id or "").partition(":")
    if not body:
        return (destination_id or "").replace("_", " ").title() if destination_id else ""
    if body == "earth" and band in EARTH_ORBITS:
        return EARTH_ORBITS[band]
    body = body.replace("_", " ").title()
    if band == "sync":
        return f"{body} synchronous orbit"
    return f"{band} {body} orbit" if band else body


def good_name(ctx, good_id):
    return ctx["good_names"].get(good_id, good_id)


class CompanyWatcher(Watcher):
    path, interval, header = "/company", INTERVAL_COMPANY, "COMPANY UPDATE"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.levels, self.blocked, self.research_queue, self.upgrades = {}, {}, None, {}
        self.research_empty_polls = 0
        self.countdown = Countdown(ctx.get("state"), "countdown_supplies")

    def blocked_eta(self, reason):
        """When a blocked queue should clear, from our craft in transit ("" if we can't tell)."""
        clock, now = self.ctx["clock"], self.ctx["clock"].now_game_day()
        ours = [m for m in (self.ctx.get("missions") or {}).values() if m.get("companyId") == self.ctx["company_id"]
                and m.get("status") in ("transit", "preparing") and (m.get("arriveDay") or 0) > now]
        if "Mission Control is at capacity" in reason:
            # A probe's slot frees the moment it arrives.
            probes = sorted((m for m in ours if m.get("payloadId") == "flyby_probe" and m.get("status") == "transit"),
                            key=lambda m: m["arriveDay"])
            if not probes:
                return " | ⏱️ No probe en route to free a slot."
            first_probe = probes[0]
            more = f" ({len(probes)} probes en route)" if len(probes) > 1 else ""
            return (f" | ⏱️ A slot frees when **{first_probe.get('name')}** arrives "
                    f"{clock.stamp(game_day=first_probe['arriveDay'])}{more}")
        # Parts on the way: the reason names the cargo pods carrying them. A pod still preparing
        # already has a projected arriveDay, which can slip if its launch does.
        pods = sorted((m for m in ours if m.get("name") and re.search(re.escape(m["name"]) + r"(?![\w-])", reason)),
                      key=lambda m: m["arriveDay"])
        return "".join(f"\n   ⏱️ **{m['name']}** lands {clock.stamp(game_day=m['arriveDay'])}"
                       + (" (not launched yet)" if m.get("status") == "preparing" else "") for m in pods)

    def update(self, data, first):
        clock, ctx = self.ctx["clock"], self.ctx
        names = ctx["facility_names"]
        lines = []

        sustainment = data.get("sustainment", [])
        ctx["our_places"] = {s.get("placeId") for s in sustainment} | {s.get("placeId") for s in data.get("stations", [])}
        ctx["company_body"] = data
        ctx["crew_by_place"] = {s.get("placeId"): (s["crew"], s["needed"]) for s in sustainment
                                if isinstance(s.get("crew"), (int, float)) and isinstance(s.get("needed"), (int, float))}
        ctx["standing"] = data.get("standing") or ctx.get("standing") or {}  # for the standing advisor

        # Construction: a facility's level going up means its upgrade finished.
        levels = data.get("facilities") or {}
        if not first:
            for facility, level in levels.items():
                if level > self.levels.get(facility, level):
                    lines.append(f"🏗️ {clock.stamp()} **{names.get(facility, facility)}** upgraded to level **{level}**.")
        self.levels = dict(levels)

        # Site upgrades (factories and the like) are workBills with upgrade true, one level at a time;
        # a bill gone at or after its readyDay means that level finished, and a gone early one was cancelled.
        # Facility bills (kind "facility") are skipped: the level rise above already announces those.
        upgrades = {(b.get("kind"), b.get("ref"), b.get("what")): b for b in data.get("workBills") or []
                    if b.get("upgrade") and b.get("kind") != "facility"}
        if not first:
            for key, bill in self.upgrades.items():
                ready = bill.get("readyDay")
                if key not in upgrades and ready is not None and ready - clock.now_game_day() <= UPGRADE_DONE_SLACK_DAYS:
                    # The game's own text, e.g. "at our place at Mare Humorum": the middle bit adds nothing.
                    where = f" {bill['where'].replace('at our place at ', 'at ')}" if bill.get("where") else ""
                    lines.append(f"🏗️ {clock.stamp()} **Upgrade complete:** {bill.get('what', 'a site')}{where}.")
        self.upgrades = upgrades

        # Queued actions the game says are blocked (the research queue's reasons are just "waiting",
        # and "Level N is being built" only means the previous level is still under way).
        queues = data.get("queues", [])
        blocked = {q["id"]: q for q in queues if q.get("reason") and q.get("list") != "research" and "id" in q
                   and not IGNORED_QUEUE_REASONS.match(q["reason"])}
        for queue_id, q in blocked.items():
            if not first and self.blocked.get(queue_id, {}).get("reason") != q["reason"]:
                lines.append(f"🚧 {clock.stamp()} {queue_heading(q)} {q['reason']}{self.blocked_eta(q['reason'])}")
        if first and blocked:
            lines.append(f"🚧 **[QUEUE]** {len(blocked)} queued action(s) blocked:\n" +
                         "\n".join(f"• {queue_label(q)}{q['reason']}{self.blocked_eta(q['reason'])}"
                                    for q in blocked.values()))
        self.blocked = blocked

        # Research queue running dry. When research finishes, the game moves the next queued item into
        # the lab, which can empty the queue for a moment, so only report it still empty on the next poll.
        research_queue = [q for q in queues if q.get("list") == "research"]
        self.research_empty_polls = 0 if research_queue else self.research_empty_polls + 1
        if first and not research_queue:
            self.research_empty_polls = 2  # reported now; don't repeat next poll
        if (first and not research_queue) or (not first and self.research_empty_polls == 2):
            job = ctx.get("research_job")
            current = f" The lab is on **{ctx['tech_names'].get(job['techId'], job['techId'])}** until " \
                      f"{clock.stamp(game_day=job.get('completeDay'))} - queue more before then." if job else ""
            lines.append(f"📭 **The research queue is empty.** {clock.stamp()}{current}")
        self.research_queue = research_queue
        ctx["research_queue"] = research_queue  # the research watcher waits for the next one to start

        # Crew supplies at each place with crew, warned at each COUNTDOWN_WARNINGS_HOURS mark.
        for place in sustainment:
            supplies = place.get("supplies") or {}
            days_left = supplies.get("netDaysLeft")
            if not place.get("crew") or days_left is None:
                continue
            hours = clock.game_days_to_hours(days_left)
            mark = self.countdown.check(("supply", place["placeId"]), hours, first)
            if mark:
                lines.append(f"🥫 {clock.stamp()} **Crew supplies at {place_name(ctx, place['placeId'])} run out in "
                             f"{hours_text(hours)}** ({mark}h warning) | {place['crew']:.0f} crew | "
                             f"{clock.stamp(game_day=clock.now_game_day() + days_left)}")
        self.countdown.prune()
        if first:
            low = [(p["placeId"], clock.game_days_to_hours(p["supplies"]["netDaysLeft"])) for p in sustainment
                   if p.get("crew") and (p.get("supplies") or {}).get("netDaysLeft") is not None
                   and clock.game_days_to_hours(p["supplies"]["netDaysLeft"]) <= max(COUNTDOWN_WARNINGS_HOURS)]
            if low:
                lines.append("🥫 **[SUPPLIES]** Running low: " +
                             ", ".join(f"{place_name(ctx, p)} ({hours_text(h)})" for p, h in low))
        return lines


def queued_launch(q):
    """(name or None, purpose or None) of a queued launch, or None for any other queued action. The purpose is
    the launch's statement ("Crew for an outpost on Pallas") or else the game's label for it."""
    action = q.get("action") or {}
    if action.get("kind") != "launch":
        return None
    request = action.get("request") or {}
    return request.get("name"), request.get("statement") or action.get("label")


def queue_heading(q):
    """The bold start of a blocked-queue post: a launch by its name and purpose when the game gives them."""
    launch = queued_launch(q)
    if not launch:
        return "**Queued action blocked:**"
    name, about = launch
    return f"**Queued launch{' ' + name if name else ''} blocked**{f' ({about})' if about else ''}:"


def queue_label(q):
    """The same for a line of the startup list: "Queued launch Footman-11 (…): ", or "" for other actions."""
    launch = queued_launch(q)
    if not launch:
        return ""
    name, about = launch
    return f"Queued launch{' ' + name if name else ''}{f' ({about})' if about else ''}: "


def keep_limited_fill_days(ctx, store, stocks, keep, has_tank=True):
    """(game days until a store fills or None, [(goodId, t/day)] still filling it, biggest first).

    Each good stops at its keep limit (or, if draining, at zero). The fill rate is the sum of
    the goods' own rates, not the store's rateT: rateT can include inflow no good accounts for
    (seen once: 30,214 t/day for a moment when the goods added up to about 6,600),
    and that leftover would otherwise count as never stopping. Propellants live in the tank
    pool and everything else in the dry pool, unless the place has no tank store.
    """
    tank = store.get("pool") == "tank"
    free, rate = store.get("freeT") or 0, 0.0
    stops = []    # (game days until this good stops changing, tonnes/day it stops contributing)
    filling = []  # goods still growing toward their limit (or with none)
    for stock in stocks:
        if stock.get("regionId") != store.get("placeId"):
            continue
        category, mass = ctx["good_info"].get(stock.get("goodId"), ("", 1))
        if has_tank and (category == "propellant") != tank:
            continue
        qty, good_rate = stock.get("qty") or 0, (stock.get("rate") or 0) * mass
        limit = keep.get((stock.get("regionId"), stock.get("goodId")))
        rate += good_rate
        if good_rate > 0 and limit is not None:
            stops.append((max(limit - qty, 0) * mass / good_rate, good_rate))
        elif good_rate < 0:
            stops.append((qty * mass / -good_rate, good_rate))
        if good_rate > 0 and (limit is None or qty < limit):
            filling.append((stock.get("goodId"), good_rate))
    filling.sort(key=lambda g: -g[1])
    return fill_after_stops(free, rate, stops), filling


def fill_after_stops(free, rate, stops):
    """Game days until free room runs out at rate, as each (stop day, t/day) contribution ends; else None."""
    elapsed = 0.0
    for stop, good_rate in sorted(stops):
        if rate > 0 and elapsed + free / rate <= stop:
            return elapsed + free / rate
        free -= rate * (stop - elapsed)
        elapsed, rate = stop, rate - good_rate
    return elapsed + free / rate if rate > 0 and free > 0 else None


class IndustryWatcher(Watcher):
    """Storage filling up, stocks our sites use running out, and production stalling."""
    path, interval, header = "/industry", INTERVAL_INDUSTRY, "INDUSTRY UPDATE"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.countdown = Countdown(ctx.get("state"), "countdown_industry")
        self.storage_low = {}     # store -> polls in a row it has looked like filling within the warnings
        self.stalls = {}          # siteId -> (heldBy, polls seen)
        self.stall_announced = {}  # (siteId, "stopped"/"slowed") -> monotonic time last announced
        self.keep_full_logged = set()  # sites held at our keep limit, logged once

    def poll(self):
        industry = self.ctx["api"].get_latest("/industry")
        flows = self.ctx["api"].get_latest("/industry/flows")
        if industry is None or flows is None:
            return
        first = not self.seeded
        lines = self.update((industry, flows), first)
        self.seeded = True
        self.publish(self.webhook, self.header, lines, first)

    def update(self, data, first):
        industry, flows = data
        clock, ctx = self.ctx["clock"], self.ctx
        lines, soon = [], []

        # Storage: the API's fullDay assumes today's rates go on forever, but a good stops arriving
        # once it reaches our keep limit, so project the fill with those limits applied.
        keep = {(r.get("placeId"), r.get("goodId")): r.get("keepMax")
                for r in (industry.get("industry") or {}).get("stockRules", [])}
        stocks = (industry.get("industry") or {}).get("stock", [])
        tank_places = {s.get("placeId") for s in industry.get("storageRoom", []) if s.get("pool") == "tank"}
        for store in industry.get("storageRoom", []):
            key = ("storage", store["placeId"], store.get("pool"))
            days, filling = (None, []) if not store.get("fullDay") else \
                keep_limited_fill_days(ctx, store, stocks, keep, store["placeId"] in tank_places)
            hours = None if days is None else clock.game_days_to_hours(days)
            # One reading isn't a trend (a freight delivery unloading looks like a flood):
            # warn only once a store has looked like filling on two polls in a row.
            low = hours is not None and hours <= max(COUNTDOWN_WARNINGS_HOURS)
            self.storage_low[key] = self.storage_low.get(key, 0) + 1 if low else 0
            if low and not first and self.storage_low[key] < 2:
                continue
            mark = self.countdown.check(key, hours, first)
            if hours is None:
                continue
            what = f"{store.get('pool', '')} storage at {place_name(ctx, store['placeId'])}"
            top = ", ".join(f"{good_name(ctx, g)} +{t:,.0f} t/day" for g, t in filling[:3]) or "nothing listed"
            if mark:
                lines.append(f"📦 {clock.stamp()} **{first_upper(what)} is full in {hours_text(hours)}** ({mark}h warning) | "
                             f"{store.get('freeT', 0):,.0f} t free | filling: {top} (keep limits counted)")
                log(f"[Storage] {store['placeId']} {store.get('pool')}: store rateT {store.get('rateT', 0):,.1f} t/day, "
                    f"goods {sum(t for _, t in filling):,.1f} t/day still filling: {filling[:8]}")
            elif first and low:
                soon.append(f"{what} full in {hours_text(hours)}")

        # Our tugs, freighters and crew pods (id -> "Crumb-3 (tug)"), for the theft check (NewsWatcher).
        remember_ships(ctx, "vehicle_history", {
            v["id"]: f"{v.get('name')} ({tidy_id(v.get('kind')).lower()})"
            for v in (industry.get("industry") or {}).get("vehicles", [])
            if "id" in v and v.get("kind") not in GROUND_VEHICLE_KINDS})
        # The diplomacy watcher checks offers that ask for our goods against these.
        ctx["stock"] = {(s.get("regionId"), s.get("goodId")): (s.get("qty") or 0, s.get("rate") or 0) for s in stocks}

        # Stocks our sites consume or need for upkeep, running down.
        uses = {(s.get("placeId"), s.get("goodId")): s for s in flows.get("stock", [])}
        for stock in (industry.get("industry") or {}).get("stock", []):
            place, good, qty, rate = stock.get("regionId"), stock.get("goodId"), stock.get("qty") or 0, stock.get("rate") or 0
            use = uses.get((place, good), {})
            consumed, upkeep = use.get("consumed") or 0, use.get("upkeep") or 0
            if rate >= 0 or qty <= 0 or (consumed >= 0 and upkeep >= 0):
                # Not falling this poll. A stock made and used at about the same rate flips between
                # falling and not, so its warnings are kept rather than forgotten and given again.
                self.countdown.hold(("stock", place, good))
                continue
            hours = clock.game_days_to_hours(qty / -rate)
            kind = "maintenance" if upkeep < 0 and consumed >= 0 else "production input"
            mark = self.countdown.check(("stock", place, good), hours, first, amount=qty)
            if mark and (hours < STOCK_ALREADY_EMPTY_HOURS or qty < STOCK_ALREADY_EMPTY_QTY):
                # e.g. "Water ice runs out in 5m" with 0.5 t left: it's empty already, and the
                # production-stall alert says so once the site stops.
                log(f"[Stock] {good} at {place}: {qty:,.1f} left, out in {hours_text(hours)}; not posted (effectively empty)")
            elif mark:
                lines.append(f"⏳ {clock.stamp()} **{good_name(ctx, good)} at {place_name(ctx, place)} runs out in "
                             f"{hours_text(hours)}** ({mark}h warning) | {kind} | {qty:,.1f} left, using {-rate:,.2f}/day")
            elif first and hours <= max(COUNTDOWN_WARNINGS_HOURS):
                soon.append(f"{good_name(ctx, good)} at {place_name(ctx, place)} out in {hours_text(hours)}")

        # Production stalls, announced once a stall has held for two polls in a row. Sites stopping at
        # the same place in the same poll are posted as one message, grouped by reason.
        # heldBy names what limits a site, not that it stopped: a site short of crew keeps running at reduced
        # output. One still producing (outputPerDay > 0) is posted as "slowed", one producing nothing as "stopped".
        # A slowed site is posted only below SLOWED_POST_BELOW_PERCENT of its full output, with that share, and
        # not again until it falls SLOWED_WORSE_BY_POINTS further or has run above it for SLOWED_RECOVERY_HOURS.
        # The game names one reason per site, so a stall blamed on crew can hide missing upkeep goods: off Earth,
        # an upkeep good counts as missing where the place has none of it, or a site there names it as its limit.
        stalls, stopped = {}, {}  # stopped: (place name, "stopped"/"slowed") -> {reason text: ["factory (Steel)", ...]}
        ctx["sites_by_place"] = {}  # placeId -> ["mine (Helium-3)", ...], for a walkout's post (NewsWatcher)
        for site in flows.get("sites", []):
            ctx["sites_by_place"].setdefault(site.get("placeId"), []).append(
                f"{site.get('kind')} ({good_name(ctx, site.get('goodId'))})")
        notes = {}  # place_state -> the upkeep stock line, for places stopped by missing upkeep goods
        crew = ctx.get("crew_by_place") or {}
        kinds, stock = ctx.get("place_kinds") or {}, ctx.get("stock") or {}
        named = {(s.get("placeId"), s.get("limitingGood")) for s in flows.get("sites", []) if s.get("heldBy") == "upkeep"}

        def upkeep_short(place):
            if kinds.get(place) in (None, "earth_region"):
                return False
            return any((stock.get((place, g)) or (0, 0))[0] <= 0 or (place, g) in named for g in UPKEEP_GOODS)

        def stock_qty(place, good):
            return qty_text((stock.get((place, good)) or (0, 0))[0])

        def at_keep_limit(site):
            limit = keep.get((site.get("placeId"), site.get("goodId")))
            have = (stock.get((site.get("placeId"), site.get("goodId"))) or (0, 0))[0]
            return isinstance(limit, (int, float)) and have >= limit - KEEP_LIMIT_SLACK

        state_store = ctx.get("state")
        slow = {site_key: dict(posted) for site_key, posted in  # a copy, so State.set sees what changed
                ((state_store.get("slowed_posted") if state_store else getattr(self, "slowed_posted", None)) or {}).items()}
        slow_now = set()  # sites slowed below SLOWED_POST_BELOW_PERCENT this poll

        for site in flows.get("sites", []):
            held, site_id = site.get("heldBy"), site.get("siteId")
            if not held or held in IGNORED_STALL_REASONS:
                continue
            if held == "staffing" and at_keep_limit(site):
                # A refinery or factory whose own good has reached our keep limit stops, as a full mine does, but
                # the game calls it "staffing", not "keepLimit", when the crew is a little short (build HML7qYMP).
                if site_id not in self.keep_full_logged:
                    log(f"[Stall] {site.get('kind')} ({site.get('goodId')}) at {site.get('placeId')}: its good is at "
                        f"our keep limit (game says {held}); not posted")
                self.keep_full_logged.add(site_id)
                continue
            self.keep_full_logged.discard(site_id)
            state = "slowed" if (site.get("outputPerDay") or 0) > 0 else "stopped"
            reason = f"{held}/{state}"  # slowed, then stopped, is a new stall
            old_reason, seen = self.stalls.get(site_id, (None, 0))
            seen = seen + 1 if old_reason == reason else 1
            stalls[site_id] = (reason, seen)
            announce_key = (site_id, state)  # a site that slowed and then stopped gets both posts
            pct = None
            if state == "slowed":
                nominal = site.get("nominalPerDay") or 0
                pct = 100 * (site.get("outputPerDay") or 0) / nominal if nominal > 0 else None
                if pct is not None and pct >= SLOWED_POST_BELOW_PERCENT:
                    continue  # hardly slowed: the game names a limit, but little or nothing is lost
                site_key = str(site_id)
                slow_now.add(site_key)
                posted = slow.get(site_key)
                if first:
                    slow.setdefault(site_key, {"pct": pct, "okSince": None})
                    continue
                worse = posted is not None and pct is not None and posted.get("pct") is not None \
                    and pct <= posted["pct"] - SLOWED_WORSE_BY_POINTS
                if seen < 2 or (posted is not None and not worse):
                    continue
                slow[site_key] = {"pct": pct, "okSince": None}
            if first:
                self.stall_announced[announce_key] = time.monotonic()
                continue
            recently = time.monotonic() - self.stall_announced.get(announce_key, -1e9) < STALL_REPEAT_HOURS * 3600
            if state == "slowed" or (seen == 2 and not recently):
                self.stall_announced[announce_key] = time.monotonic()
                walkout = (ctx.get("walkouts") or {}).get(site.get("placeId"))
                if held == "staffing" and walkout and clock.now_game_day() < walkout:
                    # A Clear Sky walkout stops every site there and reads as "staffing", even at a crewless mine.
                    # The walkout post (NewsWatcher) already listed them, with when work resumes.
                    log(f"[Stall] {site.get('kind')} ({site.get('goodId')}) at {site.get('placeId')} {state}: "
                        f"crews walked out for Clear Sky until day {walkout}; not posted")
                    continue
                limiting = good_name(ctx, site.get("limitingGood")) if site.get("limitingGood") else "goods"
                if held == "staffing" and site.get("placeId") in (ctx.get("damaged_outposts") or {}):
                    # The game reports a hazard-hit outpost's sites as short of staff, even crewless mines.
                    # When they work again is in the 🔧 hazard alert.
                    held = "damaged"
                place_id = site.get("placeId")
                staff = crew.get(place_id)
                short_staffed = staff is not None and staff[0] < staff[1]
                staff_note = f" ({staff[0]:.1f} of {staff[1]:g})" if staff is not None and short_staffed else ""
                place_state = (place_name(ctx, place_id), state)
                if held != "damaged" and state == "stopped" and upkeep_short(place_id):
                    # Every site there is stopped by it: the stock of both goods heads the post, and refineries
                    # and factories also get the crew shortage, since that will slow them once goods arrive.
                    notes[place_state] = "can't pay upkeep (stock: " + ", ".join(
                        f"{good_name(ctx, g)} {stock_qty(place_id, g)}" for g in UPKEEP_GOODS) + ")"
                    crew_note = short_staffed and site.get("kind") in CREWED_SITE_KINDS
                    why = f"crew also short{staff_note}" if crew_note else ("mines" if short_staffed else "")
                elif held == "staffing" and state == "stopped" and short_staffed:
                    # A crew shortage only slows a site, so one making nothing is missing something else too.
                    why = f"crew is short{staff_note}, but that only slows production, so something else is missing too"
                else:
                    limiting_qty = f" (stock {stock_qty(place_id, site.get('limitingGood'))})" \
                        if held == "input" and site.get("limitingGood") else ""
                    why = {"input": f"out of {limiting}{limiting_qty}",
                           "damaged": "outpost hit by a hazard, works again on its own",
                           "staffing": f"not enough crew{staff_note}",
                           "upkeep": f"can't pay upkeep ({limiting})"}.get(held, held)
                share = f", {pct:.0f}%" if pct is not None else ""
                what = f"{site.get('kind')} ({good_name(ctx, site.get('goodId'))}{share})"
                stopped.setdefault(place_state, {}).setdefault(why, []).append(what)
        # A slowed site that has run above the bar (or stopped being held) long enough may be posted again.
        now = time.time()
        for site_key, posted in list(slow.items()):
            if site_key in slow_now:
                posted["okSince"] = None
            elif posted.get("okSince") is None:
                posted["okSince"] = now
            elif now - posted["okSince"] >= SLOWED_RECOVERY_HOURS * 3600:
                del slow[site_key]
        if state_store:
            state_store.set("slowed_posted", slow)
        else:
            self.slowed_posted = slow
        for (place, state), reasons in stopped.items():
            lines.append(stall_text(clock.stamp(), place, reasons, state, notes.get((place, state))))
        self.stalls = stalls
        self.countdown.prune()
        lines += self.blocked_vehicles(industry, first)

        if first:
            summary = f"🏭 **[INDUSTRY]** {len(stalls)} sites held up (inputs, crew or upkeep)"
            lines.append(summary + (" | Running out or filling up within 72h:\n" + "\n".join(f"• {s}" for s in soon)
                                    if soon else " | nothing running out within 72h"))
        return lines


    def blocked_vehicles(self, industry, first):
        """Our freighters, tugs and trucks the game has stopped on their route ("needs 105 kg of fusion fuel more for
        this leg..."): vehicles[].blocked = {reason, since}. Each block is posted once; the ones posted are saved in
        state.json (vehicle id -> since), so a restart repeats none and still posts one that started while stopped.
        The first run ever only records them."""
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx.get("state")
        blocked = {str(v["id"]): v for v in (industry.get("industry") or {}).get("vehicles", [])
                   if "id" in v and isinstance(v.get("blocked"), dict)}
        seen = state.get("vehicles_blocked") if state else getattr(self, "vehicles_blocked", None)
        lines = []
        if seen is not None:
            for key, v in blocked.items():
                since = v["blocked"].get("since")
                if seen.get(key) == since:
                    continue
                kind = tidy_id(v.get("kind") or "vehicle").lower()
                when = f" since {clock.stamp(game_day=since)}" if since else ""
                lines.append(f"🚧 {clock.stamp()} **{first_upper(kind)} {v.get('name')} is blocked at "
                             f"{place_name(ctx, v.get('regionId'))}**{when}: {v['blocked'].get('reason') or 'no reason given'}")
        seen = {key: v["blocked"].get("since") for key, v in blocked.items()}
        if state:
            state.set("vehicles_blocked", seen)
        self.vehicles_blocked = seen
        if first and lines and ctx.get("discord"):  # blocked while we were stopped: post even after a --silent start
            ctx["discord"].post(self.webhook, self.header, lines)
            return []
        return lines


def space_place(ctx, place_id):
    """A place, or a point in space ('pt:sun:3.228:67.74:0.275': 3.2 AU from the Sun) in words."""
    parts = str(place_id or "").lower().split(":")
    if len(parts) >= 3 and parts[0] == "pt":
        if parts[1] != "sun":
            return f"deep space near {body_name(ctx, parts[1])}"
        try:
            au = float(parts[2])
        except ValueError:
            return "deep space"
        return ("the asteroid belt" if 2.0 <= au <= 3.4 else "deep space") + f" ({au:.1f} AU from the Sun)"
    return place_name(ctx, place_id)


def side_text(ctx, company_id):
    """'Some Company (ally)', 'The Ashen Fleet (pirates)' or 'us'."""
    if company_id == ctx.get("company_id"):
        return "us"
    name = ctx.get("company_names", {}).get(company_id, f"company #{company_id}")
    if company_id is not None and company_id == ctx.get("navy_id"):
        return f"{name} (our alliance navy)"
    if company_id in ctx.get("ally_ids", set()):
        return f"{name} (ally)"
    return f"{name} (pirates)" if COMPANY_KINDS.get(company_id) == "pirate" else name


def battle_text(ctx, battle, wrecks=()):
    """fleets/map battles[] (first seen 2026-11-10): {id, day, placeId, attackerId, defenderId, attackerLosses,
    defenderLosses, passes[{attackUnits, ...}], seized}. Wrecks name the ships lost (wrecks[].battleId)."""
    names = ctx.get("unit_names") or {}
    attacker, defender = side_text(ctx, battle.get("attackerId")), side_text(ctx, battle.get("defenderId"))
    lost_ships = {}
    for wreck in wrecks:
        if wreck.get("battleId") == battle.get("id") and (wreck.get("ship") or {}).get("name"):
            lost_ships.setdefault(wreck.get("ownerId"), []).append(wreck["ship"]["name"])

    def losses(side_id, units, who):
        if not units:
            return f"{who} lost nothing"
        wrecked = lost_ships.get(side_id)
        return f"{who} lost {units_text(units, names)}" + (f" ({', '.join(wrecked)} wrecked)" if wrecked else "")

    passes = [p for p in battle.get("passes") or [] if isinstance(p, dict)]
    attack_units = next((p.get("attackUnits") for p in passes if p.get("attackUnits")), None)
    detail = ", ".join(x for x in (f"{len(passes)} pass{'es' if len(passes) != 1 else ''}" if passes else "",
                                   f"attacking with {units_text(attack_units, names)}" if attack_units else "") if x)
    return (f"**{attacker}** attacked **{defender}** in {space_place(ctx, battle.get('placeId'))}: "
            f"{losses(battle.get('defenderId'), battle.get('defenderLosses'), defender)}; "
            f"{losses(battle.get('attackerId'), battle.get('attackerLosses'), attacker)}"
            + (" | the defender's place was seized" if battle.get("seized") else "") + (f" | {detail}" if detail else ""))


def wreck_text(ctx, wreck):
    """fleets/map wrecks[]: {id, placeId, unitId, ownerId, ship {name, ...}, wreckedDay, expiresDay, battleId}."""
    clock, ship = ctx["clock"], wreck.get("ship") or {}
    unit = (ctx.get("unit_names") or {}).get(wreck.get("unitId"), tidy_id(wreck.get("unitId") or "ship").lower())
    name = f" **{ship['name']}**" if ship.get("name") else ""
    until = f" | Drifts there until {clock.stamp(game_day=wreck['expiresDay'])}" \
        if isinstance(wreck.get("expiresDay"), (int, float)) else ""
    return f"{side_text(ctx, wreck.get('ownerId'))}'s {unit}{name} wrecked in {space_place(ctx, wreck.get('placeId'))}{until}"


def siege_text(ctx, siege, ended=False):
    """fleets ourSieges[] / fleets/map sieges[] (first seen 2026-11-12): {siteId, attackerId, defenderId (0 for a
    faction's outpost), defenderFactionId, startedDay, daysLeft (ourSieges only: the two sides see when it falls)}."""
    attacker = side_text(ctx, siege.get("attackerId"))
    faction = siege.get("defenderFactionId")
    if faction and not siege.get("defenderId"):
        defender = f"the {FACTION_NAMES.get(faction, faction)}"
    else:
        defender = side_text(ctx, siege.get("defenderId"))
    defender = "our" if defender == "us" else f"{defender}'s"
    head = f"**{attacker}** {'laid' if not ended else 'ended its'} siege {'to' if not ended else 'of'} {defender} " \
           f"outpost at {site_text(ctx, siege.get('siteId'))}"
    left = siege.get("daysLeft")
    if ended:
        likely = "it most likely fell" if isinstance(left, (int, float)) and left <= 1 else \
            "it fell, or was broken or lifted"
        return f"{head}: {likely}"
    if isinstance(left, (int, float)):
        hours = ctx["clock"].game_days_to_hours(left)
        return f"{head} | Falls in about {left:g} game days ({hours_text(hours)}) unless it is broken"
    return head


def mentions_company(item, company_ids):
    """True if any company-ish field anywhere in item holds one of company_ids. Only plain ids are compared: a
    company-ish field can hold a dict (a battle's attackerLosses), which a set lookup can't take."""
    def is_ours(v):
        return isinstance(v, (int, str)) and v in company_ids

    if isinstance(item, dict):
        for key, value in item.items():
            lowered = key.lower()
            if any(w in lowered for w in ("company", "attacker", "defender", "owner", "side", "target")) and (
                    is_ours(value) or (isinstance(value, list) and any(is_ours(v) for v in value))):
                return True
            if mentions_company(value, company_ids):
                return True
    elif isinstance(item, list):
        return any(mentions_company(v, company_ids) for v in item)
    return False


def summarise(ctx, item):
    """Best-effort one-line summary of an item whose exact fields we haven't seen yet."""
    if not isinstance(item, dict):
        return str(item)[:200]
    parts = []
    for key, value in item.items():
        lowered = key.lower()
        if isinstance(value, (int, str)) and ("place" in lowered or "site" in lowered or "body" in lowered):
            parts.append(f"{key}: {place_name(ctx, value) if isinstance(value, str) else value}")
        elif isinstance(value, int) and "company" in lowered:
            parts.append(f"{key}: {ctx['company_names'].get(value, value)}")
        elif lowered in ("kind", "status", "outcome", "result", "winner"):
            parts.append(f"{key}: {value}")
    return " | ".join(parts) or json.dumps(item, ensure_ascii=False)[:200]


def describe(item):
    """Every plain field of an item we haven't seen yet, e.g. 'name: X | class: frigate | price: 120'."""
    if not isinstance(item, dict):
        return str(item)[:200]
    parts = [f"{k}: {v:,.1f}" if isinstance(v, float) else f"{k}: {v}"
             for k, v in item.items() if isinstance(v, (int, float, str)) and not isinstance(v, bool)]
    return " | ".join(parts)[:300] or json.dumps(item, ensure_ascii=False)[:200]


def capsule_key(item):
    """A capsule group's id, or (no id field) its fixed fields: counts and days change as it drifts."""
    for field in ("id", "capsuleId", "groupId", "lossId", "battleId"):
        if item.get(field) is not None:
            return f"{field}:{item[field]}"
    fixed = {k: v for k, v in item.items() if isinstance(v, str) or (isinstance(v, int) and "id" in k.lower())}
    return hashlib.sha1(json.dumps(fixed, sort_keys=True).encode()).hexdigest()


def capsule_text(ctx, item):
    """Every plain field of a capsule item, with places, companies and game days in words."""
    parts = []
    for key, value in item.items():
        lowered = key.lower()
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        if isinstance(value, str) and ("place" in lowered or "where" in lowered
                                       or lowered in ("from", "to", "at", "fromid", "toid", "atid")):
            value = place_name(ctx, value)
        elif isinstance(value, int) and any(w in lowered for w in ("company", "captor", "owner", "holder")):
            value = ctx["company_names"].get(value, value)
        elif isinstance(value, (int, float)) and "day" in lowered:
            value = ctx["clock"].stamp(game_day=value)
        elif isinstance(value, float):
            value = f"{value:,.1f}"
        parts.append(f"{key}: {value}")
    return " | ".join(parts)[:400] or json.dumps(item, ensure_ascii=False)[:200]


def capture(name, item):
    """Save the raw JSON of an item type we've never seen, to refine its alert later."""
    folder = HERE / "captures"
    folder.mkdir(exist_ok=True)
    path = folder / f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(item, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"[Capture] saved a new '{name}' item to {path}")


def item_key(item):
    if isinstance(item, dict) and item.get("id") is not None:
        return str(item["id"])
    if isinstance(item, dict) and item.get("siteId") is not None and item.get("startedDay") is not None:
        # A siege has no id, and its daysLeft counts down: keyed by all its fields, it looked new (and the old one
        # ended) every game day (1.07.11).
        return f"siege:{item['siteId']}:{item.get('attackerId')}:{item['startedDay']}"
    return hashlib.sha1(json.dumps(item, sort_keys=True).encode()).hexdigest()


class FleetMapWatcher(Watcher):
    """Rival warships heading for our places, and battles anywhere in Sol."""
    path, interval, header = "/fleets/map", INTERVAL_FLEET_MAP, "COMBAT ALERT"
    combat_lists = (("battles", "battle"), ("engagements", "fight under way"), ("sieges", "outpost siege"),
                    ("wrecks", "wreck"))

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        # Fleet moves already looked at, saved so a restart doesn't re-announce incoming fleets. A move is
        # remembered by id and destination ("565>orbit/juno"): a diverted fleet keeps its id but gets a new
        # toId, and must be looked at again. Bare ids saved before 0.12.00 count as seen once, for any target.
        saved = ctx["state"].get("fleet_moves", [])
        self.moves = {k for k in saved if isinstance(k, str) and ">" in k}
        self.legacy_ids = {k for k in saved if not (isinstance(k, str) and ">" in k)}
        self.seen = {}

    @staticmethod
    def move_key(move):
        return f"{move.get('id')}>{move.get('toId')}"

    def earth_moon(self, place_id):
        """True for places in Earth and Moon space (UEG space): their body is Earth or the Moon."""
        return self.ctx.get("place_bodies", {}).get(place_id) in ("earth", "moon")

    def marked_text(self):
        """'We're marked hostile to <alliances>' / 'We're branded terrorists', or '' when neither."""
        if self.ctx.get("terrorist"):
            return "We're branded terrorists"
        marks = self.ctx.get("hostile_to") or []
        if not marks:
            return ""
        names = [str(m.get("name") or m.get("allianceName") or f"alliance #{m.get('allianceId', '?')}")
                 if isinstance(m, dict) else str(m) for m in marks]
        return "We're marked hostile to " + ", ".join(names)

    def update(self, data, first):
        clock, ctx = self.ctx["clock"], self.ctx
        friends = {ctx["company_id"]} | ctx.get("ally_ids", set()) | ({ctx["navy_id"]} if ctx.get("navy_id") else set())
        lines, elsewhere = [], []

        warships = {u for u, role in ctx["unit_roles"].items() if role in INCOMING_THREAT_ROLES}
        marked = self.marked_text()
        moves = data.get("fleetMoves", [])
        for move in moves:
            owner = ctx["company_names"].get(move.get("companyId"))
            if (self.move_key(move) in self.moves or move.get("id") in self.legacy_ids
                    or move.get("companyId") in friends or owner in INCOMING_IGNORE_COMPANIES):
                continue
            target = move.get("toId")
            armed = {u: n for u, n in (move.get("units") or {}).items() if u in warships}
            if target not in ctx.get("our_places", set()) or not armed:
                continue
            who = ctx["company_names"].get(move.get("companyId"), move.get("companyId"))
            turned = move.get("turnedFrom") or (move.get("divertedFrom") or {}).get("toId")
            if turned:  # diverted under way: fromId is a point in space, so name the old target instead
                what = (f"**{who}** has diverted {units_text(armed, ctx.get('unit_names'))} (was heading for "
                        f"{place_name(ctx, turned)}) to our **{place_name(ctx, target)}**")
            else:
                what = (f"**{who}** is sending {units_text(armed, ctx.get('unit_names'))} from "
                        f"{place_name(ctx, move.get('fromId'))} to our **{place_name(ctx, target)}**")
            safe = target in INCOMING_IGNORE_PLACES or (INCOMING_IGNORE_EARTH_MOON and self.earth_moon(target))
            if safe and not marked:
                log(f"[Fleet map] {who} is sending {units_text(armed, ctx.get('unit_names'))} to "
                    f"{place_name(ctx, target)}: Earth–Moon space or ignored, not posted")
                continue
            note = f"\n⚠️ {marked}, so we can be attacked even here." if safe else ""
            lines.append(f"🚨⚔️ **[INCOMING FLEET]** {what} | Arrives {clock.stamp(game_day=move.get('arriveDay'))}{note}")
        self.moves = {self.move_key(m) for m in moves if m.get("id") is not None}
        self.legacy_ids = set()
        self.ctx["state"].set("fleet_moves", sorted(self.moves))

        posted_battles = set()  # battles posted this poll: their wrecks are named in the battle's post
        for list_name, label in self.combat_lists:
            items = {item_key(i): i for i in data.get(list_name, [])}
            for key in items.keys() - self.seen.get(list_name, {}).keys():
                item = items[key]
                if list_name not in ctx["captured"]:
                    ctx["captured"].add(list_name)
                    capture(f"fleets_map-{list_name}", item)
                if first:
                    continue
                if list_name == "battles":
                    posted_battles.add(item.get("id"))
                elif list_name == "wrecks" and item.get("battleId") is not None and item["battleId"] in posted_battles:
                    continue
                if list_name == "battles":
                    text, when = battle_text(ctx, item, data.get("wrecks") or []), item.get("day")
                elif list_name == "wrecks":
                    text, when = wreck_text(ctx, item), item.get("wreckedDay")
                elif list_name == "sieges":
                    text, when = siege_text(ctx, item), item.get("startedDay")
                else:
                    text, when = summarise(ctx, item), None
                stamp = clock.stamp(game_day=when) if isinstance(when, (int, float)) else clock.stamp()
                if mentions_company(item, friends):
                    lines.append(f"⚔️ **[{label.upper()} - US OR ALLIES]** {stamp} {text}")
                else:
                    elsewhere.append(f"⚔️ {stamp} New {label} elsewhere: {text}")
            self.seen[list_name] = items

        # Incoming fleets are security alerts: always posted, even as a startup baseline.
        self.ctx["discord"].post(self.webhook, self.header, lines)
        self.publish(ctx["webhooks"]["general"], "BATTLES ELSEWHERE", elsewhere, first)
        return []


class FleetsWatcher(Watcher):
    """Our own battles, duels and sieges. Their item shapes are unknown until the first one happens."""
    path, interval, header = "/fleets", INTERVAL_FLEETS, "COMBAT ALERT"
    combat_lists = (("engagementOrders", "our fleet is in battle", "battle over"),
                    ("duels", "duel", "duel finished"),
                    ("ourSieges", "siege we are part of", "siege ended"))

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.seen = {}

    def update(self, data, first):
        clock, ctx = self.ctx["clock"], self.ctx
        ctx["fleet_places"] = {f.get("placeId") for f in data.get("fleets", [])}
        ctx["navy_id"] = (data.get("navy") or {}).get("companyId")  # our alliance's navy: a friend in battles
        # Our warships and other fleet ships by kind, parked or moving, for the theft check (NewsWatcher).
        units = {}
        for f in data.get("fleets", []) + data.get("fleetMoves", []):
            for unit, n in (f.get("units") or {}).items():
                if isinstance(n, (int, float)):
                    units[unit] = units.get(unit, 0) + n
        remember_ships(ctx, "ship_history", units)
        lines = []
        for list_name, label, ended in self.combat_lists:
            items = {item_key(i): i for i in data.get(list_name, [])}
            previous = self.seen.get(list_name, {})
            for key in items.keys() - previous.keys():
                if list_name not in ctx["captured"]:
                    ctx["captured"].add(list_name)
                    capture(f"fleets-{list_name}", items[key])
                if list_name == "ourSieges":
                    lines.append(f"⚔️ **[{label.upper()}]** {clock.stamp()} {siege_text(ctx, items[key])}")
                else:
                    lines.append(f"⚔️ **[{label.upper()}]** {clock.stamp()} {summarise(ctx, items[key])}")
            if not first:
                for key in previous.keys() - items.keys():
                    if list_name == "ourSieges":
                        lines.append(f"🏳️ {clock.stamp()} {siege_text(ctx, previous[key], ended=True)}")
                    else:
                        lines.append(f"🏳️ {clock.stamp()} {ended.capitalize()}: {summarise(ctx, previous[key])}")
            self.seen[list_name] = items
        return lines


class FactionsWatcher(Watcher):
    """Faction standings, reported in full whenever one moves; and watch for Clear Sky / bloodmarks."""
    path, interval, header = "/factions", INTERVAL_SLOW, "FACTION STANDING"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.reported, self.watch_hits = {}, set()
        self.ballot_countdown = Countdown(ctx["state"], "ballot_countdown", BALLOT_WARNINGS_HOURS)
        self.naming_countdown = Countdown(ctx["state"], "naming_countdown", NAMING_WARNINGS_HOURS)
        # Ships bought from the exchange so far, saved so a purchase made during a restart is still posted.
        self.listings, self.bought = {}, ctx["state"].get("broker_bought")

    def listing_summary(self, listings):
        """'12 warships: 10× Escort at $312.5M (Fair Wind, ...), 2× Cruiser at $4,000.0M (...)'"""
        groups = {}  # type: dict
        for item in listings:
            groups.setdefault((item.get("unitId"), item.get("price")), []).append(item.get("name") or "?")
        parts = []
        for (unit, price), names in sorted(groups.items(), key=lambda g: -(g[0][1] or 0)):
            shown = ", ".join(names[:4]) + (f" +{len(names) - 4} more" if len(names) > 4 else "")
            parts.append(f"**{len(names)}× {self.ctx.get('unit_names', {}).get(unit, unit)}** at {money(price)} ({shown})")
        total = sum(len(n) for n in groups.values())
        return f"{total} warship{'s' if total != 1 else ''}: " + "; ".join(parts)

    def government_changes(self, government):
        """Earth's government: Secretary-General, offices (and vacancies) and the next session.
        Saved in state.json, so a change made while the bridge was stopped is still reported."""
        clock, state = self.ctx["clock"], self.ctx["state"]
        snapshot = {"secretaryGeneral": government.get("secretaryGeneral"), "offices": government.get("offices") or {},
                    "vacant": sorted(government.get("vacant") or []), "nextSessionDay": government.get("nextSessionDay")}
        old = state.get("government")
        state.set("government", snapshot)
        if not government or old is None or old == snapshot:
            return []
        changes = []
        if old.get("secretaryGeneral") != snapshot["secretaryGeneral"]:
            changes.append(f"Secretary-General: **{faction_short(snapshot['secretaryGeneral'])}** "
                           f"(was {faction_short(old.get('secretaryGeneral'))})")
        for office in sorted(set(old.get("offices", {})) | set(snapshot["offices"])):
            was, now = old.get("offices", {}).get(office), snapshot["offices"].get(office)
            if was != now:
                changes.append(f"{office.capitalize()} office: **{faction_short(now) if now else 'nobody'}** "
                               f"(was {faction_short(was) if was else 'nobody'})")
        for office in sorted(set(snapshot["vacant"]) - set(old.get("vacant", []))):
            holder = snapshot["offices"].get(office)
            changes.append(f"**{office.capitalize()} office is now vacant**" + (f" ({faction_short(holder)}'s seat)" if holder else ""))
        for office in sorted(set(old.get("vacant", [])) - set(snapshot["vacant"])):
            changes.append(f"{office.capitalize()} office is filled again")
        if old.get("nextSessionDay") != snapshot["nextSessionDay"] and snapshot["nextSessionDay"]:
            changes.append(f"Next Security Council session: {clock.stamp(game_day=snapshot['nextSessionDay'])}")
        if not changes:
            return []
        return [f"🌍 {clock.stamp()} **Earth's government changed:**\n" + "\n".join(f"• {c}" for c in changes)]

    def ballot_changes(self, ballot, mine, government):
        """Earth's yearly policy ballot (since Update 85): a post when a ballot opens, when its phase changes
        (nominations, voting, closed) and when the policies in force change. Saved in state.json, so a change
        made while the bridge was stopped is still reported; the very first run posts the ballot as it stands.
        Seen: phase "voting", options [{id, source}]. The other phases, the result and the policies' shape
        weren't seen when this was written, so those are described in full."""
        clock, state = self.ctx["clock"], self.ctx["state"]
        if not ballot and "policies" not in government:
            return []
        policies = government.get("policies")
        snapshot = {"year": ballot.get("year"), "phase": ballot.get("phase"),
                    "options": [o.get("id") if isinstance(o, dict) else o for o in ballot.get("options") or []],
                    "result": ballot.get("result"), "policies": policies}
        old = state.get("ballot")
        state.set("ballot", snapshot)
        if old == snapshot:
            return []
        old = old or {}
        lines = []
        if ballot and (old.get("year"), old.get("phase"), old.get("options"), old.get("result")) != \
                (snapshot["year"], snapshot["phase"], snapshot["options"], snapshot["result"]):
            phase = str(snapshot["phase"] or "?")
            rows = [f"Phase: **{BALLOT_PHASES.get(phase, phase.capitalize())}**"]
            if snapshot["options"]:
                rows.append("On the ballot: " + ", ".join(f"**{policy_name(o)}**" for o in snapshot["options"]))
            nominations = ballot.get("nominations") or []
            if nominations:
                rows.append("Nominations: " + ", ".join(policy_name(n) for n in nominations))
            if snapshot["result"] is not None:
                rows.append(f"Result: {describe(snapshot['result']) if isinstance(snapshot['result'], dict) else snapshot['result']}")
            if ballot.get("voters"):
                rows.append(f"Turnout: {ballot.get('turnout', 0)} of {ballot['voters']} voters")
            closed = snapshot["result"] is not None or phase in ("closed", "done", "result", "results")
            if not closed and ballot.get("closesDay"):
                rows.append(f"Closes: {clock.stamp(game_day=ballot['closesDay'])}")
            if closed and ballot.get("nextOpensDay"):
                rows.append(f"Next ballot opens: {clock.stamp(game_day=ballot['nextOpensDay'])}")
            vote, nomination = mine.get("vote"), mine.get("nomination")
            if not closed and phase == "voting" and mine.get("canVote") and not vote:
                rows.append("⚠️ We haven't voted yet.")
            elif not closed and phase.startswith("nominat") and mine.get("canNominate") and not nomination:
                rows.append("We haven't nominated a policy yet.")
            if vote:
                rows.append(f"Our vote: {policy_name(vote)}")
            year = f" {snapshot['year']}" if snapshot["year"] else ""
            lines.append(f"🗳️ {clock.stamp()} **Earth's{year} ballot:**\n" + "\n".join(f"• {r}" for r in rows))
        if policies is not None and old.get("policies") != policies and (policies or "policies" in old):
            if isinstance(policies, list):
                shown = ", ".join(f"**{policy_name(p)}**" for p in policies)
            else:
                shown = describe(policies) if isinstance(policies, dict) else str(policies)
            lines.append(f"🏛️ {clock.stamp()} **Earth's policies in force:** {shown or 'none'}")
        return lines

    def ballot_closing(self, ballot, mine, first):
        """A warning at each BALLOT_WARNINGS_HOURS mark before the ballot's voting closes, with our vote."""
        clock = self.ctx["clock"]
        closes = ballot.get("closesDay")
        if ballot.get("phase") != "voting" or ballot.get("result") is not None or not isinstance(closes, (int, float)):
            return []
        mark = self.ballot_countdown.check(("ballot", ballot.get("year"), closes), clock.hours_until(closes), first)
        if not mark:
            return []
        options = [o.get("id") if isinstance(o, dict) else o for o in ballot.get("options") or []]
        on = "On the ballot: " + ", ".join(f"**{policy_name(o)}**" for o in options) if options else ""
        vote = mine.get("vote")
        ours = f"Our vote: {policy_name(vote)}" if vote else ("⚠️ We haven't voted yet." if mine.get("canVote") else "")
        return [ballot_closing_text(clock, closes, mark, on, ours)]

    def update(self, data, first):
        clock = self.ctx["clock"]
        standing = data.get("standing") or {}
        emerged = {f.get("id") for f in data.get("factions", [])}  # e.g. mars_republic has standing but hasn't formed
        lines = []
        has_pace = "standingPaceToday" in data
        pace = data.get("standingPaceToday") or {}
        old_pace = self.ctx.get("standing_pace")
        if old_pace and any(pace.get(fid, 0) < used - 1e-6 for fid, used in old_pace.items()):
            # When the real day turns over isn't documented; this records it.
            log(f"[Standing pace] Today's allowance reset: used {describe(old_pace)} -> {describe(pace) or 'none'}")
        self.ctx["standing_pace"] = pace

        changed = first or any(abs(value - self.reported.get(fid, value)) >= STANDING_ALERT_STEP or fid not in self.reported
                               for fid, value in standing.items())
        if changed:
            rows = []
            for fid, value in sorted(standing.items(), key=lambda kv: -kv[1]):
                delta = value - self.reported.get(fid, value)
                arrow = f" ({'▲' if delta > 0 else '▼'} {abs(delta):.1f})" if abs(delta) >= 0.05 else ""
                note = "" if fid in emerged else " _(not yet emerged)_"
                room = ""
                if has_pace:
                    more = standing_gain_today(dict(self.ctx, standing=standing), fid, float("inf"))
                    room = f" · {more:.1f} more today" if more >= 0.05 else " · capped today"
                rows.append(f"• **{FACTION_NAMES.get(fid, fid)}**{note}: {value:+.1f}{arrow}{room}")
            ueg_used = f" · {pace.get('ueg') or 0:.1f} of {standing_day_cap('ueg'):.1f} used today" if has_pace else ""
            rows.append(f"• **UEG (United Earth Government)**: not reported by the API "
                        f"(Moon months: {data.get('uegColonyStanding', '?')}){ueg_used}")
            gifts = {fid: v for fid, v in (data.get("shipGiftsToday") or {}).items() if v}
            if gifts:
                rows.append("• Earned from ship gifts today: " +
                            ", ".join(f"{'UEG' if fid == 'ueg' else FACTION_NAMES.get(fid, fid)} {v:+.1f}"
                                      for fid, v in gifts.items()))
            title = "**[FACTION STANDINGS]**" if first else f"{clock.stamp()} **Faction standing changed:**"
            lines.append(f"🏛️ {title}\n" + "\n".join(rows))
            self.reported = dict(standing)

        # Halcyon Arms Exchange: warships listed for sale ({sid, name, unitId, price, listedDay}),
        # one line per batch, and our purchases (our standing there).
        # Our own purchases (brokerBought) aren't posted: we made them. Factions' purchases come from the news
        # (NewsWatcher). Other companies' purchases and withdrawn listings aren't reported anywhere, so a listing
        # that disappears is only logged.
        listings = {str(i.get("sid", item_key(i))): i for i in data.get("shipbroker") or []}
        bought = data.get("brokerBought")
        ours = bought - self.bought if bought is not None and self.bought is not None and bought > self.bought else 0
        if first and listings:
            lines.append(f"🛒 **[SHIPBROKER]** {self.listing_summary(listings.values())} for sale at Halcyon Arms Exchange")
        elif not first:
            added = [listings[k] for k in listings.keys() - self.listings.keys()]
            gone = [self.listings[k] for k in self.listings.keys() - listings.keys()]
            if added:
                lines.append(f"🛒 {clock.stamp()} **Halcyon Arms Exchange listed** {self.listing_summary(added)}")
            if ours:
                log(f"[Exchange] We bought {ours} ship(s); gone from the list: "
                    + (self.listing_summary(gone) if gone else "none"))
            if gone and len(gone) > ours:  # who took them isn't in the API, so this is logged, not posted
                note = f" (we bought {ours} of them)" if ours else ""
                log(f"[Exchange] Sold or withdrawn: {self.listing_summary(gone)}{note}")
        self.listings = listings
        if bought is not None:
            self.bought = bought
            self.ctx["state"].set("broker_bought", bought)

        lines += self.government_changes(data.get("government") or {})
        ballot = self.ballot_changes(data.get("ballot") or {}, data.get("ballotMine") or {}, data.get("government") or {})
        ballot += self.ballot_closing(data.get("ballot") or {}, data.get("ballotMine") or {}, first)
        if first and ballot and not self.ctx["broadcast_startup"]:
            self.ctx["discord"].post(self.webhook, self.header, ballot)  # news even on a --silent start
        else:
            lines += ballot

        lines += self.ambition_naming(data.get("ambitions") or [])

        # Clear Sky / bloodmark: not in the API today. Announce the first time the words show up.
        for source, body in (("factions", data), ("company", self.ctx.get("company_body") or {})):
            for path, value in find_words(body, WATCH_WORDS):
                key = f"{source}{path}"
                if key not in self.watch_hits:
                    self.watch_hits.add(key)
                    if not first:
                        lines.append(f"🩸 {clock.stamp()} **New Clear Sky / bloodmark data in the API:** "
                                     f"`{key}` = `{json.dumps(value, ensure_ascii=False)[:150]}`")
                    else:
                        lines.append(f"🩸 **[CLEAR SKY]** Already in the API: `{key}`")
        return lines


    def is_us(self, who):
        """namedBy hasn't been seen set: a company id, a name, or {companyId, name}."""
        ctx = self.ctx
        if isinstance(who, dict):
            return who.get("companyId") == ctx.get("company_id") or (bool(who.get("name"))
                                                                     and who.get("name") == ctx.get("company_name"))
        if isinstance(who, bool):
            return False
        return (isinstance(who, int) and who == ctx.get("company_id")) or (
            isinstance(who, str) and bool(who) and who == ctx.get("company_name"))

    def ambition_naming(self, ambitions):
        """Faction Ambitions (build HML7qYMP): {id, factionId, bodyId, name, status, stages, contributors, namedBy,
        nameUntil, colonyName}. Their names label Ambition offers on the contract board. When one is done and it's
        ours to name its colony, that's posted at once (saved, so once, even if it happened while we were stopped)
        and again NAMING_WARNINGS_HOURS before the limit, until it has a name."""
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx["state"]
        ambitions = [a for a in ambitions if isinstance(a, dict) and a.get("id") is not None]
        ctx["ambition_names"] = {a["id"]: a.get("name") for a in ambitions if a.get("name")}
        told = set(state.get("ambition_naming_told") or [])
        lines, current = [], set()
        for a in ambitions:
            key = str(a["id"])
            if a.get("colonyName") or not self.is_us(a.get("namedBy")):
                continue
            current.add(key)
            until = a.get("nameUntil")
            limit = f" by {clock.stamp(game_day=until)}" if isinstance(until, (int, float)) else ""
            faction = faction_short(a.get("factionId")) if a.get("factionId") else "A faction"
            mark = self.naming_countdown.check(("naming", key), clock.hours_until(until), False) \
                if isinstance(until, (int, float)) else None
            if key not in told:
                lines.append(f"🏷️ {clock.stamp()} **We name the colony:** {faction}'s Ambition **{a.get('name')}** is "
                             f"done and we did the most stages, so its colony is ours to name{limit}. Past that, "
                             f"we lose the chance.")
            elif mark:
                lines.append(f"🏷️ {clock.stamp()} **Colony still to name ({mark}h left):** {a.get('name')}{limit}")
        ids = {str(a["id"]) for a in ambitions}
        state.set("ambition_naming_told", sorted((told & ids) | current))
        return lines


class DiplomacyWatcher(Watcher):
    """Deals on the Diplomacy table (offers to us, counters, outcomes) and agreements under way.

    deals[] items (seen Nov 2026): {id, status, us: "a"|"b", with{party, id, name},
    terms{a[], b[]}, turn, round, held[], fromParty, expiresDay, updatedDay, note}.
    terms.<side> is what that side gives; turn is the side that must act. What has been
    announced is kept in state.json, so a restart repeats nothing.
    """
    path, interval, header = "/factions", INTERVAL_DIPLOMACY, "DIPLOMACY"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.deals = ctx["state"].get("deals")            # deal id -> snapshot; None until first seen
        self.agreements = ctx["state"].get("agreements")  # agreement key -> item

    def term_text(self, term):
        ctx, kind = self.ctx, term.get("kind")
        if kind == "money":
            return money(term.get("amount"))
        if kind == "goods":
            unit = ctx.get("good_units", {}).get(term.get("goodId"), "")
            unit = f" {unit}" if unit else ""
            return (f"{term.get('qty', 0):,.0f}{unit} {good_name(ctx, term.get('goodId'))} "
                    f"at {place_name(ctx, term.get('placeId'))}")
        if f"term-{kind}" not in ctx["captured"]:  # per day, spacecraft, design, agreement: not seen yet
            ctx["captured"].add(f"term-{kind}")
            capture(f"factions-deal-term-{kind}", term)
        return f"{kind}: {describe(term)}"

    def terms_text(self, terms):
        return ", ".join(self.term_text(t) for t in terms or []) or "nothing"

    def shortfalls(self, deal):
        """Warnings for goods we'd give that we don't have, or won't have before the offer expires."""
        clock, warnings = self.ctx["clock"], []
        for term in (deal.get("terms") or {}).get(deal.get("us"), []):
            if term.get("kind") != "goods":
                continue
            have, rate = self.ctx.get("stock", {}).get((term.get("placeId"), term.get("goodId")), (0, 0))
            want, name = term.get("qty") or 0, good_name(self.ctx, term.get("goodId"))
            if have < want:
                warnings.append(f"⚠️ We only have {have:,.0f} {name} there: **{want - have:,.0f} short**, "
                                f"so it can't be accepted as it stands.")
            elif rate < 0:
                hours = clock.game_days_to_hours((have - want) / -rate)
                expires_hours = clock.hours_until(deal["expiresDay"]) if deal.get("expiresDay") else None
                if expires_hours is None or hours < expires_hours:
                    warnings.append(f"⚠️ {name} there is being used up: we'll be short in **{hours_text(hours)}**.")
        return warnings

    def describe_deal(self, deal):
        ours, theirs = deal.get("us") or "a", "b" if deal.get("us") == "a" else "a"
        terms = deal.get("terms") or {}
        return f"We give **{self.terms_text(terms.get(ours))}** | they give **{self.terms_text(terms.get(theirs))}**"

    def update(self, data, first):
        clock, reminders = self.ctx["clock"], self.ctx["reminders"]
        deals = {str(d.get("id")): d for d in data.get("deals") or [] if d.get("id") is not None}
        seeding = self.deals is None  # never run before: don't announce deals that already ended
        previous = dict(self.deals or {})  # a copy, so state.json sees the change
        lines = []

        for deal_id, deal in deals.items():
            who = (deal.get("with") or {}).get("name") or "someone"
            status, ours = deal.get("status"), deal.get("turn") == deal.get("us")
            snapshot = {"status": status, "turn": deal.get("turn"), "round": deal.get("round"),
                        "terms": deal.get("terms"), "updatedDay": deal.get("updatedDay")}
            old = previous.get(deal_id)
            expires = (f" | Expires {clock.stamp(game_day=deal['expiresDay'])}"
                       if deal.get("expiresDay") else "")
            if old is None:
                if status == "open" and ours:
                    lines.append(f"📨 **Offer from {who}** (deal #{deal_id}) · {clock.stamp()}: {self.describe_deal(deal)}. "
                                 f"**Our move.**{expires}")
                    lines += self.shortfalls(deal)
                elif status == "open":
                    log(f"[Diplomacy] Deal #{deal_id} with {who} is open, waiting on them.")
                elif status not in ("draft",) and not seeding:
                    lines.append(f"🤝 {clock.stamp()} Deal #{deal_id} with **{who}** is **{status}**: {self.describe_deal(deal)}")
            elif old.get("status") != status:
                icon = "🤝" if status == "accepted" else "❌"
                lines.append(f"{icon} {clock.stamp()} Deal #{deal_id} with **{who}** was **{status}**: {self.describe_deal(deal)}")
            elif status == "open" and ours and (old.get("turn") != deal.get("turn") or old.get("terms") != deal.get("terms")):
                lines.append(f"↩️ {clock.stamp()} **{who} countered** (deal #{deal_id}, round {deal.get('round')}): "
                             f"{self.describe_deal(deal)}. **Our move.**{expires}")
                lines += self.shortfalls(deal)

            key = f"deal:{deal_id}:{deal.get('expiresDay')}"
            for stale in [k for k in reminders.pending if k.startswith(f"deal:{deal_id}:") and k != key]:
                reminders.cancel(stale)  # the expiry moved, or went away once the deal closed
            if status == "open" and ours and deal.get("expiresDay"):
                reminders.set(key, clock.real_ms_for(deal["expiresDay"]) - DEAL_EXPIRY_WARNING_MINUTES * 60_000,
                              self.webhook, self.header,
                              f"⏰ **The offer from {who} (deal #{deal_id}) expires in {DEAL_EXPIRY_WARNING_MINUTES} minutes** "
                              f"and it's our move: {self.describe_deal(deal)}")
            else:
                reminders.cancel(key)
            previous[deal_id] = snapshot

        # Deals the game no longer lists: forget them (it keeps only the last few ended ones).
        self.deals = {deal_id: previous[deal_id] for deal_id in deals}
        self.ctx["state"].set("deals", self.deals)

        # Agreements under way. Their fields were unknown when this was written: describe them in full.
        agreements = {item_key(a): a for a in data.get("agreements") or []}
        if self.agreements is not None:
            for key in agreements.keys() - self.agreements.keys():
                if "agreements" not in self.ctx["captured"]:
                    self.ctx["captured"].add("agreements")
                    capture("factions-agreements", agreements[key])
                lines.append(f"📜 {clock.stamp()} **New agreement:** {describe(agreements[key])}")
            for key in self.agreements.keys() - agreements.keys():
                lines.append(f"📜 {clock.stamp()} **Agreement ended:** {describe(self.agreements[key])}")
        self.agreements = agreements
        self.ctx["state"].set("agreements", agreements)

        # Offers need an answer, so they always go to Discord, even at startup (state stops repeats).
        self.ctx["discord"].post(self.webhook, self.header, lines)
        return []


def find_words(value, pattern, path=""):
    """(path, value) for every key or string value matching pattern. Skips names of craft and ships."""
    hits = []
    if isinstance(value, dict):
        for key, child in value.items():
            if pattern.search(key):
                hits.append((f"{path}.{key}", child))
            elif key not in ("name", "title", "text", "callsign"):
                hits += find_words(child, pattern, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits += find_words(child, pattern, f"{path}[{index}]")
    elif isinstance(value, str) and pattern.search(value):
        hits.append((path, value))
    return hits


class AllianceWatcher(Watcher):
    """Keeps the list of allies, and reports spies caught spying on or for our alliance."""
    path, interval, header = "/alliance", INTERVAL_SLOW, "ALLIANCE INTEL"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.marks = set()

    def update(self, data, first):
        clock, ctx = self.ctx["clock"], self.ctx
        ours = data.get("alliance") or {}
        ctx["alliance_id"] = ours.get("id")
        ctx["ally_ids"] = {m.get("companyId") for m in ours.get("members", [])} - {ctx["company_id"]}
        alliance_names = {a.get("id"): a.get("name") for a in data.get("alliances", [])}
        lines = []

        marks = {(m.get("companyId"), m.get("allianceId"), m.get("sinceDay")): m for m in data.get("hostileMarks", [])}
        for key in marks.keys() - self.marks:
            m = marks[key]
            if first:
                continue
            spied_on = alliance_names.get(m.get("allianceId"), f"alliance #{m.get('allianceId')}")
            until = f"{clock.stamp(game_day=m.get('untilDay'))}"
            if m.get("allianceId") == ctx["alliance_id"]:
                lines.append(f"🕵️ {clock.stamp(game_day=m.get('sinceDay'))} **Spy caught spying on our alliance:** "
                             f"**{m.get('name')}** is marked hostile to us until {until}")
            elif m.get("companyId") in ctx["ally_ids"] | {ctx["company_id"]}:
                lines.append(f"🕵️ {clock.stamp(game_day=m.get('sinceDay'))} **{m.get('name')}** (ours) was caught spying on "
                             f"**{spied_on}** - marked hostile until {until}")
            else:
                log(f"[Intel] {m.get('name')} caught spying on {spied_on}")
        self.marks = set(marks)
        return lines


class DiscoveriesWatcher(Watcher):
    """Alliances that have marked us hostile (we were caught spying), and a Codex recovery that stopped.

    codex.finds[] (build HML7qYMP): {storyId, title, what, placeId, where, foundDay, recoverEndDay, recoveredDay,
    first}. recoverEndDay is set while ours recover it; craft that leave before then stop the recovery (glossary
    "Artifact site"), which shows only as recoverEndDay going back to null with no recoveredDay. What was
    recovering is saved in state.json, so a stop while we were stopped is still posted."""
    path, interval, header = "/discoveries", INTERVAL_SLOW, "ALLIANCE INTEL"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.hostile = {}

    def update(self, data, first):
        clock = self.ctx["clock"]
        hostile = {item_key(h): h for h in data.get("hostileTo", [])}
        # While marked, the incoming-fleet alert covers Earth and Moon space too (that alliance may hunt us).
        self.ctx["hostile_to"] = list(hostile.values())
        lines = []
        if not first:
            for key in hostile.keys() - self.hostile.keys():
                if "hostileTo" not in self.ctx["captured"]:
                    self.ctx["captured"].add("hostileTo")
                    capture("discoveries-hostileTo", hostile[key])
                lines.append(f"🕵️ {clock.stamp()} **We've been marked hostile:** {summarise(self.ctx, hostile[key])}")
        self.hostile = hostile
        stopped = self.recoveries(data.get("codex"))
        if stopped:  # ours, not the alliance's: to the Corp channel
            corp = (self.ctx.get("webhooks") or {}).get("corp")
            if corp:
                self.ctx["discord"].post(corp, "CODEX", stopped)
            else:
                lines += stopped
        return lines

    def recoveries(self, codex):
        if not isinstance(codex, dict) or not isinstance(codex.get("finds"), list):
            return []
        clock, state = self.ctx["clock"], self.ctx["state"]
        finds = {f["storyId"]: f for f in codex["finds"] if isinstance(f, dict) and f.get("storyId")}
        was = state.get("codex_recovering")
        lines = []
        for story_id in (was or {}):
            f = finds.get(story_id)
            if f and f.get("recoverEndDay") is None and f.get("recoveredDay") is None:
                lines.append(f"⏹️ {clock.stamp()} **Codex recovery stopped:** {f.get('what') or f.get('title')} at "
                             f"{f.get('where') or place_name(self.ctx, f.get('placeId'))}. Our craft there left before "
                             f"it was recovered. Ships, an outpost or a lander there can start it again.")
        state.set("codex_recovering", {story_id: f["recoverEndDay"] for story_id, f in finds.items()
                                       if f.get("recoverEndDay") is not None and f.get("recoveredDay") is None})
        return lines


class AccountWatcher(Watcher):
    """Daily login gift not yet claimed today (days are UTC, counted since 1970)."""
    path, interval, header = "/account", INTERVAL_SLOW, "DAILY GIFT"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.reminded_day = ctx["state"].get("gift_reminded_day")

    def update(self, data, first):
        streak = data.get("streak") or {}
        today = int(time.time() // 86400)
        claimed = streak.get("claimedDay")
        # Skipped on the first poll so a --silent start still posts it on the next one.
        if first or claimed is None or claimed >= today or self.reminded_day == today:
            return []
        self.reminded_day = today
        self.ctx["state"].set("gift_reminded_day", today)
        return [f"🎁 **Daily login gift not claimed yet today.** Login streak: {streak.get('day', '?')} days."]


class RecordsWatcher(Watcher):
    """Badges, firsts at worlds and first-to-research records that we earn."""
    path, interval, header = "/records", INTERVAL_RECORDS, "RECORDS"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.ours = set()

    def update(self, data, first):
        clock, ctx = self.ctx["clock"], self.ctx
        me, names = ctx["company_id"], ctx["tech_names"]
        found = {}  # type: dict  # keys are tuples of 2 or 3
        for b in data.get("badges", []):
            if b.get("companyId") == me:
                # The API has no badge names: "stronger_together" -> "Stronger together".
                found[("badge", b.get("badgeId"))] = (b.get("day"), "🏅", f"Badge earned: **{tidy_id(b.get('badgeId'))}**"
                                                      + (" - **first in Sol!**" if b.get("first") else ""))
        for f in data.get("firsts", []):
            if f.get("companyId") == me:
                found[("first", f.get("bodyId"), f.get("kind"))] = (
                    f.get("day"), "🥇", f"First **{f.get('kind')}** at **{body_name(ctx, f.get('bodyId'))}** in Sol!")
        for t in data.get("techFirsts", []):
            if t.get("companyId") == me:
                found[("tech", t.get("techId"))] = (
                    t.get("day"), "🧪", f"First company to research **{names.get(t.get('techId'), t.get('techId'))}**!")
        lines = [] if first else [f"{icon} {clock.stamp(game_day=day)} {text}"
                                  for key, (day, icon, text) in found.items() if key not in self.ours]
        self.ours = set(found)
        return lines


# Vehicles that never leave Earth's surface: thieves take ships in orbit, so these aren't counted.
GROUND_VEHICLE_KINDS = {"truck", "train", "plane", "coaster", "surface_train", "rover", "handysize", "barge"}


def remember_ships(ctx, key, snapshot):
    """Keep (game day, snapshot) in ctx[key] for SHIP_HISTORY_SECONDS, for the theft check."""
    now = time.time()
    ctx[key] = [h for h in ctx.get(key) or [] if h[1] >= now - SHIP_HISTORY_SECONDS] + \
        [(ctx["clock"].now_game_day(), now, snapshot)]


def snapshot_around(ctx, key, day, after):
    """The latest snapshot taken before game day `day`, or the latest taken after it; None if there's none."""
    history = [h for h in ctx.get(key) or [] if (h[0] > day if after else h[0] < day)]
    return history[-1][2] if history else None


def ships_missing(before, after, clearing=()):
    """What's in `before` and not in `after`: fleet ships {kind: n} lose counts, vehicles {id: name} lose ids.
    Returns a list of texts ("1 mining ship", "Crumb-3 (tug)")."""
    gone = []
    for key, value in before.items():
        if isinstance(value, (int, float)):
            n = value - after.get(key, 0)
            if n > 0:
                gone.append(f"{n:g} {tidy_id(key).lower()}" + ("s" if n > 1 else ""))
        elif key not in after and key not in clearing:
            gone.append(value)
    return gone


MARKET_EVENT_RE = re.compile(r" in (?P<region>[^.]+)\. (?P<good>.+?) (?:is short|floods the market) there")


def market_event_note(ctx, text):
    """' | we hold 1,234 there' when we have stock of the event's good in its region, else ''.
    Text: "A strike at the mines in North Sea. Nickel–cobalt ore is short there and fetches 108% more for 37.6 days."
    """
    match = MARKET_EVENT_RE.search(text)
    if not match:
        return ""
    goods = {name.lower(): gid for gid, name in (ctx.get("good_names") or {}).items()}
    places = {name.lower(): pid for pid, name in (ctx.get("place_names") or {}).items()}
    good, region = goods.get(match["good"].lower()), places.get(match["region"].lower())
    qty = (ctx.get("stock") or {}).get((region, good), (0, 0))[0]
    return f" | we hold {qty_text(qty)} there" if qty else ""


class NewsWatcher(Watcher):
    """Solar News items worth a post: every pirate raid (for now, to see how often they strike), a theft near
    Earth that took ships of ours, another company salvaging one of our derelicts or one our salvage team is
    heading for, and a faction buying warships from Halcyon Arms Exchange. (Companies' purchases from the
    exchange aren't in the news.) Market events are only logged (MARKET_EVENT_POSTS).

    News items: {id, day, companyId, kind, text}.
    - kind "salvage": companyId is the salvager; the text names the craft and its old owner: "Company A
      salvaged the derelict Bateleur II-1, abandoned by Company B, for $1.21B."
    - kind "faction", companyId null: "Halcyon Arms Exchange sold 2 corvettes (Brightwater, Fair Trade) to the
      Morningstar Aerospace for $2.25B. They sail for Aquitania south pole 132°E."
    - kind "raid", companyId null: "The Ashen Fleet shot down Some Company’s Some Craft-27.", "The Ashen Fleet
      abandoned its base at …", and thefts: "A ship slipped its moorings around Earth and the Moon, and flew dark
      toward the belt." (never whose: our ship totals from before and after it tell whether it was ours).
    - kind "market_event", companyId null: "A strike at the mines in North Sea. Nickel–cobalt ore is short there and
      fetches 108% more for 37.6 days." / "Overproduction at the works in Belo Horizonte. Steel floods the market…"
    - kind "clear_sky", companyId null: "Crews at Some Company’s Mare Humorum outpost walked off the job in support of
      Clear Sky. Its output stops until 2032-09-21." One naming us is posted, a walkout with what stops there.
    The newest id seen is saved in state.json, so news from a restart is still posted."""
    path, interval, header = "/news", INTERVAL_NEWS, "SOLAR NEWS"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.theft: Optional[tuple] = None  # (news game day, the news text, real deadline) while being checked

    def our_names(self):
        """Our company's and craft's names, to spot ours in a raid story."""
        ctx = self.ctx
        names = {m.get("name") for m in (ctx.get("missions") or {}).values()}
        vehicles = snapshot_around(ctx, "vehicle_history", float("inf"), after=False) or {}
        names |= {v.rsplit(" (", 1)[0] for v in vehicles.values()}
        return {n for n in names | {ctx.get("company_name")} if n and len(n) > 2}

    def check_theft(self, theft):
        """After a theft in the news: our ships from before it that aren't there after it. Posted once if any are
        missing; logged and dropped when none are, or when the check runs out of time without snapshots."""
        ctx = self.ctx
        day, text, deadline = theft
        pairs = [(snapshot_around(ctx, key, day, after=False), snapshot_around(ctx, key, day, after=True))
                 for key in ("ship_history", "vehicle_history")]
        if any(before is not None and after is None for before, after in pairs) and time.time() < deadline:
            return []  # the watchers haven't read our ships since the theft yet
        self.theft = None
        if all(before is None for before, _ in pairs):
            log(f"[Theft] Couldn't check \"{text}\": no snapshot of our ships from before it (just started?).")
            return []
        gone = [g for before, after in pairs if before is not None and after is not None
                for g in ships_missing(before, after, ctx.get("clearing_tugs") or ())]
        if not gone:
            log(f"[Theft] None of our ships went missing with \"{text}\"")
            return []
        return [f"🚨 {ctx['clock'].stamp(game_day=day)} **Ships of ours may have been stolen:** since the news that "
                f"\"{text.rstrip('.')}\", we're missing {', '.join(gone)}. Thieves take ships left at Earth or the Moon with "
                f"no crew aboard and no orders; check the Fleet tab."]

    def clear_sky_text(self, when, text):
        """A Clear Sky action naming us. A walkout stops all output at one of our outposts: the post lists what stops
        there and when it resumes, and the industry watcher stops blaming those sites' stall on crew."""
        ctx = self.ctx
        walkout = WALKOUT_RE.search(text)
        if not walkout:
            return f"✊ {when} **Clear Sky acted against us:** {text}"
        until = game_day_of_date(walkout["until"])
        place = walkout["place"]
        place_id = next((pid for pid in ctx.get("sites_by_place") or {} if place_name(ctx, pid) == place), None)
        sites = (ctx.get("sites_by_place") or {}).get(place_id) or []
        if place_id:
            ctx.setdefault("walkouts", {})[place_id] = until
        stops = f" | Stops: {', '.join(sites)}" if sites else ""
        return (f"✊ {when} **Clear Sky walkout at our {place} outpost:** all output there stops until "
                f"{ctx['clock'].stamp(game_day=until)}{stops}")

    def update(self, data, first):
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx["state"]
        items = [n for n in (data if isinstance(data, list) else data.get("news") or [])
                 if isinstance(n, dict) and isinstance(n.get("id"), int)]
        seen = state.get("news_seen_id")
        newest = max([n["id"] for n in items] + [seen or 0])
        lines = []
        if seen is not None:  # the first run ever only records where the news is
            ours = ctx.get("company_name")
            for n in sorted(items, key=lambda n: n["id"]):
                text, kind, when = n.get("text") or "", n.get("kind"), clock.stamp(game_day=n.get("day"))
                if n["id"] <= seen:
                    continue
                if kind == "salvage" and n.get("companyId") != ctx["company_id"] and (
                        (ours and f"abandoned by {ours}" in text)
                        or any(name and name in text for name in ctx.get("our_derelicts", ()))):
                    lines.append(f"🏴‍☠️ {when} **One of our derelicts was salvaged:** {text}")
                elif kind == "salvage" and n.get("companyId") != ctx["company_id"] and (
                        team := next((t for name, t in (ctx.get("salvage_targets") or {}).items()
                                      if f"derelict {name}," in text), None)):
                    lines.append(f"🏴‍☠️ {when} **A derelict our salvage team {team} was heading for was taken:** {text}")
                elif kind == "faction" and text.startswith("Halcyon Arms Exchange sold"):
                    lines.append(f"🛒 {when} **Faction warship purchase:** {text}")
                elif kind == "raid":
                    ours_hit = any(re.search(re.escape(name) + r"(?![\w-])", text) for name in self.our_names())
                    lines.append(f"🏴‍☠️ {when} " + ("**Pirates hit us:** " if ours_hit else "**Pirate activity:** ") + text)
                    if THEFT_NEWS in text:
                        self.theft = (n.get("day"), text, time.time() + THEFT_CHECK_SECONDS)
                elif kind == "clear_sky" and ours and re.search(re.escape(ours) + r"[’']s\b", text):
                    lines.append(self.clear_sky_text(when, text))
                elif kind == "market_event":
                    log(f"[Market event] {text}{market_event_note(ctx, text)}")
                    if MARKET_EVENT_POSTS:
                        lines.append(f"📈 {when} **Market event:** {text}{market_event_note(ctx, text)}")
        state.set("news_seen_id", newest)
        if self.theft and isinstance(self.theft[0], (int, float)):
            lines += self.check_theft(self.theft)
        if first and lines:  # news from while we were stopped: still worth posting, even after a --silent start
            ctx["discord"].post(self.webhook, self.header, lines)
            return []
        return lines


def segments_we_use(ctx):
    """{segmentId: [our craft there]}: segments our craft are in or bound for, and the low orbit of each world where
    our ships are stationed ("orbit/ganymede" -> "ganymede:low", per the glossary's Stationed in a segment)."""
    used = {}
    for m in (ctx.get("missions") or {}).values():
        if m.get("segmentId") and m.get("status") in ("preparing", "transit", "active", "decaying"):
            used.setdefault(m["segmentId"], []).append(m.get("name") or "a craft")
    for place in ctx.get("fleet_places") or ():
        if isinstance(place, str) and place.startswith("orbit/"):
            used.setdefault(place[len("orbit/"):] + ":low", []).append("our ships")
    return used


def policy_text(s):
    """'tax 10%, open' / 'tax 50%, permits $5.00B' for a segment's policy or its pending change."""
    tax = f"tax {round((s.get('taxRate') or 0) * 100):g}%"
    access = s.get("access")
    if access == "permit":
        price = s.get("permitPrice") or 0
        return f"{tax}, permits " + (f"${price / 1000:,.2f}B" if price >= 1000 else f"${price:,.0f}M")
    return f"{tax}, {access or 'open'}"


class SegmentsWatcher(Watcher):
    """Claimed segments (since 1.06.00): a holder's notice raising its tax or permit price, or closing entry, where
    our craft or ships are; and a rival taking over one of our claims, or having taken it.

    /segments: {segments, taxIncome, taxPaid}. segments items: {segmentId ("proteus:low"), companyId, claimedDay,
    taxRate (0.5 = 50%), access ("open" or "permit"), permitPrice ($M), challengerId, challengeSince,
    pending: null or {taxRate, access, permitPrice, fromDay}}. A holder must give 24 game days' notice of a rise
    (about a real day); cuts apply at once, so a pending change is always a rise. The notices posted are saved in
    state.json, so a restart doesn't repeat them."""
    path, interval, header = "/segments", INTERVAL_SLOW, "SEGMENTS"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.claims = None  # our claims last poll: segmentId -> item

    def update(self, data, first):
        ctx, clock, me, state = self.ctx, self.ctx["clock"], self.ctx["company_id"], self.ctx["state"]
        segments = {s["segmentId"]: s for s in data.get("segments") or [] if isinstance(s, dict) and s.get("segmentId")}
        names = ctx.get("company_names") or {}
        holder = lambda cid: names.get(cid, f"Company #{cid}") if cid else "nobody"
        lines = []

        seen = state.get("segment_notices")
        notices = {f"{sid}@{s['pending'].get('fromDay')}": (sid, s) for sid, s in segments.items()
                   if isinstance(s.get("pending"), dict) and s.get("companyId") != me}
        if seen is not None:  # the first run ever only records the notices already given
            used = segments_we_use(ctx)
            for key, (sid, s) in notices.items():
                if key in seen or sid not in used:
                    continue
                craft = used[sid]
                there = ", ".join(f"**{c}**" if c != "our ships" else c for c in craft[:3]) + \
                    (f" +{len(craft) - 3} more" if len(craft) > 3 else "")
                lines.append(f"💸 {clock.stamp()} **{holder(s.get('companyId'))} is raising the cost of "
                             f"{orbit_text(sid)}**, where we have {there}: {policy_text(s)} → "
                             f"**{policy_text(s['pending'])}** from {clock.stamp(game_day=s['pending'].get('fromDay'))}")
        state.set("segment_notices", sorted(notices))

        mine = {sid: s for sid, s in segments.items() if s.get("companyId") == me}
        old = self.claims if self.claims is not None else {}
        for sid, s in mine.items():
            challenger = s.get("challengerId")
            if challenger and (first or old.get(sid, {}).get("challengerId") != challenger):
                lines.append(f"⚔️ {clock.stamp()} **{holder(challenger)} is taking over our claim on {orbit_text(sid)}**: "
                             f"their presence there beats ours (since {clock.stamp(game_day=s.get('challengeSince'))}). "
                             f"Station warships there, or add craft, to hold it.")
        if self.claims is not None:
            for sid in self.claims.keys() - mine.keys():
                taker = (segments.get(sid) or {}).get("companyId")
                lines.append(f"🚩 {clock.stamp()} **We lost our claim on {orbit_text(sid)}**"
                             + (f" to {holder(taker)}." if taker else ": it lapsed."))
        self.claims = mine
        return lines


class PlaceWatcher(Watcher):
    """New places (landing sites, stations, regions) in /reference/places, which the game has added without a
    game update or an API change (seen 2026-11-07: seven sites on Juno, Hygiea and Camilla). Keeps the names
    the other watchers use up to date, which were otherwise read only at startup. New places are logged, and
    posted only with NEW_PLACE_POSTS. Known ids are saved in state.json, so one added while the bridge was
    stopped is still reported."""
    path, interval, header = "/reference/places", INTERVAL_PLACES, "NEW PLACES"

    def update(self, data, first):
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx["state"]
        places = [p for p in data.get("places") or [] if p.get("id")]
        if not places:
            return []
        ctx["place_names"].update({p["id"]: p.get("name", p["id"]) for p in places})
        ctx["place_bodies"].update({p["id"]: p["bodyId"] for p in places if p.get("bodyId")})
        ctx["place_kinds"].update({p["id"]: p["kind"] for p in places if p.get("kind")})
        ids = {p["id"] for p in places}
        known = state.get("places_known")
        state.set("places_known", sorted(ids))
        if known is None:  # the first run ever: record without announcing
            return []
        gone = set(known) - ids
        if gone:
            log(f"[Places] No longer listed: {', '.join(sorted(gone))}")
        new = [p for p in places if p["id"] not in set(known)]
        by_body = {}
        for p in new:
            by_body.setdefault(p.get("bodyId"), []).append(p)
        lines = []
        for body_id, group in by_body.items():
            sites = "\n".join(f"• **{p.get('name', p['id'])}**{self.deposits_text(p)}" for p in group)
            lines.append(f"🗺️ {clock.stamp()} **{len(group)} new place(s) at {body_name(ctx, body_id)}:**\n{sites}")
        if not NEW_PLACE_POSTS:
            for line in lines:
                log("[Places] " + line.replace("\n", " "))
            return []
        if first and lines:  # added while we were stopped: still worth posting, even after a --silent start
            ctx["discord"].post(self.webhook, self.header, lines)
            return []
        return lines

    def deposits_text(self, place):
        """' (orbit station)' or the richest deposits, e.g. ' (Iron/nickel 5, PGM ore 3, Anorthite 3)'."""
        if place.get("kind") == "orbit":
            return " (orbit station)"
        best = sorted((place.get("deposits") or {}).items(), key=lambda item: -item[1])[:3]
        return f" ({', '.join(f'{good_name(self.ctx, good)} {grade}' for good, grade in best)})" if best else ""


class PadGoalWatcher(Watcher):
    """Spaceports' community goals for a new launch pad (/ueg → rangeExpansions), which every company may fill.

    Items: {id, regionId, fromCapacity, needs {good: qty}, delivered {good: qty}, postedDay, readyDay, builtDay,
    contributors [{companyId, paid}], contributorCount}. They aren't on the contract board: the bridge used to see
    them only once filled, in the contract history (kind "pad_goal"). They can fill within minutes (Naro, Nov 4).
    Ids already seen are saved in state.json, so a goal opened during a restart is still posted."""
    path, interval, header = "/ueg", INTERVAL_PAD_GOALS, "PAD GOAL"

    def goods_text(self, goods):
        ctx, parts = self.ctx, []
        for good_id, qty in goods.items():
            unit = ctx.get("good_units", {}).get(good_id, "")  # "t" for bulk goods, "unit" for parts
            amount = f"{qty:,.0f} {unit} of" if unit and unit != "unit" else f"{qty:,.0f} ×"
            parts.append(f"{amount} {good_name(ctx, good_id).lower()}")
        return ", ".join(parts)

    def update(self, data, first):
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx["state"]
        goals = {str(g["id"]): g for g in data.get("rangeExpansions") or [] if "id" in g}
        # Branded terrorists (shape not seen yet): then fleets may attack us anywhere, so incoming fleets
        # alert everywhere. Matched by company id or name.
        our_id, our_name = ctx["company_id"], ctx.get("company_name")
        ctx["terrorist"] = any(t == our_id or (isinstance(t, dict) and (
            our_id in (t.get("companyId"), t.get("id")) or (our_name and t.get("name") == our_name)))
            for t in data.get("terrorists") or [])
        seen = state.get("pad_goals_seen")
        lines = []
        if seen is None:  # the first run ever: record without announcing
            log(f"[Pad goals] {len(goals)} pad goal(s) already posted")
        else:
            for key in sorted(goals.keys() - set(seen), key=int):
                g = goals[key]
                region = g.get("regionId")
                port = ctx.get("launch_sites", {}).get(region) or place_name(ctx, region)
                needs, delivered = g.get("needs") or {}, g.get("delivered") or {}
                missing = {k: q - (delivered.get(k) or 0) for k, q in needs.items() if q - (delivered.get(k) or 0) > 0}
                if g.get("builtDay") is not None or not missing:
                    log(f"[Pad goals] {port}'s new launch pad was filled before we saw it")
                    continue
                cap = g.get("fromCapacity")
                more = f" ({cap} → {cap + 1} launch campaigns at once)" if isinstance(cap, int) else ""
                still = f" | **Still needed:** {self.goods_text(missing)}" if missing != needs else ""
                lines.append(f"🏗️ **[PAD GOAL]** {clock.stamp(game_day=g.get('postedDay'))} **{port}** wants materials for a "
                             f"new launch pad{more}: {self.goods_text(needs)}, delivered in "
                             f"**{place_name(ctx, region)}**{still} | Open to every company")
            for key in set(seen) - goals.keys():
                log(f"[Pad goals] Pad goal #{key} is gone from the list")
        state.set("pad_goals_seen", sorted(goals.keys(), key=int))
        if first and lines:  # opened while we were stopped: still worth posting, even after a --silent start
            ctx["discord"].post(self.webhook, self.header, lines)
            return []
        return lines


class DerelictWatcher(Watcher):
    """Other companies' derelicts (abandoned craft anyone may salvage) worth at least DERELICT_ALERT_MIN_VALUE.

    From /craft/map missions with status "derelict": lootValue ($M), segmentId (where it is or is headed),
    arriveDay (an abandoned craft keeps flying to its destination) and endDay (when it drifts out of service,
    30 game days after arriving). Ids already posted are saved in state.json."""
    path, interval, header = "/craft/map", INTERVAL_DERELICTS, "DERELICT"

    def update(self, data, first):
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx["state"]
        if DERELICT_ALERT_MIN_VALUE is None:
            return []
        # Every derelict's name, for the salvage teams we send (CraftWatcher), whatever its value.
        ctx["derelict_names"] = {m["id"]: m.get("name") for m in data.get("missions") or []
                                 if "id" in m and m.get("status") == "derelict" and m.get("name")}
        derelicts = {str(m["id"]): m for m in data.get("missions") or []
                     if "id" in m and m.get("status") == "derelict" and m.get("companyId") != ctx["company_id"]
                     and (m.get("lootValue") or 0) >= DERELICT_ALERT_MIN_VALUE}
        seen = state.get("derelicts_seen")
        lines = []
        if seen is None:  # the first run ever: record without announcing
            if derelicts:
                log(f"[Derelicts] {len(derelicts)} valuable derelict(s) already out there: "
                    + ", ".join(m.get("name") or "?" for m in derelicts.values()))
        else:
            now = clock.now_game_day()
            for key in sorted(derelicts.keys() - set(seen), key=int):
                m = derelicts[key]
                owner_id = m.get("companyId")
                owner = ctx["company_names"].get(owner_id) or f"Company #{owner_id}"
                if owner_id in ctx.get("ally_ids", set()):
                    owner = f"**{owner}** (ally)"
                where = orbit_text(m.get("segmentId"), m.get("destinationId")) or "an unknown place"
                if (m.get("arriveDay") or 0) > now:
                    where = f"still flying to **{where}**, arrives {clock.stamp(game_day=m.get('arriveDay'))}"
                else:
                    where = f"in **{where}**"
                ends = f" | Drifts out of service {clock.stamp(game_day=m['endDay'])}" if m.get("endDay") else ""
                lines.append(f"💰 {clock.stamp()} **Derelict worth {money(m.get('lootValue'))}:** **{m.get('name')}**, "
                             f"abandoned by {owner}, {where}{ends}")
        state.set("derelicts_seen", sorted(derelicts.keys(), key=int))
        if first and lines:  # appeared while we were stopped: still worth posting, even after a --silent start
            ctx["discord"].post(self.webhook, self.header, lines)
            return []
        return lines


class ApiSpecWatcher(Watcher):
    """Hourly check of the API's own description, to announce new endpoints and fields."""
    path, interval, header = "https://solthegame.com/api/openapi.json", INTERVAL_API_SPEC, "API UPDATE"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        # Saved, so a change made while the bridge was stopped is still announced.
        self.fields = set(ctx["state"].get("spec_fields", []))

    def update(self, data, first):
        fields = {}  # "path" or "path → field" -> description
        for path, ops in (data.get("paths") or {}).items():
            get = ops.get("get") or {}
            schema = (((get.get("responses") or {}).get("200") or {}).get("content") or {}) \
                .get("application/json", {}).get("schema", {})
            fields[path] = get.get("summary") or ""
            for name, prop in (schema.get("properties") or {}).items():
                fields[f"{path} → {name}"] = (prop or {}).get("description") or ""
        lines = []
        spec_dir = LOG_DIR / "spec"
        if self.ctx["write_files"] and not spec_dir.exists():
            self.save_spec(data, [], [], fields)  # keep a baseline copy to compare later versions with
        if self.fields and set(fields) != self.fields:
            added, removed = sorted(set(fields) - self.fields), sorted(self.fields - set(fields))
            if self.ctx["write_files"]:
                self.save_spec(data, added, removed, fields)
            text = "🆕 **The SOL API has changed.**"
            if added:
                text += "\nNew: " + ", ".join(f"`{a.replace('/api/v1', '')}`" for a in added[:25])
            if removed:
                text += "\nRemoved: " + ", ".join(f"`{r.replace('/api/v1', '')}`" for r in removed[:25])
            if any(WATCH_WORDS.search(a + fields[a]) for a in added):
                text += "\n🩸 **Includes Clear Sky / bloodmark fields!**"
            text += (f"\nA bridge update may follow to make use of it: watch <{BRIDGE_PROJECT_URL}>"
                     + (" (the bridge also posts here when one is out)." if UPDATE_CHECK else "."))
            if first:  # changed while we were stopped: still worth posting
                self.ctx["discord"].post(self.webhook, self.header, [text])
            else:
                lines.append(text)
        self.fields = set(fields)
        self.ctx["state"].set("spec_fields", sorted(fields))
        return lines

    @staticmethod
    def save_spec(data, added, removed, fields):
        """Keep each new version of the spec, and append what changed (with descriptions) to changes.log."""
        spec_dir = LOG_DIR / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (spec_dir / f"openapi-{stamp}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        entry = [f"== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} openapi-{stamp}.json"]
        entry += [f"+ {a.replace('/api/v1', '')}: {fields.get(a, '')}" for a in added]
        entry += [f"- {r.replace('/api/v1', '')}" for r in removed]
        if not added and not removed:
            entry.append("(baseline copy)")
        with open(spec_dir / "changes.log", "a", encoding="utf-8") as handle:
            handle.write("\n".join(entry) + "\n\n")
        log(f"[API spec] Saved logs/spec/openapi-{stamp}.json ({len(added)} added, {len(removed)} removed)")


class GameUpdateWatcher(Watcher):
    """Spots game updates. The API has no release notice, but the game is a web app whose
    code files (assets/index-<fingerprint>.js) get a new fingerprint with each build."""
    interval, header = INTERVAL_GAME_SITE, "GAME UPDATE"

    def poll(self):
        try:
            response = requests.get(GAME_SITE_URL, timeout=20)  # the public page: no API key sent
        except requests.RequestException as err:
            log(f"[Network error] {GAME_SITE_URL}: {err}")
            return
        if response.status_code != 200:
            log(f"[Site error] {GAME_SITE_URL}: HTTP {response.status_code}")
            return
        bundles = sorted(set(GAME_BUNDLE.findall(response.text)))
        if not bundles:
            # Raised so safe_poll logs it once rather than every 5 minutes.
            raise ValueError("no assets/*.js or *.css found on the game page; its layout may have changed")
        self.seeded = True
        modified = response.headers.get("Last-Modified")
        saved = self.ctx["state"].get("game_site")
        self.ctx["state"].set("game_site", {"bundles": bundles, "lastModified": modified})
        if saved is None:
            log(f"[Game site] Watching for updates; current build: {', '.join(bundles)}")
            return
        if saved.get("bundles") == bundles:
            if modified != saved.get("lastModified"):
                # Not posted (yet): logged to learn whether this alone also means an update.
                log(f"[Game site] Page changed ({saved.get('lastModified')} → {modified}) with the same build")
            return
        clock = self.ctx["clock"]
        live = time.time()
        if modified:
            try:
                live = parsedate_to_datetime(modified).timestamp()
            except (TypeError, ValueError):
                pass  # an unreadable date: use now
        when = clock.stamp(real_ms=live * 1000)
        # Go-live times of the last day, so the daily status can count updates and excuse their outages.
        recent = [u for u in (self.ctx["state"].get("game_updates") or []) if u >= time.time() - 86400]
        self.ctx["state"].set("game_updates", recent + [live])
        # The build's file names are only logged (below): they mean nothing to players.
        text = (f"🔄 **[GAME UPDATE]** {when} A new version of the game is live. The bridge re-checks the API in "
                f"{SPEC_RECHECK_AFTER_UPDATE_SECONDS // 60} minutes and posts here if it changed.")
        log(f"[Game site] New build: {saved.get('bundles')} → {bundles} (Last-Modified {modified})")
        spec = self.ctx.get("spec_watcher")
        if spec is not None:
            spec.next_due = min(spec.next_due, time.monotonic() + SPEC_RECHECK_AFTER_UPDATE_SECONDS)
        if not GAME_UPDATE_POSTS:
            return  # logged above; an API change it brings is posted by the spec check
        # Posted even on the first poll: an update made while the bridge was stopped is still news.
        self.ctx["discord"].post(self.webhook, self.header, [text])


REMOTE_VERSION = re.compile(r'^VERSION = "(\d+(?:\.\d+)*)"', re.MULTILINE)


def version_tuple(text):
    """'1.02.00' -> (1, 2, 0), for comparing versions."""
    return tuple(int(part) for part in text.split("."))


class BridgeUpdateWatcher(Watcher):
    """Posts while a newer version of the bridge itself is published (the VERSION line of sol_watch.py on
    GitHub): at startup and then once a day, until this copy is upgraded."""
    interval, header = INTERVAL_UPDATE_CHECK, "BRIDGE UPDATE"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.checked_at = None  # monotonic time of the last check: --once polls twice, but GitHub is asked once

    def fetch_version(self):
        """The published VERSION, or None if GitHub couldn't be reached or the file has no VERSION line."""
        try:
            # Only the start of the file: VERSION is near the top (the whole file is ~170 KB).
            response = requests.get(BRIDGE_SOURCE_URL, timeout=20, headers={"Range": "bytes=0-16383"})
        except requests.RequestException as err:
            log(f"[Update check] Couldn't reach GitHub ({err}); trying again in a day.")
            return None
        if response.status_code not in (200, 206):
            log(f"[Update check] GitHub answered HTTP {response.status_code}; trying again in a day.")
            return None
        found = REMOTE_VERSION.search(response.text)
        if not found:
            log("[Update check] No VERSION line in the published sol_watch.py; trying again in a day.")
            return None
        return found.group(1)

    def poll(self):
        if self.checked_at is not None and time.monotonic() - self.checked_at < 60:
            return
        self.checked_at = time.monotonic()
        first = not self.seeded
        self.seeded = True
        latest = self.fetch_version()
        if latest is None:
            return
        if version_tuple(latest) <= version_tuple(VERSION):
            log(f"[Update check] Up to date (v{VERSION}; published: v{latest}).")
            return
        log(f"[Update check] v{latest} is available; this bridge runs v{VERSION}.")
        state = self.ctx["state"]
        saved = state.get("update_notice") or {}
        if (first and saved.get("version") == latest
                and time.time() - (saved.get("at") or 0) < UPDATE_NOTICE_MIN_GAP_SECONDS):
            return  # told moments ago, before a restart
        state.set("update_notice", {"version": latest, "at": time.time()})
        text = (f"🆕 **SOL-Watch {latest} is available** (this bridge runs {VERSION}). Download it from "
                f"<{BRIDGE_PROJECT_URL}>; CHANGELOG.md there says what changed, and the README's \"Upgrading\" "
                f"section how to install it. This reminder repeats once a day until the bridge is upgraded.")
        # Posted even on the first poll, like a game update: it's news whenever the bridge starts.
        self.ctx["discord"].post(self.webhook, self.header, [text])


# Which INTERVAL_* setting each watcher polls at (settings.ini can change them; see apply_poll_intervals).
# DerelictWatcher's is set by apply_derelict_settings, and BridgeUpdateWatcher's is fixed at a day.
WATCHER_INTERVALS = {
    "ChatWatcher": "INTERVAL_CHAT", "CraftWatcher": "INTERVAL_CRAFT", "MiningWatcher": "INTERVAL_MINING",
    "OreBodiesWatcher": "INTERVAL_MINING", "ContractsWatcher": "INTERVAL_CONTRACTS",
    "ContractHistoryWatcher": "INTERVAL_HISTORY", "ResearchWatcher": "INTERVAL_RESEARCH",
    "DefenseWatcher": "INTERVAL_DEFENSE", "CompanyWatcher": "INTERVAL_COMPANY",
    "IndustryWatcher": "INTERVAL_INDUSTRY", "FleetMapWatcher": "INTERVAL_FLEET_MAP",
    "FleetsWatcher": "INTERVAL_FLEETS", "FactionsWatcher": "INTERVAL_SLOW", "AllianceWatcher": "INTERVAL_SLOW",
    "DiscoveriesWatcher": "INTERVAL_SLOW", "AccountWatcher": "INTERVAL_SLOW",
    "DiplomacyWatcher": "INTERVAL_DIPLOMACY", "RecordsWatcher": "INTERVAL_RECORDS", "NewsWatcher": "INTERVAL_NEWS",
    "PadGoalWatcher": "INTERVAL_PAD_GOALS", "ApiSpecWatcher": "INTERVAL_API_SPEC",
    "GameUpdateWatcher": "INTERVAL_GAME_SITE", "PlaceWatcher": "INTERVAL_PLACES",
}


# ================= MAIN =================

# ---- Lite mode (since 1.07.00): public news and global chat only ----

def key_sections(api):
    """The section ids our key may read (from the /api/v1 index, open to any key), or None if it couldn't be read."""
    index = api.get("", use_etag=False)
    if not index or not isinstance(index.get("sections"), list):
        return None
    return {s.get("id") for s in index["sections"] if isinstance(s, dict) and s.get("id")}


def lite_key_ok(sections, state, post):
    """Lite mode never runs on a key that can read company data: if this posted, a leaked lite key or a
    channel open to anyone could show it. Says so once (state.json) and returns False until the key is fixed."""
    extra = sorted((sections or set()) - LITE_SECTIONS)
    told = state.get("lite_key_extra")
    state.set("lite_key_extra", extra)
    if not extra:
        if told:
            post("🔑 Sol-Watch Lite's API key now has only News and records and Public chat. Posting resumes.")
        return True
    if told != extra:
        post("🔒 **Sol-Watch Lite is paused:** its API key can also read company data ("
             + ", ".join(extra) + "). Lite mode posts to channels anyone may read, so it runs only from a key with "
             "just **News and records** and **Public chat**. Untick the rest under Settings › Account › API keys "
             "(the same key keeps working), or make a new key for lite mode. Nothing is posted until then.")
    log(f"[Lite] Paused: the key can also read {', '.join(extra)}.")
    return False


def lite_tables(api, once):
    """The reference tables lite mode names things from. Retried until they load, like load_startup_data."""
    delay = 10
    while True:
        tables = {name: (api.get(f"/reference/{name}", use_etag=False) or {}).get(name)
                  for name in ("techs", "places", "goods", "bodies")}
        missing = [name for name, table in tables.items() if not table]
        if not missing:
            return tables
        if once:
            log(f"[Retry] Startup data didn't load ({', '.join(missing)}); giving up because of --once.")
            sys.exit(1)
        log(f"[Retry] Startup data didn't load ({', '.join(missing)}); trying again in {delay}s.")
        nap(delay)
        delay = min(delay * 2, STARTUP_RETRY_MAX_SECONDS)


def lite_counts(ctx):
    """What the digest adds up between posts, kept in state.json so a restart doesn't lose it."""
    counts = ctx["state"].get("lite_counts") or {}
    for key, empty in (("raids", 0), ("thefts", 0), ("posted", 0), ("noise", {}), ("founded", []), ("dark", [])):
        counts.setdefault(key, empty)
    return counts


class LiteNewsWatcher(Watcher):
    """Solar News for lite mode: the kinds in LITE_NEWS_ICONS are posted as they come (flare forecasts only, of the
    hazard items), the kinds in LITE_NEWS_NOISE are only counted for the digest, and a kind in neither is new to
    the bridge, so it's posted. The newest id seen is saved in state.json, so news from a restart is still posted."""
    path, interval, header = "/news", INTERVAL_NEWS, "SOLAR NEWS"

    def update(self, data, first):
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx["state"]
        items = [n for n in (data if isinstance(data, list) else []) if isinstance(n, dict) and isinstance(n.get("id"), int)]
        seen = state.get("news_seen_id")
        newest = max([n["id"] for n in items] + [seen or 0])
        lines, counts = [], lite_counts(ctx)
        firsts = {}  # type: dict  # company -> (index of its line in lines, [(stamp, what, where)])
        navy_ships = list(state.get("lite_navy_ships") or [])
        for n in sorted(items, key=lambda n: n["id"]):
            if seen is None or n["id"] <= seen:  # the first run ever only records where the news is
                continue
            text, kind = n.get("text") or "", str(n.get("kind") or "")
            if kind == "raid":
                counts["raids"] += 1
                counts["thefts"] += THEFT_NEWS in text
            elif kind == "founded":
                counts["founded"].append(text.split(" was founded")[0])
            elif kind == "inactive":
                counts["dark"].append(text.split(" went dark")[0])
            if kind in LITE_NEWS_NOISE and not (kind == "hazard" and LITE_FORECAST_RE.search(text)):
                counts["noise"][kind] = counts["noise"].get(kind, 0) + 1
                continue
            if kind not in LITE_NEWS_ICONS:
                log(f"[Lite] News of a kind not seen before: {kind!r}: {text}")
            counts["posted"] += 1
            if kind == "politics" and (opened := LITE_BALLOT_OPEN_RE.match(text)):
                state.set("lite_ballot", {"day": n.get("day"), "options": opened["options"].rstrip(".")})
            if kind in LITE_FIRST_SENTENCE_ONLY:
                text = re.split(r"(?<=\.) ", text, maxsplit=1)[0]
            icon = LITE_NEWS_ICONS.get(kind, "📰")
            if kind == "range_expansion" and "opened a new launch pad" not in text:
                icon = "🏗️"  # a pad still being built (materials asked for, halfway); 🚀 once it opens
            if kind == "faction" and LITE_AMBITION_RE.search(text):
                icon = LITE_AMBITION_ICON
            elif kind == "faction" and LITE_FACTION_COLONY_RE.search(text):
                icon = LITE_NEWS_ICONS["colony"]
            if kind == "ueg_navy" and LITE_NAVY_NEW_SHIP_RE.search(text):
                ships = set(LITE_NAVY_SHIP_NAME_RE.findall(text))
                if ships and ships <= set(navy_ships):
                    log(f"[Lite] Already announced, not posted again: {text}")
                    counts["posted"] -= 1
                    continue
                navy_ships += sorted(ships - set(navy_ships))
            stamp = clock.stamp(game_day=n.get("day"))
            if kind == "frontier" and (found := LITE_CODEX_FIRST_RE.match(text)):
                company = found["company"]
                if company in firsts:
                    firsts[company][1].append((stamp, found["what"], found["where"]))
                    continue
                firsts[company] = (len(lines), [(stamp, found["what"], found["where"])])
            lines.append(f"{icon} {stamp} {text}")
        for company, (index, found) in firsts.items():
            if len(found) > 1:
                listed = "; ".join(f"{what} ({where})" for _, what, where in found)
                lines[index] = (f"{LITE_NEWS_ICONS['frontier']} {found[-1][0]} {company} was first to recover "
                                f"{len(found)} documents: {listed}.")
        state.set("news_seen_id", newest)
        state.set("lite_navy_ships", navy_ships[-LITE_NAVY_SHIPS_KEPT:])
        state.set("lite_counts", counts)
        if first and lines:  # news from while we were stopped: still worth posting, even after a --silent start
            ctx["discord"].post(self.webhook, self.header, lines)
            return []
        return lines


class LiteBallotWatcher(Watcher):
    """Earth's ballot closing, warned at each BALLOT_WARNINGS_HOURS mark. A lite key can't read /factions, so the
    close comes from the game's yearly schedule (BALLOT_NOMINATION_DAYS, BALLOT_VOTING_DAYS) and the game clock,
    and what's on the ballot from the news of its opening, when the bridge saw it."""
    interval, header = INTERVAL_SLOW, "EARTH'S BALLOT"

    def __init__(self, ctx, webhook):
        super().__init__(ctx, webhook)
        self.countdown = Countdown(ctx["state"], "ballot_countdown", BALLOT_WARNINGS_HOURS)

    def poll(self):
        first = not self.seeded
        lines = self.update(None, first)
        self.seeded = True
        self.publish(self.webhook, self.header, lines, first)

    def update(self, data, first):
        ctx, clock = self.ctx, self.ctx["clock"]
        now = clock.now_game_day()
        year = (J2000_EPOCH + timedelta(days=now)).year
        opens = game_day_of_date(f"{year}-01-01")
        voting, closes = opens + BALLOT_NOMINATION_DAYS, opens + BALLOT_NOMINATION_DAYS + BALLOT_VOTING_DAYS
        if not voting <= now < closes:
            return []
        mark = self.countdown.check(("ballot", year, closes), clock.hours_until(closes), first)
        if not mark:
            return []
        seen = ctx["state"].get("lite_ballot") or {}
        on = f"On the ballot: {seen['options']}" if seen.get("options") and (seen.get("day") or 0) >= voting else ""
        return [ballot_closing_text(clock, closes, mark, on,
                                    "Every company at least 14 real days old has one vote (Empire › Politics › Ballot).")]


class LiteCompaniesWatcher(Watcher):
    """From /companies for lite mode: a company changing its name, a company newly marked a bully, and each
    company's earnings a day for the digest's leaderboard. Names and marks are kept in state.json, so a change
    made while the bridge was stopped is still posted; the first run ever only records them.

    companies items: {id, name, status ("active"/"abandoned"), kind ("navy", "exchange", "pirate" or missing),
    netPerDay ($M), formerNames, ...}. bullies items: {companyId, untilDay, victims}."""
    path, interval, header = "/companies", INTERVAL_LITE_COMPANIES, "COMPANIES"

    def update(self, data, first):
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx["state"]
        companies = [c for c in data.get("companies") or [] if isinstance(c, dict) and "id" in c]
        names = {str(c["id"]): c.get("name") for c in companies if c.get("name")}
        ctx["company_names"] = {int(k): v for k, v in names.items()}
        ctx["leaderboard"] = sorted(((c.get("netPerDay") or 0, c.get("name")) for c in companies
                                     if c.get("status") == "active" and not c.get("kind")
                                     and isinstance(c.get("netPerDay"), (int, float))), reverse=True)
        lines = []
        old = state.get("lite_names")
        for cid, name in names.items():
            if old and old.get(cid) and old[cid] != name:
                lines.append(f"✏️ {clock.stamp()} **{old[cid]}** is now called **{name}**.")
        state.set("lite_names", {**(old or {}), **names})

        bullies = {f"{b.get('companyId')}@{b.get('untilDay')}": b for b in data.get("bullies") or [] if isinstance(b, dict)}
        marked = state.get("lite_bullies")
        if marked is not None:
            for key, b in bullies.items():
                if key in marked or any(k.split("@")[0] == str(b.get("companyId")) for k in marked):
                    continue  # the mark of one already marked only moved its end: not news
                victims = b.get("victims")
                lines.append(f"⚠️ {clock.stamp()} **{names.get(str(b.get('companyId')), 'A company')}** is marked a "
                             f"bully for attacking much smaller companies" + (f" ({victims} so far)" if victims else "")
                             + f", until {clock.stamp(game_day=b.get('untilDay'))}. Companies it attacked are "
                             "protected again until then.")
        state.set("lite_bullies", sorted(bullies))
        return lines


class LiteRecordsWatcher(Watcher):
    """From /records for lite mode: the first company to research a major technology, and duel records changing.
    Kept in state.json; the first run ever only records them. (World firsts are counted from the news instead.)
    techFirsts items: {techId, companyId, day}. duels items: {companyId, won, lost, drawn}."""
    path, interval, header = "/records", INTERVAL_RECORDS, "RECORDS"

    def update(self, data, first):
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx["state"]
        names, techs = ctx.get("company_names") or {}, ctx.get("tech_names") or {}
        lines = []
        firsts = {t["techId"]: t for t in data.get("techFirsts") or [] if isinstance(t, dict) and t.get("techId")}
        known = state.get("lite_tech_firsts")
        if known is not None:
            for tech_id, f in sorted(firsts.items(), key=lambda kv: kv[1].get("day") or 0):
                if tech_id not in known:
                    lines.append(f"🧪 {clock.stamp(game_day=f.get('day'))} **{names.get(f.get('companyId'), 'A company')}** "
                                 f"is the first company to research **{techs.get(tech_id, tidy_id(tech_id))}**.")
        state.set("lite_tech_firsts", sorted(firsts))

        duels = {str(d["companyId"]): [d.get("won", 0), d.get("lost", 0), d.get("drawn", 0)]
                 for d in data.get("duels") or [] if isinstance(d, dict) and d.get("companyId") is not None}
        before = state.get("lite_duels")
        if before is not None:
            for cid, record in duels.items():
                if before.get(cid) != record:
                    won, lost, drawn = record
                    lines.append(f"🤺 {clock.stamp()} Duel record of **{names.get(int(cid), f'Company #{cid}')}**: "
                                 f"{won} won, {lost} lost, {drawn} drawn.")
        state.set("lite_duels", duels)
        return lines


class MemorialWatcher(Watcher):
    """The memorial wall (/memorial, every crew lost, newest first) for lite mode: each new entry, with the names.
    Items: {id, companyId, companyName, kind, missionName, classified, ship, place, day, cause, lost, crew}."""
    path, interval, header = "/memorial", INTERVAL_RECORDS, "MEMORIAL"

    def update(self, data, first):
        ctx, clock, state = self.ctx, self.ctx["clock"], self.ctx["state"]
        items = [m for m in (data if isinstance(data, list) else []) if isinstance(m, dict) and isinstance(m.get("id"), int)]
        seen = state.get("memorial_seen_id")
        state.set("memorial_seen_id", max([m["id"] for m in items] + [seen or 0]))
        if seen is None:
            return []
        lines = []
        for m in sorted((m for m in items if m["id"] > seen), key=lambda m: m["id"]):
            crew = ", ".join(str(c["name"]) for c in m.get("crew") or [] if isinstance(c, dict) and c.get("name"))
            lost = m.get("lost") or 1
            lines.append(f"🕯️ {clock.stamp(game_day=m.get('day'))} **{m.get('companyName')}** lost {lost} crew"
                         + (f" ({crew})" if crew else "") + f": {m.get('cause') or 'cause unknown'}.")
        if first and lines:
            ctx["discord"].post(self.webhook, self.header, lines)
            return []
        return lines


class LiteDigest(DailyStatus):
    """Lite mode's post at each of status_times(): that it's running, and since the last one: the pirate tally,
    new companies and those that went dark, the top 5 earners a day with their moves, and what the news had that
    wasn't posted. The counts restart after each post."""
    header = "SOL-WATCH LITE DIGEST"

    def text(self):
        ctx, state = self.ctx, self.ctx["state"]
        up = time.time() - self.started
        uptime = f"{up / 86400:.1f} days" if up >= 86400 else f"{up / 3600:.0f}h"
        span = min(time.time() - (state.get("status_last_at") or 0), 86400)
        counts = lite_counts(ctx)
        before = state.get("lite_digest_raids")
        raids = f"{counts['raids']} raid{'s' if counts['raids'] != 1 else ''}" + \
            (f" ({counts['thefts']} of them {'a theft' if counts['thefts'] == 1 else 'thefts'} near Earth)"
             if counts["thefts"] else "") + \
            (f", {before} in the digest before" if before is not None else "")
        rows = [f"🟢 **[SOL-WATCH LITE]** v{VERSION} running for {uptime} | {counts['posted']} news item{'s' if counts['posted'] != 1 else ''} posted "
                f"in the last {span / 3600:.0f}h", f"🏴‍☠️ Pirates: {raids}"]
        if counts["founded"]:
            rows.append("🆕 New companies: " + ", ".join(f"**{n}**" for n in counts["founded"]))
        if counts["dark"]:
            rows.append("🌑 Went dark: " + ", ".join(counts["dark"]))
        board = (ctx.get("leaderboard") or [])[:5]
        if board:
            ranks = state.get("lite_ranks") or {}
            cells = []
            for rank, (net, name) in enumerate(board, 1):
                was = ranks.get(name)
                move = "" if was is None or was == rank else (f" ▲{was - rank}" if was > rank else f" ▼{rank - was}")
                per_day = f"${net / 1000:,.2f}B" if net >= 1000 else f"${net:,.0f}M"
                cells.append(f"{rank}. **{name}** {per_day}/day{move}")
            rows.append("💰 Top earners: " + " · ".join(cells))
            state.set("lite_ranks", {name: rank for rank, (_, name) in enumerate(ctx.get("leaderboard") or [], 1)})
        noise = counts["noise"]
        if noise:
            rows.append("📰 Also in the news, not posted: " + ", ".join(
                f"{n} {LITE_NEWS_NOISE.get(kind, (kind, kind))[n != 1]}" for kind, n in sorted(noise.items(), key=lambda kv: -kv[1])))
        state.set("lite_digest_raids", counts["raids"])
        state.set("lite_counts", {})
        return "\n".join(rows)


def run_lite(config, api, state, clock, discord, broadcast_startup, sections, once):
    """The bridge in lite mode: public news, records, the memorial and global chat, all to one channel."""
    webhook = config["lite"]
    api.allowed = LITE_PATHS
    api.on_alert = lambda text: discord.post(webhook, "API KEY", [text])
    why = "set in settings.ini" if BRIDGE_MODE == "lite" else "the API key can't read Company"
    log(f"[Startup] Lite mode ({why}): public news and global chat only, to one channel. Reads only "
        + ", ".join(p for p in LITE_PATHS if p) + ".")
    while not lite_key_ok(sections, state, api.on_alert):
        state.save()
        if once:
            return
        for _ in range(60):  # check the key again every 10 minutes
            heartbeat()
            discord.flush()
            time.sleep(10)
        sections = key_sections(api)
    if not state.get("lite_announced"):
        state.set("lite_announced", True)
        discord.post(webhook, "SOL-WATCH LITE", [
            f"🛰️ **Sol-Watch Lite v{VERSION} is set up** ({why}). This channel gets public game news, the memorial, "
            "firsts, duel records, name changes, bully marks and global chat, plus a digest at the status times. "
            "Nothing about any company that isn't public in the game. Its key needs only **News and records** and "
            "**Public chat**."])
    tables = lite_tables(api, once)
    ctx = {
        "api": api, "clock": clock, "discord": discord, "broadcast_startup": broadcast_startup, "state": state,
        "write_files": not once, "company_id": None, "company_name": None, "lite": True,
        "webhooks": {"general": webhook, "alliance": webhook},
        "company_names": load_company_names(api),
        "tech_names": {t["id"]: t.get("name", t["id"]) for t in tables["techs"]},
        "place_names": {p["id"]: p.get("name", p["id"]) for p in tables["places"]},
        "body_names": {b["id"]: b.get("name", b["id"]) for b in tables["bodies"]},
        "good_names": {g["id"]: g.get("name", g["id"]) for g in tables["goods"]},
        "captured": set(),
    }
    watchers = [LiteCompaniesWatcher(ctx, webhook), LiteNewsWatcher(ctx, webhook), LiteRecordsWatcher(ctx, webhook),
                MemorialWatcher(ctx, webhook), LiteBallotWatcher(ctx, webhook), ChatWatcher(ctx)]
    if UPDATE_CHECK:
        watchers.append(BridgeUpdateWatcher(ctx, webhook))
    for watcher in watchers:
        watcher.safe_poll(" at startup")
        watcher.next_due = time.monotonic() + watcher.interval
    digest = LiteDigest(ctx, webhook)
    state.save()
    log(f"Sol-Watch Lite v{VERSION} is online.\n")
    sd_notify(f"READY=1\nSTATUS=Online (lite), v{VERSION}")
    if once:
        log("[--once] Single poll finished.")
        return
    while True:
        heartbeat()
        clock.refresh()
        discord.flush()
        now = time.monotonic()
        for watcher in watchers:
            if now >= watcher.next_due:
                watcher.next_due = now + watcher.interval
                watcher.safe_poll()
        digest.check()
        state.save()
        time.sleep(1)


def ask_broadcast():
    if "--broadcast" in sys.argv:
        return True
    if "--silent" in sys.argv or "--once" in sys.argv:
        return False
    try:
        answer = input("\nPress 'y' [ENTER] to broadcast startup baselines to Discord,\n"
                       "or any other key [ENTER] for terminal only: ")
    except EOFError:
        return False
    return answer.strip().lower() == "y"


_lock_handle = None  # the open lock file, held until the process exits


def acquire_lock():
    """Refuse to start if another copy already runs from this folder (both would post every alert)."""
    try:
        import fcntl
    except ImportError:  # Windows: no lock
        return None
    handle = open(HERE / "sol_watch.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("[Lock] Another copy of the bridge is already running from this folder, so this one is stopping. "
            "Stop the other first (systemctl --user stop sol-watch), or use --once for a test run.")
        sys.exit(1)
    handle.write(str(os.getpid()))
    handle.flush()
    global _lock_handle
    _lock_handle = handle  # kept open: the lock lasts as long as the process


def require_webhooks(config, names):
    missing = [f"WEBHOOK_{name.upper()}" for name in names if not config.get(name)]
    if missing:
        sys.exit(f"Missing settings: {', '.join(missing)}. Put them in {HERE / '.env'} (see .env.example).")


def check_key_sections(api, state, post):
    """Compare the sections our key may read (the /api/v1 index, open to any key) with KEY_SECTIONS.
    Posts once what's missing and what stops without it (saved in state.json, so a restart doesn't repeat it),
    and once when nothing is missing any more. Reads of a missing section then only log their 403s.
    Returns the missing section ids, or None if the index couldn't be read."""
    index = api.get("", use_etag=False)
    if not index or not isinstance(index.get("sections"), list):
        log("[API key] Couldn't read the API index to check our key's sections.")
        return None
    have = {s.get("id") for s in index["sections"] if isinstance(s, dict)}
    missing = [entry for entry in KEY_SECTIONS if entry[0] not in have]
    missing_ids = sorted(entry[0] for entry in missing)
    log(f"[API key] Sections: {', '.join(sorted(i for i in have if i))}"
        + (f" | missing: {', '.join(missing_ids)}" if missing_ids else " | nothing the bridge reads is missing"))
    api.expected_forbidden = {path for entry in missing for path in entry[3]}
    before = state.get("key_sections_missing")
    state.set("key_sections_missing", missing_ids)
    if missing_ids == (before or []):
        return missing_ids  # already told (or nothing to tell)
    if not missing_ids:
        post("🔑 Our API key now has every section the bridge reads.")
        return missing_ids
    needed = [e for e in missing if e[2] != "optional"]
    optional = [e for e in missing if e[2] == "optional"]
    lines = []
    if needed:
        lines.append(f"🔑 **Our API key is missing {len(needed)} section{'s' if len(needed) != 1 else ''} the bridge "
                     f"reads.** Tick {'them' if len(needed) != 1 else 'it'} in the game under Settings › Account › "
                     f"API keys (the same key keeps working, and the bridge picks the change up by itself):")
        lines += [f"• **{title}**{' (required)' if need == 'required' else ''}: {effect}"
                  for _, title, need, _, effect in needed]
    if optional:
        lines.append(("Optional, not ticked: " if needed else "🔑 Optional key section not ticked: ")
                     + "; ".join(f"**{title}** ({effect})" for _, title, _, _, effect in optional))
    post("\n".join(lines))
    return missing_ids


def load_startup_data(api, once):
    """/me and the reference tables. Retried until they load: at boot the network may not be up yet."""
    delay = 10
    while True:
        me = api.get("/me", use_etag=False) or {}
        tables = {name: (api.get(f"/reference/{name}", use_etag=False) or {}).get(name)
                  for name in ("techs", "facilities", "military", "places", "goods", "bodies")}
        missing = ([] if me.get("id") else ["me"]) + [name for name, table in tables.items() if not table]
        if not missing:
            return me, tables
        if once:
            log(f"[Retry] Startup data didn't load ({', '.join(missing)}); giving up because of --once.")
            sys.exit(1)
        log(f"[Retry] Startup data didn't load ({', '.join(missing)}); trying again in {delay}s.")
        nap(delay)
        delay = min(delay * 2, STARTUP_RETRY_MAX_SECONDS)


def main():
    once = "--once" in sys.argv
    write_files = not once  # a --once test leaves no logs or state behind
    if write_files:
        setup_logs()
    config = load_env()
    print("=" * 80)
    print(f"      SOL-WATCH NOTIFICATION BRIDGE v{VERSION}")
    print("=" * 80)
    LOG.info(f"===== SOL-Watch v{VERSION} starting (pid {os.getpid()}) =====")
    # Logged so a Python or library change (e.g. after an OS upgrade) shows up next to any new problem.
    log(f"[Startup] Python {sys.version.split()[0]} | requests {requests.__version__} | "
        f"watchdog {'on' if os.environ.get('WATCHDOG_USEC') else 'off'}")
    if write_files:
        acquire_lock()

    broadcast_startup = ask_broadcast()
    # --once is a test run: it only posts to Discord when --broadcast is given.
    discord = Discord(enabled=not once or broadcast_startup)
    log("[Startup] Baselines will be " + ("sent to Discord." if broadcast_startup else "shown in the terminal only."))

    api = SolApi(config["api_key"])
    api.fields = FieldTracker(write_files)
    api.on_alert = lambda text: discord.post(config["corp"], "API KEY", [text])
    state = State(HERE / "state.json", write_files)
    clock = GameClock(api, config["irl_tz"])
    clock.refresh(force=True)

    sections = key_sections(api)
    if BRIDGE_MODE == "lite" or (BRIDGE_MODE is None and sections is not None and "company" not in sections):
        require_webhooks(config, ("lite",))
        return run_lite(config, api, state, clock, discord, broadcast_startup, sections, once)
    require_webhooks(config, ("corp", "general", "alliance"))
    # Before the startup data: without the Company section /me never loads, and this says why.
    check_key_sections(api, state, api.on_alert)
    me, tables = load_startup_data(api, once)
    techs, facilities, military, places, goods, bodies = (tables[name] for name in
                                                          ("techs", "facilities", "military", "places", "goods", "bodies"))
    api.fields.ids = (frozenset(item["id"] for table in (techs, facilities, places, goods, bodies) for item in table)
                      | frozenset(military))
    ctx = {
        "api": api,
        "clock": clock,
        "discord": discord,
        "broadcast_startup": broadcast_startup,
        "state": state,
        "write_files": write_files,
        "company_id": me.get("id"),
        "company_name": me.get("name"),
        "tech_names": {t["id"]: t.get("name", t["id"]) for t in techs},
        "webhooks": {"corp": config["corp"], "general": config["general"], "alliance": config["alliance"]},
        "reminders": Reminders(discord),
        "company_names": load_company_names(api),
        "companies_fetched": time.monotonic(),
        "facility_names": {f["id"]: f.get("name", f["id"]) for f in facilities},
        "unit_roles": {unit_id: u.get("role") for unit_id, u in military.items()},
        "unit_names": {unit_id: u.get("name", unit_id) for unit_id, u in military.items()},
        "place_names": {p["id"]: p.get("name", p["id"]) for p in places},
        # Planets, moons and asteroids. Not place_names: there "moon" is the Shackleton rim outpost.
        "body_names": {b["id"]: b.get("name", b["id"]) for b in bodies},
        "place_bodies": {p["id"]: p.get("bodyId") for p in places if p.get("bodyId")},
        "place_kinds": {p["id"]: p.get("kind") for p in places if p.get("kind")},  # "earth_region", "surface", "orbit"
        "launch_sites": {p["id"]: p["launchSite"] for p in places if p.get("launchSite")},  # "south_korea" -> "Naro"
        "good_names": {g["id"]: g.get("name", g["id"]) for g in goods},
        "good_info": {g["id"]: (g.get("category", ""), g.get("massT") or 1) for g in goods},
        "good_units": {g["id"]: g.get("unit", "") for g in goods},
        "our_places": set(),
        "ally_ids": set(),
        "captured": set(),  # combat list types already saved to captures/
    }
    log(f"[Startup] Company: {me.get('name', 'unknown')} (#{me.get('id')}) | {len(techs)} techs | "
        f"Contract tier filter: {CONTRACT_FILTER_TIER.upper()}")

    # Order matters on startup: later watchers use what earlier ones learn
    # (research job, our missions, our places, our allies).
    watchers = [
        ResearchWatcher(ctx, config["corp"]),
        CraftWatcher(ctx, config["corp"]),
        CompanyWatcher(ctx, config["corp"]),
        AllianceWatcher(ctx, config["alliance"]),
        OreBodiesWatcher(ctx, config["corp"]),  # before mining: it names the captured asteroids
        MiningWatcher(ctx, config["corp"]),
        IndustryWatcher(ctx, config["corp"]),
        FleetsWatcher(ctx, config["corp"]),
        FleetMapWatcher(ctx, config["corp"]),
        ContractHistoryWatcher(ctx, config["general"]),
        ContractsWatcher(ctx, config["general"]),
        DefenseWatcher(ctx, config["general"]),
        FactionsWatcher(ctx, config["corp"]),
        DiplomacyWatcher(ctx, config["corp"]),
        AccountWatcher(ctx, config["corp"]),
        RecordsWatcher(ctx, config["corp"]),
        DiscoveriesWatcher(ctx, config["alliance"]),
        NewsWatcher(ctx, config["corp"]),
        SegmentsWatcher(ctx, config["corp"]),
        PadGoalWatcher(ctx, config["general"]),
        PlaceWatcher(ctx, config["general"]),
        ApiSpecWatcher(ctx, config["corp"]),
        GameUpdateWatcher(ctx, config["corp"]),
        ChatWatcher(ctx),
    ]
    if DERELICT_ALERT_MIN_VALUE is None:  # off: the large craft map isn't downloaded at all
        log("[Derelicts] Check off.")
    else:
        watchers.append(DerelictWatcher(ctx, config["general"]))
        log(f"[Derelicts] Every {INTERVAL_DERELICTS // 60} minutes, for derelicts worth "
            f"{money(DERELICT_ALERT_MIN_VALUE)} or more.")
    if UPDATE_CHECK:
        watchers.append(BridgeUpdateWatcher(ctx, config["corp"]))
    else:
        log("[Update check] Off (UPDATE_CHECK = False).")
    ctx["spec_watcher"] = next(w for w in watchers if isinstance(w, ApiSpecWatcher))  # re-checked after a game update
    for watcher in watchers:
        watcher.safe_poll(" at startup")
        watcher.next_due = time.monotonic() + watcher.interval
    daily = DailyStatus(ctx, config["corp"])
    state.save()
    log(f"Sol-Watch v{VERSION} is online.\n")
    sd_notify(f"READY=1\nSTATUS=Online, v{VERSION}")

    if once:
        for watcher in watchers:
            watcher.poll()
        for key, (due, _, _, _) in sorted(ctx["reminders"].pending.items(), key=lambda kv: kv[1][0]):
            log(f"[--once] Reminder '{key}' would fire at {clock.stamp(real_ms=due)}")
        log("[--once] Single poll finished.")
        return

    while True:
        heartbeat()
        clock.refresh()
        discord.flush()
        ctx["reminders"].fire_due()
        now = time.monotonic()
        for watcher in watchers:
            if now >= watcher.next_due:
                watcher.next_due = now + watcher.interval
                watcher.safe_poll()
        daily.check()
        state.save()
        time.sleep(1)


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except KeyboardInterrupt:
        log("Sol-Watch stopped by user.")
    except Exception as err:
        log(f"[FATAL ERROR] {err!r}", traceback.format_exc())
        exit_code = 1  # so systemd records a failure, not a clean stop
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\nScript stopped. Press Enter to exit...")  # keeps a double-clicked window open
        except EOFError:
            pass
    sys.exit(exit_code)
