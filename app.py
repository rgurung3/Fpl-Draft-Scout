"""
Draft Scout - a helper app for FPL Draft leagues.

Run:  python app.py   then open http://127.0.0.1:5000
"""
import time
from collections import Counter

import requests
from flask import Flask, jsonify, request, send_from_directory

DRAFT = "https://draft.premierleague.com/api"
CLASSIC = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (DraftScout personal tool)"}
CACHE_SECONDS = 600
LOOKAHEAD = 3  # how many upcoming gameweeks count toward fixture ease
MIN_MINUTES = 180       # players need this many minutes for xGI/90 and to set the rating scale
SCALE_PERCENTILE = 95   # each stat is measured against this percentile of regular players

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


def percentile(values, pct):
    """
    The value that pct% of the list sits at or below, e.g. percentile(v, 95).
    Uses the same in-between method as a spreadsheet's PERCENTILE function.
    """
    if not values:
        return 0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


SCALED_STATS = ("form", "ppg", "xgi90", "fix")  # minutes share is already 0-1
PIECES = ("form", "ppg", "xgi", "mins", "fix")  # the five rating pieces, in the same order as WEIGHTS


def stat_scales(raw, minutes):
    """
    The number each stat gets divided by: its SCALE_PERCENTILE among regular
    players (MIN_MINUTES or more). Early in the season, before anyone has that
    many minutes, it falls back to everyone who has played, then to everyone.
    """
    regulars = [r for r, m in zip(raw, minutes) if m >= MIN_MINUTES]
    pool = regulars or [r for r, m in zip(raw, minutes) if m > 0] or raw
    return {k: percentile([r[k] for r in pool], SCALE_PERCENTILE) or 1 for k in SCALED_STATS}


def score_players(elements, fixtures_by_team, current_gw):
    raw, minutes = [], []
    games_so_far = max(current_gw, 1)
    for el in elements:
        mins = num(el.get("minutes"))
        xgi = num(el.get("expected_goal_involvements"))
        minutes.append(mins)
        raw.append({
            "form": max(num(el.get("form")), 0),
            "ppg": max(num(el.get("points_per_game")), 0),
            "xgi90": (xgi / mins * 90) if mins >= MIN_MINUTES else 0.0,
            "mins": min(mins / (games_so_far * 90), 1.0),
            "fix": fixture_ease(fixtures_by_team.get(el["team"], [])),
        })

    # measure each stat against the 95th percentile of regular players, capped at 1.0,
    # so one outlier (a hat-trick, a double gameweek) can't squash everyone else
    scale = stat_scales(raw, minutes)

    scores = {}
    for el, r in zip(elements, raw):
        w = WEIGHTS.get(el["element_type"], WEIGHTS[3])
        parts = [min(r["form"] / scale["form"], 1.0), min(r["ppg"] / scale["ppg"], 1.0),
                 min(r["xgi90"] / scale["xgi90"], 1.0), r["mins"],
                 min(r["fix"] / scale["fix"], 1.0)]
        base = sum(wi * pi for wi, pi in zip(w, parts))
        avail = availability(el)
        # the rating split into rating points per piece, plus what an injury doubt
        # takes off (0 or less). The pieces add up to the rating, give or take rounding.
        breakdown = {name: round(100 * wi * pi, 1) for name, wi, pi in zip(PIECES, w, parts)}
        breakdown["avail"] = round(100 * base * (avail - 1), 1)
        scores[el["id"]] = {
            "score": round(100 * base * avail, 1),
            "xgi90": round(r["xgi90"], 2),
            "mins_share": round(r["mins"] * 100),
            "breakdown": breakdown,
        }
    return scores


# ---------------------------------------------------------------- squads and waivers

