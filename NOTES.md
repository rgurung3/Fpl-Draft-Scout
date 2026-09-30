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
- Trade analyzer built: pick my players and another manager's, see both teams' best eleven and strength before/after, a per-position breakdown (rating points each position gains or loses in the best eleven), and a verdict (good and fair / good but unfair / barely changes / worse). Shortcut from League squads
- Waiver targets now say why: a one-line reason in rating points, plus the one thing the dropped player still does better. Worked out from the numbers, no AI
- Views renamed and reshaped: "Next 5" (5 gameweeks) and "Until the break" (up to the next international break, worked out from the gameweek deadlines; 3-10 gameweeks). Until the break counts form half as much and uses injury return dates. The internal view keys are still "week" and "season". Season view details: a toggle. Every player has both ratings; the chosen one drives waivers, reasons, squads and trades. check_league.py prints the biggest risers and fallers between the views. Not yet checked against the real league
- ruff pinned to 0.16.9 in requirements-dev.txt
- League race built: chart of each manager's league points climbing from 0 (you in amber with a dot per week, hover a line for who it is) and a weekly points grid shaded vs the league average with W/D/L. Head-to-head leagues only. Not yet checked against the real league
- /health route added for an uptime monitor to keep the free Render plan awake
- Free agents table shows the top 15 with a "Show more" button (up to 60); search and position filters look through everyone
- Tests (pytest, 118 passing) and CI (GitHub Actions); ruff clean

## How it works
- app.py fetches data from the FPL Draft site (team → league lookup, league details, who owns whom) and the classic FPL site (fixtures + difficulty), then rates every player 0–100.
- Three API routes: /api/team/<team id> (what the browser uses), /api/team/<team id>/trade (the trade analyzer) and /api/league/<league id> (used by check_league.py and tests). They all build the page data with load_league(); the two team routes find the league with league_for_team().
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
- score_players also returns a breakdown: rating points per piece (form, ppg, xgi, mins, fix = 100 x weight x stat) plus avail (what an injury doubt takes off, 0 or less). The pieces add up to the rating, give or take rounding. Every player on the page carries it.

## How the season view works
- VIEWS in app.py holds each view's settings: weights (WEIGHTS / SEASON_WEIGHTS), the injury rule and whether return dates are used. The window length comes from view_windows(): "week" = LOOKAHEAD (5); "season" = break_window().
- break_window(deadlines, next_gw): the first gameweek g from next_gw where the deadline of g+1 is more than BREAK_GAP_DAYS (10) after g's means a break after g, so the window is next_gw..g. Kept between BREAK_MIN_WEEKS (3) and BREAK_MAX_WEEKS (10), never past GW38; SEASON_LOOKAHEAD (8) if no break is found. Deadlines come from boot events data (gameweek_deadlines). Checked on the real calendar: from GW6 the window is GW6-10 (15 days between the GW10 and GW11 deadlines).
- The league data also has "windows": {view: {from, to}}, which the page uses for the button labels.
- Weights are dicts per position, {piece: weight}, each adding up to 1 (a test checks this). Missing pieces count as 0.
- New pieces (season only): cs = CS_BASELINE (2.0) minus expected goals conceded per 90, so lower xGC = better; dc = defensive_contribution per 90; crea = creativity per 90. All need MIN_MINUTES, like xGI, and use the same 95th-percentile scale.
- Season weights: form drops to 0.15 everywhere (Next 5 has 0.30), and the freed weight goes to fixtures, xGI and clean sheets. GKP adds cs; DEF adds cs, dc and crea (creativity for attacking full backs); MID adds dc; FWD leans more on xGI and fixtures. All first guesses. Fixtures get a bit less weight over the longer window.
- Injuries in the season view: chance >= SEASON_FULL_FROM (75) counts as fully fit; 50% and below are unchanged (50% halves the rating). A player at 0% who has a news_return date counts as available for the share of the window's gameweeks whose deadline is on or after that date (availability() with window_deadlines); no date, or a doubt above 0%, works as before. Waiver suggestions still use the real next-week chance, so an injured player isn't suggested. The player's "chance" field (used for the "!" warning and waiver rules) is always the real next-week chance.
- load_league(league_id, view) rates everyone in both views: week_score and season_score on every player; score, breakdown and fixtures follow the chosen view. Unknown views fall back to "week". All three routes accept ?view=season.
- The page: toggle in the league bar reloads with ?view=, the link keeps &view=season, the free agents table shows both ratings (chosen one in bold, sorted by it), fixture strips show the view's window, trades send the view.

