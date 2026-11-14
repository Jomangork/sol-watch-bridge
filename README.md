# SOL-Watch bridge

Polls the SOL game's read-only API (https://solthegame.com/api/v1) with your own API key and posts what changes in your company and the game to three Discord channels. A **lite mode** posts only what every player sees alike (Solar News, global chat and the like) to one channel, for a server anyone may read; see [Lite mode](#lite-mode). It's a single Python file with one dependency (`requests`), and runs on any always-on machine with Python 3.9 or newer: a Linux box, a Raspberry Pi 3 or newer, or a Windows PC.

It only reads. It can't play the game or act for you, and it uses the API at about a third of its rate limit. It's a player-made tool, not made or supported by the game's developers.

**Public domain.** Use it, change it, share it, sell it: no permission or credit needed (see `LICENSE`). Improvements are welcome.

## Before you start

1. **An API key.** In the game, go to Settings › Account › API keys and make a key just for the bridge (the API's limit of 60 reads a minute is per key, so don't share it with other tools). Tick these sections:
   - **Company** (required), News and records, Launches and craft, Fleets, Contracts, Defense, Factions, Industry, Research, Mining, UEG, Alliance, Discoveries, Segments and Public chat.
   - Optional: **Alliance chat**, to relay your alliance's channel (new keys leave it unticked, since passing alliance talk on is your alliance's business), and Account (only the daily gift reminder). Market isn't used.
   - **Upgrading to 1.06.00?** Tick **Segments** on your existing key. The bridge picks the change up by itself, and until then it says once that the section is missing.
   - Leaving a section out turns off what needs it. The bridge says once at startup what's missing and what that stops.
2. **Three Discord webhooks,** one per channel (they can all point at the same channel if you prefer). In Discord: Server Settings › Integrations › Webhooks › New Webhook, pick the channel, then "Copy Webhook URL". You need the Manage Webhooks permission on that server.
3. **Keep the key and the webhook URLs secret.** Anyone with a webhook URL can post in that channel. They go only in the `.env` file, never in screenshots or posts asking for help.

