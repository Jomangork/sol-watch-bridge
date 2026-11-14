# SOL-Watch bridge changelog

Newest first. The version is `VERSION` near the top of `sol_watch.py`, and the bridge prints it on startup.

Version numbers are `MAJOR.MINOR.PATCH`, two digits each. A patch changes something the bridge already does (wording, a fix). A minor version adds a feature, usually a new kind of alert. The major number moves only when upgrading means editing your settings.

## 1.07.13 (Nov 14, 2026)

- **The game time comes after the news, not before it.** Every post started with its emoji and then the game time, so the time was the first thing read. The time now follows the main information: after the headline when the details come after a " | ", after a headline that ends in a colon (a list below it), and otherwise at the end of the line. For example: "⏳ **Steel at Some Station runs out in 8h** (12h warning) · `13 Dec 2032, 06:33 PM` | production input | 3 left". A trade offer has it right after who it's from, and an empty research queue right after its headline. Dates further along a post (a deadline, an expiry, a liftoff) stay where they were. This applies to both modes, and to chat.

## 1.07.12 (Nov 12, 2026)

From the first siege seen in the API (an alliance navy besieging a faction's outpost).

- **A siege is posted once.** A siege has no id, and its days left count down, so each game day it looked like a new siege, posted again with "Siege ended" for the old one. It's now one siege until it really ends.
- **Sieges are described.** They said only "siteId: Some Outpost". Now, for example: "**[XX] Navy (our alliance navy)** laid siege to the UBN (Unified Belter Nation)'s outpost at **Some Outpost** on Callisto | Falls in about 18 game days (18h) unless it is broken", and when it ends, whether it most likely fell.
- **Our alliance's navy is a friend.** Its battles and sieges were posted as "elsewhere". They now go with ours and our allies', and it's named "(our alliance navy)".
- **Lite mode:** siege news gets 🏰, not the 📰 of an unknown kind.

## 1.07.11 (Nov 12, 2026)

For the game's Nov 12 update (pirates take captives, battles of many sides).

- **Escape capsules and captives.** The API lists our crews' escape capsules still out, and crews we hold, under `/craft` `capsules`. The bridge didn't read it; now each new one is posted (🆘), and again when it's no longer listed (picked up, handed over or lost). Pirates now take the crew of the wrecks they tow captive, so a crew left in capsules matters more. No capsule has been seen in the API yet, so the post lists its fields as the game gives them, and the first one is saved to `captures/` to word it better later.
- **Lite mode:** a battle begun gets ⚔️ and an attack beaten off 🛡️, not the 📰 of an unknown kind.

## 1.07.10 (Nov 12, 2026)

- **A full plant is no longer posted as stopped.** A refinery or factory stops once its own good reaches your keep limit, as a mine does. The game reports a full mine as held by its keep limit, but a refinery or factory as held by "staffing" whenever the crew is even slightly short. The bridge then posted "crew is short, but that only slows production, so something else is missing too", for example for fuel and catalyst plants sitting at their limit. These are now only logged.

## 1.07.09 (Nov 12, 2026)

- **A battle between two other companies no longer stops fleet alerts.** A battle where neither side was us or an ally, and one side lost ships (for example a navy attacking pirates), made the fleet map check fail each time the map changed. That check also sends INCOMING FLEET alerts, so one could have been missed, and the battle itself was never posted under "battles elsewhere". Both work again.

## 1.07.08 (Nov 11, 2026)

Fixes from a day's logs.

- **No false "Game server down".** Since Nov 11 the game drops a connection every few minutes. The retry gets through, but 1.07.06 joined these drops into outage spells: five "Game server down" lines on an afternoon when nothing failed. A read that gets through now ends a spell, so only a real outage is reported. Scattered drops are counted as problems.
- **Battles are described.** A battle involving us or our allies said only "placeId: Pt:sun:3.228:67.74:0.275". It now reads, for example, "**Some Company (ally)** attacked **The Ashen Fleet (pirates)** in the asteroid belt (3.2 AU from the Sun): The Ashen Fleet (pirates) lost 1× Mining ship (UEG-M-06 wrecked); Some Company (ally) lost nothing | 1 pass, attacking with 1× Corvette". Wrecks name the ship, its owner, the place and how long it drifts there. A battle's wrecks are named in its post, not posted again.
- **No warnings for stock already gone.** Stock that shows as "0.0 left, using 0.00/day" was warned as "runs out in 1h". It's now only logged, as other empty stock is.
- **The same line twice in one post is shown once, with ×2:** two docking rings finishing their upgrade together, or two contracts with the same title won at once.
- **Lite mode:** a finished Ambition ("… finished Europa Ice Works, built with …") gets 🎯, and a faction's colony news gets 🏘️, instead of 🛒. A UEG Navy ship the game announces again (it announced some twice, days apart, sometimes in other words) is posted only the first time.

## 1.07.07 (Nov 11, 2026)

For the game's Nov 11 update (Faction Ambitions and the Codex).

- **Standing advice uses each offer's own figures.** Board offers now say what they do to standing. An Ambition's survey gives +4 and costs rivals nothing, but was shown as a full contract's +10 and −5 off each rival. Offers that give no figure (Codex recoveries) are shown as a medium job: +4, rivals untouched.
- **Clearer board labels.** Offers for a faction's Ambition are tagged `[AMBITION · Titan Waystation]` and Codex recoveries `[CODEX]`, instead of `[SPECIAL]`.
- **Naming a colony (🏷️, Corp channel).** When an Ambition is done and we did the most stages, its colony is ours to name, but only before a time limit. The bridge posts it at once, with the limit, and again 24 real hours before it (`NAMING_WARNINGS_HOURS`).
- **A Codex recovery that stops (⏹️, Corp channel).** If our craft leave an artifact site before its story is recovered, the recovery stops, and the game doesn't make that obvious. The bridge now posts it.
- **Lite mode: Ambitions get 🎯.** "The Concord of Seven announced its next Ambition: Titan Waystation" was posted with 🛒, the icon for faction warship sales.
- **Lite mode: Codex firsts grouped.** Several Codex firsts from one company in the same poll are one line: "Some Company was first to recover 3 documents: …". One company posted 6 in a minute.

## 1.07.06 (Nov 11, 2026)

- **A server outage is one line in the status post.** When the game's server was down for nearly four hours, every read failed and the status post said "Problems logged: 4385 (API error ×4385)". Now server errors that keep coming for 5 minutes or more show as "Game server down: 1:20 AM–5:00 AM (3h 40m)", in each reader's own time, and only other problems are counted. Short blips are counted as before.
- **HTTP 530 counts as the server being down.** The game's CDN sends it when the server behind it doesn't answer; it was logged as a problem of its own.

## 1.07.05 (Nov 10, 2026)

- **Fewer "slowed" posts.** A site still producing is now posted as 🐢 slowed only when it makes under 90% of its full output, and the post gives its share, e.g. "refinery (Copper, 80%)". It is posted once. A refinery that uses more ore than its own mine digs stays short for good and drops on and off the game's list as a little ore piles up, so it used to be posted again every 6 hours. Now it is posted again only if it falls 10 more points, or after running above 90% for 24 real hours. Sites the game says are held by upkeep while still at full output, and tiny crew shortfalls, aren't posted. Stopped sites are posted as before. The three numbers are `slowed_post_below_percent`, `slowed_worse_by_points` and `slowed_recovery_hours` in `settings.ini`.
- **Lite mode: Codex firsts get 🏺.** A company first to recover a Codex story ("Some Company was first to recover an observer's notebook at low Earth orbit"), new in the game's Nov 10 update, was posted with 📰 as a kind the bridge didn't know.

## 1.07.04 (Nov 10, 2026)

- **Earth's ballot closing.** Both modes now warn 24, 12 and 1 real hours before the yearly ballot's voting closes (🗳️), with what's on the ballot. The full bridge adds our vote, or "⚠️ We haven't voted yet". Lite mode can't read the ballot, so it works the close out from the game's schedule (1 January, then 120 game days of nominations and 168 of voting). The hours are `ballot_warnings_hours` in `settings.ini`.
- **Walkout end time, fixed:** a walkout "until 2032-09-21" now ends at the start of that date, not 12 hours into it.

## 1.07.03 (Nov 10, 2026)

- **Clear Sky walkouts at our outposts.** When crews at one of our outposts walk off the job in support of Clear Sky, the bridge now posts it to the Corp channel (✊), with when output resumes and every operation that stops there. Any other Clear Sky news naming us is posted too.
- **The stall that follows isn't blamed on crew.** A walkout makes the game report every site there as short of crew, even mines, which need none. While it lasts, those stalls aren't posted again; the walkout post already lists them.
- **1.07.02's upgrade rule is undone.** The Mare Humorum stall it was written for came from a walkout, not the mine's upgrade. A site under upgrade only runs slower, so crew stalls there are posted as before.

## 1.07.02 (Nov 10, 2026)

- **No false crew stalls during an upgrade.** While a site is being upgraded, the game reports it as short of crew, even a mine, which needs none. The bridge no longer posts "stopped: not enough crew" for a site under upgrade; it logs it, and "Upgrade complete" follows as before. A site under upgrade that runs out of an input is still posted.

## 1.07.01 (Nov 9, 2026)

- **Lite mode: two more news kinds get their own icon.** A company coming back after a long silence ("Some Company returned to business") is posted with 🔙, and a fleet driving pirates off ("Some Company's fleet drove The Ashen Fleet's ships from…") with ⚔️. Both were already posted, with the 📰 icon kept for news kinds the bridge doesn't know.

## 1.07.00 (Nov 8, 2026)

- **Lite mode.** The bridge can now post only public game information, the same for every player, to one channel (`WEBHOOK_LITE`), for a server anyone may read. See "Lite mode" in the README.
- **What it posts:**
  - **News:** chosen Solar News, including pirate raids, flare forecasts, new and vanished companies, launch failures and surrendered outposts. Busy kinds such as contracts and price moves are only counted. A launch failure stops at "failed during ascent.", leaving out what it carried, and a launch pad still being built gets 🏗️ (🚀 once it opens).
  - **Records:** the memorial with crew names, tech firsts and duel records.
  - **Companies:** name changes and bully marks.
  - **Chat:** global chat.
  - **Digest:** at the status times, a summary with a pirate tally and the top five earners.
- **It needs a key with only News and records and Public chat.** The bridge switches to lite mode by itself when its key can't read Company (or with `mode = lite` in `settings.ini`). It pauses on a key that can read anything more, and refuses any read beyond the public endpoints, so company data can't reach a public channel by mistake.
- **The full bridge is unchanged.** Its webhooks are now checked after the key, so a lite install needs only `WEBHOOK_LITE`.

## 1.06.00 (Nov 8, 2026)

- **Pirate raids.** Every pirate raid in Solar News is now posted to the Corp channel (🏴‍☠️), for example "The Ashen Fleet shot down Some Company's Some Craft-11". This is for now, to show how often the pirates strike. One that names our company or one of our craft is headed "Pirates hit us".
- **Thefts near Earth.** Thieves take ships left at Earth or the Moon with no crew aboard and no orders. The news says only "A ship slipped its moorings around Earth and the Moon", never whose. After one, the bridge compares our ships from just before it with the next reading after it, and posts 🚨 with any that are missing. Tugs out on a debris clear aren't counted.
- **Tax and permit rises where we are.** When a segment's holder gives notice that it's raising its tax or permit price, the bridge posts it once (💸) if our craft are in or bound for that segment, or our ships are stationed there. The post names the holder, our craft there, the old and new terms, and when the rise starts.
- **Our claims.** A rival starting to take over one of our segments is posted (⚔️), and so is a claim we lose (🚩).
- **Needs the Segments section.** Tick **Segments** on the bridge's API key (Settings › Account › API keys). Until then, the bridge says once that it's missing, and everything else carries on.
- **Market events are logged.** Shortages and gluts in one region now go to `bridge.log` with how much of the good we hold there. They aren't posted (`MARKET_EVENT_POSTS` turns that on).

## 1.05.02 (Nov 8, 2026)

- **Research hand-over, fixed:** when research finished with more in the queue, the bridge could say the lab was idle, because the game can take a few minutes to start the next item (about 3 on Nov 7). Now it posts only "finished" and waits. If nothing has started 10 minutes later, it warns that the lab is idle, naming what's next in the queue and the game's reason for holding it. With an empty queue it still warns at once.
- **Standing pace.** The game update of Nov 8 caps how much standing a faction gives in a real day (about 5.5; the UEG about 3.9), and makes gains smaller the higher our standing is. The standing table now shows how much more each faction can give today, or "capped today", and how much of the UEG's allowance is used. The day resetting is logged.
- **Standing advisor follows it.** The "If done:" line on contracts, alliance jobs, ship orders and sieges now shows what a job would really give today, e.g. +1.3 rather than +10 at +92, or "capped today". Rivals' losses are unchanged: the game never limits them.

## 1.05.01 (Nov 7, 2026)

- **New places are logged, not posted.** A new landing site isn't urgent and can't be reached for days, so 1.05.00's post is now off by default: new places go to `bridge.log`, and names still refresh. To post them as before, set `new_place_posts = yes` under `[alerts]` in `settings.ini`.

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
