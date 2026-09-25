Draft Scout – project notes

Status
v1 working: waiver targets, free agents table, league squads
Tested against my real league: mostly working
check_league.py added to sanity-check a real league from the terminal
Friendlier error messages (league not found, FPL site updating, blocked by the site, server crashed, app.py not running)
On GitHub: https://github.com/rgurung3/Fpl-Draft-Scout
Tests (pytest, 20 passing) and CI (GitHub Actions); ruff clean
How it works
app.py fetches data from the FPL Draft site (league details, who owns whom) and the classic FPL site (fixtures + difficulty), then rates every player 0–100.
static/index.html is the page you see in the browser. It also works out the waiver suggestions and league squad strength from the ratings.
check_league.py loads a league through the app and prints a health report. Run: python check_league.py <league id>. Lines with !! need a look.
tests/test_app.py uses fake data, so tests never call the real servers.
Run locally: python app.py, then open http://127.0.0.1:5000
How the rating works
Five stats per player: form, points per game, xGI per 90 (0 if under 180 minutes), minutes share, fixture ease over the next 3 GWs (sum of 6 - difficulty; doubles count twice, blanks count zero).
Each stat is divided by the best value in the game (so the top player on that stat = 1.0), then combined with position weights (WEIGHTS), times 100, times availability (chance of playing, or 0 if injured/suspended).
How suggestions work
Per position: my players weakest first vs fully fit free agents best first, paired one-for-one, max 2 pairs per position.
A pair only shows if the free agent rates at least 3 points higher.
Top 5 pairs by gain are shown.
League squads = average rating of each team's top 11 players.
Decisions
Flask backend, because the Draft site blocks requests made directly from a browser page.
Weights differ by position and live in WEIGHTS in app.py; the fixture window is LOOKAHEAD.
Ownership: element-status owner is the manager's entry_id (not the league entry id). Confirmed against the real API.
Only catch specific exceptions (not except Exception); newer ruff versions flag the blind catch and would fail CI.
Roadmap
Trade analyzer
AI "why this pick?" explanations (Claude API, key in .env)
Deploy to Render so it works on my phone
Known issues / ideas
The FPL Draft data feed isn't officially documented and could change.
The Draft site sits behind Cloudflare and may block cloud servers (403). Watch for this when deploying to Render.
"Best eleven" ignores formation (could be 3 keepers). Could pick a valid XI instead.
Stats are scaled against the single best player, so one outlier squashes everyone else. Could scale against e.g. the 95th percentile.
Waiver targets skip doubtful (75%) players entirely.
Consider pinning the ruff version in CI so new ruff releases don't break builds by surprise.