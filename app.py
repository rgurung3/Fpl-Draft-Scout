"""
Draft Scout - a helper app for FPL Draft leagues.

Run:  python app.py   then open http://127.0.0.1:5000
"""
import re
import time
from collections import Counter
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, send_from_directory

DRAFT = "https://draft.premierleague.com/api"
CLASSIC = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (DraftScout personal tool)"}
CACHE_SECONDS = 600
LOOKAHEAD = 5         # how many upcoming gameweeks the "next 5" view looks at
# The "until the break" view runs to the next international break: a gap of more
# than BREAK_GAP_DAYS between two gameweek deadlines. The window is kept between
# BREAK_MIN_WEEKS and BREAK_MAX_WEEKS long, and is SEASON_LOOKAHEAD long if no break is found.
SEASON_LOOKAHEAD = 8
BREAK_GAP_DAYS = 10
BREAK_MIN_WEEKS = 3
BREAK_MAX_WEEKS = 10
LAST_GW = 38
MIN_MINUTES = 180       # per-90 stats count in full from this many minutes (in proportion below it)
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


# ---------------------------------------------------------------- windows

def parse_time(text):
    """An ISO date such as 2026-10-10T10:00:00Z as a datetime, or None if it's missing or unreadable."""
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


NEWS_RETURN = re.compile(r"(?:Expected back|Suspended until) (\d{1,2}) ([A-Z][a-z]{2})")


def return_date(el):
    """
    When an injured or suspended player is expected back, or None. The Draft feed's
    news_return field is empty, but the news text says "Expected back 10 Oct" or
    "Suspended until 17 Oct" (no year: it's the next such date after news_added).
    """
    back = parse_time(el.get("news_return"))
    if back:
        return back
    found = NEWS_RETURN.search(el.get("news") or "")
    if not found:
        return None
    added = parse_time(el.get("news_added")) or datetime.now(timezone.utc)
    try:
        back = datetime.strptime(f"{found[1]} {found[2]} {added.year}", "%d %b %Y")
        back = back.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return back if back >= added else back.replace(year=back.year + 1)


def gameweek_deadlines(events):
    """{gameweek: deadline datetime} from the Draft site's events. Unreadable deadlines are left out."""
    found = {e.get("id"): parse_time(e.get("deadline_time")) for e in events.get("data") or []}
    return {gw: when for gw, when in found.items() if gw and when}


def break_window(deadlines, next_gw):
    """
    How many gameweeks the "until the break" view covers, counting from next_gw.
    A gap of more than BREAK_GAP_DAYS between one gameweek's deadline and the
    next one's means an international break after that gameweek, so the window
    ends there. The length is kept between BREAK_MIN_WEEKS and BREAK_MAX_WEEKS,
    and never runs past the last gameweek. With no break in sight it is
    SEASON_LOOKAHEAD long.
    """
    weeks = SEASON_LOOKAHEAD
    for gw in range(next_gw, LAST_GW):
        if gw not in deadlines or gw + 1 not in deadlines:
            continue
        if (deadlines[gw + 1] - deadlines[gw]).days > BREAK_GAP_DAYS:
            weeks = gw - next_gw + 1
            break
    weeks = max(BREAK_MIN_WEEKS, min(weeks, BREAK_MAX_WEEKS))
    return max(1, min(weeks, LAST_GW - next_gw + 1))


def view_windows(events, next_gw):
    """How many gameweeks each view looks at: {"week": 5, "season": until the break}."""
    return {"week": LOOKAHEAD, "season": break_window(gameweek_deadlines(events), next_gw)}


# ---------------------------------------------------------------- fixtures