| Channel (webhook) | What it posts |
| --- | --- |
| `WEBHOOK_CORP` | **Combat:** rival warships heading for our places, battles involving us or our allies (who attacked whom, where, and what each side lost, with the ships wrecked by name), our duels and sieges. **Problems:** queued actions that are blocked (a launch by its name and purpose), vehicles blocked on their route, the research queue running empty, and production stalls. **72/48/24-hour warnings** for crew supplies running out, storage filling up, and inputs or maintenance goods running out. **Research:** started, finished, a 5-minute warning, and "finished with nothing next" (when more is queued, only if the game hasn't started it 10 minutes later). **Construction** completed. **Launches:** queue to pad, launched, arrived, completed, failed (craft we abandoned drifting out of service are only logged). **Salvage:** another company salvaging one of our derelicts, or a derelict our own salvage team is heading for. **Pirates:** every pirate raid in Solar News, marked when it names us or one of our craft, and a theft near Earth that took ships of ours. **Segments:** a holder raising the tax or permit price where our craft or ships are, a rival taking over one of our claims, and a claim lost. **Halcyon Arms Exchange:** warships listed, and factions buying warships there. A listing that disappears is only logged: the API doesn't say who bought it or whether it was withdrawn. Safe mode set by hand. Losses. Mining. Ore running out. **Faction standings**, as a full table whenever one moves, with how much more each faction can give today (and how much of the UEG's daily allowance is used). **Clear Sky or bloodmark** data appearing. Daily login gift unclaimed. Badges and firsts. **SOL API changes** (game updates themselves are only logged, unless `game_update_posts` is on). **Bridge updates:** a new version of this bridge. **Faction Ambitions:** a colony that is ours to name, with its time limit and a reminder 24 hours before. **Codex:** a recovery of ours that stopped because our craft left. **Diplomacy:** offers made to us (with a warning if we lack the goods they ask for), counter-offers, deals accepted, rejected or expired, a warning an hour before an offer waiting on us expires, and agreements starting or ending. |
| `WEBHOOK_GENERAL` | New contract offers (filtered by tier; offers for a faction Ambition are tagged with its name, Codex recoveries with CODEX). New alliance jobs, from factions and from other clients. **Pad goals:** spaceports asking every company for a new launch pad's materials. Ship orders, bounties and rescue contracts. A **standing advisor** line on every faction contract and siege. Offers taken by another company. **Siege contracts against us.** Our closed contracts. Asteroid threats. Hazard forecasts and arrivals. New debris fields. Hazard strikes. Battles and wrecks elsewhere in Sol. **Derelicts** of other companies (all by default, or those worth at least `min_value`). **New places:** landing sites and stations the game adds, checked every 6 hours (only logged, unless `new_place_posts` is on). Global chat. |
| `WEBHOOK_ALLIANCE` | Alliance chat, spies caught spying on our alliance or on behalf of it, and alliances marking us hostile |

## Lite mode

Lite mode posts only public game information, the same for every player, to **one** Discord channel. It's meant for a server or forum anyone may read: it never reads anything about a company that isn't public in the game.

**The key.** Make a key just for lite mode (Settings › Account › API keys) and tick **only**:
- **News and records** (Solar News, the memorial, records, company cards)
- **Public chat** (global chat)

The bridge runs in lite mode by itself when its key can't read Company. `mode = lite` under `[bridge]` in `settings.ini` forces it. Lite mode **pauses** if its key can read anything else (it says so once in the channel and posts nothing until the extra sections are unticked), so a full key can't be shared by mistake. It also refuses any read outside `/news`, `/chat`, `/companies`, `/records`, `/memorial`, `/clock` and `/reference`.

**The channel.** Put its webhook in `.env` as `WEBHOOK_LITE` (or it uses `WEBHOOK_GENERAL`). The other webhooks aren't needed.

**What it posts, as it happens:**
- **Solar News:** pirate raids and thefts near Earth, solar flare forecasts, faction warship sales and navy orders, segment claims and takeovers, Earth's ballot, holidays, sabotage, Clear Sky, new companies and companies gone dark, alliances, colonies, stations and outposts, new launch pads (🏗️ while one is being built, 🚀 when it opens), UEG projects and navy, firsts in Sol (badges), project honors, market events, salvage and abandoned craft, launch failures (only who and which rocket, not what it carried), surrendered outposts, faction Ambitions (🎯) and faction colonies (🏘️), UEG Navy ships (⚓, each ship once, though the game announces some more than once),, companies first to recover a Codex story (🏺, several from one company in a poll on one line),, battles begun (⚔️), attacks beaten off (🛡️) and sieges laid (🏰). A kind of news the bridge hasn't seen before is posted too (📰).
- **The memorial:** each crew lost, with their names.
- **Earth's ballot closing:** 24, 12 and 1 real hours before voting closes (`ballot_warnings_hours`), with what's on the ballot when the bridge saw it open. A lite key can't read the ballot itself, so the close is worked out from the game's yearly schedule: it opens on 1 January, takes nominations for 120 game days, then votes for 168.
- **Records:** the first company to research a major technology, and duel records.
- **Companies:** a company changing its name, and a company newly marked a bully (with when the mark ends).
- **Global chat**, as in the full bridge.

**Counted, not posted** (they come by the dozen): contracts won, hazard hits on single craft, price moves, launches, arrivals, UEG supply runs, world firsts, asteroid moves and discoveries. Crew-lost news is counted too, since the memorial posts it with the names.

**The digest.** At each of `status_times` (in `settings.ini`, for example `status_times = 06:00, 16:00`), one post says the bridge is running and sums up the time since the last one: raids and thefts against the digest before, new companies and those gone dark, the five companies earning most a day with their moves up or down, and what the news had that wasn't posted.

**Blocked companies.** Global chat leaves out messages from any company the key's owner has blocked in the game, so the relayed channel is missing those too. Nothing about the blocks themselves is shared.

**Running it beside the full bridge.** Use a second folder with its own `.env` (the lite key and `WEBHOOK_LITE` only), `settings.ini` and service, for example `~/sol-watch-lite` and `sol-watch-lite.service` (a copy of `sol-watch-user.service` with `WorkingDirectory` changed). Keeping them apart means the full key and the public channel are never in the same place. Each copy keeps its own `state.json`, and each key its own read limit; lite mode reads about 2 times a minute.

## Files

| File | Purpose |
| --- | --- |
| `sol_watch.py` | The bridge |
| `CHANGELOG.md` | What changed in each version, newest first |
| `.env.example` | Template for your settings. Copy it to `.env`. |
| `.env` | Your API key and webhooks, made from `.env.example`. **Keep it private.** |
| `settings.example.ini` | Every adjustable setting, explained, at its default. Copy it to `settings.ini` to change any. |
| `settings.ini` | Optional. Your own settings, made from `settings.example.ini`. Without it the defaults apply. |
| `LICENSE` | Public domain (the Unlicense) |
| `requirements.txt` | Python dependencies |
| `run.sh` | Starts the bridge from its folder, using `.venv` when there is one |
| `sol-watch-user.service` | systemd **user** unit (recommended): runs it in the background as you, no sudo needed to restart it |
| `sol-watch.service` | systemd system-wide unit, if you'd rather run it that way |

The bridge also creates these next to itself (never during a `--once` test):

| File | Purpose |
| --- | --- |
| `logs/bridge.log` | Everything it prints, with dates. Rotated at 5 MB, 5 old copies kept. |
| `logs/errors.log` | Only problems (network, API, Discord, crashes) with full error details. Rotated at 2 MB, 3 copies. |
| `logs/new-fields.log` | Each field name the API returns for the first time, at any depth, with an example value |
| `logs/known-fields.json` | The field names seen so far, per endpoint |
| `logs/spec/` | A copy of each new version of the API spec, and `changes.log` listing what changed |
| `state.json` | What has been announced (sieges, incoming fleets, the daily gift, the API spec, the game's build, countdown warnings, debris fields, derelicts, the newest news item, ships bought from the exchange, pad goals, Earth's ballot), so a restart repeats nothing |
| `sol_watch.lock` | Stops a second copy from running in the same folder |
| `captures/` | Raw JSON of item types never seen before (battles, duels, shipbroker listings, ...) |

Deleting `state.json` makes the next start announce open sieges and incoming fleets again. Nothing else is lost.

## Setup on Linux

You need Python 3.9 or newer. Check with `python3 --version`.

1. Put the files in a folder on the machine, for example `~/sol-watch` (download the zip from the project page's "Code › Download ZIP", or a release), and make your `.env`.
   ```bash
   mkdir -p ~/sol-watch && cd ~/sol-watch
   unzip ~/sol-watch-bridge.zip    # or copy the files in another way
   cp .env.example .env
   nano .env               # fill in the key and webhooks
   chmod 600 .env
   chmod +x run.sh
   ```
   That's all it needs. To change any setting later, `cp settings.example.ini settings.ini` and edit that (see Settings).
2. Create a virtual environment and install `requests`.
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
   On Debian or Ubuntu, if `venv` is missing, install it with `sudo apt install python3-venv`.
3. Run a safe test. It polls everything once and posts nothing to Discord.
   ```bash
   ./run.sh --once
   ```
   You should see your company name, then one startup line per watcher.

The same steps work on a Raspberry Pi running Raspberry Pi OS.

## Setup on Windows

Less tested than Linux, but the bridge and its test run work there.

1. Install Python 3.9 or newer from python.org (tick "Add python.exe to PATH" in the installer).
2. Put the files in a folder, e.g. `C:\sol-watch`. Copy `.env.example` to `.env` and fill it in with Notepad.
3. Open a terminal (PowerShell) in that folder and install the dependencies, which include the time zone data Windows lacks:
   ```powershell
   py -m pip install -r requirements.txt
   ```
4. Run the safe test (posts nothing to Discord):
   ```powershell
   py sol_watch.py --once
   ```
5. Run it for real with `py sol_watch.py --silent` and leave the window open (minimised is fine). To start it with Windows, press Win+R, type `shell:startup`, and in that folder make a shortcut with the target `py C:\sol-watch\sol_watch.py --silent` and "Start in" `C:\sol-watch`.

Windows has no service watchdog here, so if the PC sleeps or the window is closed, the bridge stops. The 🟢 status posts (`status_times`) are how you notice: if one doesn't arrive, the bridge isn't running.

## Running it on Linux

**In the foreground,** to try it out. Stop it with Ctrl+C.
```bash
./run.sh            # asks whether to post the startup baselines to Discord
./run.sh --silent   # startup baselines go to the terminal only
./run.sh --broadcast
```

**In the background with a systemd user service (recommended).** It restarts the bridge 30 seconds after a crash, or after a freeze of 10 minutes (the bridge checks in with systemd's watchdog while it works), and needs no sudo except once, to start at boot:
```bash
mkdir -p ~/.config/systemd/user
cp sol-watch-user.service ~/.config/systemd/user/sol-watch.service
nano ~/.config/systemd/user/sol-watch.service      # only if the folder isn't ~/sol-watch: fix WorkingDirectory
systemctl --user daemon-reload
systemctl --user enable --now sol-watch
sudo loginctl enable-linger $USER                  # once: run from boot, even when nobody is logged in
journalctl --user -u sol-watch -f                  # live log; Ctrl+C stops watching, not the bridge
```
- Check, stop or restart it with `systemctl --user status sol-watch`, `systemctl --user stop sol-watch` or `systemctl --user restart sol-watch`. The status shows "Online, v…" once it has started.
- When `sol-watch-user.service` changes in a new version, copy it again and run `systemctl --user daemon-reload` and `systemctl --user restart sol-watch`.
- After editing `sol_watch.py` or `.env`, run `systemctl --user restart sol-watch`.
- `WorkingDirectory` may contain spaces; don't put quotes around it.

**Or with a system-wide service** (needs sudo for every restart):
```bash
sed -i "s/YOUR_USER/$USER/g" sol-watch.service    # set your user name and paths
sudo cp sol-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sol-watch
journalctl -u sol-watch -f
```

**Without systemd,** for example over SSH, run it inside `tmux` or `screen`:
```bash
tmux new -s solwatch './run.sh --silent'          # detach with Ctrl+B then D; reattach: tmux attach -t solwatch
```

## Settings

Two files, both next to `sol_watch.py`. Neither is ever part of a download, so upgrading never touches them.

**`.env`: secrets only (required).** Real environment variables override it.

| Name | Meaning |
| --- | --- |
| `SOL_BRIDGE_API_KEY` | SOL API key, made in the game under Settings › Account › API keys. `SOL_API_KEY` also works. |
| `WEBHOOK_CORP`, `WEBHOOK_GENERAL`, `WEBHOOK_ALLIANCE` | Discord webhook URLs |

**`settings.ini`: everything you can adjust (optional).** Without it, the bridge runs on the defaults below. To change something, copy `settings.example.ini` to `settings.ini`, remove the `#` in front of the line and set your value, then restart the bridge. The example explains every line. A value the bridge can't use is reported in the log (and `errors.log`) and its default kept; a misspelt name is reported too.

| Section | Settings (default) |
| --- | --- |
| `[contracts]` | `tier_filter` (mid): `newcomer` posts every tier, `mid` hides newcomer offers, `leader` posts only leader offers; faction and special offers, which have no tier, are always posted. `alliance_job_min_level` (easy), `alliance_job_digest_at` (3). |
| `[alerts]` | `countdown_warnings_hours` (72, 48, 24, 12, 4, 1): real hours before something runs out or fills up. `research_warning_minutes` (5), `deal_expiry_warning_minutes` (60), `stall_repeat_hours` (6), `ignored_stall_reasons` (paused, keepLimit: both your own choice), `slowed_post_below_percent` (90), `slowed_worse_by_points` (10) and `slowed_recovery_hours` (24): a site still producing is posted as slowed only below that share of its full output, once, and again only if it falls that much further or recovers for that long; `standing_alert_step` (1.0), `status_times` (08:00): times of day for the 🟢 status post, as many as you like (e.g. `06:00, 16:00`), or `off`; `game_update_posts` (no), `new_place_posts` (no). The older `daily_status_hour` (one whole hour) still works. |
| `[incoming]` | `ignore_earth_moon` (yes): rival warships heading for Earth and Moon space, where no fighting is allowed, are only logged; while we're marked hostile to an alliance or branded terrorists they alert there too. `ignore_places` (none), `ignore_companies` (UEG Navy). |
| `[derelicts]` | `check_minutes` (60, at least 10, or `off`, which skips the 1.3 MB craft map entirely), `min_value` (0, in $M: every derelict). |
| `[time]` | `irl_timezone` (the machine's zone): e.g. `Europe/London`. Used for the log files and the daily status hour; Discord shows real times in each viewer's own zone. Servers are often set to UTC. |
| `[updates]` | `update_check` (yes), and for a fork `project_url` and `source_url`. |
| `[intervals]` | Seconds between checks of each part of the API, at least 10. The defaults use about 21 of the API's 60 reads a minute per key; the bridge warns if yours add up to more than 40. |

The 🟢 BRIDGE STATUS post gives the version, uptime, polls, and the game updates and problems since the previous status post (at most 24 h), and the game build. If the bridge was down over one or more of the times, it posts once when it's back. Brief server outages (HTTP 502/503/504, 52x/53x from the game's CDN, or a dropped connection) within 10 minutes of an update going live are counted with the update ("2 brief outages during them"), not as problems. Server errors that keep coming for 5 minutes or more at another time, with no read getting through between them, show as one line, "Game server down: 1:20 AM–5:00 AM (3h 40m)", in each reader's own time; `errors.log` still records them all.

`DERELICT_CHECK_MINUTES`, `DERELICT_MIN_VALUE` and `IRL_TIMEZONE` still work in `.env` too, as in earlier versions, and win over `settings.ini`.

**Kept in `sol_watch.py`:** how the game works rather than what you prefer: faction names and rivals, the standing rules (`FACTION_RIVALS`, `STANDING_GAIN`, `RIVAL_PENALTY`), the API key sections and the like. They change only with the game, in a new version of the bridge.

## How it behaves

- **On start,** each watcher records what is already there and posts one summary line. Nothing old is re-announced, even after a restart. Only changes after that are posted.
  - **Exception: security alerts.** Siege contracts against us and rival warships heading for our places are posted to Discord at startup, even with `--silent`, but only if they weren't announced before. `state.json` remembers what was, so a restart doesn't repeat them. A siege that closed while the bridge was stopped is reported when it starts.
  - **API changes** made while the bridge was stopped are announced when it starts.
- **Startup retries.** If the API can't be reached when the bridge starts (at boot the network can come up a few seconds late), it keeps trying: after 10 s, then 20 s, 40 s, ... up to every 5 minutes. It never runs half-started.
- **Discord outages.** Messages that can't be delivered wait in a queue (up to 200) and are sent in order once Discord answers again, retried every minute. They're lost if the bridge is stopped while they wait.
- **Repeated errors** are logged once, then every 60th time, then "working again" when they stop.
- **Webhook URLs never appear in the logs.** Their secret part is replaced with `…`.
- **Flares.**
  - When a flare is forecast (3 real hours ahead), one message gives when it lands and when it ends.
  - When it lands, a second message says how many of our craft are in safe mode for it.
  - Safe-mode entries caused by a hazard are never posted one by one.
- **Launches.**
  - A launch you just made is never announced, whether it goes straight to the pad or into the queue.
  - When a queued launch reaches the pad, you get an alert.
- **Research.**
  - The 5-minute warning is based on the expected completion time. Changing the research pace moves it.
  - The research queue comes from `/company`. "Queue empty" is posted as soon as the last queued tech starts, while the lab still has work.
- **Combat data.** No battle had happened anywhere when this was written, so the fields of battle, duel and siege items were unknown.
  - The first item of each kind is saved as raw JSON in a `captures/` folder next to the script, and its alert shows a best-effort summary.
  - Send those files over once a fight happens, so the alerts can be improved.
- **Escape capsules and captives** (`/craft` `capsules`) work the same way: none had been seen when this was written, so each is posted with its fields as the game gives them (🆘), and again when it's no longer listed. The first is saved in `captures/`.
- **Clear Sky and bloodmarks** aren't in the API yet. The bridge watches the faction and company data, and the API's own description (hourly), for those words, and posts as soon as they appear.
- **Storage filling up** counts your keep limits. The game's own "full on" date assumes every good keeps arriving at today's rate. The bridge stops each good at its keep limit (and each draining good at zero), then works out when the store really fills. Stores that never fill are not reported.
- **Diplomacy** is checked every minute.
  - **Offers made to us** always go to Discord, even at startup. `state.json` stops repeats.
  - **Goods check.** When an offer asks for goods, the bridge compares the request with your stock there. It warns if you're short already, or will be before the offer expires because the stock is being used.
  - **Your own proposals** are only logged; you'll hear when they're accepted, rejected or countered.
  - **Agreements and unseen term kinds.** Agreements, and deal terms of a kind not seen yet (per-day payments, spacecraft, designs), are shown field by field and saved to `captures/`.
- **Standing advisor.** Every faction contract, faction alliance job, faction ship order and siege against us gets a line showing what it does to our standing. For example: `If done: +1.3 Morningstar (+92 → +93), −5 C-7 (+6 → +1), −5 UBN (−54 → −59)`. A siege says "If we surrender:" instead. The 🏛️ emoji is kept for the post when our standing has actually changed.
  - **Rules used:**
    - An alliance job gives easy +2, medium +4 or hard +10.
    - A full contract (board contract, ship order, or surrendering to a siege) gives +10.
    - A board offer that lists its own standing uses that instead: an Ambition's survey gives +4 and costs rivals nothing. A Codex recovery, which lists none, counts as a medium job.
    - Hard jobs and full contracts also cost each of the client's rivals −5.
    - **Standing pace** (from game build `CJPZkdZn`): a faction gives at most about 5.5 standing a real day, and a gain is worth 1 − standing ÷ 120 of itself, so a +10 job gives +5.5 at most and only +1.3 at +92. The line shows what the job would actually give today, or "no standing … (capped today)". Losses to rivals are never limited.
  - **Warnings:** the line also warns when a faction would change tier (Hostile ≤ −50, Unfriendly ≤ −10, Neutral, Friendly ≥ +10, Trusted ≥ +50). It marks ⛔ a faction at −50 or below, which refuses our contracts.
  - **Where the rules come from:** the API doesn't give them. They're from the game's standing logs and its glossary, and are set at the top of `sol_watch.py` (`FACTION_RIVALS`, `STANDING_GAIN`, `RIVAL_PENALTY`, `STANDING_PACE_*`). Only what we've used of today's allowance comes from the API. Update them if the game changes.
- **Alliance jobs arrive in a batch.** The game posts a day's faction jobs all at once. When 3 or more arrive together (`alliance_job_digest_at`), they're posted as one digest: grouped by faction, hardest and best-paid first, each with its standing effect. `alliance_job_min_level` in `settings.ini` can hide easy jobs.
- **Repair contracts** for our craft or an ally's are highlighted (🔧) whatever the tier filter says. Hazard strikes say whose craft were hit, us and allies first. When one of ours is damaged, the alert gives the deadline for repairing it before it's lost.
- **Halcyon Arms Exchange.** New listings come as one line per batch, grouped by ship type and price. Sales and withdrawals get one line too, and so do ships you buy.
- **Earth's government.** Changes to the Secretary-General, the offices, vacancies and the next Security Council session are posted. They're saved in `state.json`, so a change made while the bridge was stopped is still reported.
- **Earth's ballot.** Earth votes on its policies once a game year. A ballot opening, changing phase (nominations, voting) and closing is posted with the policies on it, and with "⚠️ We haven't voted yet" while voting is open and we haven't. Changes to the policies in force are posted too. Voting's close is warned 24, 12 and 1 real hours ahead (`ballot_warnings_hours`), with the policies on the ballot and our vote, or that we haven't voted. The API names policies only by id, not their effects. This is saved in `state.json` and posted even on a `--silent` start.
- **Pirate raids.** Every Solar News item of kind "raid" is posted (🏴‍☠️), for now, to show how often the pirates strike. One that names our company or one of our craft says "Pirates hit us".
- **Thefts near Earth.** Thieves take ships left at Earth or the Moon with no crew aboard and no orders, and the news says only "A ship slipped its moorings around Earth and the Moon", never whose. After one, the bridge compares our fleet ships (parked and under way) and our tugs, freighters and crew pods from just before the news with the next reading after it. It posts 🚨 with what's missing, and logs it if nothing is. A tug out on a debris clear leaves the vehicle list for a while, so it isn't counted. Readings are kept for two hours, so straight after a restart a theft can only be logged.
- **Tax and permit rises.** A segment's holder must give 24 game days' notice (about a real day) before raising its tax or permit price or closing entry. The bridge reads every claimed segment from `/segments` and posts the notice once (💸) when one of our craft is in or bound for that segment, or our ships are stationed in its low orbit. The notices already given are saved in `state.json`, so a restart doesn't repeat them.
- **Our claims.** A rival whose presence overtakes ours in one of our segments starts a takeover: posted once (⚔️), with when it began. A claim taken over or lapsed is posted too (🚩).
- **Market events** (shortages and gluts in one region) are only written to `bridge.log` for now, with how much of the good we hold there. `MARKET_EVENT_POSTS` near the top of `sol_watch.py` turns posting on.
- **Diverted fleets.** A rival fleet diverted under way toward one of our places alerts like any other incoming fleet, naming where it was heading before.
- **Slowed or stopped sites.** A site held back by something (crew, inputs, upkeep) for two checks in a row is posted, grouped by place and reason. One still producing is "🐢 slowed" (short of crew, a site keeps running at reduced output), posted only below 90% of its full output, with its share, and once until it gets clearly worse or recovers; one producing nothing is "⏸️ stopped". A crew shortage gives the place's crew and the crew it needs, e.g. "not enough crew (36.7 of 38)". The game names only one reason per site, so the bridge adds what it knows: away from Earth, a place out of industrial consumables or maintenance equipment stops all its production, and its stall post shows the stock of both (plus the crew shortage for refineries and factories; mines run without full crew). A site out of a production input names that input and its stock, e.g. "out of Steel (stock 0)". A site short of crew that makes nothing at all is said to be missing something else too, since a crew shortage only slows production.
- **Blocked vehicles.** A freighter, tug or other vehicle the game has stopped on its route (for example, not enough fuel for the next leg) is posted once with the game's reason, e.g. "🚧 Freighter Hauler-1 is blocked at Lunar orbit station since …: needs 105 kg of fusion fuel more for this leg". A new block on the same vehicle is posted again. Blocks already there the very first time the bridge runs are only recorded.
- **Stock that's already empty.** A "runs out" warning with less than 15 minutes left (`STOCK_ALREADY_EMPTY_HOURS`) isn't posted. The production-stall alert covers it.
- **New fields.** `logs/new-fields.log` ignores keys that are game data rather than field names: tech levels, and any good, tech, ship, place or facility id. A new faction id still shows up.
- **Game updates.** Every 5 minutes the bridge reads the public page at solthegame.com (without the API key) and compares the fingerprinted code files it loads (`assets/index-<fingerprint>.js`). A new fingerprint means a new build: it's logged and counted in the daily status (not posted, unless `game_update_posts = yes`), and the API spec is re-checked 2 minutes later. If the update changed the API, that is posted, with the project's address: a bridge update that uses it may follow. `logs/new-fields.log` then shows any new data the game started sending, which a future version of the bridge might use.
- **Bridge updates.** At startup and then once a day, the bridge reads the `VERSION` line of `sol_watch.py` on the project's GitHub page. While a newer version is published, it posts 🆕 to the Corp channel, once a day until you upgrade (a restart within the hour doesn't repeat it). If GitHub can't be reached it only logs that and tries again the next day.
- **Countdowns.** Each 72/48/24/12/4/1-hour mark is posted once as it's crossed. The marks already reached are saved in `state.json`, so a mark crossed while the bridge was stopped is posted on startup, and one already given isn't repeated. Something seen for the very first time (a new install, or a brand-new stock) is only listed in the startup summary.
- **Contracts taken by others.**
  - This works for offers that pass your tier filter. The board's list of recent wins names the winner.
  - Offers that simply expire aren't reported.
- **Reminders** (the research warning, flare arrival) are kept in memory. After a restart, they're rebuilt on the first poll.
- **Grouping.** Several changes found in one poll are bundled into one Discord message, and split if a message would go over Discord's 2,000-character limit.
- **No pings.** Chat text can't ping anyone, because `@everyone` is disabled in its posts.
- **Unchanged data.** Unchanged endpoints cost almost nothing, because the API returns "304 not modified".
- **Times** show the game time in a grey box, day first: `23 Apr 2032, 06:22 AM`. A post's own time comes after its main information: after the headline when details follow a " | ", otherwise at the end of the line. Game time runs 24 times faster than real time. A time that's still ahead (a landing, an expiry, a forecast) also shows the real time and a live countdown, e.g. `27 Apr 2032, 09:49 AM` · 🕰️ 12:57 AM (in 3 hours). Discord draws those two itself, in each viewer's own time zone and language setting, so they're right for anyone the channel is shared with. Hover over the real time for its full date. The log files write that real time out in `IRL_TIMEZONE` instead. The formats and icons are set near the top of `sol_watch.py` (`GAME_TIME_FORMAT`, `GAME_TIME_ICON`, `REAL_TIME_ICON`).

## Upgrading

The bridge tells you when a new version is out (🆕 in the Corp channel). Download it from https://github.com/Jomangork/sol-watch-bridge, replace the program files with the new ones and restart the bridge. The download never contains `.env` or `settings.ini`, so copying it over your folder keeps your key, webhooks and settings. Keep `state.json` and `logs/` too: `state.json` is what stops a restart from repeating old news. Read the new `CHANGELOG.md` entries first.

## Privacy and what it shares

- **What it reads:** your own company's data through your API key, plus public game data (other companies' names, news, the map, chat). Nothing else, and never the game's private messages (the API can't read them).
- **Where it goes:** only to your three Discord webhooks, and to the log files next to the script. Nothing is sent anywhere else. The bridge also loads the game's public front page every 5 minutes (without your key) to notice game updates, and once a day reads the start of `sol_watch.py` on GitHub to notice a new version of the bridge. That request carries nothing about you; GitHub sees only your machine's IP address, as with any web page. `update_check = no` in `settings.ini` turns it off.
- **Chat relay:** global and alliance chat are copied into your Discord channels, and into `logs/bridge.log`. That means other players' messages end up in your server, so think about who can see those channels. To turn chat relay off, untick **Public chat** (global chat) or **Alliance chat** on the bridge's API key: the bridge says once that it's missing and carries on with everything else. Alliance chat is your alliance's private channel, so check with your alliance before relaying it.
- **Logs** hold chat text and your company's activity. They live in `logs/` and are capped at about 40 MB in total (old files rotate out).
- **Asking for help:** share log lines or `errors.log`, never `.env`. Webhook URLs are already hidden in the logs.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Missing settings: ...` | `.env` isn't next to `sol_watch.py`, or a line is empty. |
| `HTTP 401`, or 🔑 "The game is rejecting our API key" | The API key is wrong or was revoked. Changing the account password revokes every key, and so does 90 days unused. Make a new one in the game, put it in `.env` and restart. |
| 🔑 "Our API key may not read `/…`" (HTTP 403), or "Our API key is missing N sections" at startup | The key isn't allowed those sections. Tick them under Settings › Account › API keys; the same key keeps working. The bridge reads Company (required), News and records, Launches and craft, Fleets, Contracts, Defense, Factions, Industry, Research, Mining, UEG, Alliance, Discoveries, Segments and Public chat (global and alliance chat), and optionally Account (daily gift reminder). It doesn't need Market. |
| `HTTP 429 ... Too many bad keys` | The key was revoked or mistyped. The API locks out bad keys for about 5 minutes. Fix the key, then wait. |
| `[Rate limited]` | Something else is using the same key. Give the bridge its own key. |
| `[Discord error] HTTP 401/404` | The webhook was deleted or the URL is wrong. Make a new one in Discord under Channel › Integrations › Webhooks. |
| IRL times are hours off | Set `IRL_TIMEZONE` in `.env`. If it then warns "Unknown IRL_TIMEZONE", check the name's spelling, and that `.venv/bin/pip install -r requirements.txt` ran. That command installs `tzdata`. |
| Nothing in `journalctl` | Check `systemctl --user status sol-watch` and the paths in the service file. |
| `[Lock] Another copy of the bridge is already running` | The service (or another terminal) is already running it. Stop that first, or use `--once`, which doesn't take the lock. |
| `[Retry] Startup data didn't load` | No network, or the API is down. It keeps retrying by itself. |
| `[Discord queue] Couldn't reach Discord` | Messages are being held and will be sent when Discord answers. |
| No 🟢 BRIDGE STATUS post this morning | The bridge or its machine is down. Check `systemctl --user status sol-watch` and `logs/errors.log`. |
| `systemctl status` says "Watchdog timeout" | The bridge froze and systemd restarted it. Look at the end of `logs/bridge.log` before the restart for what it was doing. |
| Something odd happened | Look in `logs/errors.log` first: it has only the problems, with full details. |

## Problems and ideas

Open an issue on the project's GitHub page, with the relevant lines from `logs/bridge.log` or `logs/errors.log` (never `.env`). Pull requests are welcome too.
