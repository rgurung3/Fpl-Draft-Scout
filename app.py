"""
Draft Scout - a helper app for FPL Draft leagues.

Run:  python app.py   then open http://127.0.0.1:5000
"""
import time

import requests
from flask import Flask, jsonify, send_from_directory

DRAFT = "https://draft.premierleague.com/api"
CLASSIC = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (DraftScout personal tool)"}
CACHE_SECONDS = 600
LOOKAHEAD = 3  # how many upcoming gameweeks count toward fixture ease

app = Flask(__name__, static_folder="static")
_cache = {}


def get_json(url):
    """GET a URL with a small in-memory cache so we don't hammer the FPL servers."""
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    _cache[url] = (time.time(), data)
    return data


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- fixtures

def upcoming_fixtures(draft_teams, from_gw):
    """
    Returns {draft_team_id: [ {gw, opp, home, difficulty}, ... ]} for the next
    LOOKAHEAD gameweeks. Uses the classic FPL fixtures feed (it has difficulty
    ratings) and matches clubs by short name, so team ids never get crossed.
    """
    result = {t["id"]: [] for t in draft_teams}
    try:
        classic_teams = get_json(f"{CLASSIC}/bootstrap-static/")["teams"]
        fixtures = get_json(f"{CLASSIC}/fixtures/")
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return result  # fixture ease just drops out of the score if this fails

    by_short = {t["short_name"]: t["id"] for t in draft_teams}
    classic_to_draft = {t["id"]: by_short.get(t["short_name"]) for t in classic_teams}
    short_of = {t["id"]: t["short_name"] for t in draft_teams}
    last_gw = from_gw + LOOKAHEAD - 1

    for f in fixtures:
        gw = f.get("event")
        if not gw or gw < from_gw or gw > last_gw:
            continue
        h, a = classic_to_draft.get(f["team_h"]), classic_to_draft.get(f["team_a"])
        if not h or not a:
            continue
        result[h].append({"gw": gw, "opp": short_of[a], "home": True,
                          "difficulty": f.get("team_h_difficulty", 3)})
        result[a].append({"gw": gw, "opp": short_of[h], "home": False,
                          "difficulty": f.get("team_a_difficulty", 3)})
    for lst in result.values():
        lst.sort(key=lambda x: x["gw"])
    return result


def fixture_ease(fixtures):
    """Sum of (6 - difficulty) over the window. Doubles count twice, blanks count zero."""
    return sum(6 - f["difficulty"] for f in fixtures)


# ---------------------------------------------------------------- scoring

