# Draft Scout

A small helper app for FPL Draft leagues. Enter your team ID and it finds your
league, rates every player, and shows you:

- **Waiver targets**: free agents who rate higher than your weakest player in the same position, each with a one-line reason (for example "easier fixtures +14, better form +12. But Porro has more attacking threat −6"). Each swap also says if the player you'd drop starts for you, what it does to your best eleven, how many minutes both players have played lately, and warns when a player has few minutes so far. A "Keep" button protects a player from being suggested as a drop (it's saved in your link). Players with a 75% chance of playing are included, with a warning and a fully fit backup
- **Trade analyzer** (its own page, `/trade`): pick players from your squad and another manager's, and see how the trade changes both teams' best eleven, and whether it's a fair offer they might accept
- **League hub** (its own page, `/league?league=<league id>`: the one link to send to the whole league). The normal link is view-only: it opens on two choices, **Banter** and **Charts**, and loads only what a friend picks, with no link into the rest of Draft Scout. Add `&full=1` to get the full version for one friend: it also has a **My team** choice and a way back to the main page, and shows both links to copy. This is a convenience, not a lock (anyone who adds `&full=1` gets it). Add `&tab=banter` or `&tab=charts` to open straight on one. Old `/banter` and `/charts` links still work and redirect here. The address never has a team ID in it, so a link copied from the address bar is safe to send (your own team is highlighted from the team this browser last used). Banter: three cards for the coming gameweek (the hot match of the week, a "Battle for 3rd" and a "Wooden spoon watch", each a different match, so nobody is on two), and a **luck chart** (each manager's real league points against the points they'd have if they'd played everyone every week: bars right are lucky, left are unlucky) and facts to argue about (unluckiest and luckiest manager, harshest loss, winning and losing runs, biggest beating, closest match, lowest and best scores). Two facts show at first, with a button for the rest. Mild teasing, all worked out from the numbers
- **Free agents**: a sortable, filterable table of everyone unowned in your league, with both ratings (next 5 and until the break) and upcoming fixtures. It shows the top 15 at first, with a button for more
- **Charts** (in the league hub; head-to-head leagues only): the **league race** (each manager's league points climbing from 0 after every gameweek, plus a grid of weekly scores shaded against the league average with each week's W, D or L), a **head-to-head grid** (every manager's won-drawn-lost record against every other manager this season, with the points when you select a row), **points scored and conceded** (one dot per manager, so you can see who's strong and who's been lucky), and **weekly scores** (each manager's lowest, average and best week, so you can see who's steady and who's boom or bust)
- **Rivalries** (in the league hub): pick two managers and see every match between them, who won, the overall record and a bar chart of the margin in each meeting. Seasons are saved automatically from now on (see Saved seasons and Rivalries below), so next year this shows this season too
- **Weekly recap** (in the league hub; head-to-head leagues only): the gameweek in words, updated as the games finish. While a gameweek is on, it says who's ahead in each match, how many players each side still has to play, and how the side that's behind is placed ("wide open", "still alive", "a long shot", "needs a miracle" or "all but over"). Once the official results are in, the final recap picks out the talking points of the week (the biggest win, the closest match, the highest and lowest score), lists who moved up or down the table as bullet points, and then lists every result. Each result gets a tag where it earns one (**Upset**, **Stomping**, **Nail-biter**, **Draw**), the reason an upset counts, and a line on who made the difference (for example "Saka 14 and Salah 11 led Team A. Team B's best was Palmer on 6"). A new recap is written when a match day finishes (about 3 to 5 a gameweek, with a short one after the first games), and the scores behind it are shown underneath. Draft Scout works out every number itself; an AI only chooses the words, and without the AI the page shows a plain version of the same recap (see The weekly recap below)
- **Next 5 / Until the break**: a toggle that switches every rating on the page between the next 5 gameweeks and everything up to the next international break
- **League squads**: how every manager's best legal eleven (one keeper, 3-5 DEF, 2-5 MID, 1-3 FWD) stacks up, with your squad highlighted

## Run it

You need Python 3.10 or newer (the AI recap's library needs it).

```bash
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000

## Your team ID and personal link

On draft.premierleague.com, open your team's Points page. The number after
`/entry/` in the address bar is your team ID. Enter it in Draft Scout and it
looks up your league for you.

Once loaded, the address bar shows your personal link, for example
`http://127.0.0.1:5000/?team=276914`. Bookmark it and you'll land straight on
your own view. Everyone in the league can do the same with their own team ID.
There are no accounts: anyone with a link sees that team's view, which only
uses data the Draft site already makes public.

## Check it against your league

```bash
python check_league.py 12345          # by league ID
python check_league.py --team 276914  # by team ID, the way the browser does it
```

It loads the league through the app and prints a short report. Lines
starting with `!!` need a look: missing managers, owners that don't match
anyone in the league, your team not being in the league it found, squads
that aren't 15 players, missing xGI data, or clubs without fixtures.

At the end it lists the biggest risers and fallers between the "next 5"
and "until the break" ratings, e.g. `Pedro Porro (DEF, TOT): 34 -> 62 (+28)`.
Use it to check the season settings move the right players.

For head-to-head leagues it also checks the weekly results were found for
every manager and that the league points it works out match the official
table.

## Deploy to Render

Render runs the app on its servers so it has a public address. It deploys
straight from GitHub and redeploys every time you push to `main`.

1. Sign up at https://render.com with your GitHub account.
2. In the Render Dashboard click **New** > **Web Service** and pick this repo.
3. Fill in:
   - **Language**: Python 3
   - **Branch**: `main`
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn app:app --workers 1 --threads 4 --timeout 60`
   - **Instance type**: Free
4. Click **Deploy Web Service** and watch the log. When it says the service
   is live, open the `onrender.com` address at the top of the page.
5. First check: open `/api/team/<your team ID>` on the live address. JSON
   with your league means it works. An error mentioning 403 means the Draft
   site is blocking Render's servers.

Notes:

- Render reads the Python version from `.python-version` (3.12, the same as CI).
- `python app.py` is only for your own computer. On Render, gunicorn runs the
  app and debug mode stays off.
- Environment variables are optional. The AI-written recap needs two, added
  under **Environment** in Render, never in the code: `ANTHROPIC_API_KEY` (your
  key from the Anthropic console) and `RECAP_MODEL` (the name of the Claude
  model to use, copied from Anthropic's model list). Without them the Recap
  page still works, with a plain version of the recap.
- On the free plan the app sleeps after 15 minutes without visitors. The first
  visit after that takes about a minute while it wakes up (Render shows a
  loading page meanwhile). To keep it awake, see below.

### Keeping it awake on the free plan

Set up a free uptime monitor (for example UptimeRobot or cron-job.org) to
visit `https://<your app>.onrender.com/health` every 10 minutes. `/health`
just answers `ok`: it doesn't call the FPL servers and uses almost no
bandwidth. Use `/health`, not a page with league data, which is hundreds of
KB each time.

Only do this for one service. Render gives each workspace 750 free hours a
month, and one service that never sleeps uses about 720-744 of them. A
second always-awake service would run out mid-month and Render would
suspend both. The alternative is Render's cheapest paid instance, which
never sleeps and is also faster.

## How the trade analyzer works

Pick a manager, tick the players you'd give and the players you'd get, and
press **Analyze trade**. Both sides need the same positions (one DEF for one
DEF, say), because every Draft squad keeps 2 GKP, 5 DEF, 5 MID and 3 FWD.
The analyzer is its own page: press **Trade analyzer** under the league bar on
the main page. You can also open any team in League squads and press **Build a
trade with this team**, which opens the analyzer with that manager picked.

The analyzer works out both teams' best legal eleven before and after the
trade and compares their strength (the same number League squads shows). It
also shows where the change comes from: how many rating points each position
gains or loses in the best eleven. For example, giving a midfielder and a
forward for a better forward and a weaker midfielder might show `MID −25` and
`FWD +25`. If the weaker midfielder doesn't make your eleven, one of your own
players takes his place, and the breakdown and formation show that too.

Then it gives one of four verdicts:

- **Good for you and fair**: your team gets stronger and theirs doesn't lose much. Worth offering.
- **Good for you, but they lose out**: expect a no unless they badly need what you're offering.
- **Barely changes your team**: probably not worth the hassle.
- **Makes your team weaker.**

Bench players don't count toward strength, so swapping bench players shows as
"barely changes". Ratings look at recent form and the next few fixtures, so an
injured star rates low even if he's back soon. Use the verdict as a guide.

## How the rating works

Each player gets a 0-100 rating from recent form, points per game, attacking
threat (xGI per 90), share of minutes played, and fixture difficulty over the
next 3 gameweeks. Each stat is measured against the 95th percentile of
players with at least 180 minutes and capped there (players with fewer minutes count in proportion, not as zero), so one standout week
doesn't skew everyone else's rating. Weights differ by position (fixtures matter more for
keepers and defenders, xGI matters more for forwards). The total is then
scaled down if the player is flagged as doubtful or injured.

Because a rating is the sum of those pieces, the gain in a waiver swap splits
into piece-by-piece differences. The "Why" line under each swap names the
biggest ones in rating points, plus the biggest thing the player you'd drop
still does better.

### Until the break

The toggle at the top switches to a rating that looks ahead to the next
international break (the page shows the gameweeks, e.g. "GW6–10"). It's found
from the gameweek deadlines: a gap of more than 10 days between two deadlines
is a break. The window is always 3 to 10 gameweeks long. It's built the same
way as Next 5, with these differences:

- A 75% injury doubt counts as fully fit. 50% and below are handled as before.
- Three more stats count for the positions they matter to: clean-sheet chances
  (expected goals conceded per 90) for keepers and defenders, defensive actions
  (tackles, blocks, interceptions per 90) for defenders and midfielders, and
  creativity per 90 for defenders, so attacking full backs get credit.
- Recent form counts half as much (0.15 instead of 0.30), so fixtures and the underlying stats count for more.
- A player who is out but has a return date only loses the gameweeks of the window he'd miss, instead of rating 0 for all of it (this applies to Next 5 as well, so a one-match ban doesn't rate 0 over five gameweeks).

Your choice goes into your link (`&view=season`), so a bookmark remembers it.

You can tweak the weights in `WEIGHTS` and `SEASON_WEIGHTS`, the fixture windows in `LOOKAHEAD` and `SEASON_LOOKAHEAD`,
the scale in `SCALE_PERCENTILE` and `MIN_MINUTES`, and the trade verdict
thresholds in `TRADE_MIN_GAIN` and `FAIR_MARGIN`, all at the top of `app.py`.

## The weekly recap

The **Recap** choice in the league hub writes the gameweek up in words. Draft
Scout works out every fact itself, and an AI only chooses the words, so it
can't get a score wrong:

- While a gameweek is on, it adds up each manager's starting eleven from the
  Draft site's live points (before auto-subs, which the Draft site makes when
  the gameweek ends, so a live score is an estimate) and counts how many
  players each side still has to play. The side that's behind gets an outlook:
  "wide open", "still alive", "a long shot" or "needs a miracle" (a rough
  comeback chance from how many players are left and what they usually score),
  or "all but over" if they have nobody left to play.
