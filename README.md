# Draft Scout

A small helper app for FPL Draft leagues. Enter your team ID and it finds your
league, rates every player, and shows you:

- **Waiver targets**: free agents who rate higher than your weakest player in the same position. Players with a 75% chance of playing are included, with a warning and a fully fit backup
- **Free agents**: a sortable, filterable table of everyone unowned in your league, with upcoming fixtures
- **League squads**: how every manager's best legal eleven (one keeper, 3-5 DEF, 2-5 MID, 1-3 FWD) stacks up, with your squad highlighted

## Run it

You need Python 3.9 or newer.

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

## How the rating works

Each player gets a 0-100 rating from recent form, points per game, attacking
threat (xGI per 90), share of minutes played, and fixture difficulty over the
next 3 gameweeks. Each stat is measured against the 95th percentile of
players with at least 180 minutes and capped there, so one standout week
doesn't skew everyone else's rating. Weights differ by position (fixtures matter more for
keepers and defenders, xGI matters more for forwards). The total is then
scaled down if the player is flagged as doubtful or injured.

You can tweak the weights in `WEIGHTS`, the fixture window in `LOOKAHEAD`,
and the scale in `SCALE_PERCENTILE` and `MIN_MINUTES` at the top of `app.py`.

## Notes

- Data comes from the public FPL Draft and FPL endpoints. They aren't
  officially documented, so they can change without warning.
- Responses are cached for 10 minutes to keep requests light.
