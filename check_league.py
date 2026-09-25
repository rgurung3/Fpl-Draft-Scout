"""
Sanity-check Draft Scout against a real league.

Run:  python check_league.py 12345

It loads the league through the app exactly like the browser does, then
prints a short report. Lines starting with "!!" need a look.
"""
import sys
from collections import Counter

import app

SQUAD_SIZE = 15  # every Draft squad has 15 players once the draft is done


def check(data):
    """Return a list of (ok, message) pairs describing the league data."""
    results = []
    managers = {m["entry_id"]: m["team_name"] for m in data["managers"]}
    players = data["players"]

    results.append((bool(managers), f"{len(managers)} managers found"))
    results.append((len(players) > 500, f"{len(players)} players in the game"))

    # does every owned player belong to someone in this league?
    owned = Counter(p["owner"] for p in players if p["owner"] is not None)
    unknown = [o for o in owned if o not in managers]
    if unknown:
        results.append((False, f"owner ids that match no manager: {unknown}"))
    else:
        results.append((True, "every owned player belongs to a manager in the league"))

    for entry_id, name in managers.items():
        n = owned.get(entry_id, 0)
        results.append((n == SQUAD_SIZE, f"{name}: {n} players (expected {SQUAD_SIZE})"))

    # xGI: if nobody has any, the Draft feed is missing the stat
    with_xgi = sum(1 for p in players if p["xgi90"] > 0)
    results.append((with_xgi > 0, f"{with_xgi} players have xGI data"))

    # fixtures: every club should have a game unless it's a blank gameweek
    clubs = {p["team"] for p in players}
    with_fix = {p["team"] for p in players if p["fixtures"]}
    missing = sorted(clubs - with_fix)
    msg = f"{len(with_fix)} of {len(clubs)} clubs have upcoming fixtures"
    results.append((not missing, msg + (f" (missing: {', '.join(missing)})" if missing else "")))

    scores = [p["score"] for p in players]
    top = max(scores, default=0)
    results.append((0 < top <= 100, f"ratings run from {min(scores, default=0)} to {top}"))
    return results


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("Usage: python check_league.py <league id>")
        return 2

    res = app.app.test_client().get(f"/api/league/{sys.argv[1]}")
    data = res.get_json(silent=True)
    if res.status_code != 200 or not data:
        error = (data or {}).get("error", "the app crashed; the error details are printed above")
        print(f"!! Couldn't load the league: {error}")
        return 1

    last_gw = data["next_gw"] + data["lookahead"] - 1
    print(f"League: {data['league_name']} "
          f"(gameweek {data['current_gw']}, fixtures GW{data['next_gw']}-{last_gw})\n")

    problems = 0
    for ok, message in check(data):
        print(("   " if ok else "!! ") + message)
        problems += not ok

    if problems:
        print(f"\n{problems} thing(s) to look at. Paste this output into the chat.")
    else:
        print("\nAll checks passed.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
