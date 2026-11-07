# SOL-Watch bridge changelog

Newest first. The version is `VERSION` near the top of `sol_watch.py`, and the bridge prints it on startup.

Version numbers are `MAJOR.MINOR.PATCH`, two digits each. A patch changes something the bridge already does (wording, a fix). A minor version adds a feature, usually a new kind of alert. The major number moves only when upgrading means editing your settings.

## 1.05.00 (Nov 7, 2026)

- **New places.** The game can add landing sites and stations without a game update or any change to the API (seven asteroid sites appeared this way on Nov 7). The bridge now checks the game's list of places every 6 hours and posts new ones to the general channel, grouped by world, with each site's three richest deposits. A place added while the bridge was stopped is posted when it starts again; the first run only records what's there.
- **Place names stay current.** Names were read only at startup, so a new site showed as a code like "Hygiea/a" until a restart. The same check now refreshes them.

## 1.04.06 (Nov 7, 2026)

- **Blocked launches by name.** A queued launch the game is holding back (all launch teams busy, parts on the way) is now posted with the craft's name and what it's for, e.g. "Queued launch Kestrel-3 blocked (Crew for an outpost on Ceres): …", instead of only the reason, so two such posts can be told apart. Without a name, the game's label for the launch is used. Other queued actions are posted as before.

## 1.04.05 (Nov 6, 2026)

- **Salvage races, fixed:** a salvage team that failed (for example lost at launch) or finished kept counting as heading for its derelict until the bridge restarted, so a rival taking that derelict could be posted as taken from under a team that no longer existed. Only teams still on their way count now, and a new team sent to the same derelict is the one named.

## 1.04.04 (Nov 6, 2026)

- **Every derelict by default.** Other companies' derelicts are now posted whatever their salvage value; before, only those worth $500M or more. To keep a floor, set `min_value` under `[derelicts]` in `settings.ini` (or `DERELICT_MIN_VALUE` in `.env`), e.g. `min_value = 500`. `0` now means every derelict; `off` still turns the check off. After upgrading, derelicts already out there below your old floor are posted once.

## 1.04.03 (Nov 5, 2026)

- **Blocked vehicles.** When the game stops one of your freighters, tugs or other vehicles on its route, for example for lack of fuel for the next leg, the bridge now posts it once with the game's reason, where it is and since when. Before, a freighter could sit idle for hours unnoticed. A block that starts while the bridge is stopped is posted when it starts again.

## 1.04.02 (Nov 5, 2026)

- **Salvage races.** If another company salvages a derelict that one of your salvage teams is on its way to, the bridge now posts it, naming your team, so you know the trip will come back empty.
- **Halcyon Arms Exchange:** a listing that disappears is no longer posted as "sold or withdrawn". The API can't say who bought it or whether it was withdrawn, so the post added nothing; it's now only logged. New listings and factions' purchases are still posted.

## 1.04.01 (Nov 5, 2026)

- **Clearer production stalls.** The game gives one reason per site, which could blame crew for a site that was really out of upkeep goods. Away from Earth, a place out of industrial consumables or maintenance equipment stops all its production, so its stall post now says "can't pay upkeep" and shows the stock of both goods, adding the crew shortage for refineries and factories (mines run without full crew). A site out of a production input shows that input's stock, and one short of crew that makes nothing is said to be missing something else too.

## 1.04.00 (Nov 5, 2026)

- **Status post at any times you like.** `status_times` in `settings.ini` takes as many times of day as you want, with minutes, e.g. `status_times = 06:00, 16:00` (default `08:00`; `off` for none). Each post now counts problems and game updates since the previous post instead of always the last 24 h. If the bridge was down over several times, it posts once when it's back. The older `daily_status_hour` still works.
- **Proton storms** (announced in 1.02.00): a solar flare forecast now names its proton storm rating (S1 minor, S3 strong or S4 severe) and warns that craft without enough radiation shielding will be blinded for a while, letting rivals slip past where they watch.

## 1.03.00 (Nov 5, 2026)

- **New settings file, `settings.ini`.** Every adjustable setting now lives in one optional file of your own, so an upgrade never undoes your changes (before, many were at the top of `sol_watch.py` and were lost on each upgrade). Copy `settings.example.ini` to `settings.ini` and change what you like; the example explains each line. Without the file the bridge runs on its defaults, so it still needs only `.env`.
- A value the bridge can't use, or a misspelt setting name, is reported in the log and its default kept. Poll intervals can't go below 10 seconds, and the bridge warns if they add up to more than 40 API reads a minute.
- `.env` is now for secrets only. `DERELICT_CHECK_MINUTES`, `DERELICT_MIN_VALUE` and `IRL_TIMEZONE` still work there as before.
- **Upgrading from 1.02.00 or older:** nothing to do. If you had changed settings at the top of `sol_watch.py`, put them in `settings.ini` (the names are in `settings.example.ini`).

## 1.02.00 (Nov 5, 2026)

- **Alliance chat is its own key section.** Since the game's Nov 5 update, `/chat` includes your alliance's messages only when the key has **Alliance chat** ticked, and new keys leave it unticked. The bridge now lists it as optional and says once at startup if it's not ticked. Global chat still needs Public chat.
- **Game updates are no longer posted,** only logged and counted in the daily status: several builds can go live in a night. If an update changes the API, that is still posted. `GAME_UPDATE_POSTS = True` brings the posts back (without the build's file names, which only the log keeps).
- **API change posts** now point to the project page, where a bridge update that uses the change may follow.
- **Derelicts are checked hourly** instead of every 10 minutes: each check downloads the whole craft map (~1.3 MB). Two new optional `.env` settings, which survive upgrades: `DERELICT_CHECK_MINUTES` (at least 10, or `off`, which skips the download entirely) and `DERELICT_MIN_VALUE` (the smallest salvage value to post, in $M; default 500). See `.env.example`.
- **Coming:** the game's new proton storms (each flare now brings one, rated S1, S3 or S4, that blinds sensors for a while). The API doesn't describe them yet; once it does, the flare alert will say how severe the storm is.

## 1.01.00 (Nov 5, 2026)

- **Update notice.** At startup and once a day, the bridge checks the project's GitHub page for a newer version of itself. While one exists, it posts "🆕 SOL-Watch x.yy.zz is available" to the Corp channel, once a day until you upgrade. It reads only the start of `sol_watch.py` there, sends nothing about you, and stays quiet if GitHub can't be reached. `UPDATE_CHECK = False` at the top of `sol_watch.py` turns it off.

## 1.00.00 (Nov 4, 2026)

First public release. It grew through private versions 0.01 to 0.12, run against the live game since late October 2026.

What it watches and posts is listed in the README, under "What it posts". In short:

- **Your company:** research, construction, launches and arrivals, losses, blocked queues, production slowed or stopped, stocks and crew supplies running out, storage filling up, mining.
- **Security:** rival warships heading for your places (also when diverted mid-flight), battles, sieges and siege contracts against you, spies caught, alliances marking you hostile.
- **Contracts and politics:** the contract board (filtered by tier), alliance jobs and pad goals, a standing advisor on faction work, faction standings, diplomacy offers and agreements, Earth's government and its yearly ballot, the Halcyon Arms Exchange.
- **The wider game:** hazards, debris and asteroid threats, valuable derelicts, salvage, battles elsewhere, badges and firsts, global and alliance chat.
- **Its own health:** a daily status post, API key problems (rejected key, missing sections), API changes and new game builds.
