"""
Save a league's results so they outlive the season.

Run:  python save_snapshot.py 52607

The Draft site throws a league's results away when it renews for a new season, so this
writes them to history/<season>/league_<id>.json. Run it now and then, or let the GitHub
Action (.github/workflows/snapshot.yml) do it every day. It only writes when there are
more finished matches than the saved file already has, so running it often is harmless.
"""
import sys

import app


def main(league_id):
    details = app.fetch(f"{app.DRAFT}/league/{league_id}/details", app.LEAGUE_NOT_FOUND)
    path = app.save_snapshot(league_id, app.snapshot_league(details))
    print(f"Saved {path}" if path else "Nothing new to save.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit("Usage: python save_snapshot.py <league id>")
    try:
        main(int(sys.argv[1]))
    except app.FplError as e:
        sys.exit(e.message)