def upcoming_fixtures(draft_teams, from_gw, lookahead=LOOKAHEAD):
    """
    Returns {draft_team_id: [ {gw, opp, home, difficulty}, ... ]} for the next
    `lookahead` gameweeks. Uses the classic FPL fixtures feed (it has difficulty
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
    last_gw = from_gw + lookahead - 1

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

# The rating pieces and the stat each one is built from:
#   form, ppg        recent form and points per game
#   xgi              attacking threat: expected goal involvements per 90
#   mins             share of minutes played (already 0-1)
#   fix              fixture ease over the view's window
#   cs               clean-sheet chances: CS_BASELINE minus expected goals conceded per 90
#   dc               defensive contribution (tackles, blocks, interceptions...) per 90
#   crea             creativity per 90 (chance creation, crosses)
PIECE_STATS = {"form": "form", "ppg": "ppg", "xgi": "xgi90", "mins": "mins", "fix": "fix",
               "cs": "cs", "dc": "dc90", "crea": "crea90"}
PIECES = tuple(PIECE_STATS)
SCALED_STATS = ("form", "ppg", "xgi90", "fix", "cs", "dc90", "crea90")  # minutes share is already 0-1
CS_BASELINE = 2.0  # conceding this many expected goals per 90 counts as no clean-sheet chance at all

# next 5: weights per position. Each position's weights add up to 1.
WEIGHTS = {
    1: {"form": 0.30, "ppg": 0.25, "xgi": 0.00, "mins": 0.20, "fix": 0.25},  # GKP
    2: {"form": 0.30, "ppg": 0.20, "xgi": 0.10, "mins": 0.15, "fix": 0.25},  # DEF
    3: {"form": 0.30, "ppg": 0.20, "xgi": 0.20, "mins": 0.15, "fix": 0.15},  # MID
    4: {"form": 0.30, "ppg": 0.20, "xgi": 0.25, "mins": 0.10, "fix": 0.15},  # FWD
}

# until the break: recent form counts half as much (a few games of form say less
# about a longer stretch), so the underlying stats and the fixtures count more.
# New stats aimed at particular roles get a share too: clean-sheet chances for
# keepers and defenders, defensive actions for defenders and midfielders,
# creativity for defenders (attacking full backs).
SEASON_WEIGHTS = {
    1: {"form": 0.15, "ppg": 0.20, "mins": 0.20, "fix": 0.20, "cs": 0.25},                  # GKP
    2: {"form": 0.15, "ppg": 0.15, "xgi": 0.10, "mins": 0.10, "fix": 0.15,
        "cs": 0.15, "dc": 0.10, "crea": 0.10},                                             # DEF
    3: {"form": 0.15, "ppg": 0.20, "xgi": 0.25, "mins": 0.10, "fix": 0.20, "dc": 0.10},     # MID
    4: {"form": 0.15, "ppg": 0.20, "xgi": 0.30, "mins": 0.10, "fix": 0.25},                 # FWD
}
SEASON_FULL_FROM = 75  # until the break: players at least this likely to play count as fully fit

# Each view's settings. "return_dates": a player who is out now but has a known
# return date only loses the gameweeks of the window he'd miss (a one-match ban
# shouldn't rate 0 over five gameweeks).
VIEWS = {
    "week": {"weights": WEIGHTS, "full_from": None, "return_dates": True},
    "season": {"weights": SEASON_WEIGHTS, "full_from": SEASON_FULL_FROM, "return_dates": True},
}


def availability(el, full_from=None, window_deadlines=None):
    """
    How likely a player is to play, 0-1. If full_from is set (the until-the-break
    view), a chance of at least full_from% counts as fully fit; lower chances don't.
    If window_deadlines (the deadlines of the window's gameweeks) is given and the
    player is out (0) with a known return date, it's the share of those gameweeks
    that start on or after that date, instead of 0.
    """
    chance = el.get("chance_of_playing_next_round")
    if chance is not None:
        chance = num(chance, 100)
        value = 1.0 if full_from is not None and chance >= full_from else chance / 100
    else:
        value = AVAIL_BY_STATUS.get(el.get("status", "a"), 1.0)
    back = return_date(el) if value == 0 and window_deadlines else None
    if back:
        return sum(d >= back for d in window_deadlines) / len(window_deadlines)
    return value


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


def stat_scales(raw, minutes):
    """
    The number each stat gets divided by: its SCALE_PERCENTILE among regular
    players (MIN_MINUTES or more). Early in the season, before anyone has that
    many minutes, it falls back to everyone who has played, then to everyone.
    """
    regulars = [r for r, m in zip(raw, minutes) if m >= MIN_MINUTES]
    pool = regulars or [r for r, m in zip(raw, minutes) if m > 0] or raw
    return {k: percentile([r[k] for r in pool], SCALE_PERCENTILE) or 1 for k in SCALED_STATS}


def trust(mins):
    """How far to trust a player's per-90 stats: 1 from MIN_MINUTES, shrinking smoothly to 0 below it."""
    return min(max(mins, 0) / MIN_MINUTES, 1.0)


def per90(total, mins):
    """
    A season total per 90 minutes, shrunk in proportion to trust(mins). Under
    MIN_MINUTES the figure is noisy, so it counts for less rather than being
    thrown away (a cliff at 180 would give 0 to someone on 179).
    """
    return total / mins * 90 * trust(mins) if mins > 0 else 0.0


def score_players(elements, fixtures_by_team, current_gw, view="week", window_deadlines=None):
    """
    Rate every player 0-100 for one view ("week" or "season", see VIEWS).
    fixtures_by_team should cover that view's fixture window, and window_deadlines
    is the deadlines of that window's gameweeks (used for return dates).
    """
    settings = VIEWS[view]
    raw, minutes = [], []
    games_so_far = max(current_gw, 1)
    for el in elements:
        mins = num(el.get("minutes"))
        minutes.append(mins)
        xgc90 = num(el.get("expected_goals_conceded")) / mins * 90 if mins > 0 else 0.0
        raw.append({
            "form": max(num(el.get("form")), 0),
            "ppg": max(num(el.get("points_per_game")), 0),
            "xgi90": per90(num(el.get("expected_goal_involvements")), mins),
            "mins": min(mins / (games_so_far * 90), 1.0),
            "fix": fixture_ease(fixtures_by_team.get(el["team"], [])),
            "cs": max(CS_BASELINE - xgc90, 0) * trust(mins),
            "dc90": per90(num(el.get("defensive_contribution")), mins),
            "crea90": per90(num(el.get("creativity")), mins),
        })

    # measure each stat against the 95th percentile of regular players, capped at 1.0,
    # so one outlier (a hat-trick, a double gameweek) can't squash everyone else
    scale = stat_scales(raw, minutes)

    scores = {}
    for el, r in zip(elements, raw):
        weights = settings["weights"].get(el["element_type"], settings["weights"][3])
        values = {piece: r[stat] if stat == "mins" else min(r[stat] / scale[stat], 1.0)
                  for piece, stat in PIECE_STATS.items()}
        base = sum(w * values[piece] for piece, w in weights.items())
        avail = availability(el, settings["full_from"],
                             window_deadlines if settings["return_dates"] else None)
        # the rating split into rating points per piece, plus what an injury doubt
        # takes off (0 or less). The pieces add up to the rating, give or take rounding.
        breakdown = {piece: round(100 * w * values[piece], 1) for piece, w in weights.items()}
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
    "cs": "better clean-sheet chances",
    "dc": "more defensive actions",
    "crea": "more creativity",
}
MAX_REASONS = 3
MIN_REASON = 1.0  # rating points; smaller differences aren't worth mentioning