- A new recap is written each time a match day finishes, so a gameweek gets
  about 3 to 5. After only a few games (a lone Friday game, say) it's a short
  early look of 2 or 3 sentences. When the official results are in there's a
  final recap: the talking points of the week (biggest win, closest match,
  highest and lowest score) in a few lines, the table moves as bullet points,
  then every result with the winner in bold. The written part doesn't repeat
  the results or the table moves, because the page lists them right below it.
  Tags: a win by 20 points or more is a **Stomping**, by 5 or fewer a
  **Nail-biter**. It's an **Upset** if the winner sat 3 or more table places
  below the loser going into the gameweek, or had a squad rated at least 2
  points weaker (the average rating of a manager's best eleven, as in League
  squads, using the squads as they are now). Either reason is enough, and the
  page lists which applied. The player line names the winner's top two scorers
  and the loser's best, plus a flop (a starter who played and scored 1 point or
  less). Players' own points are shown, never added up, because the official
  totals include auto-subs and bonus points.
  Until the first game of a gameweek has finished, the page keeps showing last
  week's final recap. The scoreboard shown while the games are on also names
  the best scorers so far.
- Each recap is written once and kept, so the first visitor after a stage
  changes waits a few seconds and everyone after that just reads it. They're
  kept in memory, so a restart writes the current one again.