AVAIL_BY_STATUS = {"a": 1.0, "d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}

# weights per position: form, points-per-game, attacking threat, minutes, fixtures
WEIGHTS = {
    1: (0.30, 0.25, 0.00, 0.20, 0.25),  # GKP
    2: (0.30, 0.20, 0.10, 0.15, 0.25),  # DEF
    3: (0.30, 0.20, 0.20, 0.15, 0.15),  # MID
    4: (0.30, 0.20, 0.25, 0.10, 0.15),  # FWD
}


def availability(el):
    chance = el.get("chance_of_playing_next_round")
    if chance is not None:
        return num(chance, 100) / 100
    return AVAIL_BY_STATUS.get(el.get("status", "a"), 1.0)


def score_players(elements, fixtures_by_team, current_gw):
    raw = []
    games_so_far = max(current_gw, 1)
    for el in elements:
        mins = num(el.get("minutes"))
        xgi = num(el.get("expected_goal_involvements"))
        raw.append({
            "form": max(num(el.get("form")), 0),
            "ppg": max(num(el.get("points_per_game")), 0),
            "xgi90": (xgi / mins * 90) if mins >= 180 else 0.0,
            "mins": min(mins / (games_so_far * 90), 1.0),
            "fix": fixture_ease(fixtures_by_team.get(el["team"], [])),
        })

    # normalise each stat against the best in the game (top value -> 1.0)
    maxes = {k: max((r[k] for r in raw), default=0) or 1 for k in raw[0]} if raw else {}

    scores = {}
    for el, r in zip(elements, raw):
        w = WEIGHTS.get(el["element_type"], WEIGHTS[3])
        parts = [r["form"] / maxes["form"], r["ppg"] / maxes["ppg"],
                 r["xgi90"] / maxes["xgi90"], r["mins"], r["fix"] / maxes["fix"]]
        base = sum(wi * pi for wi, pi in zip(w, parts))
        scores[el["id"]] = {
            "score": round(100 * base * availability(el), 1),
            "xgi90": round(r["xgi90"], 2),
            "mins_share": round(r["mins"] * 100),
        }
    return scores


# ---------------------------------------------------------------- routes

def fpl_error_message(code):
    """Turn an error code from the FPL servers into advice a person can act on."""
    if code == 404:
        return "No Draft league found with that ID. Check the number in your league's URL."
    if code == 403:
        return ("The FPL Draft site refused the request (403). It sometimes blocks "
                "automated traffic for a while. Wait a few minutes and try again.")
    if code == 503:
        return ("The FPL site is updating, which happens around deadlines and after "
                "matches. Try again in a few minutes.")
    return f"The FPL Draft site returned an error ({code}). Try again shortly."


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/league/<int:league_id>")
def league(league_id):
    try:
        boot = get_json(f"{DRAFT}/bootstrap-static")
        details = get_json(f"{DRAFT}/league/{league_id}/details")
        status = get_json(f"{DRAFT}/league/{league_id}/element-status")
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        return jsonify({"error": fpl_error_message(code)}), 400 if code == 404 else 502
    except ValueError:
        # the site answered, but not with JSON (e.g. a maintenance page)
        return jsonify({"error": "The FPL Draft site sent back something unexpected. "
                                 "Try again in a few minutes."}), 502
    except requests.RequestException:
        return jsonify({"error": "Couldn't reach the FPL Draft site. Check your internet connection."}), 502

    events = boot.get("events", {})
    current_gw = events.get("current") or 1
    next_gw = events.get("next") or current_gw + 1

    teams = boot["teams"]
    team_short = {t["id"]: t["short_name"] for t in teams}
    positions = {p["id"]: p["singular_name_short"] for p in boot["element_types"]}
    elements = boot["elements"]

    fixtures = upcoming_fixtures(teams, next_gw)
    scores = score_players(elements, fixtures, current_gw)

    owner_of = {s["element"]: s.get("owner") for s in status.get("element_status", [])}

    players = []
    for el in elements:
        s = scores[el["id"]]
        players.append({
            "id": el["id"],
            "name": el.get("web_name"),
            "team": team_short.get(el["team"], "?"),
            "pos": positions.get(el["element_type"], "?"),
            "pos_id": el["element_type"],
            "owner": owner_of.get(el["id"]),
            "score": s["score"],
            "form": num(el.get("form")),
            "ppg": num(el.get("points_per_game")),
            "total": int(num(el.get("total_points"))),
            "xgi90": s["xgi90"],
            "mins_share": s["mins_share"],
            "status": el.get("status", "a"),
            "news": el.get("news") or "",
            "fixtures": fixtures.get(el["team"], []),
        })

    managers = [{
        "entry_id": e.get("entry_id"),
        "team_name": e.get("entry_name"),
        "manager": f'{e.get("player_first_name", "")} {e.get("player_last_name", "")}'.strip(),
    } for e in details.get("league_entries", []) if e.get("entry_id")]

    return jsonify({
        "league_name": details.get("league", {}).get("name", f"League {league_id}"),
        "current_gw": current_gw,
        "next_gw": next_gw,
        "lookahead": LOOKAHEAD,
        "managers": managers,
        "players": players,
    })


if __name__ == "__main__":
    app.run(debug=True)