def swap_reasons(drop, claim):
    """
    Why a claim rates higher than a drop, in a few words. Each rating is the sum
    of its breakdown pieces, so the gain splits into piece-by-piece differences.

    availability: if the drop is injured, suspended or doubtful, what that is
    worth to the claim ({"text", "points"}), or None. The page shows it on its
    own line above the reasons, because it's about fitness, not performance.
    reasons: up to MAX_REASONS pieces in the claim's favour, biggest first.
    against: the biggest piece in the drop's favour, or None. Worth knowing:
    the drop might still be the better long-term player on that measure.
    """
    diffs = [(round(claim["breakdown"].get(k, 0) - drop["breakdown"].get(k, 0), 1), label)
             for k, label in REASON_LABELS.items()]

    # the drop's injury doubt counts in the claim's favour. (The claim's own
    # doubt isn't repeated here; it already gets its "!" warning.)
    availability = None
    doubt = round(claim["breakdown"]["avail"] - drop["breakdown"]["avail"], 1)
    if doubt > 0:
        status = "is out" if drop["chance"] == 0 else f"is {drop['chance']}% to play"
        availability = {"text": f"{drop['name']} {status}", "points": doubt}

    positives = sorted((d for d in diffs if d[0] > 0), reverse=True)
    reasons = [d for d in positives if d[0] >= MIN_REASON][:MAX_REASONS] or positives[:1]
    worst = min(diffs[:len(REASON_LABELS)])
    return {
        "availability": availability,
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


# ---------------------------------------------------------------- league history (head-to-head)

WIN_POINTS = 3   # head-to-head league points for a win
DRAW_POINTS = 1  # ... and for a draw


def league_history(details):
    """
    Week-by-week results for a head-to-head league, worked out from the league's
    finished matches (already in the league details, so no extra requests).
    Returns None if no gameweek has finished yet, or if the league isn't
    head-to-head (classic-scoring leagues have no matches).

    Matches name managers by league entry ID, which league_entries translates
    into the team IDs (entry_id) the rest of the app uses.

    gws: the finished gameweeks. average: the league's average score each week.
    managers, in table order, each with:
      points        their score each week (None if missing)
      results       "W", "D" or "L" each week (None if there was no opponent's score)
      league_points running total of league points after each week
      behind        league points behind the leader after each week
      scored        total points scored (the table's tie-breaker)
      table_total   the official table's total, so check_league.py can compare
    """
    entry_of = {e.get("id"): e["entry_id"] for e in details.get("league_entries", [])
                if e.get("entry_id")}
    finished = [m for m in details.get("matches", []) if m.get("finished") and m.get("event")]
    gws = sorted({m["event"] for m in finished})
    if not gws or not entry_of:
        return None

    weeks = {entry: {} for entry in entry_of.values()}  # team ID -> {gw: (points, result)}
    for m in finished:
        sides = ((m.get("league_entry_1"), m.get("league_entry_1_points")),
                 (m.get("league_entry_2"), m.get("league_entry_2_points")))
        for (mine, my_pts), (_other, their_pts) in (sides, sides[::-1]):
            entry = entry_of.get(mine)
            if entry is None or my_pts is None:
                continue
            result = (None if their_pts is None
                      else "W" if my_pts > their_pts else "L" if my_pts < their_pts else "D")
            weeks[entry][m["event"]] = (my_pts, result)

    table_total = {entry_of.get(row.get("league_entry")): row.get("total")
                   for row in details.get("standings", [])}

    managers = []
    for entry, played in weeks.items():
        points = [played.get(gw, (None, None))[0] for gw in gws]
        results = [played.get(gw, (None, None))[1] for gw in gws]
        running, league_points = 0, []
        for r in results:
            running += {"W": WIN_POINTS, "D": DRAW_POINTS}.get(r, 0)
            league_points.append(running)
        managers.append({"entry_id": entry, "points": points, "results": results,
                         "league_points": league_points, "scored": sum(p or 0 for p in points),
                         "table_total": table_total.get(entry)})

    leader = [max(m["league_points"][i] for m in managers) for i in range(len(gws))]
    for m in managers:
        m["behind"] = [top - lp for top, lp in zip(leader, m["league_points"])]
    managers.sort(key=lambda m: (m["league_points"][-1], m["scored"]), reverse=True)

    average = []
    for i in range(len(gws)):
        week = [m["points"][i] for m in managers if m["points"][i] is not None]
        average.append(round(sum(week) / len(week), 1) if week else None)
    return {"gws": gws, "average": average, "managers": managers}


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


def load_league(league_id, view="week"):
    """
    Fetch a league from the Draft site and build everything the page needs.
    Every player gets both ratings (week_score for "next 5", season_score for
    "until the break"); "score",
    "breakdown" and "fixtures" follow the chosen view, so waivers, squads and
    trades all use it.
    """
    view = view if view in VIEWS else "week"
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

    windows = view_windows(events, next_gw)
    deadlines = gameweek_deadlines(events)
    fixtures = {v: upcoming_fixtures(teams, next_gw, windows[v]) for v in VIEWS}
    ratings = {v: score_players(elements, fixtures[v], current_gw, v,
                                [deadlines[gw] for gw in range(next_gw, next_gw + windows[v])
                                 if gw in deadlines])
               for v in VIEWS}

    owner_of = {s["element"]: s.get("owner") for s in status.get("element_status", [])}

    players = []
    for el in elements:
        s = ratings[view][el["id"]]
        players.append({
            "id": el["id"],
            "name": el.get("web_name"),
            "team": team_short.get(el["team"], "?"),
            "pos": positions.get(el["element_type"], "?"),
            "pos_id": el["element_type"],
            "owner": owner_of.get(el["id"]),
            "score": s["score"],
            "week_score": ratings["week"][el["id"]]["score"],
            "season_score": ratings["season"][el["id"]]["score"],
            "form": num(el.get("form")),
            "ppg": num(el.get("points_per_game")),
            "total": int(num(el.get("total_points"))),
            "xgi90": s["xgi90"],
            "mins_share": s["mins_share"],
            "status": el.get("status", "a"),
            "chance": round(availability(el) * 100),  # the real chance for next week, in both views
            "news": el.get("news") or "",
            "fixtures": fixtures[view].get(el["team"], []),
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
        "view": view,
        "lookahead": windows[view],
        "windows": {v: {"from": next_gw, "to": next_gw + windows[v] - 1} for v in VIEWS},
        "managers": managers,
        "players": players,
        "history": league_history(details),
    }


def league_for_team(entry_id, wanted=None, view="week"):
    """
    Find the league a team plays in and load it. If the team is in more than one
    league, `wanted` picks one (it's ignored unless it's one of the team's leagues).
    `view` is "week" or "season" (see VIEWS).
    Returns (the league data, the team's league IDs).
    """
    entry = fetch(f"{DRAFT}/entry/{entry_id}/public", TEAM_NOT_FOUND).get("entry") or {}
    leagues = entry.get("league_set") or []
    if not leagues:
        raise FplError(NO_LEAGUE_YET, 400)
    return load_league(wanted if wanted in leagues else leagues[0], view), leagues


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/health")
def health():
    """
    A tiny page for an uptime monitor to visit every 10 minutes, so Render's free
    plan never puts the app to sleep. It doesn't call the FPL servers and sends
    back almost nothing, so pinging it uses hardly any bandwidth.
    """
    return "ok"


@app.route("/api/league/<int:league_id>")
def league(league_id):
    try:
        return jsonify(load_league(league_id, request.args.get("view", "week")))
    except FplError as e:
        return jsonify({"error": e.message}), e.status


@app.route("/api/team/<int:entry_id>")
def team(entry_id):
    """
    Look up which league a team plays in, then load that league with the team
    marked as "me". If the team is in more than one league, ?league=<id> picks one.
    ?view=season rates everyone for the rest of the season instead of this week.
    """
    try:
        data, leagues = league_for_team(entry_id, request.args.get("league", type=int),
                                        request.args.get("view", "week"))
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
    (with = the other manager's team ID, give/get = player IDs). ?league= and ?view= work as above.
    """
    try:
        them = request.args.get("with", type=int)
        give, get = id_list(request.args.get("give")), id_list(request.args.get("get"))
        if them is None:
            raise TradeError("Pick a manager to trade with.")
        data, _leagues = league_for_team(entry_id, request.args.get("league", type=int),
                                         request.args.get("view", "week"))
        if them != entry_id and them not in {m["entry_id"] for m in data["managers"]}:
            raise TradeError("That manager isn't in your league.")
        return jsonify(evaluate_trade(data["players"], entry_id, them, give, get))
    except (FplError, TradeError) as e:
        return jsonify({"error": e.message}), e.status


if __name__ == "__main__":
    app.run(debug=True)
