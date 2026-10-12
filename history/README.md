# Saved seasons

Two kinds of file, both written by `save_snapshot.py` (the GitHub Action runs it daily).

- `<season>/league_<id>.json`: the league's finished matches. The Draft site deletes a league's
  results when it renews, so these files are the only copy of past seasons. Don't delete them.
- `<season>/strength_<id>.json`: how strong every squad was, one line per day (only days where
  something changed). The Draft site only knows how strong squads are right now, so this is the
  only record of how that moved. It can't be rebuilt later. Don't delete it either.
