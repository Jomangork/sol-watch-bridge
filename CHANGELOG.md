# SOL-Watch bridge changelog

Newest first. The version is `VERSION` near the top of `sol_watch.py`, and the bridge prints it on startup.

Version numbers are `MAJOR.MINOR.PATCH`, two digits each. A patch changes something the bridge already does (wording, a fix). A minor version adds a feature, usually a new kind of alert. The major number moves only when upgrading means editing your settings.

## 1.00.00 (Nov 4, 2026)

First public release. It grew through private versions 0.01 to 0.12, run against the live game since late October 2026.

What it watches and posts is listed in the README, under "What it posts". In short:

- **Your company:** research, construction, launches and arrivals, losses, blocked queues, production slowed or stopped, stocks and crew supplies running out, storage filling up, mining.
- **Security:** rival warships heading for your places (also when diverted mid-flight), battles, sieges and siege contracts against you, spies caught, alliances marking you hostile.
- **Contracts and politics:** the contract board (filtered by tier), alliance jobs and pad goals, a standing advisor on faction work, faction standings, diplomacy offers and agreements, Earth's government and its yearly ballot, the Halcyon Arms Exchange.
- **The wider game:** hazards, debris and asteroid threats, valuable derelicts, salvage, battles elsewhere, badges and firsts, global and alliance chat.
- **Its own health:** a daily status post, API key problems (rejected key, missing sections), API changes and new game builds.