## How the league race works
- league_history(details) in app.py, returned as "history" in the league data. Built from details["matches"] (already fetched), so no extra requests.
- Only matches with finished = true count; future gameweeks are listed with 0 points.
- Matches (and standings) name managers by league entry ID; league_entries maps it ("id" -> "entry_id") to the team IDs the rest of the app uses.
- Each week: W = 3 league points (WIN_POINTS), D = 1 (DRAW_POINTS), L = 0. A match with no opponent score gets no result.
- Per manager: points, results, league_points (running total), behind (vs the leader that week), scored, table_total (the official table's total). Sorted by league points, then points scored. Plus the league's average score each week.
- None for classic-scoring leagues (no matches) and before any gameweek finishes; the page then shows a short message.
- The page draws it with plain SVG (no chart library). Lines are nudged a pixel or two apart by table position so tied managers stay visible (visual only). The grid scrolls sideways once there are many gameweeks, newest weeks shown first, team names stay put.
- check_league.py checks everyone has weekly results and that our league points equal the official table.

## How suggestions work
- Per position: my players weakest first vs free agents best first, paired one-for-one, max 2 pairs per position (PAIRS_PER_POSITION).
- Free agents count if they're at least 75% likely to play (MIN_CHANCE_FOR_WAIVERS). Their rating is already scaled down for the doubt, so a 75% player has to be clearly better to show up.
- A doubtful claim gets a "!" warning and a backup: the best fully fit free agent in the same position that isn't already one of the suggestions.
- A pair only shows if the free agent rates at least 3 points higher (MIN_GAIN).
- Top 5 pairs by gain are shown (MAX_TARGETS).
- Each pair has a "why" (swap_reasons): claim breakdown minus drop breakdown, piece by piece. Up to 3 reasons (MAX_REASONS) of at least 1 point (MIN_REASON), biggest first; if none are that big, the single biggest. The drop's injury doubt counts as a reason ("Porro is 50% to play"); the claim's own doubt isn't repeated because it has the "!" row. "against" = the biggest piece in the drop's favour ("But Porro has more attacking threat -6").
- League squads = average rating of each team's best legal eleven: take the minimum at each position (1 GKP, 3 DEF, 2 MID, 1 FWD), then fill the last 4 places with the best players left without breaking a maximum (1 GKP, 5 DEF, 5 MID, 3 FWD). My squad is opened and highlighted, with formation and bench.

## How the trade analyzer works
- evaluate_trade(players, me, them, give, get) in app.py. Route: /api/team/<me>/trade?with=<them>&give=12,34&get=56&league=<id>.
- Checks first (TradeError, shown as a 400 with a friendly message): not trading with myself, at least one player each side, give are all mine, get are all theirs, same positions on both sides. Repeated IDs are dropped.
- Then it copies the player list with the traded owners swapped (the real list is never changed) and runs squad_strength for both managers before and after.
- Each side reports strength before/after/change, formation before/after, who joins or leaves the best eleven, and by_position: the change in total rating of each position in the best eleven (xi_by_position). Totals, not averages, so "MID -25, FWD +25" reads as one swap cancelling the other. A formation shift shows up here too (e.g. DEF +50 when a defender takes a midfield spot).
- Verdict: my change <= -TRADE_MIN_GAIN (0.5) = worse; under +0.5 = no change; otherwise good_and_fair if their change >= -FAIR_MARGIN (0.5), else good_but_unfair. 0.5 strength is roughly one starter improving by 5-6 rating points.
- The page ticks players, sends the IDs, and draws the verdict and both panels. It sends ?league= so the server uses the same league as the page.

## Decisions
- Flask backend, because the Draft site blocks requests made directly from a browser page.
- Weights differ by position and live in WEIGHTS in app.py; the fixture window is LOOKAHEAD.
- Ownership: element-status owner is the manager's entry_id (not the league entry id). Confirmed against the real API.
- Team ID = that same entry_id (the number after /entry/ on the Draft site). Its league comes from /api/entry/<id>/public → entry.league_set.
- If a team is in more than one league, the first is used; ?league=<id> picks another and a league dropdown appears. Only valid league IDs for that team are accepted.
- Until the break counts form half as much because a few games of form say less about a longer stretch. The windows stopped overlapping so much: Next 5 is form-led, Until the break ends at a real calendar event.
- Season view adds role-specific stats instead of turning up xGI for everyone, so a change aimed at attacking full backs doesn't move every defender with a lucky early xGI. Weights are first guesses; check_league.py's movers list is how we tune them against the real league.
- The Draft feed has these fields (checked on Porro): creativity, threat, influence, ict_index, expected_goals_conceded, clean_sheets, goals_conceded, defensive_contribution, clearances_blocks_interceptions, recoveries, tackles, starts, set-piece orders, news_return. Events have deadline_time per gameweek.
- The Ballahulics league is head-to-head (league "scoring": "h"), so the race uses league points (what decides the table), not total FPL points. The weekly grid shows FPL points.
- Waiver explanations are short and worked out from the numbers, not AI: instant, free, exact, testable. A long AI version was considered and dropped: it would only explain this week's numbers, and the bigger need is a long-term view.
- Personal links instead of accounts: no passwords, nothing stored on the server. Anyone with a link sees that team's view, which is fine because it's all public Draft data.
- Rating scale: 95th percentile rather than the single best player. Trade-off: the top ~5% look the same on a capped stat, but that rarely matters in Draft because elite players are owned; the middle of the pack (where waiver decisions happen) gets spread out properly. Percentile ranks were rejected because they throw away the size of the gaps.
- Only catch specific exceptions (not except Exception); newer ruff versions flag the blind catch and would fail CI.
- Keep-awake pings go to /health, not a league page: it doesn't call FPL and is 2 bytes, so pings use almost none of the free plan's outbound bandwidth (going over it suspends free services if no card is on file).
- On Render the app runs under gunicorn, not `python app.py`, so Flask's debug mode is never on in public. One worker (so there's one shared 10-minute cache and it fits the free plan's memory) with 4 threads (so a few people can load at once). Timeout 60s because a league load makes several FPL requests.
- Python 3.12 everywhere: CI uses it and .python-version tells Render to use it. The file name must start with a dot; downloading it can strip the dot, and Render then silently falls back to its default (3.14).
- Trades are judged on best-eleven strength (same as League squads), so bench players only matter if they'd start. Simple, and it matches how the league table is won.
- Trades must swap the same positions on both sides, because Draft squads always stay 2 GKP, 5 DEF, 5 MID, 3 FWD. Confirmed on the Draft site (it doesn't allow a MID for a DEF). Uneven-looking deals still work as long as positions match, e.g. MID + FWD for FWD + MID, where the weaker player is a filler.
- requirements.txt = what the app needs to run (what Render installs). requirements-dev.txt = that plus pytest and ruff (for my computer and CI).

## Roadmap
1. Test with real league (done)
2. Deploy to Render (done)
3. Personal view: enter team ID → auto-find league, bookmarkable link (done)
4. Trade analyzer (done)
5. "Why this pick?" explanations: short, from the numbers (done). AI long version dropped
6. Season view (now "Until the break"): a longer-term rating alongside Next 5, so players like Porro (poor form, but an attacking full back) aren't dropped too early (built; tune weights against the real league next)
7. One AI feature where AI writes words and Python does the maths (ideas: trade pitch message, weekly league recap)
8. Accounts/login (needed for watchlists + limiting AI usage)

Idea for later: trade finder. "I want Joao Pedro and I'll give Fernandes": suggest which of their players makes the best filler, so the positions match, it helps me, and it still looks fair to them.

## Known issues / ideas
- The FPL Draft data feed isn't officially documented and could change.
- The Draft site sits behind Cloudflare and may block cloud servers (403). It worked from Render at deploy time, but Cloudflare can change its mind; if the live site starts showing the 403 message, that's the cause, not a bug in the app.
- Free Render plan sleeps after 15 minutes with no visitors; the next visit takes about a minute to wake it up (Render shows a loading page). The in-memory cache is lost when it sleeps (harmless). Fix: an uptime monitor pings /health every 10 minutes. One always-awake service uses about 720-744 of the workspace's 750 free hours a month, so only ever do this for one service. Render can still restart free services at any time.
- A league load is about 430 KB of JSON (measured with ~700 fake players, season view). Compressing responses (e.g. flask-compress) would shrink it several times over; worth doing if friends use it on phones.
- If several teams have a double gameweek (more than ~5% of regular players), the fixture scale lands on a double value and single-game teams still look weak on fixtures.
- Ratings are higher overall than before the percentile change, so MIN_GAIN = 3 may need tuning after watching real suggestions for a week or two.
- The backup for a doubtful claim is taken from the wire, so someone else may claim it first. Could also suggest a bench player as the fallback.
- Doubtful players below 75% (50%, 25%) are never suggested, even if they'd still rate higher.
- Trade analyzer ignores depth: bench players don't count, so injury cover has no value.
- Trade verdicts use short-term ratings (form, next 3 GWs), but a trade lasts all season. An injured star rates near 0 now even if he's back in two weeks. A longer fixture window just for trades could help.
- TRADE_MIN_GAIN and FAIR_MARGIN (both 0.5) are first guesses; tune after trying real trades.
- "Fair" only looks at their best eleven. Real managers also judge on names and total points (shown in the lists), so a fair verdict isn't a guaranteed yes.
- Season view: the return-date logic is built but not checked against real data. No player had a news_return value when it was built (the field was empty for everyone), so the date format is assumed to be ISO like the deadlines. A return date that has already passed while the player is still flagged injured would count him as fully available. Check once a real injury with a return date shows up.
- Season view: minutes share still counts games missed through injury, so a player coming back from a knock rates lower for a while (Porro: 198 of 450 minutes).
- Season view: with only ~5 gameweeks played, per-90 stats are noisy. Last season's data would help but needs one request per player.
- League race: lines are plain league points now, so tied managers overlap (a small vertical nudge by table position keeps them visible); the gap to the leader is in the caption and your end label.
- League race: classic-scoring leagues get no graphs (they have no matches; weekly points would need one history request per manager, /api/entry/<id>/history).
- League race: while a gameweek is being played it isn't finished, so the graphs only update once it ends.
- The league dropdown for multi-league teams shows "League <id>" for leagues that aren't loaded (names would cost an extra request each).