# a legal Draft starting eleven: exactly 1 keeper, 3-5 DEF, 2-5 MID, 1-3 FWD
FORMATION = {"GKP": (1, 1), "DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}

MIN_CHANCE_FOR_WAIVERS = 75  # suggest doubtful players only if at least this likely to play
MIN_GAIN = 3                 # a swap has to be worth at least this many rating points
PAIRS_PER_POSITION = 2
MAX_TARGETS = 5


def by_score(players):
    return sorted(players, key=lambda p: p["score"], reverse=True)


def best_eleven(squad):
    """
    The highest-rated legal eleven from a squad. First take the minimum at each
    position (1 GKP, 3 DEF, 2 MID, 1 FWD), then fill the other 4 places with the
    best players left, without going over the maximum at any position.
    """
    by_pos = {pos: by_score(p for p in squad if p["pos"] == pos) for pos in FORMATION}
    xi = []
    for pos, (low, _high) in FORMATION.items():
        xi += by_pos[pos][:low]
    extras = by_score(p for pos, (low, high) in FORMATION.items()
                      for p in by_pos[pos][low:high])
    return xi + extras[:11 - len(xi)]


def squad_strength(players, entry_id):
    """Average rating of a manager's best legal eleven, plus who's in it and the shape."""
    xi = best_eleven([p for p in players if p["owner"] == entry_id])
    count = {pos: sum(p["pos"] == pos for p in xi) for pos in FORMATION}
    return {
        "strength": round(sum(p["score"] for p in xi) / len(xi), 1) if xi else 0,
        "best_xi": [p["id"] for p in xi],
        "formation": f'{count["DEF"]}-{count["MID"]}-{count["FWD"]}' if xi else "",
    }


REASON_LABELS = {  # what it means when the claim beats the drop on each piece
    "form": "better form",
    "ppg": "more points per game",
    "xgi": "more attacking threat",
    "mins": "more minutes",
    "fix": "easier fixtures",
}
MAX_REASONS = 3
MIN_REASON = 1.0  # rating points; smaller differences aren't worth mentioning


def swap_reasons(drop, claim):
    """
    Why a claim rates higher than a drop, in a few words. Each rating is the sum
    of its breakdown pieces, so the gain splits into piece-by-piece differences.

    reasons: up to MAX_REASONS pieces in the claim's favour, biggest first.
    against: the biggest piece in the drop's favour, or None. Worth knowing:
    the drop might still be the better long-term player on that measure.
    """
    diffs = [(round(claim["breakdown"][k] - drop["breakdown"][k], 1), label)
             for k, label in REASON_LABELS.items()]

    # the drop's injury doubt counts in the claim's favour. (The claim's own
    # doubt isn't repeated here; it already gets its "!" warning.)
    doubt = round(claim["breakdown"]["avail"] - drop["breakdown"]["avail"], 1)
    if doubt > 0:
        status = "is out" if drop["chance"] == 0 else f"is {drop['chance']}% to play"
        diffs.append((doubt, f"{drop['name']} {status}"))

    positives = sorted((d for d in diffs if d[0] > 0), reverse=True)
    reasons = [d for d in positives if d[0] >= MIN_REASON][:MAX_REASONS] or positives[:1]
    worst = min(diffs[:len(REASON_LABELS)])
    return {
        "reasons": [{"text": text, "points": pts} for pts, text in reasons],
        "against": ({"text": f"{drop['name']} has {worst[1]}", "points": worst[0]}
                    if worst[0] <= -MIN_REASON else None),
    }


def waiver_targets(players, me):
    """
    Suggested drop/claim swaps for one manager, best gain first.

    Per position, my weakest players are paired one-for-one with the best free
    agents who are at least MIN_CHANCE_FOR_WAIVERS% likely to play. A doubtful
    claim is still suggested (its rating is already scaled down for the risk),
    but gets a backup: the best fully fit free agent in the same position.
    """
    mine = [p for p in players if p["owner"] == me]
    free = [p for p in players if p["owner"] is None and p["chance"] >= MIN_CHANCE_FOR_WAIVERS]

    swaps = []
    for pos in FORMATION:
        weakest_first = by_score(p for p in mine if p["pos"] == pos)[::-1]
        best_first = by_score(p for p in free if p["pos"] == pos)
        for drop, claim in list(zip(weakest_first, best_first))[:PAIRS_PER_POSITION]:
            gain = round(claim["score"] - drop["score"], 1)
            if gain >= MIN_GAIN:
                swaps.append({"drop": drop, "claim": claim, "gain": gain})

    top = sorted(swaps, key=lambda s: s["gain"], reverse=True)[:MAX_TARGETS]
    claimed = {s["claim"]["id"] for s in top}

    targets = []
    for s in top:
        claim = s["claim"]
        backup = None
        if claim["chance"] < 100:
            fit = by_score(p for p in free if p["pos"] == claim["pos"]
                           and p["chance"] == 100 and p["id"] not in claimed)
            backup = fit[0]["id"] if fit else None
        targets.append({"drop": s["drop"]["id"], "claim": claim["id"],
                        "gain": s["gain"], "backup": backup,
                        "why": swap_reasons(s["drop"], claim)})
    return targets


# ---------------------------------------------------------------- trades

TRADE_MIN_GAIN = 0.5  # my best-eleven strength must rise this much for a trade to count as better
FAIR_MARGIN = 0.5     # a trade is fair if their best-eleven strength drops by no more than this

TRADE_MESSAGES = {
    "good_and_fair": "Good for you and fair: worth offering.",
    "good_but_unfair": ("Good for you, but their team gets weaker. Expect a no unless "
                        "they badly need what you're offering."),
    "no_change": "Barely changes your team. Probably not worth the hassle.",
    "worse": "Makes your team weaker.",
}


class TradeError(Exception):
    """A trade the Draft site wouldn't allow, with a message saying why."""

    status = 400

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def id_list(text):
    """Turn '12,34' from a link into [12, 34]. Anything that isn't a number is a TradeError."""
    parts = [s.strip() for s in (text or "").split(",") if s.strip()]
    if not all(s.isdigit() for s in parts):
        raise TradeError("Player IDs must be numbers, for example give=12,34.")
    return [int(s) for s in parts]


def describe_positions(count):
    """Counter({'MID': 2, 'DEF': 1}) -> '1 DEF, 2 MID' (in squad order)."""
    return ", ".join(f"{count[pos]} {pos}" for pos in FORMATION if count[pos])


def xi_by_position(players, entry_id):
    """Total rating of each position in a manager's best eleven, e.g. {"GKP": 60, "DEF": 210, ...}."""
    xi = best_eleven([p for p in players if p["owner"] == entry_id])
    return {pos: sum(p["score"] for p in xi if p["pos"] == pos) for pos in FORMATION}


def trade_side(before, after, entry_id):
    """
    How one manager's best eleven changes: strength, formation, who moves in or
    out, and by_position: how many rating points each position gains or loses
    in the best eleven (e.g. MID -25, FWD +25), so you can see where the change
    comes from.
    """
    old, new = squad_strength(before, entry_id), squad_strength(after, entry_id)
    old_pos, new_pos = xi_by_position(before, entry_id), xi_by_position(after, entry_id)
    return {
        "before": old["strength"],
        "after": new["strength"],
        "change": round(new["strength"] - old["strength"], 1),
        "formation_before": old["formation"],
        "formation_after": new["formation"],
        "joins_xi": [pid for pid in new["best_xi"] if pid not in old["best_xi"]],
        "leaves_xi": [pid for pid in old["best_xi"] if pid not in new["best_xi"]],
        "by_position": {pos: round(new_pos[pos] - old_pos[pos], 1) for pos in FORMATION},
    }


def evaluate_trade(players, me, them, give, get):
    """
    What a trade does to both teams: I give manager `them` the players in
    `give` (player IDs) and get the players in `get` back.

    First it checks the Draft rules: every player in give is mine, every player
    in get is theirs, and both sides have the same positions (squads always stay
    2 GKP, 5 DEF, 5 MID, 3 FWD). Then it swaps the owners on a copy of the
    players and compares each manager's best eleven before and after.
    """
    give, get = list(dict.fromkeys(give)), list(dict.fromkeys(get))  # drop repeats
    if me == them:
        raise TradeError("Pick another manager to trade with.")
    if not give or not get:
        raise TradeError("Pick at least one player on each side of the trade.")

    by_id = {p["id"]: p for p in players}
    for ids, owner, whose in ((give, me, "your"), (get, them, "their")):
        for pid in ids:
            p = by_id.get(pid)
            if p is None or p["owner"] != owner:
                name = (p or {}).get("name") or f"Player {pid}"
                raise TradeError(f"{name} isn't in {whose} squad. Reload the page and try again.")

    give_pos = Counter(by_id[pid]["pos"] for pid in give)
    get_pos = Counter(by_id[pid]["pos"] for pid in get)
    if give_pos != get_pos:
        raise TradeError("Both sides need the same positions, because every Draft squad keeps "
                         "2 GKP, 5 DEF, 5 MID and 3 FWD. You'd give "
                         f"{describe_positions(give_pos)} but get {describe_positions(get_pos)}.")

    # the "after" picture: a copy of the players with the traded ones' owners swapped.
    # The original list is never changed.
    new_owner = {pid: them for pid in give} | {pid: me for pid in get}
    after = [{**p, "owner": new_owner[p["id"]]} if p["id"] in new_owner else p for p in players]

    mine, theirs = trade_side(players, after, me), trade_side(players, after, them)
    if mine["change"] <= -TRADE_MIN_GAIN:
        verdict = "worse"
    elif mine["change"] < TRADE_MIN_GAIN:
        verdict = "no_change"
    elif theirs["change"] >= -FAIR_MARGIN:
        verdict = "good_and_fair"
    else:
        verdict = "good_but_unfair"
    return {"give": give, "get": get, "verdict": verdict, "message": TRADE_MESSAGES[verdict],
            "me": mine, "them": theirs}


# ---------------------------------------------------------------- talking to FPL

LEAGUE_NOT_FOUND = "No Draft league found with that ID. Check the number in your league's URL."
TEAM_NOT_FOUND = ("No Draft team found with that ID. Open your team's Points page on "
                  "draft.premierleague.com: your team ID is the number after /entry/.")
NO_LEAGUE_YET = "That team isn't in a Draft league yet. Join or create a league first."


class FplError(Exception):
    """A problem getting data from the FPL servers, with a message a person can act on."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


def fpl_error_message(code):
    """Turn an error code from the FPL servers into advice a person can act on."""
    if code == 403:
        return ("The FPL Draft site refused the request (403). It sometimes blocks "
                "automated traffic for a while. Wait a few minutes and try again.")
    if code == 503:
        return ("The FPL site is updating, which happens around deadlines and after "
                "matches. Try again in a few minutes.")
    return f"The FPL Draft site returned an error ({code}). Try again shortly."


def fetch(url, not_found):
    """
    get_json, but any failure becomes an FplError with a friendly message.
    not_found is the message to show if the site says the thing doesn't exist (404).
    """
    try:
        return get_json(url)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        if code == 404:
            raise FplError(not_found, 400) from e
        raise FplError(fpl_error_message(code)) from e
    except ValueError as e:
        # the site answered, but not with JSON (e.g. a maintenance page)
        raise FplError("The FPL Draft site sent back something unexpected. "
                       "Try again in a few minutes.") from e
    except requests.RequestException as e:
        raise FplError("Couldn't reach the FPL Draft site. "
                       "Check your internet connection.") from e


def load_league(league_id):
    """Fetch a league from the Draft site and build everything the page needs."""
    boot = fetch(f"{DRAFT}/bootstrap-static", LEAGUE_NOT_FOUND)
    details = fetch(f"{DRAFT}/league/{league_id}/details", LEAGUE_NOT_FOUND)
    status = fetch(f"{DRAFT}/league/{league_id}/element-status", LEAGUE_NOT_FOUND)

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
            "chance": round(availability(el) * 100),
            "news": el.get("news") or "",
            "fixtures": fixtures.get(el["team"], []),
            "breakdown": s["breakdown"],
        })

    managers = [{
        "entry_id": e.get("entry_id"),
        "team_name": e.get("entry_name"),
        "manager": f'{e.get("player_first_name", "")} {e.get("player_last_name", "")}'.strip(),
    } for e in details.get("league_entries", []) if e.get("entry_id")]
    for m in managers:
        m.update(squad_strength(players, m["entry_id"]))

    return {
        "league_id": league_id,
        "league_name": details.get("league", {}).get("name", f"League {league_id}"),
        "current_gw": current_gw,
        "next_gw": next_gw,
        "lookahead": LOOKAHEAD,
        "managers": managers,
        "players": players,
    }


def league_for_team(entry_id, wanted=None):
    """
    Find the league a team plays in and load it. If the team is in more than one
    league, `wanted` picks one (it's ignored unless it's one of the team's leagues).
    Returns (the league data, the team's league IDs).
    """
    entry = fetch(f"{DRAFT}/entry/{entry_id}/public", TEAM_NOT_FOUND).get("entry") or {}
    leagues = entry.get("league_set") or []
    if not leagues:
        raise FplError(NO_LEAGUE_YET, 400)
    return load_league(wanted if wanted in leagues else leagues[0]), leagues


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/league/<int:league_id>")
def league(league_id):
    try:
        return jsonify(load_league(league_id))
    except FplError as e:
        return jsonify({"error": e.message}), e.status


@app.route("/api/team/<int:entry_id>")
def team(entry_id):
    """
    Look up which league a team plays in, then load that league with the team
    marked as "me". If the team is in more than one league, ?league=<id> picks one.
    """
    try:
        data, leagues = league_for_team(entry_id, request.args.get("league", type=int))
    except FplError as e:
        return jsonify({"error": e.message}), e.status

    data["me"] = entry_id
    data["my_leagues"] = leagues
    data["waiver_targets"] = waiver_targets(data["players"], entry_id)
    return jsonify(data)


@app.route("/api/team/<int:entry_id>/trade")
def trade(entry_id):
    """
    Analyze a trade, e.g. /api/team/276914/trade?with=12345&give=301,302&get=415,420
    (with = the other manager's team ID, give/get = player IDs). ?league= works as above.
    """
    try:
        them = request.args.get("with", type=int)
        give, get = id_list(request.args.get("give")), id_list(request.args.get("get"))
        if them is None:
            raise TradeError("Pick a manager to trade with.")
        data, _leagues = league_for_team(entry_id, request.args.get("league", type=int))
        if them != entry_id and them not in {m["entry_id"] for m in data["managers"]}:
            raise TradeError("That manager isn't in your league.")
        return jsonify(evaluate_trade(data["players"], entry_id, them, give, get))
    except (FplError, TradeError) as e:
        return jsonify({"error": e.message}), e.status


if __name__ == "__main__":
    app.run(debug=True)
