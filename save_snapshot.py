"""
Save a league's results, and how strong its squads are, so they outlive the season.

Run:  python save_snapshot.py 52607

The Draft site throws a league's results away when it renews for a new season, so this
writes them to history/<season>/league_<id>.json. It also writes down how strong every
squad is today, to history/<season>/strength_<id>.json: the Draft site only knows how
strong squads are right now, so the only way to see how that moves is to record it as
we go. Run it now and then, or let the GitHub Action (.github/workflows/snapshot.yml)
do it every day. Both files only change when there's something new, so running it
often is harmless.
"""
import sys

import app


def save_strength(league_id):
    """
    Record today's squad strengths. A failure here is only printed: the results above are the
    part that can't be rebuilt later, and they shouldn't go uncommitted because of this.
    """
    try:
        data = {view: app.load_league(league_id, view) for view in app.VIEWS}
        path = app.save_strength(league_id, app.strength_snapshot(data))
    except app.FplError as e:
        print(f"!! Couldn't save the squad strengths: {e.message}")
        return
    print(f"Saved {path}" if path else "Squad strengths haven't changed since the last snapshot.")


def main(league_id):
    details = app.fetch(f"{app.DRAFT}/league/{league_id}/details", app.LEAGUE_NOT_FOUND)
    path = app.save_snapshot(league_id, app.snapshot_league(details))
    print(f"Saved {path}" if path else "Nothing new to save.")
    save_strength(league_id)


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit("Usage: python save_snapshot.py <league id>")
    try:
        main(int(sys.argv[1]))
    except app.FplError as e:
        sys.exit(e.message)
