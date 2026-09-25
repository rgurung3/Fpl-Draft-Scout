# Draft Scout

A small helper app for FPL Draft leagues. It loads your league, rates every
player, and shows you:

- **Waiver targets**: free agents who rate higher than the weakest player you own in the same position
- **Free agents**: a sortable, filterable table of everyone unowned in your league, with upcoming fixtures
- **League squads**: how every manager's best eleven stacks up

## Run it

You need Python 3.9 or newer.

```bash
pip install -r requirements.txt
python app.py
```

## Check it against your league

```bash
python check_league.py 12345
```

Replace 12345 with your league ID. It loads the league the same way the
browser does and prints a short report. Lines starting with `!!` need a look:
missing managers, owners that don't match anyone in the league, squads that
aren't 15 players, missing xGI data, or clubs without fixtures.

## How the rating works

Each player gets a 0-100 rating from recent form, points per game, attacking
threat (xGI per 90), share of minutes played, and fixture difficulty over the
next 3 gameweeks. Weights differ by position (fixtures matter more for
keepers and defenders, xGI matters more for forwards). The total is then
scaled down if the player is flagged as doubtful or injured.

You can tweak the weights in `WEIGHTS` and the fixture window in `LOOKAHEAD`
at the top of `app.py`.

## Notes

- Data comes from the public FPL Draft and FPL endpoints. They aren't
  officially documented, so they can change without warning.
- Responses are cached for 10 minutes to keep requests light.
