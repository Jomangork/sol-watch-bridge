# SOL-Watch bridge changelog

Newest first. The version is `VERSION` near the top of `sol_watch.py`, and the bridge prints it on startup.

Version numbers are `MAJOR.MINOR.PATCH`, two digits each. A patch changes something the bridge already does (wording, a fix). A minor version adds a feature, usually a new kind of alert. The major number moves only when upgrading means editing your settings.

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
