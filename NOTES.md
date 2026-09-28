# Draft Scout – project notes

## Status
- v1 working: waiver targets, free agents table, league squads
- Tested against my real league: working
- check_league.py added to sanity-check a real league from the terminal
- Friendlier error messages (league not found, team not found, team not in a league, FPL site updating, blocked by the site, server crashed, app.py not running)
- Personal view built: enter team ID → league found automatically → your waiver targets and squad → bookmarkable link (/?team=276914). Checked against the real league
- Waiver targets now include players with a 75% chance of playing, marked with a "!" warning and a fully fit backup from the wire
- Ratings now measure each stat against the 95th percentile of regular players (180+ minutes), capped at 1.0, so one outlier can't drag everyone down
- League squads now use a legal best eleven (1 keeper, 3-5 DEF, 2-5 MID, 1-3 FWD) and show the formation and bench
- On GitHub: https://github.com/rgurung3/Fpl-Draft-Scout
- Deployed to Render (free plan): https://fpl-draft-scout.onrender.com. It redeploys automatically on every push to main. The Draft site accepts requests from Render (checked /api/team/276914 after deploying)
- Tests (pytest, 43 passing) and CI (GitHub Actions); ruff clean

## How it works
- app.py fetches data from the FPL Draft site (team → league lookup, league details, who owns whom) and the classic FPL site (fixtures + difficulty), then rates every player 0–100.
- Two API routes: /api/team/<team id> (what the browser uses) and /api/league/<league id> (used by check_league.py and tests). Both build the page data with load_league().
- All requests to the Draft site go through fetch(), which turns failures into an FplError with a friendly message. The routes just catch FplError.
- app.py also works out the waiver suggestions (waiver_targets) and each squad's best eleven and strength (best_eleven, squad_strength), so that logic is covered by tests.
- static/index.html is the page you see in the browser. It reads ?team= from the address, loads that team, and puts the personal link back in the address bar. It draws what the server worked out.
- check_league.py loads a league through the app and prints a health report. Run: python check_league.py <league id> or python check_league.py --team <team id>. Lines with !! need a look.
- tests/test_app.py uses fake data, so tests never call the real servers.
- Run locally: python app.py, then open http://127.0.0.1:5000
- On Render: build command `pip install -r requirements.txt`, start command `gunicorn app:app --workers 1 --threads 4 --timeout 60`. Render picks the Python version from .python-version (3.12). Deploy steps are in README.md.

## How the rating works
- Five stats per player: form, points per game, xGI per 90 (0 if under 180 minutes), minutes share, fixture ease over the next 3 GWs (sum of 6 - difficulty; doubles count twice, blanks count zero).
- Each stat is divided by its 95th percentile (SCALE_PERCENTILE) among players with 180+ minutes (MIN_MINUTES) and capped at 1.0. Early in the season, before anyone has 180 minutes, the scale uses everyone who has played.
- Then the stats are combined with position weights (WEIGHTS), times 100, times availability (chance of playing, or 0 if injured/suspended).

## How suggestions work
- Per position: my players weakest first vs free agents best first, paired one-for-one, max 2 pairs per position (PAIRS_PER_POSITION).
- Free agents count if they're at least 75% likely to play (MIN_CHANCE_FOR_WAIVERS). Their rating is already scaled down for the doubt, so a 75% player has to be clearly better to show up.
- A doubtful claim gets a "!" warning and a backup: the best fully fit free agent in the same position that isn't already one of the suggestions.
- A pair only shows if the free agent rates at least 3 points higher (MIN_GAIN).
- Top 5 pairs by gain are shown (MAX_TARGETS).
- League squads = average rating of each team's best legal eleven: take the minimum at each position (1 GKP, 3 DEF, 2 MID, 1 FWD), then fill the last 4 places with the best players left without breaking a maximum (1 GKP, 5 DEF, 5 MID, 3 FWD). My squad is opened and highlighted, with formation and bench.

## Decisions
- Flask backend, because the Draft site blocks requests made directly from a browser page.
- Weights differ by position and live in WEIGHTS in app.py; the fixture window is LOOKAHEAD.
- Ownership: element-status owner is the manager's entry_id (not the league entry id). Confirmed against the real API.
- Team ID = that same entry_id (the number after /entry/ on the Draft site). Its league comes from /api/entry/<id>/public → entry.league_set.
- If a team is in more than one league, the first is used; ?league=<id> picks another and a league dropdown appears. Only valid league IDs for that team are accepted.
- Personal links instead of accounts: no passwords, nothing stored on the server. Anyone with a link sees that team's view, which is fine because it's all public Draft data.
- Rating scale: 95th percentile rather than the single best player. Trade-off: the top ~5% look the same on a capped stat, but that rarely matters in Draft because elite players are owned; the middle of the pack (where waiver decisions happen) gets spread out properly. Percentile ranks were rejected because they throw away the size of the gaps.
- Only catch specific exceptions (not except Exception); newer ruff versions flag the blind catch and would fail CI.
- On Render the app runs under gunicorn, not `python app.py`, so Flask's debug mode is never on in public. One worker (so there's one shared 10-minute cache and it fits the free plan's memory) with 4 threads (so a few people can load at once). Timeout 60s because a league load makes several FPL requests.
- Python 3.12 everywhere: CI uses it and .python-version tells Render to use it. The file name must start with a dot; downloading it can strip the dot, and Render then silently falls back to its default (3.14).
- requirements.txt = what the app needs to run (what Render installs). requirements-dev.txt = that plus pytest and ruff (for my computer and CI).

## Roadmap
1. Test with real league (done)
2. Deploy to Render (done)
3. Personal view: enter team ID → auto-find league, bookmarkable link (done)
4. Trade analyzer
5. AI "why this pick?" explanations
6. Accounts/login (needed for watchlists + limiting AI usage)

## Known issues / ideas
- The FPL Draft data feed isn't officially documented and could change.
- The Draft site sits behind Cloudflare and may block cloud servers (403). It worked from Render at deploy time, but Cloudflare can change its mind; if the live site starts showing the 403 message, that's the cause, not a bug in the app.
- Free Render plan sleeps after 15 minutes with no visitors; the next visit takes about a minute to wake it up. The in-memory cache is lost when it sleeps (harmless).
- If several teams have a double gameweek (more than ~5% of regular players), the fixture scale lands on a double value and single-game teams still look weak on fixtures.
- Ratings are higher overall than before the percentile change, so MIN_GAIN = 3 may need tuning after watching real suggestions for a week or two.
- The backup for a doubtful claim is taken from the wire, so someone else may claim it first. Could also suggest a bench player as the fallback.
- Doubtful players below 75% (50%, 25%) are never suggested, even if they'd still rate higher.
- The league dropdown for multi-league teams shows "League <id>" for leagues that aren't loaded (names would cost an extra request each).