The AI part is optional and needs two environment variables:
`ANTHROPIC_API_KEY` and `RECAP_MODEL` (the Claude model name, from Anthropic's
model list). On Render add both under **Environment**. On your own computer set
them in the terminal before `python app.py`, for example
`export ANTHROPIC_API_KEY=...`, and never put the key in the code. Without them,
or if the AI call fails, the page shows a plain version of the same recap, so
it never breaks. Only leagues listed in `RECAP_LEAGUES` (top of `app.py`) get
AI recaps, because each one costs a little (a cent or so); other leagues get
the plain version.

Settings at the top of `app.py`: `RECAP_LEAGUES`, `RECAP_MAX_LIVE_STAGES`,
`RECAP_SHORT_SHARE`, `RECAP_EFFORT`, `RECAP_TIMEOUT_SECONDS`,
`RECAP_RETRY_SECONDS`, `STAR_MIN_POINTS`, `MOVE_MIN_PLACES`, `UPSET_MIN_PLACES`,
`UPSET_MIN_STRENGTH`, `STOMPING_MARGIN`, `NAILBITER_MARGIN`, `FLOP_MAX_POINTS`,
`PLAYER_POINTS_SD` and `COMEBACK_LEVELS`.

## Saved seasons and Rivalries

The Draft site deletes a league's results when it renews for a new season, so
Draft Scout keeps its own copy in the `history/` folder:

```bash
python save_snapshot.py 52607   # saves history/<season>/league_52607.json
```

A GitHub Action (`.github/workflows/snapshot.yml`) does this every day and
commits the file if it changed, so you don't have to. The **Rivalries** choice
in the league hub reads those files plus this season's live results: pick two
managers and see every match between them, who won, and the overall record.
It only has seasons from the day saving started. Last season can't be
recovered from the API.

## Notes

- Data comes from the public FPL Draft and FPL endpoints. They aren't
  officially documented, so they can change without warning.
- Responses are cached for 10 minutes to keep requests light.
