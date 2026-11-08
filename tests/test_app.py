"""
Tests for Draft Scout. They use fake data, so they never call the real
FPL servers and always give the same result.

Run with:  pytest -v
"""
import json
import re
from datetime import date, datetime, timedelta, timezone

import anthropic
import pytest
import requests

import app
import check_league
import save_snapshot

# ---------------------------------------------------------------- fake data

def make_player(pid, team=1, pos=3, form="5.0", ppg="5.0", minutes=540,
                xgi="2.0", status="a", chance=None, creativity="20.0", xgc="6.0", dc=30):
    return {
        "id": pid, "code": 1000 + pid, "web_name": f"Player{pid}", "team": team, "element_type": pos,
        "form": form, "points_per_game": ppg, "total_points": 30,
        "minutes": minutes, "expected_goal_involvements": xgi,
        "status": status, "news": "", "chance_of_playing_next_round": chance,
        "creativity": creativity, "expected_goals_conceded": xgc, "defensive_contribution": dc,
    }


TEAMS = [{"id": 1, "short_name": "ARS", "name": "Arsenal"},
         {"id": 2, "short_name": "CHE", "name": "Chelsea"}]


@pytest.fixture
def fake_api(monkeypatch):
    """Replace the real web requests with fake responses."""
    boot = {
        "events": {"current": 6, "next": 7},
        "teams": TEAMS,
        "element_types": [{"id": 1, "singular_name_short": "GKP"},
                          {"id": 2, "singular_name_short": "DEF"},
                          {"id": 3, "singular_name_short": "MID"},
                          {"id": 4, "singular_name_short": "FWD"}],
        "elements": [make_player(1, team=1), make_player(2, team=2, form="1.0"),
                     make_player(3, team=1, pos=4)],
    }
    details = {"league": {"name": "Test League"},
               "league_entries": [{"entry_id": 100, "entry_name": "My FC",
                                   "player_first_name": "Sam", "player_last_name": "Lee"}]}
    status = {"element_status": [{"element": 1, "owner": 100},
                                 {"element": 2, "owner": None},
                                 {"element": 3, "owner": None}]}
    fixtures = [{"event": 7, "team_h": 1, "team_a": 2,
                 "team_h_difficulty": 2, "team_a_difficulty": 4}]
    entry = {"entry": {"id": 100, "name": "My FC", "league_set": [123]}}

    def fake_get_json(url):
        if url.endswith("/entry/100/public"):
            return entry
        if "draft" in url and "bootstrap" in url:
            return boot
        if url.endswith("/details"):
            return details
        if url.endswith("/element-status"):
            return status
        if "element-summary" in url:
            if "fantasy" in url:      # the classic site: earlier seasons (these players are all new)
                return {"history_past": []}
            return {"history": [{"event": 4, "minutes": 26}, {"event": 5, "minutes": 71},
                                {"event": 5, "minutes": 10}]}
        if "bootstrap" in url:        # the classic site's list: its ids are the draft ids + 500
            return {"teams": TEAMS, "elements": [{"id": 500 + e["id"], "code": e["code"]}
                                                 for e in boot["elements"]]}
        if "fixtures" in url:
            return fixtures
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(app, "get_json", fake_get_json)
    return boot


# ---------------------------------------------------------------- small helpers

def test_num_handles_bad_values():
    assert app.num("3.5") == 3.5
    assert app.num(None) == 0.0
    assert app.num("abc", default=7) == 7


def test_fixture_ease_counts_doubles_and_blanks():
    assert app.fixture_ease([]) == 0                      # blank gameweek
    assert app.fixture_ease([{"difficulty": 2}]) == 4
    assert app.fixture_ease([{"difficulty": 2}, {"difficulty": 3}]) == 7  # double


@pytest.mark.parametrize("player,expected", [
    ({"status": "a"}, 1.0),
    ({"status": "i"}, 0.0),
    ({"status": "d", "chance_of_playing_next_round": 75}, 0.75),
])
def test_availability(player, expected):
    assert app.availability(player) == expected


# ---------------------------------------------------------------- scoring

def test_better_form_gives_higher_score():
    good = make_player(1, form="8.0")
    bad = make_player(2, form="1.0")
    scores = app.score_players([good, bad], {}, current_gw=6)
    assert scores[1]["score"] > scores[2]["score"]


def test_injured_player_scores_zero():
    hurt = make_player(1, status="i")
    scores = app.score_players([hurt], {}, current_gw=6)
    assert scores[1]["score"] == 0


def test_scores_stay_between_0_and_100():
    players = [make_player(i, form=str(i), minutes=i * 100) for i in range(1, 10)]
    scores = app.score_players(players, {}, current_gw=6)
    assert all(0 <= s["score"] <= 100 for s in scores.values())


def test_low_minutes_players_get_a_smaller_xgi_boost():
    cameo = make_player(1, minutes=45, xgi="1.0")          # 2.0 per 90, but only a quarter trusted
    scores = app.score_players([cameo], {}, current_gw=6)
    assert scores[1]["xgi90"] == pytest.approx(0.5)


def test_per_90_stats_have_no_cliff_at_180_minutes():
    # same rate per 90 (xGI 0.333), different minutes
    players = regulars() + [make_player(90, minutes=90, xgi="0.333"),
                            make_player(179, minutes=179, xgi="0.662"),
                            make_player(180, minutes=180, xgi="0.666")]
    xgi = {i: app.score_players(players, {}, current_gw=6)[i]["breakdown"]["xgi"] for i in (90, 179, 180)}
    assert xgi[179] == pytest.approx(xgi[180], rel=0.01)    # one minute short: hardly any difference
    assert xgi[90] == pytest.approx(xgi[180] / 2, rel=0.02)  # half the minutes: half the trust
    assert xgi[90] > 0


def test_breakdown_adds_up_to_the_rating():
    players = regulars() + [make_player(99, form="8.0", status="d", chance=75)]
    scores = app.score_players(players, {}, current_gw=6)
    for s in scores.values():
        assert sum(s["breakdown"].values()) == pytest.approx(s["score"], abs=0.5)
    assert scores[1]["breakdown"]["avail"] == 0           # fully fit: nothing taken off
    assert scores[99]["breakdown"]["avail"] < 0           # 75% to play: a quarter taken off


# ---------------------------------------------------------------- rating scale (95th percentile)

def test_percentile():
    assert app.percentile([], 95) == 0
    assert app.percentile([7], 95) == 7
    assert app.percentile([0, 10], 50) == 5
    assert app.percentile(list(range(1, 101)), 95) == pytest.approx(95.05)  # like a spreadsheet


def regulars(n=20, form="5.0"):
    """n ordinary players who have all played every minute of 6 gameweeks."""
    return [make_player(i, form=form, minutes=540) for i in range(1, n + 1)]


def test_one_outlier_does_not_drag_everyone_down():
    without = app.score_players(regulars(), {}, current_gw=6)
    with_star = app.score_players(regulars() + [make_player(99, form="50.0", minutes=540)],
                                  {}, current_gw=6)
    assert with_star[1]["score"] == pytest.approx(without[1]["score"])


def test_low_minutes_players_do_not_set_the_scale():
    cameos = [make_player(90 + i, form="20.0", minutes=45) for i in range(3)]
    without = app.score_players(regulars(), {}, current_gw=6)
    with_cameos = app.score_players(regulars() + cameos, {}, current_gw=6)
    assert with_cameos[1]["score"] == pytest.approx(without[1]["score"])


def test_stats_above_the_line_are_capped():
    players = regulars() + [make_player(99, form="50.0", minutes=540)]
    scores = app.score_players(players, {}, current_gw=6)
    # form is capped at 1.0, so the star rates the same as an ordinary player
    # whose form is already at the 95th percentile
    assert scores[99]["score"] == pytest.approx(scores[1]["score"])


def test_scale_works_before_anyone_has_180_minutes():
    first_week = [make_player(1, form="9.0", minutes=90), make_player(2, form="2.0", minutes=90),
                  make_player(3, form="0.0", minutes=0)]
    scores = app.score_players(first_week, {}, current_gw=1)
    assert scores[1]["score"] > scores[2]["score"] > 0


# ---------------------------------------------------------------- season view

def test_availability_in_the_season_view():
    assert app.availability({"chance_of_playing_next_round": 75}, full_from=75) == 1.0
    assert app.availability({"chance_of_playing_next_round": 50}, full_from=75) == 0.5
    assert app.availability({"chance_of_playing_next_round": 75}) == 0.75   # this week: unchanged


def test_season_view_treats_75_percent_as_fit():
    fit, doubt = make_player(1), make_player(2, status="d", chance=75)
    week = app.score_players([fit, doubt], {}, current_gw=6)
    season = app.score_players([fit, doubt], {}, current_gw=6, view="season")
    assert week[2]["score"] < week[1]["score"]
    assert season[2]["score"] == season[1]["score"]


@pytest.mark.parametrize("status,chance,share", [("d", 50, 0.5), ("d", 25, 0.25),
                                                 ("i", 0, 0.0), ("i", None, 0.0)])
def test_season_view_keeps_bigger_doubts(status, chance, share):
    fit, hurt = make_player(1), make_player(2, status=status, chance=chance)
    season = app.score_players([fit, hurt], {}, current_gw=6, view="season")
    assert season[2]["score"] == pytest.approx(season[1]["score"] * share, abs=0.1)


def test_fixture_window_follows_the_length_given(monkeypatch):
    fixtures = [{"event": gw, "team_h": 1, "team_a": 2, "team_h_difficulty": 2,
                 "team_a_difficulty": 4} for gw in (7, 9, 11, 12, 13)]
    monkeypatch.setattr(app, "get_json", lambda url: {"teams": TEAMS} if "bootstrap" in url else fixtures)
    week = app.upcoming_fixtures(TEAMS, 7)                  # the next 5 is the default
    longer = app.upcoming_fixtures(TEAMS, 7, 7)
    assert app.LOOKAHEAD == 5
    assert [f["gw"] for f in week[1]] == [7, 9, 11]         # GW7-11
    assert [f["gw"] for f in longer[1]] == [7, 9, 11, 12, 13]   # GW7-13


# ---------------------------------------------------------------- the break window

def deadlines_with_gap(after, gap_days=21, first=1):
    """Weekly deadlines for GW1-38, with gap_days between the deadlines of GW `after` and `after + 1`."""
    start, out = app.parse_time("2026-08-15T10:00:00Z"), {}
    when = start
    for gw in range(first, 39):
        out[gw] = when
        when += timedelta(days=gap_days if gw == after else 7)
    return out


def test_window_ends_at_the_next_break():
    # the break comes after GW11, so from GW7 the window is GW7-11
    assert app.break_window(deadlines_with_gap(after=11), next_gw=7) == 5


def test_window_ignores_a_break_that_already_happened():
    assert app.break_window(deadlines_with_gap(after=6), next_gw=7) == app.SEASON_LOOKAHEAD


def test_window_is_never_shorter_than_the_minimum():
    assert app.break_window(deadlines_with_gap(after=7), next_gw=7) == app.BREAK_MIN_WEEKS


def test_window_is_never_longer_than_the_maximum():
    assert app.break_window(deadlines_with_gap(after=30), next_gw=7) == app.BREAK_MAX_WEEKS


def test_window_without_deadlines_uses_the_fallback():
    assert app.break_window({}, next_gw=7) == app.SEASON_LOOKAHEAD


def test_window_stops_at_the_last_gameweek():
    assert app.break_window({}, next_gw=36) == 3
    assert app.break_window({}, next_gw=38) == 1


def test_view_windows_reads_the_events():
    events = {"data": [{"id": gw, "deadline_time": d.strftime("%Y-%m-%dT%H:%M:%SZ")}
                       for gw, d in deadlines_with_gap(after=10).items()]}
    assert app.view_windows(events, 7) == {"week": 5, "season": 4}
    assert app.view_windows({}, 7) == {"week": 5, "season": app.SEASON_LOOKAHEAD}


@pytest.mark.parametrize("text", [None, "", "soon", 5])
def test_parse_time_gives_none_for_bad_dates(text):
    assert app.parse_time(text) is None


# ---------------------------------------------------------------- return dates

WINDOW = [app.parse_time(f"2026-10-{day}T10:00:00Z") for day in ("03", "10", "17", "24")]


def injured(back):
    return {"status": "i", "chance_of_playing_next_round": 0, "news_return": back}


def test_a_returning_player_only_loses_the_games_he_misses():
    # back on 12 October: he misses GW1-2 of the window and plays GW3-4
    assert app.availability(injured("2026-10-12T00:00:00Z"), 75, WINDOW) == 0.5
    assert app.availability(injured("2026-10-01T00:00:00Z"), 75, WINDOW) == 1.0   # back before it starts
    assert app.availability(injured("2026-12-01T00:00:00Z"), 75, WINDOW) == 0.0   # back after it ends


def test_return_dates_are_ignored_without_a_window_or_a_date():
    assert app.availability(injured("2026-10-12T00:00:00Z"), 75) == 0.0           # this view doesn't use them
    assert app.availability(injured(None), 75, WINDOW) == 0.0                     # no date known
    assert app.availability(injured("rubbish"), 75, WINDOW) == 0.0


@pytest.mark.parametrize("news,year,expected", [
    ("Hamstring injury - Expected back 10 Oct", 2026, "2026-10-10"),
    ("Suspended until 17 Oct", 2026, "2026-10-17"),
    ("Knee injury - Expected back 3 Jan", 2026, "2027-01-03"),      # a date before the news means next year
])
def test_return_date_is_read_from_the_news_text(news, year, expected):
    el = {"news": news, "news_added": f"{year}-09-20T10:00:00Z"}
    assert app.return_date(el).strftime("%Y-%m-%d") == expected


@pytest.mark.parametrize("news", ["Back injury - Unknown return date", "75% chance of playing", "", None])
def test_return_date_is_none_when_the_news_has_no_date(news):
    assert app.return_date({"news": news, "news_added": "2026-09-20T10:00:00Z"}) is None


def test_a_suspended_player_misses_only_the_ban():
    banned = {"status": "s", "chance_of_playing_next_round": 0, "news": "Suspended until 8 Oct",
              "news_added": "2026-10-01T10:00:00Z"}
    assert app.availability(banned, None, WINDOW) == 0.75      # misses the first of four gameweeks


def test_a_doubt_keeps_its_chance_even_with_a_return_date():
    doubt = {"status": "d", "chance_of_playing_next_round": 50, "news_return": "2026-10-12T00:00:00Z"}
    assert app.availability(doubt, 75, WINDOW) == 0.5


def test_both_views_use_return_dates():
    fit, hurt = make_player(1), make_player(2, status="i", chance=0)
    hurt["news_return"] = "2026-10-12T00:00:00Z"
    week = app.score_players([fit, hurt], {}, current_gw=6, window_deadlines=WINDOW)
    season = app.score_players([fit, hurt], {}, current_gw=6, view="season", window_deadlines=WINDOW)
    for scores in (week, season):
        assert scores[2]["score"] == pytest.approx(scores[1]["score"] * 0.5, abs=0.1)
        assert sum(scores[2]["breakdown"].values()) == pytest.approx(scores[2]["score"], abs=0.5)


def defenders(**changes):
    """20 ordinary defenders, plus player 99 who differs in the stats given."""
    return [make_player(i, pos=2) for i in range(1, 21)] + [make_player(99, pos=2, **changes)]


@pytest.mark.parametrize("changes,piece", [
    ({"creativity": "0.0"}, "crea"),    # creates nothing, unlike an attacking full back
    ({"xgc": "12.0"}, "cs"),            # concedes 2 expected goals per 90: no clean-sheet chance
    ({"dc": 0}, "dc"),                  # no tackles, blocks or interceptions
])
def test_season_view_uses_the_new_stats(changes, piece):
    # player 99 is weaker than the other defenders on one new stat. (Testing a
    # standout instead wouldn't work: with the 95th-percentile cap, ordinary
    # players here are already at the top of the scale.)
    players = defenders(**changes)
    week = app.score_players(players, {}, current_gw=6)
    season = app.score_players(players, {}, current_gw=6, view="season")
    assert week[99]["score"] == week[1]["score"]           # this week ignores these stats
    assert season[99]["score"] < season[1]["score"]
    assert season[99]["breakdown"][piece] < season[1]["breakdown"][piece]


def test_new_stats_count_in_proportion_under_180_minutes():
    # player 99 has the same rates per 90 as the regulars, but only 90 minutes
    players = defenders(minutes=90, creativity="3.33", xgc="1.0", dc=5)
    scores = app.score_players(players, {}, current_gw=6, view="season")
    for piece in ("crea", "cs", "dc"):
        cameo, regular = scores[99]["breakdown"][piece], scores[1]["breakdown"][piece]
        assert cameo == pytest.approx(regular / 2, abs=0.3)
        assert cameo > 0


def test_forwards_ignore_the_defensive_pieces():
    forwards = [make_player(i, pos=4, form=str(i), creativity=str(10 * i)) for i in range(1, 8)]
    season = app.score_players(forwards, {}, current_gw=6, view="season")
    for s in season.values():
        assert not {"cs", "dc", "crea"} & set(s["breakdown"])


def test_until_the_break_counts_form_less_than_next_5():
    assert all(app.SEASON_WEIGHTS[pos]["form"] < app.WEIGHTS[pos]["form"] for pos in app.WEIGHTS)


def test_season_breakdown_adds_up_to_the_rating():
    players = defenders(creativity="80.0") + [make_player(50, pos=1, status="d", chance=50)]
    scores = app.score_players(players, {}, current_gw=6, view="season")
    for s in scores.values():
        assert sum(s["breakdown"].values()) == pytest.approx(s["score"], abs=0.5)


@pytest.mark.parametrize("view", list(app.VIEWS))
def test_each_positions_weights_add_up_to_one(view):
    for weights in app.VIEWS[view]["weights"].values():
        assert sum(weights.values()) == pytest.approx(1.0)
        assert set(weights) <= set(app.PIECES)


def test_team_endpoint_season_view(fake_api):
    client = app.app.test_client()
    season = client.get("/api/team/100?view=season").get_json()
    assert season["view"] == "season"
    assert season["lookahead"] == app.SEASON_LOOKAHEAD     # the fake data has no deadlines
    assert season["windows"] == {"week": {"from": 7, "to": 11}, "season": {"from": 7, "to": 14}}
    assert all(p["score"] == p["season_score"] for p in season["players"])

    week = client.get("/api/team/100").get_json()
    assert week["view"] == "week"
    assert week["lookahead"] == app.LOOKAHEAD
    assert all(p["score"] == p["week_score"] for p in week["players"])


def test_unknown_view_falls_back_to_week(fake_api):
    data = app.app.test_client().get("/api/team/100?view=banana").get_json()
    assert data["view"] == "week"


# ---------------------------------------------------------------- league history (head-to-head)

def match(gw, a, a_pts, b, b_pts, finished=True):
    """One head-to-head match. a and b are league entry IDs (10-13 = teams 100-103 below)."""
    return {"event": gw, "finished": finished, "league_entry_1": a, "league_entry_1_points": a_pts,
            "league_entry_2": b, "league_entry_2_points": b_pts}


def h2h(matches, standings=()):
    """League details for a 4-team head-to-head league: league entry 10 is team 100, 11 is 101..."""
    return {"league_entries": [{"id": 10 + i, "entry_id": 100 + i} for i in range(4)],
            "matches": matches, "standings": list(standings)}


TWO_WEEKS = [match(1, 10, 60, 11, 40), match(1, 12, 50, 13, 50),      # 100 beats 101; a draw
             match(2, 10, 30, 12, 45), match(2, 11, 70, 13, 20),      # 102 beats 100; 101 beats 103
             match(3, 10, 0, 13, 0, finished=False)]                  # not played yet


def test_history_builds_the_league_race():
    history = app.league_history(h2h(TWO_WEEKS))
    assert history["gws"] == [1, 2]                          # the unfinished GW3 is left out
    by_team = {m["entry_id"]: m for m in history["managers"]}
    assert by_team[100]["points"] == [60, 30]
    assert by_team[100]["results"] == ["W", "L"]
    assert by_team[102]["results"] == ["D", "W"]
    assert by_team[102]["league_points"] == [1, 4]           # 1 for the draw, 3 for the win
    assert by_team[103]["behind"] == [2, 3]                  # leader had 3, then 4
    assert history["average"] == [50, pytest.approx(41.2, abs=0.1)]


def test_history_is_in_table_order():
    # 102 has 4 league points; 101 and 100 have 3 each, but 101 scored more (110 v 90)
    history = app.league_history(h2h(TWO_WEEKS))
    assert [m["entry_id"] for m in history["managers"]] == [102, 101, 100, 103]


def test_history_keeps_the_official_table_total():
    history = app.league_history(h2h(TWO_WEEKS, standings=[{"league_entry": 12, "total": 4}]))
    by_team = {m["entry_id"]: m for m in history["managers"]}
    assert by_team[102]["table_total"] == 4
    assert by_team[100]["table_total"] is None


def test_history_handles_a_missing_opponent():
    history = app.league_history(h2h([match(1, 10, 55, None, None)]))
    by_team = {m["entry_id"]: m for m in history["managers"]}
    assert by_team[100]["points"] == [55]
    assert by_team[100]["results"] == [None]                 # no score to compare against
    assert by_team[101]["points"] == [None]


@pytest.mark.parametrize("details", [
    h2h([match(1, 10, 0, 11, 0, finished=False)]),           # nothing finished yet
    {"league_entries": [{"id": 10, "entry_id": 100}]},       # classic scoring: no matches at all
])
def test_no_history_without_finished_matches(details):
    assert app.league_history(details) is None


def test_league_endpoint_includes_history(fake_api, monkeypatch):
    fake = app.get_json

    def with_matches(url):
        data = fake(url)
        if url.endswith("/details"):
            entries = [{**e, "id": 10} for e in data["league_entries"]]
            return {**data, "league_entries": entries, "matches": [match(1, 10, 64, None, 50)]}
        return data

    monkeypatch.setattr(app, "get_json", with_matches)
    history = app.app.test_client().get("/api/team/100").get_json()["history"]
    assert history["gws"] == [1]
    assert history["managers"][0] == {"entry_id": 100, "points": [64], "results": ["W"],
                                      "league_points": [3], "behind": [0], "scored": 64,
                                      "table_total": None}


def test_league_without_matches_has_no_history(fake_api):
    assert app.app.test_client().get("/api/league/123").get_json()["history"] is None


# ---------------------------------------------------------------- the web route

def test_league_endpoint_returns_players_and_owners(fake_api):
    client = app.app.test_client()
    res = client.get("/api/league/123")
    assert res.status_code == 200

    data = res.get_json()
    assert data["league_name"] == "Test League"
    assert data["managers"][0]["team_name"] == "My FC"

    owners = {p["id"]: p["owner"] for p in data["players"]}
    assert owners == {1: 100, 2: None, 3: None}


def test_league_endpoint_includes_fixtures(fake_api):
    data = app.app.test_client().get("/api/league/123").get_json()
    arsenal_player = next(p for p in data["players"] if p["team"] == "ARS")
    assert arsenal_player["fixtures"][0]["opp"] == "CHE"
    assert arsenal_player["fixtures"][0]["home"] is True


@pytest.mark.parametrize("code,status,text", [
    (404, 400, "No Draft league found"),
    (403, 502, "refused the request"),
    (503, 502, "FPL site is updating"),
    (500, 502, "returned an error (500)"),
])
def test_fpl_errors_give_friendly_messages(monkeypatch, code, status, text):
    def failing(url):
        resp = requests.Response()
        resp.status_code = code
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(app, "get_json", failing)
    res = app.app.test_client().get("/api/league/999999")
    assert res.status_code == status
    assert text in res.get_json()["error"]


def test_non_json_reply_gives_friendly_message(monkeypatch):
    def not_json(url):
        raise ValueError("Expecting value")  # what r.json() raises on an HTML page

    monkeypatch.setattr(app, "get_json", not_json)
    res = app.app.test_client().get("/api/league/123")
    assert res.status_code == 502
    assert "something unexpected" in res.get_json()["error"]


def test_fixtures_feed_failure_does_not_break_the_page(fake_api, monkeypatch):
    real = app.get_json

    def classic_down(url):
        if "fantasy.premierleague.com" in url:
            raise requests.ConnectionError()
        return real(url)

    monkeypatch.setattr(app, "get_json", classic_down)
    res = app.app.test_client().get("/api/league/123")
    assert res.status_code == 200
    assert all(p["fixtures"] == [] for p in res.get_json()["players"])


# ---------------------------------------------------------------- personal view (team ID)

def test_team_endpoint_finds_the_league_and_marks_me(fake_api, monkeypatch):
    requested = []
    fake = app.get_json

    def spy(url):
        requested.append(url)
        return fake(url)

    monkeypatch.setattr(app, "get_json", spy)
    res = app.app.test_client().get("/api/team/100")
    assert res.status_code == 200

    data = res.get_json()
    assert data["me"] == 100
    assert data["league_id"] == 123
    assert data["league_name"] == "Test League"
    assert any(u.endswith("/league/123/details") for u in requested)


def test_team_in_two_leagues_can_pick_one(fake_api, monkeypatch):
    fake = app.get_json

    def two_leagues(url):
        if url.endswith("/entry/100/public"):
            return {"entry": {"id": 100, "league_set": [123, 456]}}
        return fake(url)

    monkeypatch.setattr(app, "get_json", two_leagues)
    client = app.app.test_client()

    first = client.get("/api/team/100").get_json()
    assert first["league_id"] == 123           # no choice given: first league
    assert first["my_leagues"] == [123, 456]

    chosen = client.get("/api/team/100?league=456").get_json()
    assert chosen["league_id"] == 456

    ignored = client.get("/api/team/100?league=999").get_json()
    assert ignored["league_id"] == 123         # not one of this team's leagues


def test_team_with_no_league_gets_a_friendly_message(fake_api, monkeypatch):
    fake = app.get_json

    def no_league(url):
        if url.endswith("/entry/100/public"):
            return {"entry": {"id": 100, "league_set": []}}
        return fake(url)

    monkeypatch.setattr(app, "get_json", no_league)
    res = app.app.test_client().get("/api/team/100")
    assert res.status_code == 400
    assert "isn't in a Draft league" in res.get_json()["error"]


def test_unknown_team_id_gets_a_friendly_message(monkeypatch):
    def not_found(url):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/team/999999")
    assert res.status_code == 400
    assert "No Draft team found" in res.get_json()["error"]


# ---------------------------------------------------------------- best eleven

def piece(**values):
    """A rating breakdown: every piece 0 except the ones given, e.g. piece(form=12, fix=8)."""
    return {k: values.get(k, 0) for k in (*app.PIECES, "avail")}


def rated(pid, pos, score, owner=None, chance=100, breakdown=None):
    """
    A player as the page sees it, with only the fields the squad logic uses.
    Unless a breakdown is given, the whole rating counts as form.
    """
    return {"id": pid, "name": f"P{pid}", "pos": pos, "score": score, "owner": owner,
            "chance": chance, "breakdown": breakdown or piece(form=score)}


def full_squad(owner=100):
    """15 players: 2 GKP, 5 DEF, 5 MID, 3 FWD, like a real Draft squad."""
    shape = ["GKP"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    return [rated(i, pos, 50, owner) for i, pos in enumerate(shape, 1)]


def positions(xi):
    return sorted(p["pos"] for p in xi)


def test_best_eleven_uses_only_one_keeper():
    squad = [rated(1, "GKP", 95), rated(2, "GKP", 90), rated(3, "GKP", 85)]
    squad += [rated(10 + i, pos, 40) for i, pos in enumerate(["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3)]
    xi = app.best_eleven(squad)
    assert len(xi) == 11
    assert [p["id"] for p in xi if p["pos"] == "GKP"] == [1]   # the best keeper only


def test_best_eleven_always_has_three_defenders():
    squad = full_squad()
    for p in squad:
        p["score"] = 10 if p["pos"] == "DEF" else 80   # defenders are all weak
    xi = app.best_eleven(squad)
    assert len(xi) == 11
    assert positions(xi).count("DEF") == 3


def test_best_eleven_never_plays_more_than_three_forwards():
    squad = full_squad() + [rated(20, "FWD", 50, 100), rated(21, "FWD", 50, 100)]
    for p in squad:
        if p["pos"] == "FWD":
            p["score"] = 99   # five brilliant forwards
    xi = app.best_eleven(squad)
    assert positions(xi).count("FWD") == 3


def test_best_eleven_with_an_unfinished_squad():
    xi = app.best_eleven([rated(1, "MID", 60), rated(2, "FWD", 70)])
    assert {p["id"] for p in xi} == {1, 2}
    assert app.best_eleven([]) == []


def test_squad_strength_reports_formation():
    squad = full_squad()
    for p in squad:
        p["score"] = {"MID": 90, "FWD": 70}.get(p["pos"], 50)
    result = app.squad_strength(squad, 100)
    assert result["formation"] == "3-5-2"
    assert len(result["best_xi"]) == 11
    assert result["strength"] == round((50 + 3 * 50 + 5 * 90 + 2 * 70) / 11, 1)


# ---------------------------------------------------------------- waiver targets

def test_waivers_suggest_a_clear_upgrade():
    players = [rated(1, "DEF", 40, owner=100), rated(2, "DEF", 60)]
    targets = app.waiver_targets(players, 100)
    assert [(t["drop"], t["claim"], t["gain"], t["backup"]) for t in targets] == [(1, 2, 20, None)]


def test_waivers_skip_small_gains():
    players = [rated(1, "DEF", 40, owner=100), rated(2, "DEF", 42)]
    assert app.waiver_targets(players, 100) == []


def test_doubtful_75_player_is_suggested_with_a_backup():
    players = [rated(1, "MID", 30, owner=100),
               rated(2, "MID", 70, chance=75),   # best option, but a doubt
               rated(3, "MID", 55)]              # fully fit, so the backup
    targets = app.waiver_targets(players, 100)
    assert targets[0]["claim"] == 2
    assert targets[0]["backup"] == 3


def test_backup_is_not_a_player_already_suggested():
    players = [rated(1, "MID", 30, owner=100), rated(2, "MID", 35, owner=100),
               rated(3, "MID", 70, chance=75), rated(4, "MID", 60), rated(5, "MID", 50)]
    targets = app.waiver_targets(players, 100)
    risky = next(t for t in targets if t["claim"] == 3)
    assert {t["claim"] for t in targets} == {3, 4}
    assert risky["backup"] == 5          # 4 is already a suggestion of its own


def test_risky_claim_without_a_fit_backup():
    players = [rated(1, "GKP", 20, owner=100), rated(2, "GKP", 60, chance=75)]
    assert app.waiver_targets(players, 100)[0]["backup"] is None


def test_players_under_75_percent_are_not_suggested():
    players = [rated(1, "FWD", 20, owner=100), rated(2, "FWD", 80, chance=50),
               rated(3, "FWD", 90, chance=0)]
    assert app.waiver_targets(players, 100) == []


def test_reasons_name_the_biggest_differences_first():
    drop = rated(1, "DEF", 30, owner=100, breakdown=piece(form=10, fix=5, xgi=15))
    claim = rated(2, "DEF", 60, breakdown=piece(form=22, fix=19, mins=10, xgi=9))
    why = app.swap_reasons(drop, claim)
    assert why["reasons"] == [{"text": "easier fixtures", "points": 14},
                              {"text": "better form", "points": 12},
                              {"text": "more minutes", "points": 10}]
    # the one thing the dropped player still does better
    assert why["against"] == {"text": "P1 has more attacking threat", "points": -6}


def test_small_differences_are_left_out():
    drop = rated(1, "MID", 20, owner=100, breakdown=piece(form=10, ppg=10))
    claim = rated(2, "MID", 24.5, breakdown=piece(form=14, ppg=10.5))
    why = app.swap_reasons(drop, claim)
    assert [r["text"] for r in why["reasons"]] == ["better form"]   # ppg +0.5 is too small
    assert why["against"] is None


def test_many_tiny_differences_still_give_one_reason():
    drop = rated(1, "MID", 20, owner=100, breakdown=piece())
    claim = rated(2, "MID", 3, breakdown=piece(form=0.8, ppg=0.7, xgi=0.6, mins=0.5, fix=0.4))
    assert [r["text"] for r in app.swap_reasons(drop, claim)["reasons"]] == ["better form"]


def test_reasons_use_the_season_pieces():
    drop = rated(1, "DEF", 30, owner=100, breakdown=piece(crea=2, cs=5))
    claim = rated(2, "DEF", 45, breakdown=piece(crea=12, cs=10))
    reasons = app.swap_reasons(drop, claim)["reasons"]
    assert [r["text"] for r in reasons] == ["more creativity", "better clean-sheet chances"]


@pytest.mark.parametrize("chance,text", [(50, "P1 is 50% to play"), (0, "P1 is out")])
def test_drop_injury_doubt_gets_its_own_line(chance, text):
    drop = rated(1, "DEF", 20, owner=100, chance=chance, breakdown=piece(form=40, avail=-20))
    claim = rated(2, "DEF", 40, breakdown=piece(form=40))
    why = app.swap_reasons(drop, claim)
    assert why["availability"] == {"text": text, "points": 20}
    assert why["reasons"] == []        # it isn't mixed in with the performance reasons


def test_no_availability_line_when_the_drop_is_fit():
    drop = rated(1, "DEF", 20, owner=100, breakdown=piece(form=20))
    claim = rated(2, "DEF", 40, breakdown=piece(form=40))
    assert app.swap_reasons(drop, claim)["availability"] is None


def test_small_samples_name_players_on_few_minutes():
    new = {**rated(1, "MID", 30), "minutes": 168, "starts": 2}
    regular = {**rated(2, "MID", 60), "minutes": 500, "starts": 5}
    assert app.small_samples([new, regular]) == [{"id": 1, "name": "P1", "minutes": 168, "starts": 2}]
    assert app.small_samples([rated(3, "MID", 50)]) == []      # no minutes known: nothing to say


def test_waiver_targets_flag_small_samples():
    drop = {**rated(1, "DEF", 40, owner=100, breakdown=piece(form=40)), "minutes": 168, "starts": 2}
    claim = {**rated(2, "DEF", 60, breakdown=piece(form=60)), "minutes": 236, "starts": 2}
    target = app.waiver_targets([drop, claim], me=100)[0]
    assert [p["id"] for p in target["small_sample"]] == [1, 2]


def test_players_carry_minutes_and_starts(fake_api):
    players = app.app.test_client().get("/api/team/100").get_json()["players"]
    assert all("minutes" in p and "starts" in p for p in players)


def test_kept_players_are_never_suggested_as_a_drop():
    mine = [rated(1, "MID", 20, owner=100), rated(2, "MID", 30, owner=100)]
    free = [rated(3, "MID", 60), rated(4, "MID", 55)]
    assert [t["drop"] for t in app.waiver_targets(mine + free, me=100)] == [1, 2]
    assert [t["drop"] for t in app.waiver_targets(mine + free, me=100, keep={1})] == [2]   # next weakest


def test_kept_ids_ignore_rubbish():
    assert app.kept_ids("12, 34,x,,5") == {12, 34, 5}
    assert app.kept_ids(None) == set()


def test_team_endpoint_keeps_only_my_players(fake_api):
    client = app.app.test_client()
    data = client.get("/api/team/100?keep=1,2,999").get_json()
    assert data["keep"] == [1]                      # 2 isn't mine, 999 doesn't exist
    assert all(t["drop"] != 1 for t in data["waiver_targets"])
    assert client.get("/api/team/100").get_json()["keep"] == []


def test_swap_says_whether_the_drop_starts_and_what_it_does_to_the_xi():
    squad = full_squad()
    squad[7]["score"] = 20                     # one midfielder is far weaker than the rest
    claim = rated(99, "MID", 60, breakdown=piece(form=60))
    target = app.waiver_targets(squad + [claim], me=100)[0]
    assert target["drop"] == 8 and target["claim"] == 99
    assert target["xi_before"] == pytest.approx(50, abs=0.1)
    assert target["xi_after"] > target["xi_before"]


def test_a_bench_swap_can_leave_the_best_eleven_unchanged():
    squad = full_squad()
    squad[11]["score"] = 10                    # the fifth midfielder, on the bench
    claim = rated(99, "MID", 40, breakdown=piece(form=40))   # better than him, but not a starter
    target = app.waiver_targets(squad + [claim], me=100)[0]
    assert target["drop"] == squad[11]["id"]
    assert target["drop_in_xi"] is False
    assert target["xi_after"] == target["xi_before"]


def test_strength_after_swap_leaves_the_real_list_alone():
    squad = full_squad()
    claim = rated(99, "MID", 90)
    app.strength_after_swap(squad + [claim], 100, squad[7], claim)
    assert squad[7]["owner"] == 100 and claim["owner"] is None


# ---------------------------------------------------------------- "worth doing" and "no move needed"

def test_a_swap_that_lifts_the_best_eleven_is_worth_doing():
    squad = full_squad()
    squad[7]["score"] = 20
    targets = app.waiver_targets(squad + [rated(99, "MID", 60)], me=100)
    assert [(t["claim"], t["worth"]) for t in targets] == [(99, True)]
    assert app.waiver_verdict(targets) == "move"


def test_a_bench_swap_is_never_worth_doing():
    squad = full_squad()
    squad[11]["score"] = 10                                    # the fifth midfielder, on the bench
    targets = app.waiver_targets(squad + [rated(99, "MID", 40)], me=100)
    assert [(t["claim"], t["worth"]) for t in targets] == [(99, False)]    # still listed, as a small upgrade
    assert app.waiver_verdict(targets) == "none"


def test_a_small_lift_is_not_worth_doing():
    squad = full_squad()
    squad[7]["score"] = 20
    # 54 replaces a 50 in the eleven: strength goes 50.0 -> 50.4, under WORTH_XI_GAIN (0.5)
    target = app.waiver_targets(squad + [rated(99, "MID", 54)], me=100)[0]
    assert app.xi_lift(target) == 0.4 and target["worth"] is False
    # 56 gets it over the line
    assert app.waiver_targets(squad + [rated(99, "MID", 56)], me=100)[0]["worth"] is True


def test_only_the_biggest_lifts_are_marked_worth_doing():
    squad = full_squad()
    for weak in (7, 12, 15):                                   # the weakest DEF, MID and FWD (all benched)
        squad[weak - 1]["score"] = 20
    claims = [rated(90, "MID", 99), rated(91, "DEF", 80), rated(92, "FWD", 70)]   # lifts 4.5, 2.7 and 1.8
    targets = app.waiver_targets(squad + claims, me=100)
    assert {t["claim"]: t["worth"] for t in targets} == {90: True, 91: True, 92: False}
    assert app.MAX_WORTH_DOING == 2


def test_swaps_worth_doing_come_first_then_by_gain():
    squad = full_squad()
    squad[11]["score"] = 5                                # a benched MID, claim 45: gain 40, never starts
    squad[6]["score"] = 42                                # a benched DEF, claim 75: gain 33, does start
    claims = [rated(90, "MID", 45), rated(91, "DEF", 75)]
    targets = app.waiver_targets(squad + claims, me=100)
    assert [(t["claim"], t["gain"], t["worth"]) for t in targets] == [(91, 33, True), (90, 40, False)]


def test_waiver_verdict_is_none_unless_a_swap_is_worth_doing():
    assert app.waiver_verdict([]) == "none"                    # no swaps at all
    assert app.waiver_verdict([{"worth": False}, {"worth": False}]) == "none"
    assert app.waiver_verdict([{"worth": False}, {"worth": True}]) == "move"


def test_team_endpoint_carries_the_verdict(fake_api):
    data = app.app.test_client().get("/api/team/100").get_json()
    assert all(isinstance(t["worth"], bool) for t in data["waiver_targets"])
    assert data["waiver_verdict"] == app.waiver_verdict(data["waiver_targets"])
    assert "waiver_verdict" not in app.app.test_client().get("/api/team/100?swaps=0").get_json()


def test_recent_minutes_cover_the_last_gameweeks(monkeypatch):
    history = {"history": [{"event": 4, "minutes": 26}, {"event": 5, "minutes": 71},
                           {"event": 5, "minutes": 10}]}     # GW5 was a double gameweek
    monkeypatch.setattr(app, "get_json", lambda url: history)
    recent = app.recent_minutes([7, 7, 8], current_gw=5)
    assert recent["gws"] == [2, 3, 4, 5]
    assert recent["minutes"] == {7: [0, 0, 26, 81], 8: [0, 0, 26, 81]}   # didn't play = 0, doubles add up


def test_recent_minutes_skip_players_that_fail(monkeypatch):
    def fake(url):
        if url.endswith("/7"):
            raise requests.ConnectionError("down")
        return {"history": [{"event": 1, "minutes": 90}]}
    monkeypatch.setattr(app, "get_json", fake)
    assert app.recent_minutes([7, 8], current_gw=1)["minutes"] == {8: [90]}


def test_team_endpoint_includes_recent_minutes(fake_api):
    data = app.app.test_client().get("/api/team/100").get_json()
    assert data["recent"]["gws"] == [3, 4, 5, 6]
    assert set(data["recent"]["minutes"]) <= {p["id"] for p in data["players"]}
    for t in data["waiver_targets"]:
        assert data["recent"]["minutes"][str(t["claim"])] == [0, 26, 81, 0]


# ---------------------------------------------------------------- reasons to hold a player

TODAY = datetime(2026, 10, 8, tzinfo=timezone.utc)                   # so "last season" is 2025/26
WILSON = {"season": "2025/26", "points": 168, "minutes": 2674}     # his real figures on the classic site


def holder(chance=0, minutes=145, total=3, pos="MID", news="Thigh injury - Unknown return date", pid=260):
    """A player as hold_case sees him, plus this season's figures. Default: injured Wilson."""
    return {**rated(pid, pos, 0, owner=100, chance=chance), "name": "Wilson",
            "minutes": minutes, "total": total, "news": news,
            "news_added": "2026-10-08T12:00:00Z"}     # so "back 4 Apr" means 2027, whatever today is


def weekly_deadlines(first_gw=7):
    """Deadlines for gameweeks first_gw to 38, a week apart, starting Saturday 10 Oct 2026 at 10:00."""
    start = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
    return {gw: start + timedelta(weeks=gw - first_gw) for gw in range(first_gw, 39)}


def injured_until(missed, deadlines):
    """News for a player whose return date makes him miss exactly `missed` gameweeks from GW7."""
    back = deadlines[6 + missed] + timedelta(days=1) if missed else deadlines[7] - timedelta(days=1)
    return f"Hamstring injury - Expected back {back.day} {back:%b}"


def test_season_names_follow_the_season_not_the_calendar_year():
    assert app.recent_season_names(TODAY) == ["2025/26", "2024/25"]
    assert app.recent_season_names(datetime(2027, 3, 1, tzinfo=timezone.utc)) == ["2025/26", "2024/25"]
    assert app.recent_season_names(datetime(2027, 8, 1, tzinfo=timezone.utc)) == ["2026/27", "2025/26"]


def test_an_injured_proven_scorer_with_no_return_date_is_your_call():
    note = app.hold_case(holder(), [WILSON], TODAY)
    assert note["kind"] == "call"                       # we can't tell how long he's out
    assert (note["season"], note["points"], note["minutes"], note["gws_out"]) == ("2025/26", 168, 2674, None)
    assert "Wilson scored 168 points last season (2,674 minutes, 5.7 per 90)." in note["text"]
    assert "There's no return date yet, so we can't tell how long he's out." in note["text"]
    assert "Press Keep to hold him." in note["text"]


def test_gameweeks_out_counts_the_deadlines_before_the_return_date():
    dl = weekly_deadlines()                              # GW7 is 10 Oct, then a week apart; 32 gameweeks left
    day = timedelta(days=1)
    assert app.gameweeks_out(dl[7] - day, dl, 7) == (0, 32)          # back before the next deadline
    assert app.gameweeks_out(dl[10] + day, dl, 7) == (4, 28)         # misses GW7-10
    assert app.gameweeks_out(dl[38] + day, dl, 7) == (32, 0)         # not back before the season ends
    assert app.gameweeks_out(dl[10] + day, dl, 9) == (2, 28)         # counted from the next gameweek on
    assert app.gameweeks_out(None, dl, 7) is None                    # no return date
    assert app.gameweeks_out(dl[10], {}, 7) is None                  # no deadlines
    assert app.gameweeks_out(dl[10], dl, None) is None


@pytest.mark.parametrize("missed,kind", [(0, "hold"), (1, "hold"), (4, "hold"), (5, "call"),
                                         (9, "call"), (10, "let_go"), (25, "let_go")])
def test_how_long_he_is_out_decides_the_advice(missed, kind):
    dl = weekly_deadlines()
    player = holder(news=injured_until(missed, dl))
    note = app.hold_case(player, [WILSON], TODAY, deadlines=dl, next_gw=7)
    assert (note["kind"], note["gws_out"]) == (kind, missed)


def test_a_short_absence_says_so_and_how_much_season_is_left():
    dl = weekly_deadlines()
    note = app.hold_case(holder(news=injured_until(4, dl)), [WILSON], TODAY, deadlines=dl, next_gw=7)
    assert "He's out for about 4 gameweeks (back around 1 Nov, 28 left after that)." in note["text"]
    assert "only a short while" in note["text"]
    assert note["text"].endswith("Press Keep to hold him.")


def test_one_gameweek_is_singular_and_a_return_before_the_deadline_says_so():
    dl = weekly_deadlines()
    one = app.hold_case(holder(news=injured_until(1, dl)), [WILSON], TODAY, deadlines=dl, next_gw=7)
    assert "He's out for about 1 gameweek (" in one["text"]
    none = app.hold_case(holder(news=injured_until(0, dl)), [WILSON], TODAY, deadlines=dl, next_gw=7)
    assert "in time for the next gameweek." in none["text"]


def test_a_long_absence_is_fine_to_let_go_but_keep_is_still_offered():
    dl = weekly_deadlines()
    note = app.hold_case(holder(news=injured_until(10, dl)), [WILSON], TODAY, deadlines=dl, next_gw=7)
    assert note["kind"] == "let_go"
    assert "so it's fine to let him go." in note["text"]
    assert note["text"].endswith("Press Keep if you'd rather hold him.")


def test_out_for_the_rest_of_the_season():
    dl = weekly_deadlines()
    note = app.hold_case(holder(news=injured_until(32, dl)), [WILSON], TODAY, deadlines=dl, next_gw=7)
    assert note["kind"] == "let_go" and "He's out for the rest of the season (back around " in note["text"]


def test_a_middle_length_absence_is_left_to_you():
    dl = weekly_deadlines()
    note = app.hold_case(holder(news=injured_until(7, dl)), [WILSON], TODAY, deadlines=dl, next_gw=7)
    assert note["kind"] == "call" and "it's your call." in note["text"]


def test_a_return_date_with_no_deadlines_is_given_but_not_judged():
    note = app.hold_case(holder(news="Hamstring injury - Expected back 10 Oct"), [WILSON], TODAY)
    assert note["kind"] == "call"
    assert "Expected back 10 Oct, but we can't work out how many gameweeks that is." in note["text"]


def test_season_deadlines_come_from_the_draft_events(monkeypatch):
    events = {"current": 6, "next": 7, "data": [{"id": 7, "deadline_time": "2026-10-10T10:00:00Z"},
                                                {"id": 8, "deadline_time": "2026-10-17T10:00:00Z"}]}
    monkeypatch.setattr(app, "get_json", lambda url: {"events": events})
    assert app.season_deadlines() == {7: datetime(2026, 10, 10, 10, tzinfo=timezone.utc),
                                      8: datetime(2026, 10, 17, 10, tzinfo=timezone.utc)}


def test_season_deadlines_are_empty_when_the_request_fails(monkeypatch):
    def down(url):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(app, "get_json", down)
    assert app.season_deadlines() == {}


@pytest.mark.parametrize("pos,bar", [("GKP", 135), ("DEF", 140), ("MID", 150), ("FWD", 130)])
def test_each_position_has_its_own_points_bar(pos, bar):
    def season(points):
        return [{"season": "2025/26", "points": points, "minutes": 2500}]

    assert app.hold_case(holder(pos=pos), season(bar), TODAY) is not None
    assert app.hold_case(holder(pos=pos), season(bar - 1), TODAY) is None


def test_a_season_needs_1500_minutes_to_count():
    short = [{"season": "2025/26", "points": 200, "minutes": 1499}]
    enough = [{"season": "2025/26", "points": 200, "minutes": 1500}]
    assert app.hold_case(holder(), short, TODAY) is None
    assert app.hold_case(holder(), enough, TODAY) is not None


def test_only_the_last_two_seasons_count():
    old = {"season": "2023/24", "points": 230, "minutes": 3000}
    assert app.hold_case(holder(), [old], TODAY) is None
    two_back = {"season": "2024/25", "points": 180, "minutes": 2600}
    note = app.hold_case(holder(), [old, two_back], TODAY)
    assert note["season"] == "2024/25" and "points in 2024/25 (" in note["text"]   # not "last season"


def test_the_best_qualifying_season_is_the_one_quoted():
    seasons = [{"season": "2024/25", "points": 190, "minutes": 2900}, WILSON]
    assert app.hold_case(holder(), seasons, TODAY)["points"] == 190


def test_a_fit_proven_player_who_is_playing_well_gets_no_note():
    assert app.hold_case(holder(chance=100, minutes=540, total=30), [WILSON], TODAY) is None   # 5.0 per 90


def test_a_slump_gets_a_note_even_when_fit():
    note = app.hold_case(holder(chance=100, minutes=540, total=10), [WILSON], TODAY)   # 1.7 per 90 vs 5.7
    assert "This season he's on 1.7 per 90." in note["text"]
    assert "return date" not in note["text"]           # he isn't out, so no return date to give


@pytest.mark.parametrize("chance,recent,flagged", [
    (0, None, True),               # out
    (50, [90, 90, 90, 90], True),  # 50% or less counts as out whatever he played
    (75, [90, 90, 90, 90], False), # a 75% flag on a man playing every week is not "out"
    (75, [90, 90, 90, 0], True),   # ... unless he missed the latest gameweek
    (75, None, False),             # no minutes to look at: only the bigger doubts count
    (100, [90, 90, 90, 0], False), # fit
])
def test_who_counts_as_out(chance, recent, flagged):
    player = holder(chance=chance, minutes=540, total=30)      # playing well, so no slump
    note = app.hold_case(player, [WILSON], TODAY, recent=recent)
    assert (note is not None) == flagged


def test_a_player_who_has_left_his_club_gets_no_note():
    gone = {**holder(news="Has joined Juventus on loan for the rest of the season"), "status": "u"}
    assert app.hold_case(gone, [WILSON], TODAY) is None
    assert app.hold_case({**gone, "status": "i"}, [WILSON], TODAY) is not None   # the same news, but injured


def test_a_doubt_with_no_return_date_is_a_short_absence():
    note = app.hold_case(holder(chance=75, news="Knock - 75% chance of playing"), [WILSON], TODAY,
                         recent=[90, 90, 90, 0])
    assert note["kind"] == "hold"
    assert "He's only a doubt (75% to play), so this may not last." in note["text"]


def test_too_few_minutes_this_season_is_not_called_a_slump():
    assert app.hold_case(holder(chance=100, minutes=100, total=0), [WILSON], TODAY) is None


def test_a_player_with_no_history_gets_no_worth_holding_note():
    assert app.hold_case(holder(), [], TODAY) is None
    assert app.hold_case(holder(), [{"season": "2016/17", "points": 0, "minutes": 0}], TODAY) is None


def test_new_signing_with_rising_minutes_is_flagged():
    barcola = {**rated(628, "MID", 30), "name": "Barcola"}
    note = app.new_signing(barcola, [], [0, 26, 71, 71])
    assert note["kind"] == "new"
    assert "New to the Premier League, and Barcola's minutes are going up (0, 26, 71, 71)." in note["text"]


def test_a_player_with_minutes_in_an_earlier_season_is_not_new():
    assert app.new_signing(rated(1, "MID", 30), [WILSON], [0, 26, 71, 71]) is None
    # a season on record with no minutes at all doesn't make him a regular
    assert app.new_signing(rated(1, "MID", 30), [{"season": "2016/17", "points": 0, "minutes": 0}],
                           [0, 26, 71, 71]) is not None


@pytest.mark.parametrize("recent", [[71, 71, 26, 0], [0, 71, 71, 0], [71, 71, 71, 71], [], [0, 0, 0, 0]])
def test_new_signing_needs_minutes_that_are_going_up(recent):
    assert app.new_signing(rated(1, "MID", 30), [], recent) is None


@pytest.mark.parametrize("recent,flagged", [([0, 26, 71, 71], True), ([0, 1, 90, 79], True),
                                            ([74, 90, 90, 90], False), ([76, 90, 90, 90], False),
                                            ([13, 90, 71, 80], False)])
def test_a_new_signing_who_has_settled_in_gets_no_note(recent, flagged):
    note = app.new_signing(rated(1, "MID", 30), [], recent)   # 80 minutes or more last gameweek = settled
    assert (note is not None) == flagged


def test_keep_notes_are_keyed_by_player_and_skip_players_without_history():
    wilson, nothing = holder(pid=1), rated(3, "DEF", 20)
    newcomer = {**rated(2, "MID", 30), "name": "Newcomer"}
    seasons = {1: [WILSON], 2: []}                         # 3's history couldn't be fetched
    notes = app.keep_notes([wilson, newcomer, nothing], seasons, {2: [0, 26, 71, 71], 3: [0, 90]}, TODAY)
    assert {k: v["kind"] for k, v in notes.items()} == {1: "call", 2: "new"}   # no return date for Wilson


def test_keep_notes_pass_the_schedule_and_minutes_on_to_hold_case():
    dl = weekly_deadlines()
    wilson = holder(pid=1, chance=75, news=injured_until(2, dl), minutes=540, total=30)
    played = app.keep_notes([wilson], {1: [WILSON]}, {1: [90, 90, 90, 90]}, TODAY, dl, 7)
    missed = app.keep_notes([wilson], {1: [WILSON]}, {1: [90, 90, 90, 0]}, TODAY, dl, 7)
    assert played == {}                                  # a 75% doubt who played last week isn't out
    assert (missed[1]["kind"], missed[1]["gws_out"]) == ("hold", 2)


def classic_site(rows_by_classic_id):
    """A fake get_json for the classic site: players 'code' 700 -> id 7, 800 -> id 8, 900 -> id 9."""
    def fake(url):
        if "bootstrap" in url:
            return {"elements": [{"id": 7, "code": 700}, {"id": 8, "code": 800}, {"id": 9, "code": 900}]}
        classic = int(url.rstrip("/").rsplit("/", 1)[1])
        if classic not in rows_by_classic_id:
            raise requests.ConnectionError("down")
        return {"history_past": rows_by_classic_id[classic]}
    return fake


def test_past_seasons_are_matched_by_code_not_by_id(monkeypatch):
    rows = [{"season_name": "2025/26", "total_points": 168, "minutes": 2674, "goals_scored": 10}]
    monkeypatch.setattr(app, "get_json", classic_site({7: rows}))
    wilson = {"id": 260, "code": 700}                    # draft id 260 means nothing on the classic site
    assert app.past_seasons([wilson]) == {260: [{"season": "2025/26", "points": 168, "minutes": 2674}]}


def test_past_seasons_leave_out_unknown_and_failed_players_but_keep_new_ones(monkeypatch):
    short_season = [{"season_name": "2025/26", "total_points": 5, "minutes": 90}]
    monkeypatch.setattr(app, "get_json", classic_site({7: [], 8: short_season}))
    players = [{"id": 1, "code": 700},       # new player: empty history is kept
               {"id": 2, "code": 12345},     # the classic site doesn't know him
               {"id": 3, "code": 900},       # his request fails
               {"id": 4},                    # no code at all
               {"id": 5, "code": 800}]
    assert app.past_seasons(players) == {1: [], 5: [{"season": "2025/26", "points": 5, "minutes": 90}]}


def test_past_seasons_survive_the_classic_list_failing(monkeypatch):
    def down(url):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(app, "get_json", down)
    assert app.past_seasons([{"id": 1, "code": 700}]) == {}


def test_players_carry_their_code(fake_api):
    players = app.app.test_client().get("/api/team/100").get_json()["players"]
    assert [p["code"] for p in players] == [1001, 1002, 1003]


def proven_injured_drop(fake_api, monkeypatch, news):
    """Make fake player 1 (my only player) injured with this news, and a proven scorer on the classic site."""
    fake_api["elements"][0].update(status="i", chance_of_playing_next_round=0, news=news,
                                   news_added="2026-10-08T12:00:00Z")
    last_season = app.recent_season_names()[0]
    real = app.get_json

    def with_history(url):
        if "fantasy" in url and url.rstrip("/").endswith("/501"):      # player 1 is classic id 501
            return {"history_past": [{"season_name": last_season, "total_points": 168, "minutes": 2674}]}
        return real(url)

    monkeypatch.setattr(app, "get_json", with_history)


def test_team_endpoint_adds_a_note_for_a_proven_injured_drop(fake_api, monkeypatch):
    proven_injured_drop(fake_api, monkeypatch, "Thigh injury - Unknown return date")
    data = app.app.test_client().get("/api/team/100").get_json()
    assert [t["drop"] for t in data["waiver_targets"]] == [1]
    assert data["keep_notes"]["1"]["kind"] == "call"             # no return date, so no way to tell how long


def test_team_endpoint_works_out_how_long_he_is_out_from_the_deadlines(fake_api, monkeypatch):
    dl = weekly_deadlines()
    fake_api["events"]["data"] = [{"id": gw, "deadline_time": when.strftime("%Y-%m-%dT%H:%M:%SZ")}
                                  for gw, when in dl.items()]
    proven_injured_drop(fake_api, monkeypatch, injured_until(3, dl))
    note = app.app.test_client().get("/api/team/100").get_json()["keep_notes"]["1"]
    assert (note["kind"], note["gws_out"]) == ("hold", 3)       # next gameweek is 7 in the fake data
    assert "29 left after that" in note["text"]


def test_team_endpoint_has_no_notes_when_there_is_nothing_to_say(fake_api):
    data = app.app.test_client().get("/api/team/100").get_json()
    assert data["keep_notes"] == {}


def test_swaps_off_skips_the_notes(fake_api):      # the trade page loads with ?swaps=0
    data = app.app.test_client().get("/api/team/100?swaps=0").get_json()
    assert "keep_notes" not in data


def test_waiver_targets_come_with_reasons():
    players = [rated(1, "DEF", 40, owner=100, breakdown=piece(form=20, fix=20)),
               rated(2, "DEF", 60, breakdown=piece(form=20, fix=40))]
    target = app.waiver_targets(players, 100)[0]
    assert target["why"]["reasons"] == [{"text": "easier fixtures", "points": 20}]


def test_team_endpoint_includes_targets_and_strength(fake_api):
    data = app.app.test_client().get("/api/team/100").get_json()
    assert "waiver_targets" in data
    me = data["managers"][0]
    assert me["best_xi"] == [1]          # the only player this team owns
    assert all("chance" in p and "breakdown" in p for p in data["players"])


def test_health_check_is_tiny_and_never_calls_fpl(monkeypatch):
    def no_fpl(url):
        raise AssertionError("the health check must not call the FPL servers")

    monkeypatch.setattr(app, "get_json", no_fpl)
    res = app.app.test_client().get("/health")
    assert res.status_code == 200
    assert res.data == b"ok"


def test_homepage_loads():
    res = app.app.test_client().get("/")
    assert res.status_code == 200
    assert b"Draft Scout" in res.data


# ---------------------------------------------------------------- league checker

def test_checker_accepts_matching_owners(fake_api):
    data = app.app.test_client().get("/api/league/123").get_json()
    results = {msg: ok for ok, msg in check_league.check(data)}
    assert results["every owned player belongs to a manager in the league"] is True


def test_checker_flags_owner_ids_that_match_no_manager(fake_api):
    data = app.app.test_client().get("/api/league/123").get_json()
    data["players"][0]["owner"] = 555  # nobody in the league has this id
    problems = [msg for ok, msg in check_league.check(data) if not ok]
    assert any("555" in msg for msg in problems)


def test_checker_confirms_my_team_is_in_the_league(fake_api):
    data = app.app.test_client().get("/api/team/100").get_json()
    results = {msg: ok for ok, msg in check_league.check(data)}
    assert results["your team (My FC) is one of this league's managers"] is True


def test_checker_lists_the_biggest_movers():
    players = [{"name": "Porro", "week_score": 34, "season_score": 62},
               {"name": "Riser2", "week_score": 50, "season_score": 55},
               {"name": "Faller", "week_score": 70, "season_score": 50},
               {"name": "Bench", "week_score": 10, "season_score": 30},    # under 40 in both
               {"name": "Steady", "week_score": 60, "season_score": 60}]
    risers, fallers = check_league.movers(players)
    assert [p["name"] for p in risers] == ["Porro", "Riser2"]
    assert [p["name"] for p in fallers] == ["Faller"]


def test_checker_prints_the_notes_on_dropped_players(capsys):
    names = {1: "Wilson", 2: "Barcola", 3: "Plain"}
    data = {"players": [{"id": i, "name": n} for i, n in names.items()],
            "waiver_targets": [{"drop": 1}, {"drop": 2}, {"drop": 3}],
            "keep_notes": {"1": {"kind": "hold", "text": "Wilson scored 168 points last season."}}}
    check_league.print_keep_notes(data)
    out = capsys.readouterr().out
    assert "Wilson: [hold] Wilson scored 168 points last season." in out
    assert "Barcola: no note" in out and "Plain: no note" in out


def test_checker_prints_nothing_for_a_league_only_load(capsys):
    check_league.print_keep_notes({"players": []})          # loaded by league ID: no swaps were worked out
    check_league.print_waiver_verdict({"players": []})
    assert capsys.readouterr().out == ""


def test_checker_prints_the_waiver_verdict(capsys):
    players = [{"id": 1, "name": "Weak"}, {"id": 2, "name": "Strong"}]
    swap = {"drop": 1, "claim": 2, "worth": True, "xi_before": 50.0, "xi_after": 51.0}

    def printed(verdict, targets):
        check_league.print_waiver_verdict({"players": players, "waiver_verdict": verdict,
                                           "waiver_targets": targets})
        return capsys.readouterr().out

    assert "worth doing: drop Weak, claim Strong (best eleven 50.0 -> 51.0, +1.0)" in printed("move", [swap])
    assert "no move needed (1 small upgrades, all optional)" in printed("none", [{**swap, "worth": False}])
    assert "no move needed (no upgrades on the wire)" in printed("none", [])


def history_data(table_total):
    return {"managers": [{"entry_id": 100, "team_name": "My FC"}], "players": [],
            "history": {"gws": [1, 2], "average": [50, 40],
                        "managers": [{"entry_id": 100, "points": [60, 30], "results": ["W", "W"],
                                      "league_points": [3, 6], "behind": [0, 0], "scored": 90,
                                      "table_total": table_total}]}}


def test_checker_confirms_league_points_match_the_table():
    results = {msg: ok for ok, msg in check_league.check(history_data(6))}
    assert results["league points match the official table"] is True
    assert results["weekly results found for 1 of 1 managers (GW1-2)"] is True


def test_checker_flags_league_points_that_differ_from_the_table():
    problems = [msg for ok, msg in check_league.check(history_data(9)) if not ok]
    assert any("My FC" in msg for msg in problems)


def test_checker_flags_my_team_missing_from_the_league(fake_api):
    data = app.app.test_client().get("/api/team/100").get_json()
    data["me"] = 777
    problems = [msg for ok, msg in check_league.check(data) if not ok]
    assert any("777" in msg for msg in problems)


# ---------------------------------------------------------------- trade analyzer

def trade_league():
    """
    Two full squads: mine (manager 100, players 1-15) and theirs (manager 200,
    players 101-115). Same shape as full_squad: 1-2 GKP, 3-7 DEF, 8-12 MID, 13-15 FWD.
    Everyone rates 50 until a test changes it.
    """
    return full_squad(100) + [dict(p, id=p["id"] + 100) for p in full_squad(200)]


def with_scores(players, changes):
    """Give some players a different rating, e.g. {8: 30, 108: 80}."""
    for p in players:
        p["score"] = changes.get(p["id"], p["score"])
    return players


def test_trade_good_for_both_sides_is_good_and_fair():
    # I have two great keepers but only one can play; they have two poor keepers.
    # I give my spare keeper and a midfielder for their keeper and their best midfielder.
    players = with_scores(trade_league(), {1: 80, 2: 80, 101: 30, 102: 30, 108: 80})
    result = app.evaluate_trade(players, 100, 200, give=[2, 9], get=[102, 108])
    assert result["me"]["change"] > 0
    assert result["them"]["change"] > 0          # my spare keeper walks into their eleven
    assert result["verdict"] == "good_and_fair"
    assert result["me"]["joins_xi"] == [108]
    assert result["me"]["leaves_xi"] == [9]


def test_trade_that_guts_their_team_is_flagged():
    players = with_scores(trade_league(), {8: 30, 108: 80})   # my bench MID for their star
    result = app.evaluate_trade(players, 100, 200, give=[8], get=[108])
    assert result["me"]["change"] >= app.TRADE_MIN_GAIN
    assert result["them"]["change"] < -app.FAIR_MARGIN
    assert result["verdict"] == "good_but_unfair"


def test_giving_away_my_star_makes_me_weaker():
    players = with_scores(trade_league(), {8: 80, 108: 30})
    result = app.evaluate_trade(players, 100, 200, give=[8], get=[108])
    assert result["verdict"] == "worse"


def test_bench_swap_barely_changes_either_team():
    # both second-choice keepers sit on the bench before and after
    players = with_scores(trade_league(), {1: 70, 2: 40, 101: 70, 102: 45})
    result = app.evaluate_trade(players, 100, 200, give=[2], get=[102])
    assert result["me"]["change"] == 0
    assert result["them"]["change"] == 0
    assert result["me"]["joins_xi"] == [] and result["me"]["leaves_xi"] == []
    assert result["verdict"] == "no_change"


def test_trade_reports_a_formation_change():
    changes = {pid: 70 for pid in range(8, 13)}            # my five midfielders are strong...
    changes.update({13: 40, 14: 40, 15: 40, 108: 30})       # ...my forwards are weak
    players = with_scores(trade_league(), changes)
    result = app.evaluate_trade(players, 100, 200, give=[12], get=[108])
    assert result["me"]["formation_before"] == "4-5-1"
    assert result["me"]["formation_after"] == "5-4-1"      # a defender takes the MID spot


def test_trade_shows_where_the_change_comes_from():
    # Fernandes (MID 80) and Delap (FWD 60) for Joao Pedro (FWD 85) and Anderson (MID 55):
    # midfield loses 25 points, attack gains 25, so overall it barely changes
    players = with_scores(trade_league(), {8: 80, 13: 60, 113: 85, 108: 55})
    result = app.evaluate_trade(players, 100, 200, give=[8, 13], get=[113, 108])
    assert result["me"]["by_position"] == {"GKP": 0, "DEF": 0, "MID": -25, "FWD": 25}
    assert result["me"]["change"] == 0
    assert result["verdict"] == "no_change"


def test_filler_who_does_not_start_shows_up_in_the_breakdown():
    # my other midfielders (65) are better than the filler (40), so he sits on the
    # bench and a defender takes the fifth midfield spot: 4-5-1 becomes 5-4-1
    changes = {8: 80, 13: 60, 113: 85, 108: 40}
    changes.update({pid: 65 for pid in range(9, 13)})
    players = with_scores(trade_league(), changes)
    result = app.evaluate_trade(players, 100, 200, give=[8, 13], get=[113, 108])
    assert 108 not in result["me"]["joins_xi"]
    assert result["me"]["formation_before"] == "4-5-1"
    assert result["me"]["formation_after"] == "5-4-1"
    assert result["me"]["by_position"] == {"GKP": 0, "DEF": 50, "MID": -80, "FWD": 25}


def test_trade_does_not_change_the_real_owners():
    players = trade_league()
    app.evaluate_trade(players, 100, 200, give=[8], get=[108])
    owners = {p["id"]: p["owner"] for p in players}
    assert owners[8] == 100 and owners[108] == 200


@pytest.mark.parametrize("them,give,get,text", [
    (100, [8], [9], "another manager"),
    (200, [], [108], "at least one player on each side"),
    (200, [101], [108], "isn't in your squad"),
    (200, [8], [9], "isn't in their squad"),
    (200, [8], [999], "isn't in their squad"),
    (200, [3], [108], "same positions"),
    (200, [8, 8], [108, 109], "same positions"),   # a repeated ID doesn't count twice
])
def test_trade_rules(them, give, get, text):
    with pytest.raises(app.TradeError, match=text):
        app.evaluate_trade(trade_league(), 100, them, give, get)


def test_position_mismatch_message_says_what_is_wrong():
    with pytest.raises(app.TradeError, match="You'd give 1 DEF but get 1 MID"):
        app.evaluate_trade(trade_league(), 100, 200, give=[3], get=[108])


@pytest.fixture
def two_managers(fake_api, monkeypatch):
    """The fake league plus a second manager (200) who owns players 2 (MID) and 3 (FWD)."""
    fake = app.get_json

    def with_rival(url):
        data = fake(url)
        if url.endswith("/details"):
            rival = {"entry_id": 200, "entry_name": "Rival FC",
                     "player_first_name": "Alex", "player_last_name": "Kim"}
            return {**data, "league_entries": data["league_entries"] + [rival]}
        if url.endswith("/element-status"):
            return {"element_status": [{"element": 1, "owner": 100},
                                       {"element": 2, "owner": 200},
                                       {"element": 3, "owner": 200}]}
        return data

    monkeypatch.setattr(app, "get_json", with_rival)


def test_trade_endpoint_analyzes_a_trade(two_managers):
    res = app.app.test_client().get("/api/team/100/trade?with=200&give=1&get=2&league=123")
    assert res.status_code == 200
    data = res.get_json()
    assert data["verdict"] == "worse"              # player 2 has much worse form than player 1
    assert data["me"]["joins_xi"] == [2]
    assert data["me"]["leaves_xi"] == [1]
    assert data["message"] == app.TRADE_MESSAGES["worse"]
    assert set(data["me"]["by_position"]) == {"GKP", "DEF", "MID", "FWD"}


def test_trade_endpoint_follows_the_view(two_managers):
    res = app.app.test_client().get("/api/team/100/trade?with=200&give=1&get=2&view=season")
    assert res.status_code == 200


@pytest.mark.parametrize("query,text", [
    ("give=1&get=2", "Pick a manager to trade with"),
    ("with=999&give=1&get=2", "isn't in your league"),
    ("with=200&give=1&get=3", "same positions"),
    ("with=200&give=abc&get=2", "must be numbers"),
])
def test_trade_endpoint_explains_bad_trades(two_managers, query, text):
    res = app.app.test_client().get(f"/api/team/100/trade?{query}")
    assert res.status_code == 400
    assert text in res.get_json()["error"]


# ---------------------------------------------------------------- trade ideas by club

IDEA_MANAGERS = [{"entry_id": 100, "team_name": "Me FC"}, {"entry_id": 200, "team_name": "Rival FC"}]


def club_league(clubs=None):
    """trade_league with a Premier League club (default ARS) and a points total on everyone."""
    players = trade_league()
    for p in players:
        p["team"] = (clubs or {}).get(p["id"], "ARS")
        p["total"] = 40
    return players


def idea_for(out, pid):
    """The row for one player in a trade_ideas result."""
    return next(p for c in out["clubs"] for p in c["players"] if p["id"] == pid)


def test_trade_ideas_list_every_club_with_its_whole_squad():
    out = app.trade_ideas(club_league({1: "CHE", 101: "CHE"}), 100, IDEA_MANAGERS)
    assert [c["club"] for c in out["clubs"]] == ["ARS", "CHE"]           # A-Z
    assert sorted(p["id"] for p in out["clubs"][1]["players"]) == [1, 101]
    assert sum(len(c["players"]) for c in out["clubs"]) == 30             # nobody left out


def test_trade_ideas_squad_is_in_position_order_best_first():
    players = with_scores(club_league(), {105: 90, 4: 70})   # two defenders stand out
    squad = app.trade_ideas(players, 100, IDEA_MANAGERS)["clubs"][0]["players"]
    order = [p["pos"] for p in squad]
    assert order == sorted(order, key=["GKP", "DEF", "MID", "FWD"].index)
    defenders = [p["id"] for p in squad if p["pos"] == "DEF"]
    assert defenders[:2] == [105, 4]


def test_trade_idea_is_my_best_offer_and_matches_the_analyzer():
    # my bench midfielder (30) for their star (80): the cheapest offer that does the job
    players = with_scores(club_league(), {8: 30, 108: 80})
    star = idea_for(app.trade_ideas(players, 100, IDEA_MANAGERS), 108)
    idea = star["idea"]
    assert star["owner"] == 200 and star["owner_name"] == "Rival FC" and not star["mine"]
    assert idea["give"] == 8 and idea["give_name"] == "P8"      # equal lifts: the lower-rated player goes
    assert idea["verdict"] == "good_but_unfair"
    analyzer = app.evaluate_trade(players, 100, 200, give=[8], get=[108])
    assert idea["me"] == analyzer["me"]["change"] >= app.TRADE_MIN_GAIN
    assert idea["them"] == analyzer["them"]["change"] < -app.FAIR_MARGIN


def test_trade_ideas_only_swap_the_same_position_and_my_own_players():
    players = club_league()
    for p in players:                                  # uneven ratings, so there is plenty to find
        p["score"] = 30 + (p["id"] * 7) % 50
    by_id = {p["id"]: p for p in players}
    out = app.trade_ideas(players, 100, IDEA_MANAGERS)
    ideas = [(r, r["idea"]) for c in out["clubs"] for r in c["players"] if r["idea"]]
    assert ideas
    for row, idea in ideas:
        assert by_id[idea["give"]]["owner"] == 100
        assert by_id[idea["give"]]["pos"] == row["pos"]
        assert row["owner"] == 200


def test_my_players_and_free_agents_get_no_idea():
    players = with_scores(club_league(), {108: 80, 8: 30})
    next(p for p in players if p["id"] == 115)["owner"] = None   # a free agent
    out = app.trade_ideas(players, 100, IDEA_MANAGERS)
    assert idea_for(out, 8)["mine"] and idea_for(out, 8)["idea"] is None
    free = idea_for(out, 115)
    assert free["owner"] is None and free["owner_name"] is None and free["idea"] is None


def test_trade_ideas_need_a_real_lift():
    out = app.trade_ideas(club_league(), 100, IDEA_MANAGERS)       # everyone rates 50: nothing improves
    assert all(r["idea"] is None for c in out["clubs"] for r in c["players"])
    assert all(c["best_gain"] is None for c in out["clubs"])


def test_each_club_reports_its_best_gain():
    players = with_scores(club_league({108: "CHE", 109: "MUN"}), {8: 30, 108: 80, 109: 65})
    clubs = {c["club"]: c for c in app.trade_ideas(players, 100, IDEA_MANAGERS)["clubs"]}
    assert clubs["CHE"]["best_gain"] == idea_for({"clubs": [clubs["CHE"]]}, 108)["idea"]["me"]
    assert clubs["CHE"]["best_gain"] > clubs["MUN"]["best_gain"] > 0
    assert clubs["ARS"]["best_gain"] is None


def test_trade_ideas_do_not_change_the_real_owners():
    players = with_scores(club_league(), {8: 30, 108: 80})
    before = [dict(p) for p in players]
    app.trade_ideas(players, 100, IDEA_MANAGERS)
    assert players == before


def free_agents(players, ids):
    """Make these players free agents (nobody owns them)."""
    for p in players:
        if p["id"] in ids:
            p["owner"] = None
    return players


def test_free_agent_claim_drops_my_weakest_and_matches_the_waiver_swap():
    # free agent 109 (a MID) rates 80; my MIDs rate 50 except bench player 8 on 30
    players = free_agents(with_scores(club_league(), {8: 30, 109: 80}), {109})
    star = idea_for(app.trade_ideas(players, 100, IDEA_MANAGERS), 109)
    claim = star["claim"]
    assert star["owner"] is None and star["idea"] is None          # a claim, not a trade
    assert claim["drop"] == 8 and claim["drop_name"] == "P8"
    # the same pair the waiver cards at the top of the page would suggest, with the same lift
    swap = next(t for t in app.waiver_targets(players, 100) if t["claim"] == 109)
    assert swap["drop"] == claim["drop"]
    assert claim["me"] == app.xi_lift(swap) >= app.WORTH_XI_GAIN


def test_claim_never_drops_a_kept_player():
    players = free_agents(with_scores(club_league(), {8: 30, 109: 80}), {109})
    claim = idea_for(app.trade_ideas(players, 100, IDEA_MANAGERS, keep={8}), 109)["claim"]
    assert claim["drop"] == 9        # the next weakest (tied at 50, lowest ID first)
    everyone = {8, 9, 10, 11, 12}    # all five of my midfielders kept: nobody left to drop
    assert idea_for(app.trade_ideas(players, 100, IDEA_MANAGERS, keep=everyone), 109)["claim"] is None


def test_a_free_agent_who_would_not_help_gets_no_claim():
    for score in (40, 50, 51):       # worse than, level with, and only just better than my weakest
        players = free_agents(with_scores(club_league(), {109: score}), {109})
        assert idea_for(app.trade_ideas(players, 100, IDEA_MANAGERS), 109)["claim"] is None


def test_only_free_agents_get_a_claim():
    players = free_agents(with_scores(club_league(), {8: 30, 108: 80, 109: 80}), {109})
    out = app.trade_ideas(players, 100, IDEA_MANAGERS)
    assert idea_for(out, 109)["claim"] is not None and idea_for(out, 109)["idea"] is None
    assert idea_for(out, 108)["claim"] is None and idea_for(out, 108)["idea"] is not None
    assert idea_for(out, 8)["claim"] is None


def test_each_club_reports_its_best_claim_apart_from_its_best_trade():
    players = free_agents(with_scores(club_league({109: "CHE", 110: "MUN"}), {8: 30, 109: 80, 110: 65}),
                          {109, 110})
    clubs = {c["club"]: c for c in app.trade_ideas(players, 100, IDEA_MANAGERS)["clubs"]}
    assert clubs["CHE"]["best_claim"] == idea_for({"clubs": [clubs["CHE"]]}, 109)["claim"]["me"]
    assert clubs["CHE"]["best_claim"] > clubs["MUN"]["best_claim"] > 0
    assert clubs["ARS"]["best_claim"] is None
    assert clubs["CHE"]["best_gain"] is None      # a claim isn't a trade


def test_free_agent_rows_carry_their_fitness():
    players = free_agents(with_scores(club_league(), {8: 30, 109: 80}), {109})
    for p in players:
        if p["id"] == 109:
            p["chance"], p["news"] = 50, "Knock - 50% chance of playing"
    row = idea_for(app.trade_ideas(players, 100, IDEA_MANAGERS), 109)
    assert row["chance"] == 50 and row["news"] == "Knock - 50% chance of playing"
    assert idea_for(app.trade_ideas(players, 100, IDEA_MANAGERS), 1)["news"] == ""   # no news on file


def test_claims_do_not_change_the_real_owners():
    players = free_agents(with_scores(club_league(), {8: 30, 109: 80}), {109})
    before = [dict(p) for p in players]
    app.trade_ideas(players, 100, IDEA_MANAGERS)
    assert players == before


def test_trade_ideas_endpoint(two_managers, monkeypatch):
    inner = app.get_json

    def stronger_rival_player(url):   # player 2 (rival's MID, CHE) is in better form than my player 1
        data = inner(url)
        if "draft" in url and "bootstrap" in url:
            elements = [dict(e, form="9.0") if e["id"] == 2 else e for e in data["elements"]]
            return {**data, "elements": elements}
        return data

    monkeypatch.setattr(app, "get_json", stronger_rival_player)
    res = app.app.test_client().get("/api/team/100/trade-ideas?league=123")
    assert res.status_code == 200
    data = res.get_json()
    assert data["league_id"] == 123 and data["view"] == "week" and data["me"] == 100
    assert [c["club"] for c in data["clubs"]] == ["ARS", "CHE"]
    target = idea_for(data, 2)
    assert target["owner_name"] == "Rival FC"
    assert target["idea"]["give"] == 1 and target["idea"]["me"] >= app.TRADE_MIN_GAIN
    assert idea_for(data, 1)["mine"]
    assert idea_for(data, 3)["idea"] is None          # a forward: I have none to give


def test_trade_ideas_endpoint_follows_the_view(two_managers):
    res = app.app.test_client().get("/api/team/100/trade-ideas?view=season")
    assert res.status_code == 200
    assert res.get_json()["view"] == "season"


def test_trade_ideas_endpoint_claims_free_agents_and_honours_keep(fake_api, monkeypatch):
    inner = app.get_json

    def better_free_agent(url):   # player 2 (a free MID) is in better form than my only player, 1
        data = inner(url)
        if "draft" in url and "bootstrap" in url:
            elements = [dict(e, form="9.0", points_per_game="9.0") if e["id"] == 2 else e
                        for e in data["elements"]]
            return {**data, "elements": elements}
        return data

    monkeypatch.setattr(app, "get_json", better_free_agent)
    client = app.app.test_client()
    claim = idea_for(client.get("/api/team/100/trade-ideas?league=123").get_json(), 2)["claim"]
    assert claim["drop"] == 1 and claim["me"] >= app.WORTH_XI_GAIN
    kept = client.get("/api/team/100/trade-ideas?league=123&keep=1").get_json()
    assert idea_for(kept, 2)["claim"] is None          # his only possible drop is kept


def test_trade_ideas_endpoint_explains_an_unknown_team(monkeypatch):
    def not_found(url):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/team/999999/trade-ideas")
    assert res.status_code == 400
    assert "No Draft team found" in res.get_json()["error"]


# ---------------------------------------------------------------- weekly lineups

# players 1-2 GKP, 3-7 DEF, 8-12 MID, 13-15 FWD (full_squad); varied ratings so the shapes differ
LINEUP_SCORES = {1: 70, 2: 40, 3: 80, 4: 75, 5: 60, 6: 55, 7: 50, 8: 90, 9: 85, 10: 65, 11: 45,
                 12: 40, 13: 88, 14: 72, 15: 30}


def lineup_squad(scores=None, blank=(), gw=7):
    """My 15 players, each with a club, no news and one game in gameweek `gw` (none for `blank`)."""
    squad = with_scores(full_squad(100), scores or {})
    for p in squad:
        p["team"], p["news"] = "ARS", ""
        p["fixtures"] = [] if p["id"] in blank else [{"gw": gw, "opp": "CHE", "home": True, "difficulty": 3}]
    return squad


def shape_totals(squad):
    """Each legal shape's total rating (best players of each position), for checking by hand."""
    top = {pos: sorted((p["score"] for p in squad if p["pos"] == pos), reverse=True) for pos in app.FORMATION}
    return {f"{d}-{m}-{f}": top["GKP"][0] + sum(top["DEF"][:d]) + sum(top["MID"][:m]) + sum(top["FWD"][:f])
            for d, m, f in app.SHAPES}


def test_there_are_eight_legal_shapes():
    assert len(app.SHAPES) == 8 and (4, 4, 2) in app.SHAPES and (5, 5, 0) not in app.SHAPES
    assert all(sum(s) == 10 for s in app.SHAPES)


def test_best_lineup_is_the_best_eleven_in_the_best_shape():
    squad = lineup_squad(LINEUP_SCORES)
    best = app.week_lineups(squad, 100, 7)["best"]
    league_squads = app.squad_strength(squad, 100)
    assert {p["id"] for p in best["xi"]} == set(league_squads["best_xi"])     # the League squads eleven
    assert best["formation"] == league_squads["formation"]
    assert best["strength"] == league_squads["strength"] == round(max(shape_totals(squad).values()) / 11, 1)
    positions = [p["pos"] for p in best["xi"]]
    assert positions == sorted(positions, key=list(app.FORMATION).index)       # keeper, DEF, MID, FWD


def test_second_best_lineup_is_another_shape_with_who_comes_in_and_out():
    squad = lineup_squad(LINEUP_SCORES)
    out = app.week_lineups(squad, 100, 7)
    best, second = out["best"], out["second"]
    assert second["formation"] != best["formation"] and len(second["xi"]) == 11
    others = {s: t for s, t in shape_totals(squad).items() if s != best["formation"]}
    assert second["formation"] == max(others, key=others.get)                  # the best of the rest
    assert second["strength"] == round(max(others.values()) / 11, 1) <= best["strength"]
    assert second["change"] <= 0     # and it adds up from the two numbers the page shows
    assert second["change"] == round(second["strength"] - best["strength"], 1)
    best_ids, second_ids = {p["id"] for p in best["xi"]}, {p["id"] for p in second["xi"]}
    assert {p["id"] for p in second["xi"] if p["new"]} == second_ids - best_ids
    assert {p["id"] for p in second["out"]} == best_ids - second_ids
    assert second_ids - best_ids and not any(p["new"] for p in best["xi"])


def test_level_shapes_report_no_change_not_minus_zero():
    out = app.week_lineups(lineup_squad(), 100, 7)           # everyone rates 50
    assert out["second"]["change"] == 0 and str(out["second"]["change"]) == "0.0"


def test_bench_has_the_spare_keeper_first_then_the_best_of_the_rest():
    squad = lineup_squad(LINEUP_SCORES)
    best = app.week_lineups(squad, 100, 7)["best"]
    bench = best["bench"]
    assert len(bench) == 4 and len(best["xi"]) == 11
    assert bench[0]["pos"] == "GKP"
    assert [p["score"] for p in bench[1:]] == sorted((p["score"] for p in bench[1:]), reverse=True)
    assert {p["id"] for p in bench}.isdisjoint(p["id"] for p in best["xi"])


def test_a_player_with_no_game_that_week_sits_on_the_bench():
    squad = lineup_squad({13: 95}, blank={13})              # a star forward whose club doesn't play
    out = app.week_lineups(squad, 100, 7)
    assert 13 in app.squad_strength(squad, 100)["best_xi"]  # League squads would still start him
    assert 13 not in {p["id"] for p in out["best"]["xi"]}
    star = next(p for p in out["best"]["bench"] if p["id"] == 13)
    assert star["no_game"] and star["games"] == []
    assert not any(p["no_game"] for p in out["best"]["xi"])
    without = [p for p in squad if p["id"] != 13]
    assert out["best"]["strength"] == app.squad_strength(without, 100)["strength"]


def test_nobody_is_benched_when_no_club_has_a_game_that_week():
    squad = lineup_squad({13: 95}, blank=set(range(1, 16)))   # an empty fixture feed
    out = app.week_lineups(squad, 100, 7)
    assert 13 in {p["id"] for p in out["best"]["xi"]}
    assert not any(p["no_game"] for line in (out["best"]["xi"], out["best"]["bench"]) for p in line)
    squad = lineup_squad(blank=set(range(1, 16)), gw=9)       # games only in a later gameweek
    assert 1 in {p["id"] for p in app.week_lineups(squad, 100, 7)["best"]["xi"]}


def test_lineup_rows_carry_games_doubles_and_fitness():
    squad = lineup_squad(LINEUP_SCORES)
    star = next(p for p in squad if p["id"] == 8)           # a double gameweek and a knock
    star["fixtures"] = star["fixtures"] + [{"gw": 7, "opp": "LIV", "home": False, "difficulty": 5},
                                           {"gw": 8, "opp": "MCI", "home": True, "difficulty": 4}]
    star["chance"], star["news"] = 75, "Knock - 75% chance of playing"
    row = next(p for p in app.week_lineups(squad, 100, 7)["best"]["xi"] if p["id"] == 8)
    assert [g["opp"] for g in row["games"]] == ["CHE", "LIV"]       # gameweek 8 isn't this week's
    assert row["games"][1] == {"opp": "LIV", "home": False, "difficulty": 5}
    assert row["club"] == "ARS" and row["chance"] == 75 and row["news"] == "Knock - 75% chance of playing"


def test_a_thin_position_only_offers_the_shapes_it_can_fill():
    squad = [p for p in lineup_squad(LINEUP_SCORES) if p["id"] != 15]    # only two forwards
    out = app.week_lineups(squad, 100, 7)
    for lineup in (out["best"], out["second"]):
        assert int(lineup["formation"].split("-")[2]) <= 2 and len(lineup["xi"]) == 11


def test_no_lineup_without_an_eleven_and_other_managers_are_left_out():
    assert app.week_lineups(lineup_squad()[:10], 100, 7) is None
    rivals = [dict(p, id=p["id"] + 100, owner=200) for p in lineup_squad(LINEUP_SCORES)]
    out = app.week_lineups(lineup_squad() + rivals, 100, 7)
    assert all(p["id"] <= 15 for line in (out["best"]["xi"], out["best"]["bench"]) for p in line)


def test_lineups_do_not_change_the_real_players():
    squad = lineup_squad(LINEUP_SCORES, blank={13})
    before = [dict(p) for p in squad]
    app.week_lineups(squad, 100, 7)
    assert squad == before


def test_team_endpoint_has_lineups_but_the_trade_page_view_skips_them(fake_api):
    client = app.app.test_client()
    full = client.get("/api/team/100").get_json()
    assert "lineups" in full and full["lineups"] is None          # one player isn't an eleven
    assert "lineups" not in client.get("/api/team/100?swaps=0").get_json()


# ---------------------------------------------------------------- league banter

def banter_league(matches):
    """A 5-team head-to-head league (team 100-104). Entries carry names so facts can mention them."""
    entries = [{"id": 10 + i, "entry_id": 100 + i, "entry_name": f"Team {i}",
                "player_first_name": "M", "player_last_name": str(i)} for i in range(5)]
    return {"league": {"name": "Banter League"}, "league_entries": entries, "matches": matches}


# GW1: 0 beats 1 (60-40), 2 beats 3 (90-50); 4 has a bye.  GW2: 0 beats 2 (70-69), 1 beats 4 (50-20).
BANTER_WEEKS = [match(1, 10, 60, 11, 40), match(1, 12, 90, 13, 50),
                match(2, 10, 70, 12, 69), match(2, 11, 50, 14, 20),
                match(3, 10, 0, 11, 0, finished=False), match(3, 12, 0, 13, 0, finished=False),
                match(4, 13, 0, 14, 0, finished=False)]


def test_banter_needs_a_finished_gameweek():
    assert app.league_banter(banter_league([match(1, 10, 0, 11, 0, finished=False)])) is None
    assert app.league_banter({"league_entries": [{"id": 10, "entry_id": 100}]}) is None


def test_hot_match_is_the_best_placed_pair_in_the_next_gameweek():
    # table after GW2: Team 0 (6 pts), Team 2, Team 1 (3 each), then Teams 3 and 4 (0).
    # GW3 has 0 v 1 (1st + 3rd) and 2 v 3 (2nd + 4th), so 0 v 1 is the hot one
    hot = app.league_banter(banter_league(BANTER_WEEKS))["hot_match"]
    assert hot["gw"] == 3
    assert {hot["a"]["entry_id"], hot["b"]["entry_id"]} == {100, 101}
    assert hot["line"] == "3 points apart."


def test_no_hot_match_when_nothing_is_left_to_play():
    assert app.league_banter(banter_league(BANTER_WEEKS[:4]))["hot_match"] is None


def test_cards_are_three_different_matches_from_the_next_gameweek():
    # 6 teams, 3 matches in GW2. After GW1: 0, 2, 4 won (3 pts); 1, 3, 5 lost
    entries = [{"id": 10 + i, "entry_id": 100 + i, "entry_name": f"Team {i}"} for i in range(6)]
    matches = [match(1, 10, 90, 11, 10), match(1, 12, 80, 13, 10), match(1, 14, 70, 15, 10),
               match(2, 10, 0, 15, 0, finished=False), match(2, 12, 0, 13, 0, finished=False),
               match(2, 14, 0, 11, 0, finished=False), match(3, 10, 0, 11, 0, finished=False)]
    banter = app.league_banter({"league_entries": entries, "matches": matches})
    cards = [banter["hot_match"], *banter["battles"]]
    assert [c["gw"] for c in cards] == [2, 2, 2]               # GW3 is further ahead, so it's ignored
    teams = [c[side]["entry_id"] for c in cards for side in "ab"]
    assert len(teams) == len(set(teams)) == 6                  # nobody is on two cards
    assert {c["title"] for c in banter["battles"]} == {"Battle for 3rd", "Wooden spoon watch"}


def test_battle_cards_follow_the_table():
    entries = [{"id": 10 + i, "entry_id": 100 + i, "entry_name": f"Team {i}"} for i in range(6)]
    matches = [match(1, 10, 90, 11, 10), match(1, 12, 80, 13, 10), match(1, 14, 70, 15, 10),
               match(2, 10, 0, 15, 0, finished=False), match(2, 12, 0, 13, 0, finished=False),
               match(2, 14, 0, 11, 0, finished=False)]
    banter = app.league_banter({"league_entries": entries, "matches": matches})
    by_title = {b["title"]: b for b in banter["battles"]}
    # table: Team 0, 2, 4 (3 pts, in that order of points scored), then Teams 1, 3, 5
    assert {banter["hot_match"]["a"]["entry_id"], banter["hot_match"]["b"]["entry_id"]} == {100, 105}
    def teams(card):
        return {card["a"]["entry_id"], card["b"]["entry_id"]}

    assert teams(by_title["Battle for 3rd"]) == {104, 101}
    assert teams(by_title["Wooden spoon watch"]) == {102, 103}
    assert by_title["Wooden spoon watch"]["line"] == "3 points apart."
    assert banter["hot_match"]["tease"] in app.CARD_TEASES["hot"]       # each card has its own line of banter
    assert by_title["Battle for 3rd"]["tease"] in app.CARD_TEASES["third"]
    assert by_title["Wooden spoon watch"]["tease"] in app.CARD_TEASES["spoon"]


def test_cards_are_left_out_when_there_are_too_few_matches():
    banter = app.league_banter(banter_league(BANTER_WEEKS))   # GW3 has two matches
    assert banter["hot_match"] is not None
    assert [b["title"] for b in banter["battles"]] == ["Battle for 3rd"]


def test_no_battles_when_nothing_is_left_to_play():
    banter = app.league_banter(banter_league(BANTER_WEEKS[:4]))
    assert banter["hot_match"] is None and banter["battles"] == []


@pytest.fixture
def first_wording(monkeypatch):
    """Always use a fact's first wording, so a test can check the exact text whatever the gameweek is."""
    monkeypatch.setattr(app, "banter_wording", lambda options, kind, gw: options[0])


def facts_by_kind(matches):
    return {f["kind"]: f["text"] for f in app.league_banter(banter_league(matches))["facts"]}


def test_facts_are_capped_and_not_repeated(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 4)
    kinds = [f["kind"] for f in app.league_banter(banter_league(BANTER_WEEKS))["facts"]]
    assert len(kinds) == 4
    assert len(kinds) == len(set(kinds))


def test_harsh_fact_names_the_highest_losing_score(monkeypatch, first_wording):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    assert facts_by_kind(BANTER_WEEKS)["harsh"] == (
        "**Team 2** scored 69 in GW2 and still lost to **Team 0** (70). "
        "Take that up with whoever drew the fixtures.")


def test_each_fact_reads_from_the_numbers(monkeypatch, first_wording):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    facts = facts_by_kind(BANTER_WEEKS)
    assert facts["thrashing"].startswith(
        "Biggest beating so far: **Team 2** 90-50 **Team 3** in GW1, a 40-point gap. "
        "**Team 3** may want to mute the group chat.")
    assert facts["closest"] == ("Closest match so far: **Team 0** edged **Team 2** 70-69 in GW2. "
                                "**Team 2** will be thinking about that one for a while.")
    assert facts["low"].startswith("Lowest score so far: **Team 4** managed 20 in GW2")
    assert facts["high"] == ("Best week so far: **Team 2** put up 90 in GW1. "
                             "The rest of you can take notes.")


def test_luck_fact_finds_the_manager_whose_table_place_lags_their_points(monkeypatch, first_wording):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    monkeypatch.setattr(app, "LUCK_FACT_MIN", 99)   # no "play everyone" facts (they name Team 2 too)
    # Team 2 scores 200 (the most) but loses both games by a point, so it sits 4th in the table
    weeks = [match(1, 12, 100, 13, 101), match(1, 10, 10, 11, 5),
             match(2, 12, 100, 14, 101), match(2, 10, 10, 13, 5)]
    assert facts_by_kind(weeks)["luck"] == (
        "**Team 2** are 1st for points scored but only 4th in the table. "
        "The fixture list has it in for them.")


def test_a_winning_run_is_called_out(monkeypatch, first_wording):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    weeks = [match(gw, 10, 60, 11 + gw, 40) for gw in (1, 2, 3)]
    assert facts_by_kind(weeks)["streak"] == ("**Team 0** have won 3 in a row. "
                                              "Somebody stop them before the rest of you get embarrassed.")


def test_a_losing_run_gets_a_poke(monkeypatch, first_wording):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    weeks = [match(1, 10, 60, 11, 40), match(2, 12, 60, 11, 40), match(3, 13, 60, 11, 40)]
    assert facts_by_kind(weeks)["streak"] == ("**Team 1** have lost 3 in a row. "
                                              "Has anyone checked they're still setting a lineup?")


def test_short_runs_are_not_mentioned(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    assert "streak" not in facts_by_kind(BANTER_WEEKS)


def test_banter_route_returns_the_league_name_and_banter(fake_api, monkeypatch):
    fake = app.get_json

    def with_matches(url):
        if url.endswith("/league/123/details"):
            return banter_league(BANTER_WEEKS)
        return fake(url)

    monkeypatch.setattr(app, "get_json", with_matches)
    data = app.app.test_client().get("/api/league/123/banter").get_json()
    assert data["league_name"] == "Banter League"
    assert data["banter"]["hot_match"]["gw"] == 3


def test_banter_route_is_empty_for_a_league_without_matches(fake_api):
    data = app.app.test_client().get("/api/league/123/banter").get_json()
    assert data["banter"] is None


def test_banter_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/banter")
    assert res.status_code == 400
    assert "No Draft league found" in res.get_json()["error"]


# ---------------------------------------------------------------- banter wording and last gameweek's facts

# the fields each kind of wording is given (the comments in BANTER_LINES list the same)
WORDING_FIELDS = {
    "unlucky": {"team", "pts", "exp"}, "lucky": {"team", "pts", "exp"}, "luck": {"team", "sp", "tp"},
    "harsh": {"loser", "mine", "winner", "theirs", "gw"},
    "streak_win": {"team", "n"}, "streak_loss": {"team", "n"},
    "thrashing": {"win", "lose", "top", "bottom", "gw", "diff"},
    "closest": {"win", "lose", "top", "bottom", "gw", "diff"},
    "low": {"team", "pts", "gw"}, "high": {"team", "pts", "gw"},
    "wk_high": {"team", "pts", "gw"}, "wk_low": {"team", "pts", "gw"},
    "wk_win": {"win", "lose", "top", "bottom", "gw", "diff"},
    "wk_close": {"win", "lose", "top", "bottom", "gw", "diff"},
    "wk_draw": {"a", "b", "pts", "gw"},
    "wk_climb": {"team", "n", "old", "new", "gw"}, "wk_fall": {"team", "n", "old", "new", "gw"},
}


def test_every_kind_of_fact_has_three_wordings_that_only_use_its_fields():
    assert set(app.BANTER_LINES) == set(WORDING_FIELDS)
    for kind, options in app.BANTER_LINES.items():
        assert len(options) == 3 and len(set(options)) == 3
        for text in options:
            assert set(re.findall(r"{(\w+)}", text)) <= WORDING_FIELDS[kind], (kind, text)
    for kind, options in app.CARD_TEASES.items():
        assert len(options) == 3 and not any("{" in t for t in options)


def test_a_wording_stays_the_same_all_gameweek_and_changes_the_next():
    options = app.BANTER_LINES["wk_high"]

    def pick(gw):
        return app.banter_wording(options, "wk_high", gw)

    assert pick(5) == pick(5)
    assert len({pick(5), pick(6), pick(7)}) == 3         # three weeks running, three different wordings
    assert pick(8) == pick(5)                            # and then round again


def test_different_kinds_of_fact_do_not_all_use_the_same_wording():
    positions = {options.index(app.banter_wording(options, kind, 5))
                 for kind, options in app.BANTER_LINES.items()}
    assert len(positions) > 1


# GW1: Team 0 beats 1 (60-40), Team 2 beats 3 (50-30). GW2: Team 3 beats 0 (80-10), Team 1 beats 2 (75-25).
# That leaves all four on 3 points, so points scored decides: Team 1 climbs 3rd to 1st, Team 0 falls 1st to
# 4th.
# (Team 4, the fifth in banter_league, never plays and stays bottom.)
CLIMB_WEEKS = [match(1, 10, 60, 11, 40), match(1, 12, 50, 13, 30),
               match(2, 13, 80, 10, 10), match(2, 11, 75, 12, 25)]


def banter_leagues():
    """Leagues that, between them, produce every kind of fact."""
    return [
        banter_league(BANTER_WEEKS),
        banter_league([match(gw, 10, 60, 11 + gw, 40) for gw in (1, 2, 3)]),    # Team 0 keep winning
        banter_league([match(1, 10, 60, 11, 40), match(2, 12, 60, 11, 40),
                       match(3, 13, 60, 11, 40)]),                              # Team 1 keep losing
        h2h([match(1, 10, 60, 11, 65), match(1, 12, 10, 13, 20),                # lucky and unlucky
             match(2, 10, 58, 11, 70), match(2, 12, 10, 13, 15)]),
        banter_league([match(1, 10, 60, 11, 40), match(1, 12, 50, 13, 50)]),                  # a draw
        banter_league(CLIMB_WEEKS),                                             # a climb and a fall
        {"league_entries": [{"id": 10 + i, "entry_id": 100 + i, "entry_name": f"Team {i}"} for i in range(6)],
         "matches": [match(1, 10, 90, 11, 10), match(1, 12, 80, 13, 10), match(1, 14, 70, 15, 10),   # 3 cards
                     match(2, 10, 0, 15, 0, finished=False), match(2, 12, 0, 13, 0, finished=False),
                     match(2, 14, 0, 11, 0, finished=False)]},
    ]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_every_wording_of_every_fact_fills_in_from_real_leagues(monkeypatch, index):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    used = set()

    def pick(options, kind, gw):
        used.add(kind)
        return options[index]

    monkeypatch.setattr(app, "banter_wording", pick)
    texts = []
    for league in banter_leagues():
        banter = app.league_banter(league)
        texts += [f["text"] for f in banter["facts"] + banter["week"]["facts"]]
        texts += [c["tease"] for c in [banter["hot_match"], *banter["battles"]] if c]
    # a table-place luck fact only shows when no "play everyone" fact names the same team
    monkeypatch.setattr(app, "LUCK_FACT_MIN", 99)
    banter = app.league_banter(banter_league([match(1, 12, 100, 13, 101), match(1, 10, 10, 11, 5),
                                              match(2, 12, 100, 14, 101), match(2, 10, 10, 13, 5)]))
    texts += [f["text"] for f in banter["facts"]]
    assert used == set(app.BANTER_LINES) | set(app.CARD_TEASES)       # every kind of wording was used
    assert texts and not any("{" in t or "}" in t for t in texts)


def week_facts(matches):
    return {f["kind"]: f["text"] for f in app.league_banter(banter_league(matches))["week"]["facts"]}


def test_the_week_facts_are_about_the_latest_gameweek_only(first_wording):
    week = app.league_banter(banter_league(BANTER_WEEKS))["week"]
    assert week["gw"] == 2                              # GW3 and GW4 haven't been played
    texts = {f["kind"]: f["text"] for f in week["facts"]}
    assert list(texts) == ["wk_high", "wk_low", "wk_win", "wk_close"]      # nobody moved two places
    assert texts["wk_high"] == "Score of GW2: **Team 0** with 70. Everyone else was playing for second."
    assert texts["wk_low"].startswith("Worst score of GW2: **Team 4** with 20.")
    assert texts["wk_win"].startswith("Biggest win of GW2: **Team 1** 50-20 **Team 4**, a 30-point gap.")
    assert texts["wk_close"].startswith("Closest match of GW2: **Team 0** edged **Team 2** 70-69.")


def test_the_week_facts_name_the_biggest_climb_and_fall(first_wording):
    texts = week_facts(CLIMB_WEEKS)
    assert texts["wk_climb"] == ("**Team 1** climbed from 3rd to 1st in GW2. Expect them to mention it.")
    assert texts["wk_fall"].startswith("**Team 0** dropped from 1st to 4th in GW2.")


def test_small_moves_are_not_facts():
    assert {"wk_climb", "wk_fall"}.isdisjoint(week_facts(BANTER_WEEKS))     # everyone moved a place or none


def test_a_draw_is_the_closest_match_of_the_week(first_wording):
    texts = week_facts([match(1, 10, 60, 11, 40), match(1, 12, 50, 13, 50)])
    assert texts["wk_draw"] == (
        "**Team 2** and **Team 3** drew 50-50 in GW1. Nobody won and everybody's annoyed.")
    assert "wk_close" not in texts and "wk_climb" not in texts       # one gameweek: nobody can have moved


def test_a_lone_match_is_the_biggest_win_but_not_also_the_closest(first_wording):
    texts = week_facts([match(1, 10, 60, 11, 40)])
    assert "wk_win" in texts and "wk_close" not in texts


def test_the_week_wording_changes_with_the_gameweek():
    gw2 = week_facts(BANTER_WEEKS)["wk_high"]
    gw3 = week_facts(BANTER_WEEKS[:4] + [match(3, 10, 70, 11, 40), match(3, 12, 30, 13, 20)])["wk_high"]
    assert gw2.startswith(("Score of", "**Team 0** topped", "70 from")) and gw3 != gw2
    assert "GW3" in gw3 and "GW2" in gw2


def test_banter_route_has_the_week_facts_and_a_tease_on_each_card(fake_api, monkeypatch):
    fake = app.get_json
    monkeypatch.setattr(app, "get_json", lambda url: banter_league(BANTER_WEEKS)
                        if url.endswith("/league/123/details") else fake(url))
    banter = app.app.test_client().get("/api/league/123/banter").get_json()["banter"]
    assert banter["week"]["gw"] == 2 and len(banter["week"]["facts"]) == 4
    assert banter["hot_match"]["tease"] in app.CARD_TEASES["hot"]


# ---------------------------------------------------------------- places over time

def places_of(matches):
    places = app.league_charts(banter_league(matches))["places"]
    return {m["entry_id"]: m["places"] for m in places["managers"]}


def test_places_follow_the_table_after_every_gameweek():
    places = app.league_charts(banter_league(CLIMB_WEEKS))["places"]
    assert places["gws"] == [1, 2]
    # all four are level on points after GW2, so points scored (115, 110, 75, 70) decides
    assert [m["entry_id"] for m in places["managers"]] == [101, 103, 102, 100, 104]       # table order now
    assert places_of(CLIMB_WEEKS) == {101: [3, 1], 103: [4, 2], 102: [2, 3], 100: [1, 4], 104: [5, 5]}
    assert places["managers"][0]["team"] == "Team 1"


def test_places_notes_name_who_led_and_the_biggest_climb_and_fall():
    notes = app.league_charts(banter_league(CLIMB_WEEKS))["places"]["notes"]
    assert notes == ["Most time on top: **Team 1**, first after 1 of 2 gameweeks.",
                     "Biggest climb since GW1: **Team 1**, 3rd to 1st.",
                     "Biggest fall since GW1: **Team 0**, 1st to 4th."]


def test_places_notes_skip_a_move_of_one_place():
    notes = app.league_charts(banter_league(BANTER_WEEKS))["places"]["notes"]
    assert notes == ["Most time on top: **Team 0**, first after 1 of 2 gameweeks."]


def test_a_manager_who_led_every_week_is_said_to_have_done_so():
    weeks = [match(gw, 10, 90, 10 + gw, 10) for gw in (1, 2, 3)]
    assert app.league_charts(banter_league(weeks))["places"]["notes"][0] == (
        "Most time on top: **Team 0**, first after every gameweek.")


def test_places_look_back_at_most_three_gameweeks():
    # the big moves all happen between GW1 and GW2, then Teams 1 and 3 keep winning and nobody moves again
    later = [m for gw in (3, 4, 5) for m in (match(gw, 11, 80, 10, 10), match(gw, 13, 80, 12, 10))]
    weeks = CLIMB_WEEKS + later
    places = app.league_charts(banter_league(weeks))["places"]
    assert places["managers"][0]["places"] == [3, 1, 1, 1, 1]
    # looking back three gameweeks from GW5 starts at GW2, after the moves: no climb or fall to name
    assert places["notes"] == ["Most time on top: **Team 1**, first after 4 of 5 gameweeks."]


def test_places_need_two_finished_gameweeks():
    assert app.league_charts(banter_league([match(1, 10, 60, 11, 40)]))["places"] is None
    assert app.league_charts(banter_league([match(1, 10, 60, 11, 40), match(2, 10, 0, 11, 0, finished=False)])
                             )["places"] is None


def test_the_charts_route_includes_the_places(fake_api, monkeypatch):
    fake = app.get_json
    monkeypatch.setattr(app, "get_json", lambda url: banter_league(CLIMB_WEEKS)
                        if url.endswith("/league/123/details") else fake(url))
    places = app.app.test_client().get("/api/league/123/charts").get_json()["charts"]["places"]
    assert places["managers"][0]["places"] == [3, 1]


@pytest.mark.parametrize("n,text", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
                                    (11, "11th"), (12, "12th"), (21, "21st")])
def test_ordinal(n, text):
    assert app.ordinal(n) == text


# ---------------------------------------------------------------- trade and banter pages

@pytest.mark.parametrize("path,text", [("/trade", b"Trade analyzer"), ("/trade", b"Trade ideas by club"),
                                       ("/", b"Players by club"), ("/league", b"League hub")])
def test_extra_pages_load(path, text):
    res = app.app.test_client().get(path)
    assert res.status_code == 200
    assert text in res.data


def test_team_endpoint_can_skip_the_swaps(fake_api):
    data = app.app.test_client().get("/api/team/100?swaps=0").get_json()
    assert "waiver_targets" not in data and "recent" not in data
    assert data["me"] == 100 and data["players"]


def test_shared_stylesheet_is_served():
    assert app.app.test_client().get("/static/common.css").status_code == 200


# ---------------------------------------------------------------- league charts

def charts_for(matches):
    return app.league_charts(banter_league(matches))


def versus(charts, team, opponent):
    row = next(r for r in charts["head_to_head"] if r["entry_id"] == team)
    return next((o for o in row["vs"] if o["entry_id"] == opponent), None)


def test_charts_need_a_finished_gameweek():
    assert charts_for([match(1, 10, 0, 11, 0, finished=False)]) is None
    assert app.league_charts({"league_entries": [{"id": 10, "entry_id": 100}]}) is None


def test_charts_include_the_race_history():
    charts = charts_for(BANTER_WEEKS)
    assert charts["history"]["gws"] == [1, 2]
    race_order = [m["entry_id"] for m in charts["history"]["managers"]]
    assert [m["entry_id"] for m in charts["managers"]] == race_order


def test_head_to_head_counts_wins_losses_and_points():
    charts = charts_for(BANTER_WEEKS)
    assert versus(charts, 100, 101) == {"entry_id": 101, "won": 1, "drawn": 0, "lost": 0,
                                        "points_for": 60, "points_against": 40}
    assert versus(charts, 101, 100) == {"entry_id": 100, "won": 0, "drawn": 0, "lost": 1,
                                        "points_for": 40, "points_against": 60}   # the same match, other side
    assert versus(charts, 100, 102)["won"] == 1                                    # won 70-69


def test_head_to_head_counts_draws_and_skips_unmet_opponents():
    charts = app.league_charts(h2h(TWO_WEEKS))
    draw = versus(charts, 102, 103)
    assert (draw["won"], draw["drawn"], draw["lost"]) == (0, 1, 0)
    assert versus(charts, 100, 103) is None        # their GW3 match isn't finished, so they haven't met


def test_head_to_head_adds_up_repeat_meetings():
    weeks = [match(1, 10, 60, 11, 40), match(2, 11, 70, 10, 50), match(3, 10, 30, 11, 30)]
    record = versus(charts_for(weeks), 100, 101)
    assert (record["won"], record["drawn"], record["lost"]) == (1, 1, 1)
    assert (record["points_for"], record["points_against"]) == (140, 140)


def test_charts_total_points_scored_and_conceded():
    by_team = {m["entry_id"]: m for m in charts_for(BANTER_WEEKS)["managers"]}
    assert (by_team[100]["points_for"], by_team[100]["points_against"]) == (130, 109)
    assert (by_team[104]["points_for"], by_team[104]["points_against"]) == (20, 50)
    assert by_team[102]["team"] == "Team 2" and by_team[102]["manager"] == "M 2"


def test_charts_weekly_range_ignores_a_bye():
    by_team = {m["entry_id"]: m for m in charts_for(BANTER_WEEKS)["managers"]}
    assert by_team[100]["low"] == {"gw": 1, "points": 60}
    assert by_team[100]["high"] == {"gw": 2, "points": 70}
    assert by_team[100]["average"] == 65
    assert by_team[104]["low"] == by_team[104]["high"] == {"gw": 2, "points": 20}   # no GW1 match


def test_charts_leave_out_unfinished_matches():
    charts = charts_for(BANTER_WEEKS)                  # GW3 and GW4 aren't played
    assert versus(charts, 103, 104) is None
    assert all(m["points_for"] for m in charts["managers"] if m["entry_id"] != 104)


def test_charts_route_returns_the_league_name_and_charts(fake_api, monkeypatch):
    fake = app.get_json

    def with_matches(url):
        if url.endswith("/league/123/details"):
            return banter_league(BANTER_WEEKS)
        return fake(url)

    monkeypatch.setattr(app, "get_json", with_matches)
    data = app.app.test_client().get("/api/league/123/charts").get_json()
    assert data["league_name"] == "Banter League"
    assert len(data["charts"]["head_to_head"]) == 5


def test_charts_route_is_empty_for_a_league_without_matches(fake_api):
    assert app.app.test_client().get("/api/league/123/charts").get_json()["charts"] is None


def test_charts_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/charts")
    assert res.status_code == 400
    assert "No Draft league found" in res.get_json()["error"]


def test_league_hub_keeps_my_team_and_the_way_back_for_the_full_link_only():
    res = app.app.test_client().get("/league?league=123")
    assert res.status_code == 200
    page = res.data.decode()
    res.close()
    assert 'data-tab="banter"' in page and 'data-tab="charts"' in page
    # My team, the way back and the full link are on the page but hidden unless the address has &full=1
    assert 'class="choice fullonly hidden" type="button" data-tab="team"' in page
    assert 'class="back fullonly hidden"' in page
    assert 'const FULL = new URLSearchParams(location.search).get("full") === "1"' in page
    # the league ID box is hidden for view-only visitors whose link already has a league
    assert 'if (!FULL) $("loadForm").classList.add("hidden")' in page
    # the only link out of the page is the hidden way back
    assert page.count('href="/"') == 1


@pytest.mark.parametrize("old,tab", [("/banter", "banter"), ("/charts", "charts")])
def test_old_page_links_open_the_hub_on_that_tab(old, tab):
    res = app.app.test_client().get(old + "?league=123&team=100&junk=1")
    assert res.status_code == 302
    assert res.headers["Location"] == f"/league?league=123&tab={tab}"   # the team ID and the junk are dropped


def test_old_page_link_without_a_league_still_opens_the_hub():
    res = app.app.test_client().get("/charts")
    assert res.headers["Location"] == "/league?tab=charts"


# ---------------------------------------------------------------- saved seasons and rivalries

def snapshot_for(matches, today=None):
    return app.snapshot_league(banter_league(matches), today=today)


@pytest.mark.parametrize("day,label", [
    (datetime(2026, 10, 1), "2026-27"), (datetime(2027, 5, 20), "2026-27"),
    (datetime(2027, 7, 1), "2027-28"), (datetime(2099, 12, 31), "2099-00")])
def test_season_label_starts_in_july(day, label):
    assert app.season_label(day) == label


def test_snapshot_keeps_finished_matches_by_team_id():
    snap = snapshot_for(BANTER_WEEKS, today=datetime(2026, 10, 1))
    assert snap["season"] == "2026-27" and snap["league_name"] == "Banter League"
    assert {"gw": 1, "a": 100, "b": 101, "a_points": 60, "b_points": 40} in snap["matches"]
    assert all(m["gw"] in (1, 2) for m in snap["matches"])           # GW3 and GW4 aren't played
    assert {m["entry_id"] for m in snap["managers"]} == {100, 101, 102, 103, 104}


def test_snapshot_is_none_before_a_gameweek_finishes():
    assert snapshot_for([match(1, 10, 0, 11, 0, finished=False)]) is None


def test_save_snapshot_writes_a_file_and_never_replaces_a_fuller_one(tmp_path):
    full = snapshot_for(BANTER_WEEKS, today=datetime(2026, 10, 1))
    path = app.save_snapshot(52607, full, folder=tmp_path)
    assert path == tmp_path / "2026-27" / "league_52607.json"
    assert json.loads(path.read_text(encoding="utf-8"))["league_id"] == 52607
    assert app.save_snapshot(52607, full, folder=tmp_path) is None            # nothing new
    smaller = snapshot_for(BANTER_WEEKS[:1], today=datetime(2026, 10, 1))
    assert app.save_snapshot(52607, smaller, folder=tmp_path) is None         # e.g. the league reset
    assert len(json.loads(path.read_text(encoding="utf-8"))["matches"]) == len(full["matches"])
    assert app.save_snapshot(52607, None, folder=tmp_path) is None


def test_save_snapshot_updates_when_more_matches_are_finished(tmp_path):
    day = datetime(2026, 10, 1)
    app.save_snapshot(1, snapshot_for(BANTER_WEEKS[:1], today=day), folder=tmp_path)
    assert app.save_snapshot(1, snapshot_for(BANTER_WEEKS, today=day), folder=tmp_path) is not None


def test_saved_snapshots_come_back_oldest_first_and_skip_bad_files(tmp_path):
    app.save_snapshot(7, snapshot_for(BANTER_WEEKS, today=datetime(2027, 8, 1)), folder=tmp_path)
    app.save_snapshot(7, snapshot_for(BANTER_WEEKS, today=datetime(2026, 8, 1)), folder=tmp_path)
    (tmp_path / "2028-29").mkdir()
    (tmp_path / "2028-29" / "league_7.json").write_text("not json", encoding="utf-8")
    other_league = snapshot_for(BANTER_WEEKS, today=datetime(2026, 8, 1))
    app.save_snapshot(8, other_league, folder=tmp_path)
    assert [s["season"] for s in app.saved_snapshots(7, folder=tmp_path)] == ["2026-27", "2027-28"]


def season(label, *games):
    """A snapshot for the rivalry tests: games are (gw, a, a_points, b, b_points)."""
    return {"season": label, "managers": [],
            "matches": [{"gw": g, "a": a, "b": b, "a_points": pa, "b_points": pb}
                        for g, a, pa, b, pb in games]}


def test_rivalry_counts_meetings_whichever_way_round_they_were_listed():
    last = season("2026-27", (1, 100, 60, 101, 40), (9, 101, 50, 100, 50), (4, 100, 30, 102, 99))
    this = season("2027-28", (3, 101, 70, 100, 55))
    r = app.league_rivalry([last, this], 100, 101)
    assert [(m["season"], m["gw"], m["a_points"], m["b_points"], m["winner"]) for m in r["meetings"]] == [
        ("2026-27", 1, 60, 40, "a"), ("2026-27", 9, 50, 50, None), ("2027-28", 3, 55, 70, "b")]
    assert r["record"] == {"a_won": 1, "b_won": 1, "drawn": 1, "a_points": 165, "b_points": 160}


def test_rivalry_with_no_meetings_is_empty():
    r = app.league_rivalry([season("2026-27", (1, 100, 60, 102, 40))], 100, 101)
    assert r["meetings"] == [] and r["record"]["a_won"] == 0


def test_rivalries_combine_saved_seasons_with_live_results(tmp_path):
    old = season("2025-26", (1, 100, 10, 101, 90))
    old["managers"] = [{"entry_id": 100, "team": "Old Name", "manager": "M 0"}]
    (tmp_path / "2025-26").mkdir()
    (tmp_path / "2025-26" / "league_5.json").write_text(json.dumps(old), encoding="utf-8")
    data = app.league_rivalries(banter_league(BANTER_WEEKS), 5, 100, 101, folder=tmp_path)
    assert data["seasons"] == ["2025-26", app.season_label()]
    assert [m["winner"] for m in data["rivalry"]["meetings"]] == ["b", "a"]    # last season, then this one
    assert {m["entry_id"]: m["team"] for m in data["managers"]}[100] == "Team 0"   # the newest name wins


def test_rivalries_live_results_replace_the_saved_copy_of_this_season(tmp_path):
    app.save_snapshot(5, snapshot_for(BANTER_WEEKS[:1]), folder=tmp_path)
    data = app.league_rivalries(banter_league(BANTER_WEEKS), 5, 100, 102, folder=tmp_path)
    assert data["seasons"] == [app.season_label()]                  # not listed twice
    assert len(data["rivalry"]["meetings"]) == 1                    # 100 v 102 is only in the live data


def test_rivalries_without_a_pair_only_list_the_managers(tmp_path):
    data = app.league_rivalries(banter_league(BANTER_WEEKS), 5, folder=tmp_path)
    assert data["rivalry"] is None and len(data["managers"]) == 5
    assert app.league_rivalries(banter_league(BANTER_WEEKS), 5, 100, 100, folder=tmp_path)["rivalry"] is None


def test_rivalries_route_returns_the_pair_history(fake_api, monkeypatch):
    fake = app.get_json

    def with_matches(url):
        return banter_league(BANTER_WEEKS) if url.endswith("/league/123/details") else fake(url)

    monkeypatch.setattr(app, "get_json", with_matches)
    data = app.app.test_client().get("/api/league/123/rivalries?a=100&b=101").get_json()
    assert data["league_name"] == "Banter League"
    assert data["rivalry"]["record"]["a_won"] == 1


def test_rivalries_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/rivalries?a=1&b=2")
    assert res.status_code == 400
    assert "No Draft league found" in res.get_json()["error"]


def test_league_hub_offers_rivalries_to_everyone():
    res = app.app.test_client().get("/league?league=123")
    page = res.data.decode()
    res.close()
    assert '<button class="choice" type="button" data-tab="rivalries"' in page      # not fullonly


def test_league_hub_address_never_carries_a_team_id():
    res = app.app.test_client().get("/league?league=123")
    page = res.data.decode()
    res.close()
    setter = page.split("function setAddress()")[1].split("\n}")[0]
    code = "\n".join(line for line in setter.splitlines() if not line.strip().startswith("//"))
    assert "team" not in code                    # a copied address has the league, tab and full: never a team
    assert 'params.get("team")' not in page      # and an old link's &team= can't make a team look like "you"


def test_main_page_links_to_the_hub_without_a_team_id():
    res = app.app.test_client().get("/")
    page = res.data.decode()
    res.close()
    assert '"/league?league=" + DATA.league_id + "&full=1"' in page


# ---------------------------------------------------------------- luck (league points vs playing everyone)

def luck_of(matches):
    return {m["entry_id"]: m for m in app.league_banter(h2h(matches))["luck"]}


def test_luck_compares_actual_points_with_playing_everyone():
    # scores 60, 55, 10 and 50: playing everyone would give 3, 2, 0 and 1 league points
    by_team = luck_of([match(1, 10, 60, 13, 50), match(1, 11, 55, 12, 10)])
    assert [by_team[t]["expected_points"] for t in (100, 101, 102, 103)] == [3, 2, 0, 1]
    assert [by_team[t]["luck"] for t in (100, 101, 102, 103)] == [0, 1, 0, -1]   # 101 got a weak opponent


def test_luck_counts_a_draw_as_one_point_against_that_opponent():
    by_team = luck_of([match(1, 10, 60, 11, 40), match(1, 12, 50, 13, 50)])
    assert by_team[102]["expected_points"] == pytest.approx(1.3, abs=0.05)   # beat 101, drew 103, lost to 100
    assert by_team[102]["luck"] == pytest.approx(-0.3, abs=0.05)             # the real draw gave only 1


def test_luck_adds_up_over_the_weeks_and_ignores_unfinished_matches():
    weeks = [match(1, 10, 60, 13, 50), match(1, 11, 55, 12, 10),
             match(2, 10, 10, 11, 80), match(2, 12, 70, 13, 20),
             match(3, 10, 0, 13, 0, finished=False)]
    by_team = luck_of(weeks)
    assert by_team[100]["expected_points"] == 3                    # 3 in week 1, 0 in week 2 (lowest score)
    assert by_team[101]["expected_points"] == 2 + 3                # 2, then the top score
    assert by_team[100]["luck"] == 0 and by_team[101]["luck"] == 1


def test_luck_skips_a_bye_week():
    by_team = luck_of([match(1, 10, 55, None, None), match(1, 11, 40, 12, 30)])
    assert by_team[100]["expected_points"] == 0 and by_team[100]["luck"] == 0   # no match to compare with
    assert by_team[101]["expected_points"] == 1.5                                # beat 30, lost to 55
    assert by_team[101]["luck"] == 1.5


def test_banter_route_includes_luck(fake_api, monkeypatch):
    fake = app.get_json

    def with_matches(url):
        return banter_league(BANTER_WEEKS) if url.endswith("/league/123/details") else fake(url)

    monkeypatch.setattr(app, "get_json", with_matches)
    luck = app.app.test_client().get("/api/league/123/banter").get_json()["banter"]["luck"]
    assert len(luck) == 5 and all({"team", "expected_points", "luck"} <= set(r) for r in luck)
    assert luck == sorted(luck, key=lambda r: r["luck"], reverse=True)       # luckiest first


def test_luck_facts_name_the_luckiest_and_unluckiest(monkeypatch, first_wording):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    # 100 scores well but meets 101, the top scorer, both weeks; 103 scores poorly but meets the bottom team
    weeks = [match(1, 10, 60, 11, 65), match(1, 12, 10, 13, 20),
             match(2, 10, 58, 11, 70), match(2, 12, 10, 13, 15)]
    facts = {f["kind"]: f["text"] for f in app.league_banter(h2h(weeks))["facts"]}
    assert facts["lucky"] == ("**Team 103** have 6 league points, but their scores only deserved 2. "
                              "Daylight robbery, and they're keeping the points.")
    assert facts["unlucky"] == ("**Team 100** have 0 league points, but their scores deserved 4. "
                                "Robbed by the fixture list, and nobody has even apologised.")


def test_the_table_place_luck_fact_steps_aside_for_the_same_team(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    # 100 is the unluckiest by both measures, so only the "play everyone" fact names them
    weeks = [match(1, 10, 60, 11, 65), match(1, 12, 10, 13, 20),
             match(2, 10, 58, 11, 70), match(2, 12, 10, 13, 15)]
    facts = {f["kind"]: f["text"] for f in app.league_banter(h2h(weeks))["facts"]}
    assert "unlucky" in facts and "luck" not in facts


def test_luck_facts_need_a_big_enough_gap(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    facts = facts_by_kind([match(1, 10, 60, 11, 40), match(1, 12, 55, 13, 50)])   # luck is only ever 0 or 1
    assert "lucky" not in facts and "unlucky" not in facts


def test_league_hub_has_a_luck_chart():
    res = app.app.test_client().get("/league?league=123")
    page = res.data.decode()
    res.close()
    assert 'id="luck"' in page and "function renderLuck" in page
    assert page.index('id="luckSection"') < page.index('id="panel-charts"')   # in Banter, not Charts


# ---------------------------------------------------------------- weekly recap

def game(finished=False, started=False, day=10, hour=15):
    """One game for the recap tests, on a day in October 2026: finished, or started (part-way through)."""
    return {"kickoff": app.parse_time(f"2026-10-{day:02d}T{hour:02d}:00:00Z"),
            "started": started or finished, "finished": finished}


def recap_league(matches=()):
    """A 4-team head-to-head league: league entries 10-13 are teams 100-103, named Team 0 to Team 3."""
    entries = [{"id": 10 + i, "entry_id": 100 + i, "entry_name": f"Team {i}",
                "player_first_name": "M", "player_last_name": str(i)} for i in range(4)]
    return {"league": {"name": "Recap League"}, "league_entries": entries,
            "matches": list(matches), "standings": []}


# GW1: Team 0 beats 1, Team 2 beats 3. GW2: Team 1 beats 2 (90-50), Team 3 beats 0 (70-60). That moves
# Team 1 from 4th to 1st and Team 2 from 2nd to 4th. GW3 hasn't been played: 0 v 1 and 2 v 3.
RECAP_WEEKS = [match(1, 10, 60, 11, 40), match(1, 12, 50, 13, 45),
               match(2, 11, 90, 12, 50), match(2, 13, 70, 10, 60),
               match(3, 10, 0, 11, 0, finished=False), match(3, 12, 0, 13, 0, finished=False)]

# what live_scores needs: club 1's game is over, club 2 is mid-game, club 3 hasn't kicked off
PLAYERS = {1: {"name": "Saka", "club": 1, "ppg": 6.0, "rating": 70.0},
           2: {"name": "Palmer", "club": 2, "ppg": 5.0, "rating": 40.0},
           3: {"name": "Salah", "club": 3, "ppg": 8.0, "rating": 80.0},
           4: {"name": "Bench Boy", "club": 3, "ppg": 2.0, "rating": 20.0}}
GAMES_BY_TEAM = {1: [game(True, day=10, hour=12)],
                 2: [{**game(started=True, day=10, hour=15), "opp": "LIV", "home": True, "difficulty": 4}],
                 3: [{**game(day=11, hour=14), "opp": "MCI", "home": False, "difficulty": 2}]}
STATS = {1: {"points": 12, "minutes": 90}, 2: {"points": 3, "minutes": 45},
         3: {"points": 0, "minutes": 0}, 4: {"points": 9, "minutes": 90}}


def live_picture():
    """Live scores for GW3: Team 0 leads Team 1 45-31 (2 players left against 5), Teams 2 and 3 are level."""
    def row(points, left, expected, star=None, top=(), coming=()):
        return {"points": points, "left": left, "playing": 0, "expected": expected, "star": star,
                "top": list(top), "coming": list(coming)}

    return {100: row(45, 2, 10.0, {"name": "Saka", "points": 14}),
            101: row(31, 5, 25.0, {"name": "Palmer", "points": 5}),
            102: row(20, 3, 15.0), 103: row(20, 3, 15.0)}


def final_facts(**changes):
    scores = live_picture()
    return {**app.recap_facts(recap_league(RECAP_WEEKS), 2, "final", None, scores), **changes}


# which games have been played

def test_progress_counts_games_and_finished_match_days():
    # Friday: one game, played. Saturday: three games, two played. Sunday: two games, not yet.
    games = [game(True, day=9, hour=20), game(True, day=10, hour=12), game(True, day=10, hour=15),
             game(day=10, hour=17), game(day=11, hour=14), game(day=11, hour=16)]
    assert app.gameweek_progress(games) == {"games": 6, "played": 3, "days": 3, "days_done": 1}


def test_progress_is_none_without_games():
    assert app.gameweek_progress([]) is None
    assert app.gameweek_progress(None) is None


@pytest.mark.parametrize("played,days_done,official,expected", [
    (0, 0, False, None),                          # nothing to say before a game has finished
    (1, 0, False, 0),                             # the first games of a day: an early look
    (3, 1, False, 1),
    (9, 4, False, 4),
    (10, 7, False, app.RECAP_MAX_LIVE_STAGES),    # a long gameweek stops adding recaps
    (10, 4, True, "final"),                       # the official results are in
])
def test_recap_stage(played, days_done, official, expected):
    progress = {"games": 10, "played": played, "days": 8, "days_done": days_done}
    assert app.recap_stage(progress, official) == expected


def test_recap_stage_without_fixtures_waits_for_the_official_results():
    assert app.recap_stage(None, False) is None
    assert app.recap_stage(None, True) == "final"


def test_a_lone_friday_game_gives_a_short_recap():
    # Friday's only game is done (1 of 10 games). That completes a match day, but it's still an early look.
    games = [game(True, day=9, hour=20)] + [game(day=10, hour=15) for _ in range(9)]
    progress = app.gameweek_progress(games)
    assert app.recap_stage(progress, False) == 1
    facts = app.recap_facts(recap_league(RECAP_WEEKS), 3, 1, progress, live_picture())
    assert facts["length"] == "short"
    assert facts["games"] == {"played": 1, "total": 10}


def test_recaps_are_full_length_once_enough_games_are_played():
    progress = {"games": 10, "played": 3, "days": 4, "days_done": 1}
    assert app.recap_facts(recap_league(RECAP_WEEKS), 3, 1, progress, live_picture())["length"] == "full"


def test_gameweek_games_reads_which_games_have_started_and_finished(monkeypatch):
    clubs = [{"id": i, "short_name": n, "name": n} for i, n in enumerate(["ARS", "CHE", "LIV", "MCI"], 1)]
    fixtures = [
        # full time, with the bonus points not confirmed yet
        {"event": 3, "team_h": 1, "team_a": 2, "kickoff_time": "2026-10-10T11:30:00Z",
         "started": True, "finished": False, "finished_provisional": True,
         "team_h_difficulty": 2, "team_a_difficulty": 4},
        {"event": 3, "team_h": 3, "team_a": 4, "kickoff_time": "2026-10-11T14:00:00Z",
         "started": False, "finished": False},
        {"event": 4, "team_h": 1, "team_a": 3, "kickoff_time": "2026-10-18T14:00:00Z"},
        {"event": None, "team_h": 2, "team_a": 4}]                                   # postponed
    monkeypatch.setattr(app, "get_json", lambda url: {"teams": clubs} if "bootstrap" in url else fixtures)
    games, by_team = app.gameweek_games(clubs, 3)
    assert [g["finished"] for g in games] == [True, False]
    assert [g["started"] for g in games] == [True, False]
    assert [g["finished"] for g in by_team[1] + by_team[2]] == [True, True]
    assert [g["finished"] for g in by_team[3] + by_team[4]] == [False, False]
    # each club also gets its opponent, whether it's at home and how hard the game is for it
    assert (by_team[1][0]["opp"], by_team[1][0]["home"], by_team[1][0]["difficulty"]) == ("CHE", True, 2)
    assert (by_team[2][0]["opp"], by_team[2][0]["home"], by_team[2][0]["difficulty"]) == ("ARS", False, 4)


def test_gameweek_games_is_none_when_the_fixtures_cant_be_read(monkeypatch):
    def down(url):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(app, "get_json", down)
    assert app.gameweek_games([{"id": 1, "short_name": "ARS"}], 3) is None


# live scores

def test_live_stats_reads_either_shape_of_feed():
    as_dict = {"elements": {"1": {"stats": {"total_points": 12, "minutes": 90}}, "2": {"stats": {}}}}
    as_list = {"elements": [{"id": 1, "stats": {"total_points": 12, "minutes": 90}}, {"id": 2, "stats": {}}]}
    expected = {1: {"points": 12, "minutes": 90}, 2: {"points": 0, "minutes": 0}}
    assert app.live_stats(as_dict) == expected
    assert app.live_stats(as_list) == expected


@pytest.mark.parametrize("junk", [None, [], "oops", {"elements": 5}, {"elements": {"x": {"stats": {}}}}])
def test_live_stats_gives_nothing_for_an_unexpected_feed(junk):
    assert app.live_stats(junk) == {}


def test_starting_eleven_is_places_1_to_11():
    picks = {"picks": [{"element": 100 + i, "position": i} for i in range(1, 16)]}
    assert app.starting_eleven(picks) == list(range(101, 112))
    assert app.starting_eleven(None) == [] and app.starting_eleven({"picks": None}) == []


def test_live_scores_add_up_the_starters_and_count_who_is_still_to_play():
    scores = app.live_scores({100: [1, 2, 3]}, STATS, PLAYERS, GAMES_BY_TEAM)
    # Saka's game is over (12). Palmer is half-way through his (3 so far, half his usual 5 to come).
    # Salah hasn't kicked off (his usual 8 to come). The bench player's 9 points don't count.
    assert scores[100]["points"] == 15 and scores[100]["left"] == 2 and scores[100]["playing"] == 1
    assert scores[100]["expected"] == 10.5 and scores[100]["star"] == {"name": "Saka", "points": 12}
    # carrying the side: the two highest scorers so far (Salah is on 0). Still to come, best rated first
    assert scores[100]["top"] == [{"name": "Saka", "points": 12}, {"name": "Palmer", "points": 3}]
    assert scores[100]["coming"] == [
        {"name": "Salah", "rating": 80.0, "opp": "MCI", "home": False, "difficulty": 2,
         "playing": False, "standout": True},
        {"name": "Palmer", "rating": 40.0, "opp": "LIV", "home": True, "difficulty": 4,
         "playing": True, "standout": False}]


@pytest.mark.parametrize("rating,difficulty,standout", [
    (65.0, 5, True),       # a big name whatever the fixture
    (64.9, 5, False),
    (50.0, 2, True),       # a good player with an easy fixture
    (49.9, 2, False),
    (50.0, 3, False),      # ... not an easy enough one
    (None, 2, False),      # no rating: nothing is claimed
])
def test_a_player_still_to_play_is_a_big_name_by_rating_or_an_easy_fixture(rating, difficulty, standout):
    players = {1: {"name": "X", "club": 1, "ppg": 5.0, "rating": rating}}
    games = {1: [{**game(day=11), "opp": "BHA", "home": True, "difficulty": difficulty}]}
    assert app.live_scores({100: [1]}, {}, players, games)[100]["coming"][0]["standout"] is standout


def test_a_player_whose_game_is_over_is_not_still_to_come():
    games = {1: [game(True, day=10)]}
    assert app.live_scores({100: [1]}, STATS, PLAYERS, games)[100]["coming"] == []


def test_live_scores_ignore_unknown_players_and_clubs_without_a_game():
    scores = app.live_scores({100: [1, 99]}, STATS, PLAYERS, {})
    assert scores[100]["points"] == 12
    assert scores[100]["left"] == 0            # no games listed, so nothing left to play


def test_a_player_with_a_double_gameweek_still_has_the_second_game_to_come():
    games = {1: [game(True, day=10), game(day=14)]}
    scores = app.live_scores({100: [1]}, STATS, PLAYERS, games)
    assert scores[100]["left"] == 1 and scores[100]["expected"] == 6.0


@pytest.mark.parametrize("args,label", [
    ((0, 3, 3, 9, 9), "level"),
    ((30, 0, 2, 0, 10), "all but over"),         # the side behind has nobody left to play
    ((2, 5, 5, 25, 25), "wide open"),
    ((8, 4, 4, 20, 20), "still alive"),
    ((12, 3, 3, 15, 15), "a long shot"),
    ((40, 1, 1, 5, 5), "needs a miracle"),
])
def test_outlook(args, label):
    assert app.outlook(*args) == label


def test_a_side_expected_to_score_more_than_the_gap_is_wide_open():
    # 4 behind, but they have four players to play (about 30 points) against one (about 4)
    assert app.outlook(4, 4, 1, 30, 4) == "wide open"


def test_the_longer_odds_are_the_less_likely():
    chances = [app.comeback_chance(m, 3, 3, 15, 15) for m in (0, 5, 10, 20, 40)]
    assert chances == sorted(chances, reverse=True) and chances[0] == 0.5


# matches and facts

def test_live_matches_show_who_leads_and_how_the_other_side_is_placed():
    first, second = app.recap_matches(recap_league(RECAP_WEEKS), 3, live_picture())
    assert (first["ahead"], first["behind"], first["margin"]) == ("Team 0", "Team 1", 14)
    assert first["leader"] == "a" and (first["a"]["left"], first["b"]["left"]) == (2, 5)
    assert first["outlook"] == "wide open"        # Team 1 has 5 players left to Team 0's 2
    assert (second["leader"], second["ahead"], second["outlook"]) == (None, None, "level")


def test_official_matches_are_the_finished_results_with_nobody_left_to_play():
    first, second = app.recap_matches(recap_league(RECAP_WEEKS), 2)
    assert (first["ahead"], first["behind"], first["margin"]) == ("Team 1", "Team 2", 40)
    assert (first["a"]["points"], first["b"]["points"]) == (90, 50)
    assert (second["ahead"], second["margin"]) == ("Team 3", 10)       # Team 3 was the second match's "a"
    assert first["outlook"] is None and first["a"]["left"] == 0
    assert app.recap_matches(recap_league(RECAP_WEEKS), 3) == []        # unfinished matches aren't results


def test_a_bye_is_left_out():
    assert app.recap_matches(recap_league([match(3, 10, 0, None, None)]), 3, live_picture()) == []


def test_final_facts_have_the_results_highlights_table_and_moves():
    facts = final_facts()
    assert facts["state"] == "final" and facts["length"] == "full" and facts["gameweek"] == 2
    assert facts["league"] == "Recap League"
    assert [m["ahead"] for m in facts["matches"]] == ["Team 1", "Team 3"]
    assert facts["highlights"]["biggest_win"] == {"winner": "Team 1", "loser": "Team 2", "winner_points": 90,
                                                  "loser_points": 50, "margin": 40}
    assert [(r["place"], r["team"]) for r in facts["table"]] == [(1, "Team 1"), (2, "Team 0"),
                                                                 (3, "Team 3"), (4, "Team 2")]
    assert facts["moves"] == [{"team": "Team 1", "from": 4, "to": 1}, {"team": "Team 2", "from": 2, "to": 4},
                              {"team": "Team 0", "from": 1, "to": 2}]     # biggest first; Team 3 stayed 3rd
    assert 0 < len(facts["season_notes"]) <= 3


def test_small_table_moves_can_be_left_out(monkeypatch):
    monkeypatch.setattr(app, "MOVE_MIN_PLACES", 2)
    assert [m["team"] for m in final_facts()["moves"]] == ["Team 1", "Team 2"]   # Team 0 moved one place


def test_a_final_recap_has_no_stars_and_no_next_up_even_with_live_scores():
    facts = final_facts()        # final_facts passes live scores in, which a final recap ignores
    assert facts["stars"] == [] and "next_up" not in facts


def test_live_facts_have_the_scoreboard_stars_and_highlights_but_no_moves():
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    facts = app.recap_facts(recap_league(RECAP_WEEKS), 3, 2, progress, live_picture())
    assert facts["state"] == "in progress" and facts["games"] == {"played": 6, "total": 10}
    assert [m["margin"] for m in facts["matches"]] == [14, 0]
    assert facts["stars"] == [{"player": "Saka", "points": 14, "team": "Team 0"}]    # Palmer's 5 isn't a star
    assert facts["moves"] == [] and facts["season_notes"] == []
    assert facts["highlights"] == {
        "biggest_lead": {"leader": "Team 0", "trailer": "Team 1", "leader_points": 45, "trailer_points": 31,
                         "margin": 14},
        "closest_match": {"level": ["Team 2", "Team 3"], "points": 20, "margin": 0},
        "highest_score": {"team": "Team 0", "points": 45}}
    assert facts["table"][0]["team"] == "Team 1"          # the official table, before this gameweek's results


def test_live_facts_without_live_scores_have_no_matches():
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    facts = app.recap_facts(recap_league(RECAP_WEEKS), 3, 2, progress, None)
    assert facts["matches"] == [] and facts["stars"] == []


def test_the_first_gameweek_has_no_table_yet():
    league = recap_league([match(1, 10, 0, 11, 0, finished=False), match(1, 12, 0, 13, 0, finished=False)])
    progress = {"games": 10, "played": 2, "days": 4, "days_done": 0}
    assert app.recap_facts(league, 1, 0, progress, live_picture())["table"] is None


def test_clean_name_strips_control_characters_and_long_names():
    assert app.clean_name("Team\nOne\x00") == "Team One"
    assert len(app.clean_name("x" * 100)) == 40
    assert app.clean_name(None) == ""


# what's driving a match that's still on

def coming(name, rating, standout=True, opp="BHA", home=True, difficulty=3, playing=False):
    return {"name": name, "rating": rating, "opp": opp, "home": home, "difficulty": difficulty,
            "playing": playing, "standout": standout}


def live_row(a_points, b_points, a_left=3, b_left=3, a_top=(), b_top=(), a_coming=(), b_coming=()):
    """A live match row between "A" and "B", as recap_matches gives it, for the story tests."""
    def side(team, points, left, top, to_come):
        return {"team": team, "points": points, "left": left, "top": list(top), "coming": list(to_come)}

    margin = abs(a_points - b_points)
    return {"a": side("A", a_points, a_left, a_top, a_coming),
            "b": side("B", b_points, b_left, b_top, b_coming), "margin": margin,
            "leader": None if margin == 0 else "a" if a_points > b_points else "b"}


def test_coming_text_gives_the_rating_the_fixture_and_whether_it_is_easy_or_live():
    assert app.coming_text(coming("Foden", 82.4, difficulty=2)) == (
        "Foden (rated 82, home v BHA, an easy fixture)")
    assert app.coming_text(coming("Salah", 71.0, opp="ARS", home=False, difficulty=4, playing=True)) == (
        "Salah (rated 71, away at ARS, playing now)")
    assert app.coming_text({"name": "X", "rating": 50.0, "opp": None, "home": None, "difficulty": None,
                            "playing": False}) == "X (rated 50)"


def test_the_story_names_who_is_carrying_and_the_big_names_still_to_come():
    m = live_row(45, 31, a_top=[{"name": "Saka", "points": 14}],
                 b_coming=[coming("Foden", 82.0, difficulty=2), coming("Palmer", 40.0, standout=False)])
    assert app.live_story(m) == ("Saka (14) is carrying **A**. "
                                 "**B** still have Foden (rated 82, home v BHA, an easy fixture) to play.")


def test_the_story_says_luck_when_the_side_behind_has_no_big_names_left():
    m = live_row(45, 31, a_top=[{"name": "Saka", "points": 14}],
                 b_coming=[coming("Palmer", 40.0, standout=False)])
    assert app.live_story(m) == (
        "Saka (14) is carrying **A**. No big names left for **B**, so they'll need luck.")


def test_the_story_says_the_odds_are_with_the_leader_when_the_other_side_has_nobody_left():
    m = live_row(60, 22, b_left=0, a_top=[{"name": "Saka", "points": 14}])
    assert app.live_story(m) == (
        "Saka (14) is carrying **A**. **B** have nobody left to play: the odds are with **A**.")


def test_a_small_score_is_not_called_carrying():
    m = live_row(20, 10, a_top=[{"name": "Saka", "points": 5}], b_coming=[coming("Foden", 82.0)])
    assert "carrying" not in app.live_story(m)                       # 5 is under STAR_MIN_POINTS


def test_a_level_match_mentions_each_sides_big_names_or_says_it_is_down_to_luck():
    both = live_row(20, 20, a_coming=[coming("Saka", 80.0)], b_coming=[coming("Foden", 82.0)])
    assert app.live_story(both) == ("**A** still have Saka (rated 80, home v BHA). "
                                    "**B** still have Foden (rated 82, home v BHA).")
    quiet = live_row(20, 20, a_coming=[coming("X", 40.0, standout=False)],
                     b_coming=[coming("Y", 41.0, standout=False)])
    assert app.live_story(quiet) == "Nothing between them and no big names to come, so it's down to luck."


def test_the_story_claims_nothing_about_luck_when_ratings_are_unknown():
    unknown = [coming("Foden", None, standout=False)]
    assert app.live_story(live_row(45, 31, b_coming=unknown)) is None
    assert app.live_story(live_row(20, 20, a_coming=unknown, b_coming=unknown)) is None
    assert app.live_story(live_row(45, 31, a_top=[{"name": "Saka", "points": 14}], b_coming=unknown)) == (
        "Saka (14) is carrying **A**.")


def test_live_matches_carry_each_sides_top_scorers_and_best_to_come():
    scores = live_picture()
    scores[101]["coming"] = [coming("Foden", 82.0)]
    first, second = app.recap_matches(recap_league(RECAP_WEEKS), 3, scores)
    assert first["b"]["coming"][0]["name"] == "Foden" and first["a"]["top"] == []
    assert first["story"] == "**Team 1** still have Foden (rated 82, home v BHA) to play."
    assert second["story"] is None


def test_a_final_match_has_no_story():
    assert all("story" not in m for m in final_facts()["matches"])


# highlights of a finished gameweek

def played(a, a_points, b, b_points):
    """One finished match as recap_matches gives it, for the highlights tests."""
    margin = abs(a_points - b_points)
    return {"a": {"team": a, "points": a_points}, "b": {"team": b, "points": b_points}, "margin": margin,
            "leader": None if margin == 0 else "a" if a_points > b_points else "b"}


def test_highlights_pick_the_biggest_win_the_closest_match_and_the_best_and_worst_scores():
    # A beat B by 40 and E beat F by 40 (the first wins the tie); C beat D by 1, listed as the second team
    found = app.recap_highlights(
        [played("A", 90, "B", 50), played("D", 70, "C", 71), played("E", 60, "F", 20)])
    assert found["biggest_win"] == {"winner": "A", "loser": "B", "winner_points": 90,
                                    "loser_points": 50, "margin": 40}
    assert found["closest_match"] == {"winner": "C", "loser": "D", "winner_points": 71,
                                      "loser_points": 70, "margin": 1}
    assert found["highest_score"] == {"team": "A", "points": 90}
    assert found["lowest_score"] == {"team": "F", "points": 20}


def test_highlights_for_a_single_match_are_just_the_win():
    found = app.recap_highlights([played("A", 60, "B", 55)])
    assert found["biggest_win"]["margin"] == 5
    assert found["closest_match"] is None and found["highest_score"] is None and found["lowest_score"] is None


def test_a_draw_can_be_the_closest_match():
    found = app.recap_highlights([played("A", 80, "B", 50), played("C", 60, "D", 60)])
    assert found["closest_match"] == {"draw": ["C", "D"], "points": 60, "margin": 0}


def test_highlights_when_every_match_is_a_draw_have_no_biggest_win():
    found = app.recap_highlights([played("A", 60, "B", 60), played("C", 55, "D", 55)])
    assert found["biggest_win"] is None
    assert found["closest_match"]["draw"] == ["A", "B"]


def test_highlights_leave_out_highest_and_lowest_when_every_score_is_the_same():
    found = app.recap_highlights([played("A", 50, "B", 50), played("C", 50, "D", 50)])
    assert found["highest_score"] is None and found["lowest_score"] is None


def test_highlights_with_no_matches_are_empty():
    assert set(app.recap_highlights([]).values()) == {None}


def test_live_highlights_pick_the_biggest_lead_the_closest_match_and_the_highest_score_so_far():
    found = app.recap_highlights(
        [played("A", 45, "B", 31), played("D", 20, "C", 19), played("E", 10, "F", 40)], live=True)
    assert found["biggest_lead"] == {"leader": "F", "trailer": "E", "leader_points": 40, "trailer_points": 10,
                                     "margin": 30}
    assert found["closest_match"] == {"leader": "D", "trailer": "C", "leader_points": 20,
                                      "trailer_points": 19, "margin": 1}
    assert found["highest_score"] == {"team": "A", "points": 45}
    assert "lowest_score" not in found and "biggest_win" not in found   # a low score may just be unplayed


def test_live_highlights_call_a_level_match_level_and_need_a_lead_for_the_biggest_lead():
    found = app.recap_highlights([played("A", 20, "B", 20), played("C", 15, "D", 15)], live=True)
    assert found["biggest_lead"] is None
    assert found["closest_match"] == {"level": ["A", "B"], "points": 20, "margin": 0}
    assert found["highest_score"] == {"team": "A", "points": 20}


def test_live_highlights_for_one_match_or_none():
    found = app.recap_highlights([played("A", 20, "B", 5)], live=True)
    assert found["biggest_lead"]["margin"] == 15
    assert found["closest_match"] is None and found["highest_score"] is None
    assert set(app.recap_highlights([], live=True).values()) == {None}


# what made a match worth talking about

def story_match(a_points, b_points):
    """A finished match between teams 100 ("A") and 101 ("B"), in the shape recap_matches gives."""
    margin = abs(a_points - b_points)
    return {"a": {"entry_id": 100, "team": "A", "points": a_points},
            "b": {"entry_id": 101, "team": "B", "points": b_points}, "margin": margin,
            "leader": None if margin == 0 else "a" if a_points > b_points else "b"}


def story(a_points, b_points, **extra):
    return app.add_stories([story_match(a_points, b_points)], **extra)[0]


@pytest.mark.parametrize("a_points,b_points,tags", [
    (60, 40, ["Stomping"]),          # won by exactly 20
    (59, 40, []),
    (45, 40, ["Nail-biter"]),        # won by exactly 5
    (46, 40, []),
    (40, 70, ["Stomping"]),          # the second team can win it too
    (50, 50, ["Draw"]),
])
def test_tags_for_stompings_nail_biters_and_draws(a_points, b_points, tags):
    assert story(a_points, b_points)["tags"] == tags


def test_a_winner_far_below_in_the_table_is_an_upset():
    m = story(50, 40, places={100: 9, 101: 6})
    assert (m["tags"], m["basis"]) == (["Upset"], ["9th in the table beat 6th"])
    assert (m["a"]["place"], m["b"]["place"]) == (9, 6)
    assert story(50, 40, places={100: 8, 101: 6})["tags"] == []              # only two places apart


def test_a_winner_with_a_weaker_squad_is_an_upset():
    m = story(50, 40, strength={100: 50.0, 101: 52.0})
    assert (m["tags"], m["basis"]) == (["Upset"], ["weaker squad on paper (50.0 v 52.0)"])
    assert story(50, 40, strength={100: 50.1, 101: 52.0})["tags"] == []      # not weaker enough


def test_an_upset_can_have_both_reasons_and_still_be_a_stomping():
    m = story(80, 40, places={100: 9, 101: 2}, strength={100: 48.0, 101: 55.5})
    assert m["tags"] == ["Upset", "Stomping"]
    assert m["basis"] == ["9th in the table beat 2nd", "weaker squad on paper (48.0 v 55.5)"]


def test_the_favourite_winning_is_not_an_upset_and_neither_is_a_draw():
    assert story(50, 40, places={100: 2, 101: 9}, strength={100: 60.0, 101: 40.0})["tags"] == []
    assert story(50, 50, places={100: 9, 101: 2}, strength={100: 40.0, 101: 60.0})["tags"] == ["Draw"]


def test_without_a_table_or_squads_only_the_margin_counts():
    m = story(50, 40)                                      # e.g. the first gameweek: no table yet
    assert (m["tags"], m["basis"], m["players"]) == ([], [], None)
    assert (m["a"]["place"], m["a"]["strength"]) == (None, None)


PLAYERS_SEEN = {1: {"name": "Saka"}, 2: {"name": "Palmer"}, 3: {"name": "Salah"}, 4: {"name": "Foden"},
                5: {"name": "Bench"}}


def test_team_players_name_the_top_two_and_a_flop():
    stats = {1: {"points": 14, "minutes": 90}, 2: {"points": 11, "minutes": 90},
             3: {"points": 6, "minutes": 90}, 4: {"points": -2, "minutes": 60},
             5: {"points": 0, "minutes": 0}}
    found = app.team_players({100: [1, 2, 3, 4, 5]}, stats, PLAYERS_SEEN)
    assert found[100] == {"top": [{"name": "Saka", "points": 14}, {"name": "Palmer", "points": 11}],
                          "flop": {"name": "Foden", "points": -2}}      # the bench player never played


def test_team_players_without_a_flop_or_with_nobody_scoring():
    stats = {1: {"points": 5, "minutes": 90}, 2: {"points": 0, "minutes": 0}}
    found = app.team_players({100: [1, 2, 99]}, stats, PLAYERS_SEEN)    # 99 isn't a known player
    assert found[100] == {"top": [{"name": "Saka", "points": 5}], "flop": None}
    assert app.team_players({100: [2]}, {}, PLAYERS_SEEN)[100] == {"top": [], "flop": None}


def scorers_for(top_a=(), top_b=(), flop_b=None):
    def top(rows):
        return [{"name": n, "points": p} for n, p in rows]

    return {100: {"top": top(top_a), "flop": None},
            101: {"top": top(top_b), "flop": {"name": flop_b[0], "points": flop_b[1]} if flop_b else None}}


def test_players_line_names_the_winners_top_scorers_and_the_losers_best_and_flop():
    scorers = scorers_for([("Saka", 14), ("Palmer", 11)], [("Haaland", 6)], ("Foden", -2))
    assert story(70, 50, scorers=scorers)["players"] == (
        "Saka (14) and Palmer (11) led **A**. **B**'s best was Haaland (6); Foden (-2) flopped.")


def test_players_line_with_less_to_say():
    assert story(70, 50, scorers=scorers_for([("Saka", 14)]))["players"] == "Saka (14) led **A**."
    assert story(70, 50, scorers=scorers_for(flop_b=("Foden", 0)))["players"] == "Foden (0) flopped."
    same = scorers_for(top_b=[("Palmer", 1)], flop_b=("Palmer", 1))      # best and flop: say it once
    assert story(70, 50, scorers=same)["players"] == "**B**'s best was Palmer (1)."
    assert story(70, 50, scorers=scorers_for())["players"] is None
    assert story(70, 50, scorers=None)["players"] is None


def test_players_line_for_a_draw_gives_each_sides_best():
    scorers = scorers_for([("Saka", 12)], [("Haaland", 11)])
    assert story(50, 50, scorers=scorers)["players"] == (
        "**A**'s best was Saka (12). **B**'s best was Haaland (11).")


def test_final_facts_use_the_table_going_into_the_gameweek_for_upsets(monkeypatch):
    monkeypatch.setattr(app, "UPSET_MIN_PLACES", 2)
    strength = {100: 50.0, 101: 50.0, 102: 50.0, 103: 50.0}
    facts = app.recap_facts(recap_league(RECAP_WEEKS), 2, "final", None, None, strength, None)
    # going into GW2 the table was Team 0, 2, 3, 1: so Team 1 (4th) beat Team 2 (2nd)
    # and Team 3 (3rd) beat Team 0 (1st)
    first, second = facts["matches"]
    assert (first["tags"], first["basis"]) == (["Upset", "Stomping"], ["4th in the table beat 2nd"])
    assert (second["tags"], second["basis"]) == (["Upset"], ["3rd in the table beat 1st"])
    assert first["a"]["strength"] == 50.0 and first["players"] is None


def test_a_live_recap_has_no_stories():
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    facts = app.recap_facts(recap_league(RECAP_WEEKS), 3, 2, progress, live_picture())
    assert all("tags" not in m for m in facts["matches"])


# the plain recap

def test_plain_recap_for_a_finished_gameweek_tells_the_story_without_listing_every_result():
    text = app.plain_recap(final_facts())
    assert text == ("Gameweek 2 is done.\n"
                    "Biggest win: **Team 1** beat **Team 2** 90-50, by 40 points.\n"
                    "Closest match: **Team 3** beat **Team 0** 70-60.\n"
                    "Highest score: **Team 1** with 90. Lowest: **Team 2** with 50.")   # one point a line
    assert "Table moves" not in text and "Best performances" not in text and "Next up" not in text


def final_text(*matches):
    return app.plain_recap(app.recap_facts(recap_league(matches), 1, "final"))


def test_plain_recap_for_a_gameweek_with_one_match():
    assert final_text(match(1, 10, 60, 11, 55)) == (
        "Gameweek 1 is done.\nBiggest win: **Team 0** beat **Team 1** 60-55, by 5 points.")
    assert final_text(match(1, 10, 56, 11, 55)).endswith("by 1 point.")


def test_plain_recap_when_the_closest_match_was_a_draw():
    text = final_text(match(1, 10, 80, 11, 50), match(1, 12, 60, 13, 60))
    assert text == ("Gameweek 1 is done.\n"
                    "Biggest win: **Team 0** beat **Team 1** 80-50, by 30 points.\n"
                    "Closest match: **Team 2** and **Team 3** drew 60-60.\n"
                    "Highest score: **Team 0** with 80. Lowest: **Team 1** with 50.")


def test_plain_recap_with_no_results_just_says_the_gameweek_is_done():
    assert final_text(match(1, 10, 0, 11, 0, finished=False)) == "Gameweek 1 is done."


def test_plain_recap_for_an_early_look_is_short():
    progress = {"games": 10, "played": 1, "days": 4, "days_done": 0}
    facts = app.recap_facts(recap_league(RECAP_WEEKS), 3, 0, progress, live_picture())
    text = app.plain_recap(facts)
    assert facts["length"] == "short"
    assert text == ("1 of 10 games played in gameweek 3.\n"
                    "Biggest lead: **Team 0** lead **Team 1** 45-31, by 14 points.")   # just the one thing


def test_plain_recap_while_the_gameweek_is_on_gives_the_talking_points_so_far():
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    text = app.plain_recap(app.recap_facts(recap_league(RECAP_WEEKS), 3, 2, progress, live_picture()))
    assert text.split("\n") == [
        "6 of 10 games played in gameweek 3.",
        "Biggest lead: **Team 0** lead **Team 1** 45-31, by 14 points.",
        "Closest: **Team 2** and **Team 3** are level on 20.",
        "Highest score so far: **Team 0** with 45."]


def test_the_live_text_leaves_each_matchs_outlook_and_story_to_the_scoreboard():
    scores = live_picture()
    scores[100]["top"] = [{"name": "Saka", "points": 14}]
    scores[101]["coming"] = [coming("Foden", 82.0, difficulty=2)]
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    facts = app.recap_facts(recap_league(RECAP_WEEKS), 3, 2, progress, scores)
    text = app.plain_recap(facts)
    assert "wide open" not in text and "Saka" not in text and "Foden" not in text
    assert "Saka (14) is carrying **Team 0**" in facts["matches"][0]["story"]   # the scoreboard has them
    assert facts["matches"][0]["outlook"] == "wide open"


def test_plain_recap_for_a_live_gameweek_with_a_single_match_ahead():
    scores = live_picture()
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    facts = app.recap_facts(recap_league(RECAP_WEEKS[:4] + [match(3, 10, 0, 11, 0, finished=False)]), 3, 2,
                            progress, {100: scores[100], 101: scores[101]})
    assert app.plain_recap(facts) == ("6 of 10 games played in gameweek 3.\n"
                                      "Biggest lead: **Team 0** lead **Team 1** 45-31, by 14 points.")


def test_plain_recap_says_so_when_the_live_scores_are_missing():
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    text = app.plain_recap(app.recap_facts(recap_league(RECAP_WEEKS), 3, 2, progress, None))
    assert text == "6 of 10 games played in gameweek 3. The live scores aren't available right now."


# what the AI is asked

def test_the_request_asks_for_the_right_length():
    assert "2 or 3 sentences" in app.recap_request(final_facts(length="short"))
    assert "2 or 3 short paragraphs" in app.recap_request(final_facts(length="full"))


def test_the_final_request_asks_for_the_story_and_not_the_lists_the_page_already_has():
    text = app.recap_request(final_facts())
    assert "The gameweek is over" in text and "don't go through them one by one" in text
    assert '"highlights"' in text and '"biggest_win"' in text and '"moves"' in text
    assert "star players" not in text and "next week" not in text and "next_up" not in text


def test_the_request_for_a_live_recap_explains_the_outlook_words():
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    live = app.recap_request(app.recap_facts(recap_league(RECAP_WEEKS), 3, 2, progress, live_picture()))
    assert "still being played" in live and "'needs a miracle'" in live and "auto-subs" in live
    assert "The gameweek is over" in app.recap_request(final_facts())


def test_the_live_request_asks_for_who_is_carrying_and_who_is_still_to_come():
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    text = app.recap_request(app.recap_facts(recap_league(RECAP_WEEKS), 3, 2, progress, live_picture()))
    assert "story line" in text and "carrying" in text and "luck" in text
    assert "repeat those lines" in text and "two or three most interesting matches" in text
    assert '"highlights"' in text and '"biggest_lead"' in text and '"highest_score"' in text
    assert "lowest_score" not in text and "Only mention players that are in the facts" in text


def test_the_request_carries_the_facts_but_leaves_out_empty_ones():
    progress = {"games": 10, "played": 6, "days": 4, "days_done": 2}
    facts = app.recap_facts(recap_league(RECAP_WEEKS), 3, 2, progress, live_picture())
    text = app.recap_request(facts)
    assert '"Team 0"' in text and '"margin": 14' in text and '"outlook": "wide open"' in text
    assert '"moves"' not in text and '"next_up"' not in text


# the AI call

class FakeAI:
    """Stands in for the Anthropic client: records each request and replies with what it was given."""

    def __init__(self, text="Ha!", stop_reason="end_turn", error=None):
        self.calls, self.text, self.stop_reason, self.error = [], text, stop_reason, error
        self.messages = self   # so client.messages.create(...) lands in create()

    def create(self, **request):
        self.calls.append(request)
        if self.error:
            raise self.error
        block = type("Block", (), {"type": "text", "text": self.text})()
        return type("Reply", (), {"stop_reason": self.stop_reason, "content": [block]})()


def test_write_recap_sends_the_facts_to_the_model_named_in_the_environment(monkeypatch):
    monkeypatch.setenv("RECAP_MODEL", "test-model")
    ai = FakeAI("  A fine week.  ")
    facts = final_facts()
    assert app.write_recap(facts, ai) == "A fine week."
    request = ai.calls[0]
    assert request["model"] == "test-model" and request["system"] == app.RECAP_SYSTEM
    assert request["max_tokens"] == app.RECAP_MAX_TOKENS
    assert request["output_config"] == {"effort": app.RECAP_EFFORT}
    assert request["messages"] == [{"role": "user", "content": app.recap_request(facts)}]


def test_write_recap_does_nothing_without_a_model(monkeypatch):
    monkeypatch.delenv("RECAP_MODEL", raising=False)
    ai = FakeAI()
    assert app.write_recap(final_facts(), ai) is None and ai.calls == []


@pytest.mark.parametrize("ai", [
    FakeAI(error=anthropic.AnthropicError("down")),
    FakeAI(error=TypeError("Could not resolve authentication method")),   # the SDK's error when no key is set
    FakeAI(stop_reason="refusal"),
    FakeAI(stop_reason="max_tokens"),
    FakeAI(text="   "),
])
def test_write_recap_gives_up_quietly_when_the_ai_cant_deliver(monkeypatch, ai):
    monkeypatch.setenv("RECAP_MODEL", "test-model")
    assert app.write_recap(final_facts(), ai) is None


@pytest.fixture
def ai_recaps(monkeypatch):
    """The AI switched on for league 52607, with nothing written yet."""
    monkeypatch.setenv("RECAP_MODEL", "test-model")
    monkeypatch.setattr(app, "RECAP_LEAGUES", {52607})
    monkeypatch.setattr(app, "_recaps", {})
    monkeypatch.setattr(app, "_recap_failed", {})


def test_an_ai_recap_is_written_once_per_stage(ai_recaps):
    ai, facts = FakeAI("Words."), final_facts()
    first = app.get_recap(52607, facts, 1, now=1000, client=ai)
    assert first == ("Words.", "ai", 1000)
    assert app.get_recap(52607, facts, 1, now=2000, client=ai) == first       # kept: nobody pays twice
    assert len(ai.calls) == 1
    app.get_recap(52607, facts, 2, now=3000, client=ai)                       # a new stage is written afresh
    assert len(ai.calls) == 2


def test_other_leagues_get_the_plain_recap_for_free(ai_recaps):
    ai, facts = FakeAI(), final_facts()
    assert app.get_recap(999, facts, "final", client=ai) == (app.plain_recap(facts), "plain", None)
    assert ai.calls == []


def test_without_a_model_the_recap_is_plain(ai_recaps, monkeypatch):
    monkeypatch.delenv("RECAP_MODEL")
    ai, facts = FakeAI(), final_facts()
    assert app.get_recap(52607, facts, "final", client=ai)[1] == "plain" and ai.calls == []


def test_a_failed_ai_call_falls_back_and_waits_before_trying_again(ai_recaps):
    facts, broken = final_facts(), FakeAI(error=anthropic.AnthropicError("down"))
    assert app.get_recap(52607, facts, 1, now=1000, client=broken)[1] == "plain"
    app.get_recap(52607, facts, 1, now=1000 + app.RECAP_RETRY_SECONDS - 1, client=broken)
    assert len(broken.calls) == 1                                             # still waiting
    again = app.get_recap(52607, facts, 1, now=1000 + app.RECAP_RETRY_SECONDS, client=FakeAI("Back!"))
    assert again[:2] == ("Back!", "ai")


def test_only_the_latest_recaps_are_kept(ai_recaps, monkeypatch):
    monkeypatch.setattr(app, "RECAP_KEPT", 2)
    ai, facts = FakeAI(), final_facts()
    for stage in (0, 1, 2):
        app.get_recap(52607, facts, stage, now=stage, client=ai)
    assert list(app._recaps) == [(52607, 2, 1), (52607, 2, 2)]


# the recap route

RECAP_CLUBS = [{"id": i, "short_name": n, "name": n} for i, n in enumerate(["ARS", "CHE", "LIV", "MCI"], 1)]


@pytest.fixture
def recap_world(monkeypatch):
    """
    Fake Draft and classic sites for the recap route. The league is RECAP_WEEKS and it's gameweek 3:
    ARS v CHE (Saturday) is over, LIV v MCI (Sunday) hasn't kicked off. Each team starts two players
    (and has a bench player on 20 points, who must not count). Saka (ARS) has 8 points and Palmer (CHE) 3,
    so Team 0 leads Team 1 8-3. The returned dict can be changed before the first request,
    e.g. world["fixtures"] = None takes that feed down.
    """
    world = {
        "matches": list(RECAP_WEEKS),
        "fixtures": [
            {"event": 3, "team_h": 1, "team_a": 2, "kickoff_time": "2026-10-10T11:30:00Z",
             "started": True, "finished": True},
            {"event": 3, "team_h": 3, "team_a": 4, "kickoff_time": "2026-10-11T14:00:00Z",
             "started": False, "finished": False}],
        "live": {"elements": {"1": {"stats": {"total_points": 8, "minutes": 90}},
                              "2": {"stats": {"total_points": 3, "minutes": 90}},
                              "9": {"stats": {"total_points": 20, "minutes": 90}}}},
        "calls": [],     # every address asked for, so a test can check what a recap did and didn't need
        "owners": True,  # False takes the squads feed down
    }
    names = {1: "Saka", 2: "Palmer", 3: "Salah", 4: "Foden"}
    elements = [{**make_player(i, team=(i - 1) % 4 + 1), "web_name": names.get(i, f"Player{i}"),
                 "points_per_game": "5.0"} for i in range(1, 10)]
    starters = {100: [1, 3], 101: [2, 4], 102: [5, 7], 103: [6, 8]}
    owner_of = {pid: team_id for team_id, ids in starters.items() for pid in ids}

    def fake(url):
        world["calls"].append(url)
        if "fantasy" in url:                                              # the classic site
            if "bootstrap" in url:
                return {"teams": RECAP_CLUBS}
            if world["fixtures"] is None:
                raise requests.ConnectionError("down")
            return world["fixtures"]
        if url.endswith("/bootstrap-static"):
            return {"events": {"current": 3, "next": 4}, "teams": RECAP_CLUBS, "elements": elements,
                    "element_types": [{"id": 3, "singular_name_short": "MID"}]}
        if url.endswith("/element-status"):
            if not world["owners"]:
                raise requests.HTTPError(response=type("R", (), {"status_code": 503})())
            return {"element_status": [{"element": pid, "owner": owner_of.get(pid)} for pid in range(1, 10)]}
        if url.endswith("/details"):
            return recap_league(world["matches"])
        if "/live" in url:
            if world["live"] is None:
                raise requests.ConnectionError("down")
            return world["live"]
        if "/entry/" in url:
            team_id = int(url.split("/entry/")[1].split("/")[0])
            picks = [{"element": pid, "position": n} for n, pid in enumerate(starters[team_id], 1)]
            return {"picks": picks + [{"element": 9, "position": 12}]}
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(app, "get_json", fake)
    monkeypatch.setattr(app, "_recaps", {})
    monkeypatch.setattr(app, "_recap_failed", {})
    monkeypatch.delenv("RECAP_MODEL", raising=False)
    return world


def get_recap_page(league=123):
    return app.app.test_client().get(f"/api/league/{league}/recap").get_json()


def asked_for_live_data(world):
    """Did the recap ask the Draft site for live points or anyone's picks?"""
    return any("/live" in url or "/entry/" in url for url in world["calls"])


# ---------------------------------------------------------------- live scores on the match-up cards

def get_live(league=123):
    return app.app.test_client().get(f"/api/league/{league}/live").get_json()


def test_the_live_route_scores_the_six_managers_on_the_cards(recap_world):
    data = get_live()
    assert data["live"]["gw"] == 3
    assert data["live"]["scores"] == {                  # no bench points, same sums as the recap
        "100": {"points": 8, "left": 1, "playing": 0}, "101": {"points": 3, "left": 1, "playing": 0},
        "102": {"points": 0, "left": 1, "playing": 0}, "103": {"points": 0, "left": 1, "playing": 0}}


def test_the_live_route_only_asks_for_the_managers_on_the_cards(recap_world):
    recap_world["matches"] = RECAP_WEEKS[:4] + [match(3, 10, 0, 11, 0, finished=False)]   # one match
    get_live()
    picks = [url for url in recap_world["calls"] if "/entry/" in url]
    assert {int(u.split("/entry/")[1].split("/")[0]) for u in picks} == {100, 101} and len(picks) == 2


def test_nothing_is_live_until_a_game_has_kicked_off(recap_world):
    for fixture in recap_world["fixtures"]:
        fixture["started"] = fixture["finished"] = False
    assert get_live() == {"league_id": 123, "live": None}
    assert not asked_for_live_data(recap_world)         # and it didn't bother the Draft site for scores


def test_nothing_is_live_when_a_feed_is_down(recap_world):
    recap_world["fixtures"] = None                      # which games have started is unknown
    assert get_live()["live"] is None
    recap_world["fixtures"] = [{"event": 3, "team_h": 1, "team_a": 2, "kickoff_time": "2026-10-10T11:30:00Z",
                                "started": True, "finished": True}]
    recap_world["live"] = None                          # the points feed is down
    assert get_live()["live"] is None


def test_nothing_is_live_for_a_league_with_no_cards(recap_world):
    recap_world["matches"] = []
    assert get_live()["live"] is None
    recap_world["matches"] = RECAP_WEEKS[:4]            # everything played: no match-ups to show
    assert get_live()["live"] is None


def test_the_live_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/live")
    assert res.status_code == 400 and "No Draft league found" in res.get_json()["error"]


def test_recap_route_shows_the_live_scoreboard_with_a_plain_recap(recap_world):
    data = get_recap_page()
    recap = data["recap"]
    assert data["league_name"] == "Recap League"
    assert (recap["gameweek"], recap["stage"], recap["label"]) == (3, 1, "After 1 of 2 games")
    assert recap["games"] == {"played": 1, "total": 2}
    assert (recap["written_by"], recap["written_at"]) == ("plain", None)
    first, second = recap["matches"]
    assert (first["a"]["points"], first["b"]["points"], first["leader"]) == (8, 3, "a")   # no bench points
    assert (first["a"]["left"], first["b"]["left"], first["outlook"]) == (1, 1, "still alive")
    assert (second["leader"], second["outlook"]) == (None, "level")
    assert recap["stars"] == [{"player": "Saka", "points": 8, "team": "Team 0"}]
    assert recap["text"].startswith(
        "1 of 2 games played in gameweek 3.\nBiggest lead: **Team 0** lead **Team 1** 8-3, by 5 points.")
    assert "still have" not in recap["text"]                        # the stories are on the scoreboard
    assert "Saka (8) is carrying **Team 0**. **Team 1** still have Foden (" in first["story"]


def test_recap_route_uses_the_ai_for_a_listed_league(recap_world, monkeypatch):
    monkeypatch.setenv("RECAP_MODEL", "test-model")
    monkeypatch.setattr(app, "write_recap", lambda facts, client=None: "AI words.")
    recap = get_recap_page(52607)["recap"]
    assert (recap["text"], recap["written_by"]) == ("AI words.", "ai")
    assert recap["written_at"].endswith("+00:00")
    assert get_recap_page(123)["recap"]["written_by"] == "plain"        # not a listed league


def test_recap_route_gives_the_final_recap_once_the_official_results_are_in(recap_world):
    recap_world["matches"] = RECAP_WEEKS[:4] + [match(3, 10, 70, 11, 60), match(3, 12, 50, 13, 40)]
    recap = get_recap_page()["recap"]
    assert (recap["gameweek"], recap["stage"], recap["label"]) == (3, "final", "Final recap")
    assert [(m["a"]["points"], m["b"]["points"]) for m in recap["matches"]] == [(70, 60), (50, 40)]
    assert recap["text"].startswith("Gameweek 3 is done.\nBiggest win: ")
    assert recap["stars"] == []                                    # a final recap has no stars
    # Teams 1 and 2 moved two places each; of the two one-place movers only three moves are listed in all
    assert {m["team"] for m in recap["moves"][:2]} == {"Team 1", "Team 2"} and len(recap["moves"]) == 3
    assert asked_for_live_data(recap_world)                        # who scored what: the live points


def test_recap_route_keeps_last_weeks_final_recap_until_a_game_finishes(recap_world):
    for fixture in recap_world["fixtures"]:
        fixture["finished"] = fixture["started"] = False
    recap = get_recap_page()["recap"]
    assert (recap["gameweek"], recap["label"]) == (2, "Final recap")
    assert recap["matches"][0]["a"]["points"] == 90


def test_recap_route_still_works_when_the_fixtures_feed_is_down(recap_world):
    recap_world["fixtures"] = None
    recap = get_recap_page()["recap"]
    assert (recap["gameweek"], recap["label"]) == (2, "Final recap")


def test_recap_route_tags_the_matches_and_names_the_players(recap_world):
    recap_world["matches"] = RECAP_WEEKS[:4] + [match(3, 10, 90, 11, 60), match(3, 12, 50, 13, 48)]
    first, second = get_recap_page()["recap"]["matches"]
    assert first["tags"] == ["Stomping"] and second["tags"] == ["Nail-biter"]
    # the bench player's 20 points aren't counted
    assert first["players"] == "Saka (8) led **Team 0**. **Team 1**'s best was Palmer (3)."
    assert second["players"] is None                                                  # nobody scored a point
    assert first["a"]["place"] == 2 and first["b"]["place"] == 1       # the table going into GW3
    assert isinstance(first["a"]["strength"], float)


def test_recap_route_final_still_tags_the_matches_when_the_live_feed_is_down(recap_world):
    recap_world["matches"] = RECAP_WEEKS[:4] + [match(3, 10, 90, 11, 60), match(3, 12, 50, 13, 48)]
    recap_world["live"] = None
    first, second = get_recap_page()["recap"]["matches"]
    assert first["tags"] == ["Stomping"] and second["tags"] == ["Nail-biter"]
    assert first["players"] is None and second["players"] is None


def test_recap_route_final_still_works_when_the_squads_feed_is_down(recap_world):
    recap_world["matches"] = RECAP_WEEKS[:4] + [match(3, 10, 90, 11, 60), match(3, 12, 50, 13, 48)]
    recap_world["owners"] = False
    first = get_recap_page()["recap"]["matches"][0]
    assert first["tags"] == ["Stomping"] and first["a"]["strength"] is None
    assert first["players"] == "Saka (8) led **Team 0**. **Team 1**'s best was Palmer (3)."


def test_recap_route_live_story_keeps_what_it_knows_when_ratings_are_unavailable(recap_world):
    recap_world["owners"] = False                       # load_league needs this feed, so no ratings
    first = get_recap_page()["recap"]["matches"][0]
    assert first["story"] == "Saka (8) is carrying **Team 0**."   # nothing about who's to come, or luck
    assert first["b"]["coming"][0]["rating"] is None


def test_recap_route_asks_for_live_data_while_the_gameweek_is_on(recap_world):
    get_recap_page()
    assert asked_for_live_data(recap_world)


def test_recap_route_writes_with_less_when_the_live_feed_is_down(recap_world):
    recap_world["live"] = None
    recap = get_recap_page()["recap"]
    assert recap["label"] == "After 1 of 2 games" and recap["matches"] == [] and recap["stars"] == []
    assert recap["text"] == "1 of 2 games played in gameweek 3. The live scores aren't available right now."


def test_recap_route_is_empty_before_any_game_has_finished(recap_world):
    recap_world["matches"] = [match(3, 10, 0, 11, 0, finished=False), match(3, 12, 0, 13, 0, finished=False)]
    for fixture in recap_world["fixtures"]:
        fixture["finished"] = fixture["started"] = False
    assert get_recap_page()["recap"] is None


def test_recap_route_is_empty_for_a_league_without_matches(recap_world):
    recap_world["matches"] = []
    assert get_recap_page()["recap"] is None


def test_recap_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/recap")
    assert res.status_code == 400
    assert "No Draft league found" in res.get_json()["error"]


def test_league_hub_offers_the_recap_to_everyone():
    res = app.app.test_client().get("/league?league=123")
    page = res.data.decode()
    res.close()
    assert '<button class="choice" type="button" data-tab="recap"' in page         # not fullonly
    assert 'id="panel-recap"' in page and "function renderRecap" in page


# ---------------------------------------------------------------- squads on the league page

def squad_data():
    """What load_league gives, cut down to what league_squads reads: two managers and a few players."""
    def player(pid, name, pos, score, owner, form=5.0, minutes=900):
        # the rating is half form, half fixtures, so the pieces are easy to add up
        return {"id": pid, "name": name, "pos": pos, "team": "ARS", "score": score, "owner": owner,
                "form": form, "ppg": 5.0, "minutes": minutes,
                "breakdown": {"form": score / 2, "fix": score / 2}}

    return {
        "view": "week", "windows": {"week": {"from": 7, "to": 11}, "season": {"from": 7, "to": 13}},
        "players": [player(1, "Raya", "GKP", 70.0, 100), player(2, "Saka", "MID", 80.0, 100, form=8.0),
                    player(3, "Gabriel", "DEF", 60.0, 100, form=2.0), player(4, "Haaland", "FWD", 90.0, 100),
                    player(5, "Odegaard", "MID", 55.0, 100, form=9.0, minutes=90),
                    player(6, "Bench", "DEF", 30.0, 100),
                    player(7, "Palmer", "MID", 75.0, 200), player(8, "Free", "FWD", 99.0, None)],
        "managers": [
            {"entry_id": 100, "team_name": "Weaker FC", "manager": "A B", "strength": 51.0,
             "formation": "1-2-1", "best_xi": [1, 2, 3, 4, 5]},
            {"entry_id": 200, "team_name": "Stronger FC", "manager": "C D", "strength": 60.0,
             "formation": "0-1-0", "best_xi": [7]},
            {"entry_id": 300, "team_name": "Empty FC", "manager": "E F", "strength": 0,
             "formation": "", "best_xi": []}]}


def test_squads_rank_managers_by_strength_with_their_eleven_by_position_and_their_bench():
    out = app.league_squads(squad_data())
    assert [m["team"] for m in out["managers"]] == ["Stronger FC", "Weaker FC", "Empty FC"]
    weaker = out["managers"][1]
    assert weaker["strength"] == 51.0 and weaker["formation"] == "1-2-1"
    assert [(p["name"], p["pos"]) for p in weaker["best_xi"]] == [
        ("Raya", "GKP"), ("Gabriel", "DEF"), ("Saka", "MID"), ("Odegaard", "MID"), ("Haaland", "FWD")]
    assert weaker["best_xi"][0] == {"name": "Raya", "pos": "GKP", "club": "ARS", "rating": 70.0,
                                    "form": 5.0, "ppg": 5.0, "heat": None}
    assert [p["name"] for p in weaker["bench"]] == ["Bench"]
    assert (out["view"], out["windows"]["week"]) == ("week", {"from": 7, "to": 11})


def test_squads_leave_out_players_nobody_owns_and_cope_with_an_empty_squad():
    out = app.league_squads(squad_data())
    everyone = [p["name"] for m in out["managers"] for p in m["best_xi"] + m["bench"]]
    assert "Free" not in everyone
    empty = out["managers"][2]
    assert empty["best_xi"] == [] and empty["bench"] == []


def test_squads_show_what_the_strength_is_made_of_and_how_it_compares_with_the_league():
    out = app.league_squads(squad_data())
    weaker, stronger, empty = out["managers"][1], out["managers"][0], out["managers"][2]
    assert weaker["pieces"]["form"] == 35.5 and weaker["pieces"]["fix"] == 35.5  # (70+80+60+90+55) / 5 / 2
    assert weaker["pieces"]["xgi"] == 0 and weaker["pieces"]["avail"] == 0       # none of it: 0, not missing
    assert stronger["pieces"]["form"] == 37.5
    # the league average is 55.5 strength and 36.5 form: the empty squad isn't counted in it
    assert weaker["vs_league"] == {"strength": -4.5, "pieces": {**{k: 0 for k in weaker["pieces"]},
                                                                "form": -1.0, "fix": -1.0}}
    assert (stronger["vs_league"]["strength"], stronger["vs_league"]["pieces"]["form"]) == (4.5, 1.0)
    assert empty["pieces"] == {} and empty["vs_league"] == {"strength": 0, "pieces": {}}


def test_squads_mark_players_running_hot_or_cold_but_not_ones_with_few_minutes():
    best = {p["name"]: p["heat"] for p in app.league_squads(squad_data())["managers"][1]["best_xi"]}
    assert best == {"Raya": None, "Gabriel": "cold", "Saka": "hot", "Odegaard": None, "Haaland": None}
    # Odegaard's form is 9.0 against 5.0 a game, but on 90 minutes that's one game: not a trend


@pytest.mark.parametrize("form,ppg,minutes,heat", [
    (7.3, 4.5, 900, "hot"),
    (2.0, 5.2, 900, "cold"),
    (6.0, 4.5, 900, "hot"),     # exactly the gap counts
    (4.1, 2.6, 900, "hot"),     # 4.1 - 2.6 is 1.4999999999999996 in floating point: still the gap
    (4.7, 3.3, 900, None),      # 1.4 is inside the gap
    (5.0, 5.0, 900, None),
    (9.0, 5.0, app.MIN_MINUTES - 1, None),   # too few minutes to say
    (9.0, 5.0, app.MIN_MINUTES, "hot"),
])
def test_form_heat(form, ppg, minutes, heat):
    assert app.form_heat({"form": form, "ppg": ppg, "minutes": minutes}) == heat


def test_squads_route_returns_the_ranking_for_either_view(two_managers):
    data = app.app.test_client().get("/api/league/123/squads").get_json()
    assert data["league_name"] == "Test League" and data["view"] == "week"
    assert {m["entry_id"] for m in data["managers"]} == {100, 200}
    assert data["managers"] == sorted(data["managers"], key=lambda m: -m["strength"])
    assert app.app.test_client().get("/api/league/123/squads?view=season").get_json()["view"] == "season"


def test_squads_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/squads")
    assert res.status_code == 400 and "No Draft league found" in res.get_json()["error"]


def test_league_hub_charts_page_has_the_squad_strength_chart_for_everyone():
    res = app.app.test_client().get("/league?league=123")
    page = res.data.decode()
    res.close()
    assert 'id="squadsSection"' in page and "function renderSquads" in page
    where = [page.index(f'id="{name}"') for name in ("panel-charts", "squadsSection", "panel-rivalries")]
    assert where == sorted(where)                              # inside the Charts page, before Rivalries
    assert 'data-view="season"' in page                       # the Next 5 / Until the break toggle
    assert "What the rating is made of" in page and "running ${p.heat}" in page


def test_league_hub_charts_page_draws_how_strength_has_moved():
    res = app.app.test_client().get("/league?league=123")
    page = res.data.decode()
    res.close()
    assert 'id="strengthChart"' in page and "function renderStrengthChart" in page   # strength over time
    assert page.index('id="strengthWrap"') < page.index('id="squads"')               # chart above the bars
    assert "function movedBlock" in page and "Since the end of GW" in page           # block in an opened row
    assert "m.movement" in page and "SQ.since" in page                               # change next to each bar


# ---------------------------------------------------------------- saved squad strength

def strength_snap(strength, day):
    """A one-manager, one-view strength snapshot built by hand, with a number to change."""
    data = {"current_gw": 6, "next_gw": 7, "windows": {"week": {"from": 7, "to": 11}},
            "players": [{"id": 1, "name": "Raya", "score": strength, "breakdown": {"form": strength}}],
            "managers": [{"entry_id": 100, "team_name": "My FC", "manager": "Sam Lee",
                          "strength": strength, "formation": "0-0-0", "best_xi": [1]}]}
    return app.strength_snapshot({"week": data}, today=day)


def test_strength_snapshot_records_both_views_for_every_manager(two_managers):
    by_view = {view: app.load_league(123, view) for view in app.VIEWS}
    snap = app.strength_snapshot(by_view, today=datetime(2026, 10, 12))
    assert (snap["season"], snap["date"]) == ("2026-27", "2026-10-12")
    assert (snap["current_gw"], snap["next_gw"]) == (6, 7)
    assert snap["windows"] == by_view["week"]["windows"] and set(snap["windows"]) == {"week", "season"}
    assert [m["entry_id"] for m in snap["managers"]] == [100, 200]
    names = {p["id"]: p["name"] for p in by_view["week"]["players"]}
    for m in snap["managers"]:
        for view in app.VIEWS:
            mine = next(x for x in by_view[view]["managers"] if x["entry_id"] == m["entry_id"])
            score = {p["id"]: p["score"] for p in by_view[view]["players"]}
            assert m[view]["strength"] == mine["strength"] and m[view]["formation"] == mine["formation"]
            assert m[view]["xi"] == [[i, score[i]] for i in mine["best_xi"]]
            assert abs(sum(m[view]["pieces"].values()) - m[view]["strength"]) <= 0.5   # the pieces add up
            for i in mine["best_xi"]:
                assert snap["players"][str(i)] == names[i]


def test_a_strength_snapshot_is_the_same_after_a_round_trip_through_json(two_managers):
    # save_strength tells a quiet day apart by comparing with what it read back from the file
    by_view = {v: app.load_league(123, v) for v in app.VIEWS}
    snap = app.strength_snapshot(by_view, today=datetime(2026, 10, 12))
    assert json.loads(json.dumps(snap)) == snap


def test_save_strength_adds_one_line_per_date_and_each_season_gets_its_own_file(tmp_path):
    path = app.save_strength(5, strength_snap(60.0, datetime(2026, 10, 12)), folder=tmp_path)
    assert path == tmp_path / "2026-27" / "strength_5.json"
    app.save_strength(5, strength_snap(62.5, datetime(2026, 10, 13)), folder=tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert (saved["league_id"], saved["season"]) == (5, "2026-27")
    assert [e["date"] for e in saved["snapshots"]] == ["2026-10-12", "2026-10-13"]
    assert saved["snapshots"][1]["managers"][0]["week"]["strength"] == 62.5
    assert "season" not in saved["snapshots"][0]                      # the file's header says it once
    assert len(path.read_text(encoding="utf-8").splitlines()) == 4    # header, two entries, the end
    later = app.save_strength(5, strength_snap(60.0, datetime(2027, 8, 1)), folder=tmp_path)
    assert later == tmp_path / "2027-28" / "strength_5.json"


def test_save_strength_skips_a_day_where_nothing_changed(tmp_path):
    path = app.save_strength(5, strength_snap(60.0, datetime(2026, 10, 12)), folder=tmp_path)
    before = path.read_text(encoding="utf-8")
    assert app.save_strength(5, strength_snap(60.0, datetime(2026, 10, 13)), folder=tmp_path) is None
    assert path.read_text(encoding="utf-8") == before


def test_save_strength_replaces_a_dates_entry_when_the_numbers_changed(tmp_path):
    app.save_strength(5, strength_snap(60.0, datetime(2026, 10, 12)), folder=tmp_path)
    path = app.save_strength(5, strength_snap(61.0, datetime(2026, 10, 12)), folder=tmp_path)
    entries = json.loads(path.read_text(encoding="utf-8"))["snapshots"]
    assert [(e["date"], e["managers"][0]["week"]["strength"]) for e in entries] == [("2026-10-12", 61.0)]


def test_save_strength_never_writes_over_a_file_it_cannot_read(tmp_path):
    path = app.strength_path(5, "2026-27", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError):
        app.save_strength(5, strength_snap(60.0, datetime(2026, 10, 12)), folder=tmp_path)
    assert path.read_text(encoding="utf-8") == "{oops"


def test_the_snapshot_script_still_keeps_the_results_when_strengths_cant_be_loaded(monkeypatch, capsys):
    def blocked(league_id, view):
        raise app.FplError("The Draft site is blocking us (403).")

    monkeypatch.setattr(app, "load_league", blocked)
    save_snapshot.save_strength(52607)           # prints a warning and carries on, it doesn't raise
    assert "Couldn't save the squad strengths" in capsys.readouterr().out


# ---------------------------------------------------------------- how squad strength has moved

def saved_day(gw, date, *managers, view="week", names=None):
    """
    A saved strength snapshot as read back from the file. Each manager is (entry_id, strength, xi),
    xi being [[player id, rating], ...]; the pieces are half form, half fixtures, like squad_data's.
    """
    def entry(eid, strength, xi):
        half = round(sum(r for _, r in xi) / len(xi) / 2, 1) if xi else 0
        return {"entry_id": eid, "team": f"Team {eid}", "manager": "A B",
                view: {"strength": strength, "formation": "0-0-0", "xi": xi,
                       "pieces": {"form": half, "fix": half}}}

    return {"date": date, "current_gw": gw, "next_gw": gw + 1, "windows": {},
            "managers": [entry(*m) for m in managers],
            "players": {"1": "Raya", "2": "Saka", "3": "Gabriel", "4": "Haaland"} if names is None else names}


def live_data(strength, scores, xi, view="week", gw=7):
    """What load_league gives, cut down to what strength_moves reads: one manager (100) with players."""
    names = {1: "Raya", 2: "Saka", 3: "Gabriel", 4: "Haaland"}
    return {"view": view, "current_gw": gw,
            "players": [{"id": i, "name": names[i], "score": s, "breakdown": {"form": s / 2, "fix": s / 2}}
                        for i, s in scores.items()],
            "managers": [{"entry_id": 100, "team_name": "Team 100", "manager": "A B",
                          "strength": strength, "best_xi": xi}]}


# last gameweek Raya 70, Saka 60 and Gabriel 50 (strength 60.0); now Raya is up 6, Saka down 2, and
# Haaland (80) has come in for Gabriel
BEFORE = saved_day(6, "2026-10-16", (100, 60.0, [[1, 70.0], [2, 60.0], [3, 50.0]]))
NOW = live_data(71.3, {1: 76.0, 2: 58.0, 3: 49.0, 4: 80.0}, [1, 2, 4])


def test_strength_baseline_is_the_last_snapshot_saved_before_this_gameweek():
    days = [saved_day(5, "2026-10-09"), saved_day(6, "2026-10-14"), saved_day(6, "2026-10-16"),
            saved_day(7, "2026-10-17")]
    assert app.strength_baseline(days, 7)["date"] == "2026-10-16"     # the end of GW6, not the start of GW7
    assert app.strength_baseline(days, 8)["date"] == "2026-10-17"
    assert app.strength_baseline(days, 5) is None                      # nothing from before GW5
    assert app.strength_baseline(days[1:3], 6) is None                 # only this gameweek so far
    assert app.strength_baseline([], 7) is None


def test_the_change_in_strength_splits_into_same_players_and_different_players():
    move = app.strength_moves(NOW, BEFORE)[100]
    assert (move["from"], move["to"], move["change"]) == (60.0, 71.3, 11.3)
    # Raya +6 and Saka -2 are 4 rating points over 3 players; Haaland (80) for Gabriel (50) is 30 over 3
    assert move["same_players"] == 1.3 and move["new_players"] == 10.0
    assert move["same_players"] + move["new_players"] == move["change"]
    assert move["came_in"] == [{"name": "Haaland", "rating": 80.0}]
    assert move["went_out"] == [{"name": "Gabriel", "rating": 50.0}]      # his rating then, from the file
    assert move["riser"] == {"name": "Raya", "change": 6.0}
    assert move["faller"] == {"name": "Saka", "change": -2.0}


def test_strength_moves_say_how_the_pieces_of_the_strength_changed():
    pieces = app.strength_moves(NOW, BEFORE)[100]["pieces"]
    # form and fixtures were 30.0 each; now (76 + 58 + 80) / 3 / 2 = 35.7 each
    assert pieces["form"] == pieces["fix"] == 5.7
    assert pieces["xgi"] == 0 and pieces["avail"] == 0                     # not missing: nothing moved


def test_with_the_same_eleven_nothing_came_in_and_small_moves_are_not_risers():
    # ratings wobble by less than MOVER_MIN_RATING and cancel out: nothing to report
    now = live_data(60.0, {1: 70.4, 2: 59.6, 3: 50.0}, [1, 2, 3])
    move = app.strength_moves(now, BEFORE)[100]
    assert move["came_in"] == [] and move["went_out"] == []
    assert move["riser"] is None and move["faller"] is None
    shown = [str(move[k]) for k in ("change", "same_players", "new_players")]
    assert shown == ["0.0", "0.0", "0.0"]                                  # never -0.0


def test_a_player_the_file_has_no_name_for_is_still_listed_as_gone():
    before = saved_day(6, "2026-10-16", (100, 60.0, [[1, 70.0], [2, 60.0], [3, 50.0]]), names={})
    assert app.strength_moves(NOW, before)[100]["went_out"] == [{"name": "A player", "rating": 50.0}]


def test_strength_moves_leave_out_a_manager_it_cannot_compare():
    baseline = saved_day(6, "2026-10-16", (100, 60.0, [[1, 70.0], [2, 60.0], [3, 50.0]]),
                         (300, 50.0, [[1, 50.0]]))
    assert set(app.strength_moves(NOW, baseline)) == {100}
    assert app.strength_moves(live_data(0, {1: 70.0}, []), baseline) == {}            # no eleven now
    assert app.strength_moves(live_data(70.0, {1: 70.0, 2: 70.0}, [1, 2]), baseline) == {}   # 2 players v 3
    assert app.strength_moves(live_data(60.0, {1: 70.0, 2: 60.0, 3: 50.0}, [1, 2, 3], view="season"),
                              baseline) == {}                                       # the file only has "week"


def day_for_chart(gw, date, strength_100, strength_200=None, view="week"):
    managers = [(100, strength_100, [[1, strength_100]])]
    if strength_200 is not None:
        managers.append((200, strength_200, [[2, strength_200]]))
    return saved_day(gw, date, *managers, view=view)


def test_the_strength_chart_has_every_saved_day_and_one_line_per_manager():
    days = [day_for_chart(6, "2026-10-14", 60.0, 50.0), day_for_chart(6, "2026-10-16", 61.0),
            day_for_chart(7, "2026-10-17", 62.0, 55.0)]
    data = {**live_data(62.0, {1: 62.0}, [1]), "managers": [
        {"entry_id": 100, "team_name": "Team 100"}, {"entry_id": 200, "team_name": "Team 200"},
        {"entry_id": 300, "team_name": "Never saved"}]}
    chart = app.strength_chart(data, days)
    assert chart["days"] == [{"date": "2026-10-14", "gw": 6}, {"date": "2026-10-16", "gw": 6},
                             {"date": "2026-10-17", "gw": 7}]
    assert chart["lines"] == [{"entry_id": 100, "team": "Team 100", "values": [60.0, 61.0, 62.0]},
                              {"entry_id": 200, "team": "Team 200", "values": [50.0, None, 55.0]}]
    # a day a manager isn't in is None, and a manager who was never saved gets no line at all


def test_the_strength_chart_follows_the_chosen_view_and_needs_two_days():
    days = [day_for_chart(6, "2026-10-14", 60.0), day_for_chart(7, "2026-10-17", 65.0)]
    assert app.strength_chart(live_data(0, {}, [], view="season"), days)["lines"] == []   # no "season" saved
    assert app.strength_chart(live_data(0, {}, []), days[:1]) is None
    assert app.strength_chart(live_data(0, {}, []), []) is None


def test_strength_history_waits_for_a_snapshot_from_an_earlier_gameweek():
    days = [BEFORE, saved_day(7, "2026-10-17", (100, 65.0, [[1, 65.0]]))]
    history = app.strength_history(NOW, days)
    assert history["since"] == {"gw": 6, "date": "2026-10-16"}
    assert set(history["moves"]) == {100} and history["chart"] is not None
    early = app.strength_history(live_data(60.0, {1: 60.0}, [1], gw=6), days[:1])
    assert early == {"since": None, "moves": {}, "chart": None}


def test_saved_strengths_reads_this_seasons_file_and_copes_with_a_missing_or_broken_one(tmp_path):
    today = datetime(2026, 10, 12)
    assert app.saved_strengths(5, today, tmp_path) == []                    # no file yet
    app.save_strength(5, strength_snap(60.0, today), folder=tmp_path)
    app.save_strength(5, strength_snap(62.5, datetime(2026, 10, 13)), folder=tmp_path)
    days = app.saved_strengths(5, datetime(2026, 10, 14), tmp_path)
    assert [d["date"] for d in days] == ["2026-10-12", "2026-10-13"]
    assert app.saved_strengths(5, datetime(2027, 8, 1), tmp_path) == []     # a new season starts a new file
    app.strength_path(5, "2026-27", tmp_path).write_text("{oops", encoding="utf-8")
    assert app.saved_strengths(5, today, tmp_path) == []                    # the page just has no history


def test_squads_route_adds_movement_since_the_last_gameweek(two_managers, monkeypatch):
    by_view = {view: app.load_league(123, view) for view in app.VIEWS}
    earlier = {**app.strength_snapshot(by_view, today=datetime(2026, 10, 16)), "current_gw": 5}
    later = {**earlier, "date": "2026-10-17", "current_gw": 6}
    monkeypatch.setattr(app, "saved_strengths", lambda league_id: [earlier, later])
    data = app.app.test_client().get("/api/league/123/squads").get_json()
    assert data["since"] == {"gw": 5, "date": "2026-10-16"}    # GW6 is on, so the end of GW5 is the baseline
    assert [d["gw"] for d in data["chart"]["days"]] == [5, 6]
    for m in data["managers"]:
        assert m["movement"]["change"] == 0 and m["movement"]["came_in"] == []   # the same squads as then
        assert m["movement"]["from"] == m["movement"]["to"] == m["strength"]
    lines = {line["entry_id"]: line["values"] for line in data["chart"]["lines"]}
    for m in data["managers"]:
        assert lines[m["entry_id"]] == [m["strength"], m["strength"]]


def test_squads_route_has_no_movement_until_there_is_history(two_managers, monkeypatch):
    monkeypatch.setattr(app, "saved_strengths", lambda league_id: [])
    data = app.app.test_client().get("/api/league/123/squads").get_json()
    assert data["since"] is None and data["chart"] is None
    assert all(m["movement"] is None for m in data["managers"])


# ---------------------------------------------------------------- season awards and your matches

def played_weeks(rows):
    """Finished matches as season_awards reads them: rows are (gw, a, a_points, b, b_points)."""
    return [{"gw": gw, "a": a, "b": b, "pa": pa, "pb": pb} for gw, a, pa, b, pb in rows]


# scored: team 1 [60, 62, 58], 2 [40, 41, 42], 3 [50, 45, 44], 4 [30, 35, 50]
AWARD_WEEKS = played_weeks([(1, 1, 60, 2, 40), (1, 3, 50, 4, 30),
                            (2, 1, 62, 3, 45), (2, 2, 41, 4, 35),
                            (3, 1, 58, 4, 50), (3, 2, 42, 3, 44)])
AWARD_NAMES = {1: "Team 1", 2: "Team 2", 3: "Team 3", 4: "Team 4", 5: "Team 5", 6: "Team 6"}
AWARD_PLACES = {1: 1, 2: 4, 3: 2, 4: 3, 5: 5, 6: 6}


def luck_rows(*rows):
    """banter's luck rows, luckiest first: rows are (team ID, league points, deserved points)."""
    return [{"entry_id": e, "team": AWARD_NAMES[e], "league_points": lp, "expected_points": exp,
             "luck": round(lp - exp, 1)} for e, lp, exp in rows]


def awards_by_kind(played=AWARD_WEEKS, luck=()):
    return {a["kind"]: a for a in app.season_awards(played, AWARD_NAMES, AWARD_PLACES, list(luck))}


def test_awards_go_to_the_best_and_worst_of_each_kind():
    awards = awards_by_kind(luck=luck_rows((1, 9, 8.0), (2, 0, 3.0)))
    got = {k: (a["team"], a["detail"]) for k, a in awards.items()}
    assert got == {"machine": ("Team 1", "60.0 points a week"),
                   "fortress": ("Team 3", "only 44.7 conceded a week"),    # 30, 62 and 42 conceded
                   "steady": ("Team 2", "always between 40 and 42"),
                   "wild": ("Team 4", "anything from 30 to 50"),
                   "unlucky": ("Team 2", "0 league points, deserved 3")}
    assert awards["machine"]["entry_id"] == 1 and awards["machine"]["title"] == "Points machine"
    assert [a["kind"] for a in app.season_awards(AWARD_WEEKS, AWARD_NAMES, AWARD_PLACES, [])] == [
        "machine", "fortress", "steady", "wild"]


def test_awards_wait_for_enough_gameweeks():
    assert app.season_awards(AWARD_WEEKS[:4], AWARD_NAMES, AWARD_PLACES, []) == []   # two gameweeks
    assert app.season_awards([], AWARD_NAMES, AWARD_PLACES, []) == []


def test_nobody_is_called_unluckiest_without_a_real_gap():
    assert "unlucky" not in awards_by_kind(luck=luck_rows((1, 9, 8.0), (2, 2, 3.0)))   # 1.0 short of a point


def test_a_tied_award_goes_to_the_better_table_place():
    level = played_weeks([(gw, 1, 50, 2, 50) for gw in (1, 2, 3)])
    awards = awards_by_kind(level)
    assert {a["team"] for a in awards.values()} == {"Team 1"}      # level on every count: 1st place wins


def test_only_managers_with_enough_matches_can_win_an_award():
    one_off = AWARD_WEEKS + played_weeks([(3, 5, 99, 6, 0)])       # teams 5 and 6 have played once each
    assert awards_by_kind(one_off)["machine"]["team"] == "Team 1"
    assert app.season_awards(played_weeks([(gw, 1, 50, 2, 40) for gw in (1, 2)]),
                             AWARD_NAMES, AWARD_PLACES, []) == []


def test_banter_carries_the_awards_once_three_gameweeks_are_in():
    assert app.league_banter(banter_league(BANTER_WEEKS))["awards"] == []              # two gameweeks
    weeks = BANTER_WEEKS[:4] + [match(3, 10, 55, 12, 40), match(3, 11, 45, 13, 35)]    # GW3 played
    awards = {a["kind"]: a for a in app.league_banter(banter_league(weeks))["awards"]}
    assert awards["machine"]["team"] == "Team 2"       # 90, 69 and 40: the most points a week of the regulars
    assert awards["machine"]["detail"] == "66.3 points a week"


def test_people_are_listed_by_manager_name_and_need_a_team_id():
    details = {"league_entries": [
        {"entry_id": 2, "entry_name": "Zed FC", "player_first_name": "Zed", "player_last_name": "Z"},
        {"entry_id": 1, "entry_name": "", "player_first_name": "amy", "player_last_name": "A"},
        {"id": 9}]}                                                  # no team ID: can't be picked
    assert app.league_people(details) == [
        {"entry_id": 1, "team": "Team 1", "manager": "amy A"},
        {"entry_id": 2, "team": "Zed FC", "manager": "Zed Z"}]


def upcoming_for(weeks=BANTER_WEEKS, **kwargs):
    return app.league_upcoming(banter_league(weeks), **kwargs)


def test_upcoming_lists_every_remaining_match_soonest_first_for_both_sides():
    out = upcoming_for()
    assert out["upcoming"][100] == [{"gw": 3, "opponent": 101, "live": False, "edge": None}]
    assert out["upcoming"][101] == [{"gw": 3, "opponent": 100, "live": False, "edge": None}]
    assert [(g["gw"], g["opponent"]) for g in out["upcoming"][103]] == [(3, 102), (4, 104)]
    assert [(g["gw"], g["opponent"]) for g in out["upcoming"][104]] == [(4, 103)]


def test_upcoming_has_everyone_in_table_order_with_their_recent_results():
    out = upcoming_for()
    top = out["managers"][0]
    assert (top["entry_id"], top["position"], top["league_points"], top["form"]) == (100, 1, 6, "WW")
    assert out["managers"][0]["team"] == "Team 0" and out["managers"][0]["strength"] is None
    assert [m["position"] for m in out["managers"]] == [1, 2, 3, 4, 5]


def test_upcoming_form_only_goes_back_a_few_results(monkeypatch):
    monkeypatch.setattr(app, "FORM_LENGTH", 1)
    forms = {m["entry_id"]: m["form"] for m in upcoming_for()["managers"]}
    assert forms[101] == "W"           # lost GW1, won GW2


def test_a_match_that_has_started_is_marked_live():
    weeks = BANTER_WEEKS[:4] + [{**match(3, 10, 12, 11, 7, finished=False), "started": True}]
    assert upcoming_for(weeks)["upcoming"][100][0]["live"] is True


def test_upcoming_says_who_is_the_favourite_by_squad_strength():
    strengths = {100: 70.0, 101: 66.0, 102: 69.0, 103: 69.5, 104: 50.0}
    out = upcoming_for(strengths=strengths)
    assert out["upcoming"][100][0]["edge"] == "favourite"          # 70 v 66: 4 ahead
    assert out["upcoming"][101][0]["edge"] == "underdog"
    assert out["upcoming"][102][0]["edge"] == "even"               # 69 v 69.5: under EDGE_MIN_STRENGTH
    assert out["upcoming"][103][0]["edge"] == "even"
    assert out["upcoming"][104][0]["edge"] == "underdog"           # 50 v 69.5
    assert [m["strength"] for m in out["managers"] if m["entry_id"] == 100] == [70.0]
    partial = upcoming_for(strengths={100: 70.0})                  # a missing strength: no verdict
    assert partial["upcoming"][100][0]["edge"] is None


def test_upcoming_gives_the_record_against_the_next_opponent():
    snapshots = [app.snapshot_league(banter_league(BANTER_WEEKS), today=datetime(2026, 10, 1))]
    out = upcoming_for(snapshots=snapshots)
    assert out["head_to_head"][100] == {"opponent": 101, "won": 1, "drawn": 0, "lost": 0,
                                        "last": {"season": "2026-27", "gw": 1, "mine": 60, "theirs": 40}}
    assert out["head_to_head"][101]["lost"] == 1 and out["head_to_head"][101]["last"]["mine"] == 40
    unmet = out["head_to_head"][104]                               # 104 has never played 103
    assert unmet == {"opponent": 103, "won": 0, "drawn": 0, "lost": 0, "last": None}


def test_upcoming_copes_with_no_matches_left_and_no_history():
    done = [m for m in BANTER_WEEKS if m["finished"]]
    out = upcoming_for(done)
    assert out["upcoming"][100] == [] and 100 not in out["head_to_head"]
    assert upcoming_for([match(1, 10, 0, 11, 0, finished=False)]) is None


def with_banter_matches(monkeypatch):
    fake = app.get_json

    def get(url):
        if url.endswith("/league/123/details"):
            return banter_league(BANTER_WEEKS)
        return fake(url)

    monkeypatch.setattr(app, "get_json", get)


def test_upcoming_route_returns_everyones_matches_with_strengths(fake_api, monkeypatch):
    with_banter_matches(monkeypatch)
    data = app.app.test_client().get("/api/league/123/upcoming").get_json()
    assert data["league_name"] == "Banter League"
    up = data["upcoming"]
    assert up["upcoming"]["100"][0]["opponent"] == 101
    assert all(m["strength"] is not None for m in up["managers"])
    assert up["upcoming"]["100"][0]["edge"] in {"favourite", "underdog", "even"}


def test_upcoming_route_still_answers_when_the_strengths_cannot_be_loaded(fake_api, monkeypatch):
    with_banter_matches(monkeypatch)

    def updating(league_id, view):
        raise app.FplError("The Draft site is updating.")

    monkeypatch.setattr(app, "load_league", updating)
    up = app.app.test_client().get("/api/league/123/upcoming").get_json()["upcoming"]
    assert up["upcoming"]["100"][0]["edge"] is None
    assert up["managers"][0]["strength"] is None


def test_upcoming_route_is_empty_for_a_league_without_matches(fake_api):
    assert app.app.test_client().get("/api/league/123/upcoming").get_json()["upcoming"] is None


def test_upcoming_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/upcoming")
    assert res.status_code == 400 and "No Draft league found" in res.get_json()["error"]


def test_people_route_lists_everyone_for_the_who_are_you_popup(fake_api, monkeypatch):
    with_banter_matches(monkeypatch)

    def too_heavy(league_id, view):
        raise AssertionError("the pop-up's list must not load the whole league")

    monkeypatch.setattr(app, "load_league", too_heavy)
    data = app.app.test_client().get("/api/league/123/people").get_json()
    assert data["league_name"] == "Banter League"
    assert [p["entry_id"] for p in data["people"]] == [100, 101, 102, 103, 104]
    assert data["people"][0] == {"entry_id": 100, "team": "Team 0", "manager": "M 0"}
    banter = app.app.test_client().get("/api/league/123/banter").get_json()
    assert "people" not in banter                    # the pop-up asks for them on its own


def test_people_route_works_before_any_gameweek_has_finished(fake_api):
    people = app.app.test_client().get("/api/league/123/people").get_json()["people"]
    assert [p["entry_id"] for p in people] == [100]   # the fake league has one manager and no matches


def test_people_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/people")
    assert res.status_code == 400 and "No Draft league found" in res.get_json()["error"]


def test_league_hub_asks_new_visitors_who_they_are_and_has_no_share_links():
    res = app.app.test_client().get("/league?league=123")
    page = res.data.decode()
    res.close()
    assert '<dialog id="whoDialog"' in page and "function askWho" in page
    # Your matches is a choice for everyone (not fullonly), hidden until the page knows who you are
    assert 'class="choice hidden" id="mineChoice"' in page and 'data-tab="upcoming"' in page
    # the old picker and the share bar with its links to copy are gone
    for gone in ('id="whoAmI"', "function fillWho", 'id="shareLink"', 'id="fullLink"', "Share this page",
                 "Copy link", "copyButton"):
        assert gone not in page
    # the address never carries Your matches, so a copied link opens on the choices
    assert 'tab && tab !== "upcoming"' in page
    order = ("panel-upcoming", "panel-banter", "weekSection", "awardsSection", "factsSection")
    where = [page.index(f'id="{name}"') for name in order]
    assert where == sorted(where)


def hub_page():
    res = app.app.test_client().get("/league?league=123")
    page = res.data.decode()
    res.close()
    return page


def test_the_match_ups_are_pinned_above_the_choices_so_every_choice_shows_them():
    page = hub_page()
    # not inside any panel: they come before the choice buttons and so before every panel
    assert page.index('id="matchups"') < page.index('id="choices"') < page.index('id="panel-upcoming"')
    assert page.index('id="cards"') < page.index('id="choices"')
    assert "The last gameweek in review" in page                       # Banter's own description changed
    assert "This week's match-ups, season awards" not in page


def test_the_hub_loads_the_match_ups_on_opening_and_keeps_them_fresh():
    page = hub_page()
    assert "loadMatchups();" in page and "setInterval" in page and "MATCH_REFRESH_MS" in page
    assert "/live`" in page                                         # the live scores for the cards
    assert 'name === "recap" || name === "banter"' in page     # Banter is asked for each time it's opened
    assert "banter: null" not in page                               # and is no longer a "loaded once" choice
    assert "visibilitychange" in page                               # a background tab catches up


def test_the_hub_shows_last_gameweeks_facts_above_the_records():
    page = hub_page()
    assert 'id="weekFacts"' in page and "function renderWeek" in page
    assert page.index('id="weekSection"') < page.index('id="factsSection"')


def test_the_league_race_and_places_over_time_are_one_section_with_a_switch():
    page = hub_page()
    assert 'id="placesSection"' not in page                      # no longer its own section...
    assert "<h2>Places over time</h2>" not in page
    assert 'data-race="points"' in page and 'data-race="places"' in page and 'id="raceView"' in page
    race = page.index("<h2>League race</h2>")
    # both views, then the weekly points grid, are in that one section, before Weekly scores
    views = (page.index('id="raceChart"'), page.index('id="placesChart"'), page.index('id="weeksTable"'))
    assert race < views[0] < views[1] < views[2]
    assert page.index('id="weeksTable"') < page.index('id="spread"') < page.index('id="panel-rivalries"')
    assert page.index('id="racePoints"') < page.index('id="racePlaces"') < page.index('id="placesNotes"')
    assert 'id="racePlaces" class="hidden"' in page            # it opens on the points race, as before
    assert "function renderPlaces" in page and "function showRace" in page
    assert page.count("renderPlaces()") >= 2       # drawn with the charts, and again when you pick a name
    assert 'if (!P) { showRace("points"); return; }' in page    # under two gameweeks: only the points race


# ---------------------------------------------------------------- team names stand out

def test_mark_wraps_a_team_name_and_drops_stray_asterisks():
    assert app.mark("Wattu Wanderers") == "**Wattu Wanderers**"
    assert app.mark("Salah *Fan* Club") == "**Salah Fan Club**"      # a * in a name can't break the marking
    assert app.mark(12) == "**12**"


def test_the_shared_script_turns_marked_names_into_bold_and_only_the_hub_uses_it():
    res = app.app.test_client().get("/static/common.js")
    script = res.data.decode()
    res.close()
    assert "function rich" in script and "function mgr" in script and 'class="mgr"' in script
    res = app.app.test_client().get("/league?league=123")
    hub = res.data.decode()
    res.close()
    assert '<script src="/static/common.js"></script>' in hub and "rich(" in hub and "mgr(" in hub
    res = app.app.test_client().get("/")
    main = res.data.decode()
    res.close()
    assert "common.js" not in main                          # the main page has no team names in sentences


def test_the_ai_is_told_to_mark_team_names_and_give_player_points_in_brackets():
    system = app.RECAP_SYSTEM
    assert "between double asterisks, like **Wattu Wanderers**" in system
    assert "Saka (14)" in system and "**Saka**" not in system       # footballers are plain, with points


# ---------------------------------------------------------------- deals (trades and waiver moves)

DEAL_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
# one deadline a week: GW1's is the 8th of September
DEAL_DEADLINES = {gw: DEAL_START + timedelta(days=7 * gw) for gw in range(1, 10)}
DEAL_NAMES = {pid: {"name": f"Star{pid}", "pos": "MID", "club": "ARS"} for pid in range(1, 13)}
DEAL_TEAMS = {100: {"team": "Team A", "manager": "Sam Lee"}, 101: {"team": "Team B", "manager": "Kim Ray"}}


def after_deadline(gw):
    """A moment just after a gameweek's deadline, so a deal made then counts from the next gameweek."""
    return (DEAL_DEADLINES[gw] + timedelta(hours=1)).isoformat()


def make_trade(given=((1, 3), (2, 4)), after=4, trade_id=9, state="p"):
    """Team A (100) offers: it receives each pair's first player and gives up the second. Team B is 101."""
    return {"id": trade_id, "event": after + 1, "offered_entry": 100, "received_entry": 101, "state": state,
            "offer_time": after_deadline(after), "response_time": after_deadline(after),
            "tradeitem_set": [{"element_in": i, "element_out": o} for i, o in given]}


def deal_points(*weeks):
    """{gameweek: {player ID: points}} from one {player ID: points} per gameweek, starting at GW5."""
    return {5 + n: pts for n, pts in enumerate(weeks)}


def judge(trade, points, latest=None):
    return app.build_trade(trade, DEAL_NAMES, DEAL_TEAMS, DEAL_DEADLINES, points, latest or max(points))


def test_a_deal_counts_from_the_first_gameweek_after_it_was_made():
    assert app.first_gameweek_after(DEAL_DEADLINES[4] + timedelta(hours=1), DEAL_DEADLINES) == 5
    assert app.first_gameweek_after(DEAL_DEADLINES[4] - timedelta(hours=1), DEAL_DEADLINES) == 4
    assert app.first_gameweek_after(DEAL_DEADLINES[9] + timedelta(days=1), DEAL_DEADLINES) is None


@pytest.mark.parametrize("swing, players, weeks, size", [
    (5, 2, 1, "even"),         # under the minimum swing
    (8, 2, 1, "win"),          # more than 1.5 times the usual luck of two players over one gameweek
    (-8, 2, 1, "win"),         # it works for either side
    (13, 2, 1, "robbery"),     # more than 2.5 times the luck
    (-13, 2, 1, "robbery"),
    (13, 10, 4, "even"),       # the same swing across ten players and four gameweeks is just luck
    (0, 4, 0, "even"),         # nothing has been played yet
])
def test_a_swing_is_only_a_win_when_it_beats_luck(swing, players, weeks, size):
    assert app.deal_size(swing, players, weeks) == size


def test_a_trade_is_scored_on_the_points_each_side_got_and_gave_up():
    points = deal_points({1: 10, 2: 6, 3: 1, 4: 0}, {1: 8, 2: 2, 3: 0, 4: 2})
    deal = judge(make_trade(), points)
    a, b = deal["a"], deal["b"]
    assert (a["entry_id"], b["entry_id"]) == (100, 101)
    assert (a["got_points"], a["gave_points"], a["swing"]) == (26, 3, 23)
    assert (b["got_points"], b["gave_points"], b["swing"]) == (3, 26, -23)     # the exact mirror image
    assert [(p["name"], p["points"]) for p in a["got"]] == [("Star1", 18), ("Star2", 8)]
    assert [(p["name"], p["points"]) for p in b["got"]] == [("Star3", 1), ("Star4", 2)]
    assert deal["running"] == [{"gw": 5, "swing": 15}, {"gw": 6, "swing": 23}]
    assert (deal["first_gw"], deal["weeks"], deal["settled"]) == (5, 2, False)
    assert (deal["state"], deal["size"], deal["winner"], deal["loser"]) == ("decided", "win", 100, 101)
    assert (a["label"], b["label"]) == ("Ahead so far", "Behind so far")


def test_the_side_that_lost_is_the_loser_whoever_offered():
    points = deal_points({1: 0, 2: 1, 3: 12, 4: 9}, {1: 0, 2: 0, 3: 6, 4: 3})
    deal = judge(make_trade(), points)
    assert (deal["winner"], deal["loser"]) == (101, 100)
    assert (deal["a"]["label"], deal["b"]["label"]) == ("Behind so far", "Ahead so far")


def test_a_robbery_gets_praise_for_the_winner_and_a_poke_for_the_loser():
    points = deal_points({1: 20, 2: 15, 3: 0, 4: 1}, {1: 12, 2: 10, 3: 0, 4: 0})
    deal = judge(make_trade(), points)
    assert deal["size"] == "robbery"
    praise, poke = deal["lines"]
    assert (praise["tone"], poke["tone"]) == ("praise", "roast")
    assert "**Team A**" in praise["text"] and "57 points" in praise["text"] and "1 point" in praise["text"]
    assert "**Team B**" in poke["text"] and "57 points" in poke["text"] and "1 point" in poke["text"]


def test_an_ordinary_win_uses_the_milder_lines():
    points = deal_points({1: 10, 2: 6, 3: 1, 4: 0}, {1: 8, 2: 2, 3: 0, 4: 2})
    praise, poke = judge(make_trade(), points)["lines"]
    assert praise["text"] in [t.format(w="**Team A**", l="**Team B**", w_in="26 points", w_out="3 points")
                              for t in app.PRAISE_WIN]
    assert poke["text"] in [t.format(w="**Team A**", l="**Team B**", l_in="3 points", l_out="26 points")
                            for t in app.POKE_WIN]


def test_a_small_swing_is_too_close_to_call_until_four_gameweeks_have_passed():
    deal = judge(make_trade(), deal_points({1: 3, 2: 2, 3: 3, 4: 1}, {1: 2, 2: 2, 3: 2, 4: 3}))
    assert (deal["state"], deal["winner"], deal["loser"]) == ("even", None, None)
    assert deal["a"]["label"] == deal["b"]["label"] == "Too close to call"
    assert [line["tone"] for line in deal["lines"]] == ["even"]
    assert "**Team A**" in deal["lines"][0]["text"] and "**Team B**" in deal["lines"][0]["text"]
    even = {1: 3, 2: 2, 3: 3, 4: 2}
    settled = judge(make_trade(), deal_points(even, even, even, even))
    assert (settled["settled"], settled["a"]["label"]) == (True, "A wash")


def test_a_decided_trade_gets_its_final_verdict_after_four_gameweeks():
    week = {1: 8, 2: 6, 3: 0, 4: 1}
    deal = judge(make_trade(), deal_points(week, week, week, week))
    assert deal["settled"] and deal["weeks"] == 4
    assert (deal["a"]["label"], deal["b"]["label"]) == ("Worth it", "Regretted")


def test_a_trade_made_after_the_last_finished_gameweek_waits():
    deal = judge(make_trade(after=6), deal_points({1: 5}, {1: 5}), latest=6)
    assert (deal["state"], deal["weeks"], deal["first_gw"]) == ("waiting", 0, 7)
    assert deal["lines"] == [] and deal["running"] == []
    assert deal["a"]["label"] == deal["b"]["label"] == "Waiting for a gameweek"


def test_a_gameweek_that_could_not_be_read_is_not_counted():
    points = {5: {1: 10, 2: 6, 3: 1, 4: 0}, 7: {1: 8, 2: 2, 3: 0, 4: 2}}          # GW6's feed was down
    deal = judge(make_trade(), points, latest=7)
    assert deal["weeks"] == 2 and [r["gw"] for r in deal["running"]] == [5, 7]


@pytest.mark.parametrize("change", [
    {"tradeitem_set": []},
    {"tradeitem_set": [{"element_in": 1}]},
    {"offered_entry": None},
    {"response_time": "soon", "offer_time": None},
])
def test_a_trade_row_that_cant_be_read_is_skipped(change):
    assert judge({**make_trade(), **change}, deal_points({1: 1})) is None


def test_players_and_teams_the_feed_does_not_know_still_get_a_name():
    trade = make_trade(given=((99, 98),))
    deal = app.build_trade(trade, {}, {}, DEAL_DEADLINES, deal_points({99: 4, 98: 1}), 5)
    assert deal["a"]["team"] == "Team 100" and deal["a"]["got"][0]["name"] == "Player 99"


def test_the_star_of_a_deal_is_its_best_scorer_and_who_has_him_now():
    deal = judge(make_trade(), deal_points({1: 10, 2: 6, 3: 12, 4: 0}))
    assert app.deal_star(deal) == "Best scorer in the deal: Star3 (12), now with **Team B**."
    assert app.deal_star(judge(make_trade(), deal_points({1: 0, 2: 0, 3: 0, 4: 0}))) is None


def test_the_same_deal_always_gets_the_same_line_and_other_deals_can_differ():
    assert app.choose(("a", "b", "c"), 4) == "b"
    points = deal_points({1: 20, 2: 15, 3: 0, 4: 1}, {1: 12, 2: 10, 3: 0, 4: 0})
    deal = judge(make_trade(), points)
    assert app.trade_lines(deal) == deal["lines"]
    wording = {judge(make_trade(trade_id=n), points)["lines"][0]["text"] for n in range(6)}
    assert len(wording) > 1


def test_points_are_counted_with_the_right_unit():
    assert [app.points_word(n) for n in (0, 1, 12, -2)] == ["0 points", "1 point", "12 points", "-2 points"]


# claims and drops

def make_claim(claim_id=1, got=5, gave=6, after=4, kind="w", result="a", entry=100):
    return {"id": claim_id, "entry": entry, "element_in": got, "element_out": gave, "kind": kind,
            "result": result, "added": after_deadline(after), "event": after + 1}


def moves_for(claims, points, latest=5):
    return app.build_moves(claims, DEAL_NAMES, DEAL_TEAMS, DEAL_DEADLINES, points, latest)


def test_only_accepted_claims_from_the_week_just_finished_are_judged():
    claims = [make_claim(1), make_claim(2, kind="f"), make_claim(3, result="do"), make_claim(4, kind="t"),
              make_claim(5, after=3), make_claim(6, got=None)]
    moves = moves_for(claims, {5: {5: 9, 6: 1}})
    assert [m["id"] for m in moves] == [1, 2]                  # waivers and free agents; denied, trades,
    assert moves[0]["team"] == "Team A"                         # older weeks and no player are all left out


def test_a_claim_is_the_player_added_against_the_player_dropped_for_him():
    [move] = moves_for([make_claim(1, got=5, gave=6)], {5: {5: 9, 6: 1}})
    assert (move["got"]["name"], move["got"]["points"]) == ("Star5", 9)
    assert (move["gave"]["name"], move["gave"]["points"], move["swing"]) == ("Star6", 1, 8)
    [bare] = moves_for([make_claim(2, gave=None)], {5: {5: 9}})
    assert bare["gave"] is None and bare["swing"] == 9


def test_no_claims_are_judged_when_the_gameweeks_points_are_missing():
    assert moves_for([make_claim()], {4: {5: 9}}) == []


def fake_move(move_id, swing):
    return {"id": move_id, "swing": swing, "got": {}, "gave": {}}


def test_the_best_pickup_and_worst_drop_need_a_real_swing():
    moves = [fake_move(1, 3), fake_move(2, 9), fake_move(3, -7), fake_move(4, -2)]
    pickup, regret = app.standout_moves(moves)
    assert (pickup["id"], regret["id"]) == (2, 3)
    small = [fake_move(1, app.MOVE_MIN_SWING - 1), fake_move(2, 1 - app.MOVE_MIN_SWING)]
    assert app.standout_moves(small) == (None, None)
    assert app.standout_moves([fake_move(1, app.MOVE_MIN_SWING)])[0]["id"] == 1
    assert app.standout_moves([]) == (None, None)


def test_a_pickup_and_a_regretted_drop_are_told_with_both_players_points():
    got, gave = {"name": "Star5", "points": 9}, {"name": "Star6", "points": 1}
    claim = {"id": 1, "team": "Team A", "got": got, "gave": gave, "swing": 8}
    [praise] = app.move_lines(claim, False)
    assert praise["tone"] == "praise" and "**Team A**" in praise["text"]
    assert "Star5" in praise["text"] and "9 points" in praise["text"] and "1 point" in praise["text"]
    [roast] = app.move_lines({**claim, "got": gave, "gave": got, "swing": -8}, True)
    assert roast["tone"] == "roast" and "Star5" in roast["text"] and "9 points" in roast["text"]
    [bare] = app.move_lines({**claim, "gave": None}, False)
    assert "picked up Star5" in bare["text"] and "9 points" in bare["text"]


# the free agents everyone missed

WIRE = [{"id": 1, "name": "Iwobi", "club": "FUL", "score": 84.2},
        {"id": 2, "name": "Cherif", "club": "COV", "score": 40.0},
        {"id": 3, "name": "Dull", "club": "ARS", "score": 30.0}]


def test_the_best_rated_free_agent_nobody_bid_on_is_missed():
    [line] = app.missed_players(WIRE, set(), {}, 12, 6)
    assert line["tone"] == "roast"
    assert "Iwobi" in line["text"] and "FUL" in line["text"] and "84" in line["text"]


def test_a_bid_takes_a_free_agent_out_and_a_low_rating_is_not_a_miss():
    assert app.missed_players(WIRE, {1}, {}, 12, 6) == []             # the next best rates 40: under the bar
    assert app.missed_players(WIRE[1:], set(), {}, 12, 6) == []
    assert app.missed_players([], set(), {}, 12, 6) == []


def test_the_free_agent_who_scored_most_is_missed_too_but_not_named_twice():
    top, haul = app.missed_players(WIRE, set(), {2: 12, 3: 1}, 12, 6)
    assert "Iwobi" in top["text"]
    assert "Cherif" in haul["text"] and "12 points" in haul["text"] and "GW6" in haul["text"]
    assert len(app.missed_players(WIRE, set(), {1: 14}, 12, 6)) == 1      # Iwobi again: said once
    assert len(app.missed_players(WIRE, set(), {2: app.MISSED_HAUL_POINTS - 1}, 12, 6)) == 1
    [only] = app.missed_players(WIRE[1:], set(), {2: 12}, 12, 6)           # a haul alone is enough
    assert "Cherif" in only["text"]


def test_the_count_of_managers_is_used_when_there_is_one_to_use():
    wire = [{"id": 2, "name": "Iwobi", "club": "FUL", "score": 84.2}]
    assert "12 managers, 12 misses" in app.missed_players(wire, set(), {}, 12, 6)[0]["text"]
    assert "Iwobi" in app.missed_players(wire, set(), {}, 0, 6)[0]["text"]       # no count: another line


# putting it together

def deals_for(trades=(), claims=(), points=None, free=(), finished=(1, 2, 3, 4, 5, 6), now=None, managers=12):
    now = now or DEAL_DEADLINES[6] + timedelta(days=1)
    return app.league_deals(list(trades), list(claims), DEAL_NAMES, DEAL_TEAMS, DEAL_DEADLINES,
                            list(finished), points or {}, now, list(free), managers)


FLAT_POINTS = {gw: {pid: 2 for pid in range(1, 13)} for gw in range(1, 7)}


def test_there_are_no_deals_before_a_gameweek_has_finished():
    assert deals_for([make_trade()], finished=()) is None


def test_recent_trades_are_news_and_older_ones_only_stay_in_the_tracker():
    old, mid = make_trade(after=1, trade_id=1), make_trade(after=3, trade_id=3)
    new = make_trade(after=5, trade_id=2)
    deals = deals_for([old, mid, new], points=FLAT_POINTS)
    assert deals["gameweek"] == 6
    assert [t["id"] for t in deals["trades"]] == [2, 3, 1]                  # newest first, all of them
    shown = [i for i in deals["items"] if i["kind"] == "trade"]
    assert [i["note"] for i in shown] == ["After 1 finished gameweek (GW6)",
                                          "After 3 finished gameweeks (GW4-6)"]
    assert shown[0]["heading"] == "Trade: Team A and Team B"
    assert shown[0]["lines"] and shown[0]["detail"].startswith("Best scorer in the deal")


def test_only_a_few_trades_are_on_the_recap_and_only_processed_ones_count(monkeypatch):
    monkeypatch.setattr(app, "DEALS_SHOWN", 1)
    trades = [make_trade(after=4, trade_id=1), make_trade(after=5, trade_id=2),
              make_trade(after=5, trade_id=3, state="d")]                    # declined: not a deal
    deals = deals_for(trades, points=FLAT_POINTS)
    assert [t["id"] for t in deals["trades"]] == [2, 1]
    assert [i["note"] for i in deals["items"] if i["kind"] == "trade"] == ["After 1 finished gameweek (GW6)"]


def test_a_pickup_and_a_regretted_drop_make_the_recap():
    claims = [make_claim(1, got=5, gave=6, after=5), make_claim(2, got=7, gave=8, after=5)]
    points = {**FLAT_POINTS, 6: {5: 9, 6: 1, 7: 0, 8: 8}}
    deals = deals_for(claims=claims, points=points)
    assert [i["kind"] for i in deals["items"]] == ["pickup", "regret"]
    assert [i["heading"] for i in deals["items"]] == ["Pickup of the week", "Dropped and regretted"]
    pickup, regret = deals["items"]
    assert "Star5" in pickup["lines"][0]["text"] and "Star8" in regret["lines"][0]["text"]


def test_the_free_agents_everyone_missed_make_the_recap_unless_someone_bid_this_week():
    [item] = deals_for(points=FLAT_POINTS, free=WIRE)["items"]
    assert (item["kind"], item["heading"], item["note"]) == (
        "missed", "Everyone missed this one", f"No bids in the last {app.MISSED_DAYS} days")
    now = DEAL_DEADLINES[6] + timedelta(days=1)
    recent = {**make_claim(1, got=1, gave=9), "added": (now - timedelta(days=2)).isoformat()}
    assert deals_for(claims=[recent], points=FLAT_POINTS, free=WIRE)["items"] == []      # a bid: not missed
    old = {**recent, "added": (now - timedelta(days=app.MISSED_DAYS + 1)).isoformat()}
    assert [i["kind"] for i in deals_for(claims=[old], points=FLAT_POINTS, free=WIRE)["items"]] == ["missed"]


def test_a_quiet_week_has_nothing_to_show_but_the_tracker_still_lists_trades():
    deals = deals_for([make_trade(after=1)], points=FLAT_POINTS)
    assert deals["items"] == [] and len(deals["trades"]) == 1


# the deals route

@pytest.fixture
def deals_world(monkeypatch):
    """
    Fake Draft and classic sites for the deals route. Gameweeks 1-6 have finished. Team A (100) and Team B
    (101) made one trade just after GW4's deadline (so it counts from GW5), Star1 and Star2 for Star3 and
    Star4, and stars 5-8 are free agents. The returned dict can be changed before the first request:
    "down" takes feeds out ("transactions", "owners" or "live5"), "finished" is the finished gameweeks.
    """
    world = {
        "trades": [make_trade()],
        "transactions": [],
        "points": {5: {1: 10, 2: 6, 3: 1, 4: 0}, 6: {1: 8, 2: 2, 3: 0, 4: 2}},
        "finished": [1, 2, 3, 4, 5, 6],
        "down": set(),
        "calls": [],
    }
    elements = [{**make_player(i, team=1 if i % 2 else 2), "web_name": f"Star{i}"} for i in range(1, 9)]
    details = {"league": {"name": "Deal League"},
               "league_entries": [{"entry_id": 100, "entry_name": "Team A",
                                   "player_first_name": "Sam", "player_last_name": "Lee"},
                                  {"entry_id": 101, "entry_name": "Team B",
                                   "player_first_name": "Kim", "player_last_name": "Ray"}]}
    owners = {1: 100, 2: 100, 3: 101, 4: 101}
    kinds = [{"id": 1, "singular_name_short": "GKP"}, {"id": 2, "singular_name_short": "DEF"},
             {"id": 3, "singular_name_short": "MID"}, {"id": 4, "singular_name_short": "FWD"}]

    def fake(url):
        world["calls"].append(url)

        def down():
            raise requests.ConnectionError("down")

        if "fantasy" in url:                                              # the classic site
            return {"teams": TEAMS, "elements": []} if "bootstrap" in url else []
        if url.endswith("/bootstrap-static"):
            data = [{"id": gw, "deadline_time": DEAL_DEADLINES[gw].isoformat(),
                     "finished": gw in world["finished"]} for gw in range(1, 10)]
            return {"events": {"current": 6, "next": 7, "data": data}, "teams": TEAMS, "elements": elements,
                    "element_types": kinds}
        if url.endswith("/details"):
            return details
        if url.endswith("/element-status"):
            if "owners" in world["down"]:
                down()
            return {"element_status": [{"element": i, "owner": owners.get(i)} for i in range(1, 9)]}
        if url.endswith("/trades"):
            return {"trades": world["trades"]}
        if url.endswith("/transactions"):
            if "transactions" in world["down"]:
                down()
            return {"transactions": world["transactions"]}
        if "/live" in url:
            gw = int(url.split("/event/")[1].split("/")[0])
            if f"live{gw}" in world["down"]:
                down()
            return {"elements": {str(pid): {"stats": {"total_points": pts, "minutes": 90}}
                                 for pid, pts in world["points"].get(gw, {}).items()}}
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(app, "get_json", fake)
    return world


def get_deals(league=123):
    return app.app.test_client().get(f"/api/league/{league}/deals").get_json()


def test_deals_route_scores_the_trade_from_the_live_feeds(deals_world):
    data = get_deals()
    assert data["league_name"] == "Deal League"
    deals = data["deals"]
    assert deals["gameweek"] == 6
    [trade] = deals["trades"]
    a = trade["a"]
    assert (a["team"], a["manager"], a["swing"], trade["weeks"]) == ("Team A", "Sam Lee", 23, 2)
    first = deals["items"][0]
    assert first["kind"] == "trade" and [line["tone"] for line in first["lines"]] == ["praise", "roast"]
    live = sorted(url.split("/event/")[1].split("/")[0] for url in deals_world["calls"] if "/live" in url)
    assert live == ["5", "6"]                      # only the gameweeks the trade covers, not the whole season


def test_deals_route_names_a_free_agent_everyone_missed(deals_world, monkeypatch):
    monkeypatch.setattr(app, "MISSED_MIN_RATING", 0)
    items = get_deals()["deals"]["items"]
    missed = [i for i in items if i["kind"] == "missed"]
    assert len(missed) == 1 and any(f"Star{n}" in missed[0]["lines"][0]["text"] for n in range(5, 9))


def test_deals_route_is_empty_before_a_gameweek_has_finished(deals_world):
    deals_world["finished"] = []
    assert get_deals()["deals"] is None
    assert not any("/live" in url for url in deals_world["calls"])


def test_deals_route_still_shows_the_trades_without_the_claims_or_the_ratings(deals_world):
    deals_world["down"] = {"transactions", "owners"}
    deals = get_deals()["deals"]
    assert [i["kind"] for i in deals["items"]] == ["trade"] and len(deals["trades"]) == 1


def test_deals_route_counts_fewer_gameweeks_when_one_feed_is_down(deals_world):
    deals_world["down"] = {"live5"}
    [trade] = get_deals()["deals"]["trades"]
    assert trade["weeks"] == 1 and trade["running"] == [{"gw": 6, "swing": 8}]


def test_deals_route_skips_a_trade_feed_row_it_cannot_read(deals_world):
    deals_world["trades"] = [{"id": 1, "state": "p"}, make_trade()]
    assert [t["id"] for t in get_deals()["deals"]["trades"]] == [9]


def test_deals_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/deals")
    assert res.status_code == 400
    assert "No Draft league found" in res.get_json()["error"]


def test_league_hub_has_the_trades_choice_and_the_deals_card_on_the_recap():
    res = app.app.test_client().get("/league?league=123")
    page = res.data.decode()
    res.close()
    assert '<button class="choice" type="button" data-tab="deals"' in page          # not fullonly
    assert 'id="panel-deals"' in page and "function renderDeals" in page and "function fillDealCard" in page
    assert page.index('id="dealsCard"') < page.index('id="recapEmpty"')               # on top of the recap
    assert page.count('"deals"') >= 3 and "deals: null" in page                        # a tab like the others


# ---------------------------------------------------------------- match-up card records, and the scoring data

def snapshot(season, *matches):
    """A saved season for the record tests: each match is (gw, a, b, a_points, b_points)."""
    return {"season": season, "managers": [],
            "matches": [{"gw": gw, "a": a, "b": b, "a_points": pa, "b_points": pb}
                        for gw, a, b, pa, pb in matches]}


RECORD_NAMES = {100: "Team 0", 101: "Team 1"}


def record_of(snapshots, a=100, b=101):
    return app.pair_record(snapshots, a, b, RECORD_NAMES)


def test_a_pairing_that_has_never_met_says_so():
    assert record_of([snapshot("2026-27")]) == {
        "won": 0, "drawn": 0, "lost": 0, "meetings": 0, "line": "First meeting on record."}


def test_one_meeting_names_the_winner_the_score_and_the_gameweek():
    rec = record_of([snapshot("2026-27", (3, 100, 101, 45, 38))])
    assert rec["line"] == "Met once, in GW3: **Team 0** won 45-38."
    assert (rec["won"], rec["drawn"], rec["lost"], rec["meetings"]) == (1, 0, 0, 1)
    flipped = record_of([snapshot("2026-27", (3, 100, 101, 45, 38))], a=101, b=100)  # other way round
    assert flipped["line"] == "Met once, in GW3: **Team 0** won 45-38."
    assert (flipped["won"], flipped["lost"]) == (0, 1)


def test_a_draw_is_said_to_be_a_draw():
    rec = record_of([snapshot("2026-27", (3, 100, 101, 40, 40))])
    assert rec["line"] == "Met once, in GW3: **Team 0** and **Team 1** drew 40-40." and rec["drawn"] == 1


def test_several_meetings_give_the_series_and_the_latest_result():
    old, new = snapshot("2025-26", (38, 100, 101, 50, 40)), snapshot("2026-27", (3, 101, 100, 45, 38))
    both = record_of([old, new])
    assert both["line"] == "Met 2 times: All square at 1-1. Last, in GW3: **Team 1** won 45-38."
    assert (both["won"], both["lost"], both["meetings"]) == (1, 1, 2)
    meetings = snapshot("2026-27", (3, 100, 101, 45, 38), (14, 100, 101, 60, 60), (20, 100, 101, 70, 30))
    assert record_of([meetings])["line"] == (
        "Met 3 times: **Team 0** lead 2-0 (1 drawn). Last, in GW20: **Team 0** won 70-30.")


def test_a_meeting_from_an_earlier_season_names_the_season():
    old = snapshot("2025-26", (38, 100, 101, 50, 40))
    assert record_of([old, snapshot("2026-27")])["line"] == (
        "Met once, in GW38 of 2025-26: **Team 0** won 50-40.")


def test_cards_carry_the_pairings_record_when_snapshots_are_given():
    snaps = [snapshot("2026-27", (1, 100, 101, 60, 40))]
    banter = app.league_banter(banter_league(BANTER_WEEKS), snaps)
    assert banter["hot_match"]["record"]["line"] == "Met once, in GW1: **Team 0** won 60-40."
    assert app.league_banter(banter_league(BANTER_WEEKS))["hot_match"]["record"] is None   # nothing to go on


def test_banter_route_has_each_cards_record_from_this_seasons_results(fake_api, monkeypatch):
    fake = app.get_json
    monkeypatch.setattr(app, "get_json", lambda url: banter_league(BANTER_WEEKS)
                        if url.endswith("/league/123/details") else fake(url))
    hot = app.app.test_client().get("/api/league/123/banter").get_json()["banter"]["hot_match"]
    assert hot["record"]["line"] == "Met once, in GW1: **Team 0** won 60-40."
    assert hot["record"]["meetings"] == 1


def test_banter_has_each_managers_points_for_and_against_in_table_order():
    scoring = app.league_banter(banter_league(BANTER_WEEKS))["scoring"]
    assert [m["entry_id"] for m in scoring] == [100, 102, 101, 103, 104]
    got = {m["entry_id"]: (m["points_for"], m["points_against"], m["league_points"]) for m in scoring}
    assert got == {100: (130, 109, 6), 102: (159, 120, 3), 101: (90, 80, 3),
                   103: (50, 90, 0), 104: (20, 50, 0)}
    assert scoring[0]["team"] == "Team 0"


def test_rivalries_route_sends_the_head_to_head_grid_only_with_the_manager_list(fake_api, monkeypatch):
    fake = app.get_json
    monkeypatch.setattr(app, "get_json", lambda url: banter_league(BANTER_WEEKS)
                        if url.endswith("/league/123/details") else fake(url))
    client = app.app.test_client()
    grid = client.get("/api/league/123/rivalries").get_json()["grid"]
    assert [m["team"] for m in grid["managers"]] == ["Team 0", "Team 2", "Team 1", "Team 3", "Team 4"]
    row = next(r for r in grid["rows"] if r["entry_id"] == 100)
    assert {o["entry_id"]: (o["won"], o["lost"]) for o in row["vs"]} == {101: (1, 0), 102: (1, 0)}
    pair = client.get("/api/league/123/rivalries?a=100&b=101").get_json()
    assert "grid" not in pair and pair["rivalry"]["record"]["a_won"] == 1  # asked for a pair: no grid again


def test_the_head_to_head_grid_is_empty_for_a_league_without_matches(fake_api):
    assert app.app.test_client().get("/api/league/123/rivalries").get_json()["grid"] is None


# ---------------------------------------------------------------- season odds

# GW1-3 finished. Team 0 wins all three; Teams 1, 2 and 3 end on 3 points each (Team 2 has scored the most)
ODDS_WEEKS = [match(1, 10, 60, 11, 40), match(1, 12, 50, 13, 30),
              match(2, 10, 70, 12, 45), match(2, 13, 55, 11, 35),
              match(3, 10, 65, 13, 30), match(3, 11, 50, 12, 48)]
TO_PLAY = [match(4, 10, 0, 11, 0, finished=False), match(4, 12, 0, 13, 0, finished=False),
           match(5, 10, 0, 12, 0, finished=False), match(5, 11, 0, 13, 0, finished=False),
           match(6, 10, 0, 13, 0, finished=False), match(6, 11, 0, 12, 0, finished=False)]


def test_odds_wait_for_three_finished_gameweeks():
    assert app.season_odds(recap_league(ODDS_WEEKS[:4] + TO_PLAY), runs=50) is None       # only two so far
    assert app.season_odds({"league_entries": [{"id": 10, "entry_id": 100}]}) is None      # no matches at all


def test_each_managers_odds_and_each_places_odds_add_up_to_one():
    odds = app.season_odds(recap_league(ODDS_WEEKS + TO_PLAY), runs=400)
    assert (odds["runs"], odds["gws_played"], odds["gws_left"]) == (400, 3, 3)
    # level on 3 points, so points scored decides the table: Team 2 (143), Team 1 (125), Team 3 (115)
    assert [m["entry_id"] for m in odds["managers"]] == [100, 102, 101, 103]
    for m in odds["managers"]:
        assert sum(m["odds"]) == pytest.approx(1, abs=0.001)
        assert m["win"] == m["odds"][0] and m["last"] == m["odds"][-1]
        assert m["top3"] == pytest.approx(sum(m["odds"][:3]), abs=0.001)
    for place in range(4):
        assert sum(m["odds"][place] for m in odds["managers"]) == pytest.approx(1, abs=0.001)


def test_a_lead_nobody_can_catch_is_certain():
    odds = app.season_odds(recap_league(ODDS_WEEKS + TO_PLAY[:2]), runs=300)  # one week left: 9 v at most 6
    top = odds["managers"][0]
    assert (top["team"], top["win"], top["odds"][0], odds["gws_left"]) == ("Team 0", 1.0, 1.0, 1)
    assert odds["lines"][0] == "Title favourite: **Team 0**, 100%."
    assert odds["lines"][1].startswith("Wooden spoon favourite: **Team")


def test_with_no_matches_left_the_odds_are_just_the_table():
    odds = app.season_odds(recap_league(ODDS_WEEKS), runs=50)
    assert odds["gws_left"] == 0
    assert [m["odds"].index(1.0) + 1 for m in odds["managers"]] == [1, 2, 3, 4]


def test_the_odds_are_the_same_every_time_for_the_same_results():
    league = recap_league(ODDS_WEEKS + TO_PLAY)
    assert app.season_odds(league, runs=200) == app.season_odds(league, runs=200)


@pytest.fixture
def flat_odds(monkeypatch):
    """The plain model: every week counts the same and only scores count (no squad or table share)."""
    monkeypatch.setattr(app, "ODDS_HALF_LIFE", None)
    monkeypatch.setattr(app, "ODDS_SQUAD_WEIGHT", 0)
    monkeypatch.setattr(app, "ODDS_TABLE_WEIGHT", 0)


def test_averages_are_pulled_towards_the_league_average_and_a_stronger_scorer_rates_higher(flat_odds):
    odds = app.season_odds(recap_league(ODDS_WEEKS + TO_PLAY), runs=1500)
    by_team = {m["entry_id"]: m for m in odds["managers"]}
    # Team 0 averaged 65 in three weeks; the league averaged 48.2, and five weeks of that are mixed in:
    # (195 + 5 x 48.17) / 8 = 54.5
    assert by_team[100]["average"] == pytest.approx(54.5, abs=0.1)
    assert by_team[102]["average"] > by_team[101]["average"] > by_team[103]["average"]
    assert by_team[102]["expected_place"] < by_team[103]["expected_place"]  # level on points, better scorer


# four managers, four gameweeks: Team 0 scores 20, 20, 60, 60 and Team 1 scores 60, 60, 20, 20 (the same
# total, and each wins two matches); Teams 2 and 3 score 30 every week
SWING_WEEKS = [match(1, 10, 20, 12, 30), match(1, 11, 60, 13, 30),
               match(2, 10, 20, 13, 30), match(2, 11, 60, 12, 30),
               match(3, 10, 60, 12, 30), match(3, 11, 20, 13, 30),
               match(4, 10, 60, 13, 30), match(4, 11, 20, 12, 30)]


def estimates_for(matches, strengths=None):
    return app.odds_estimates(app.league_history(recap_league(list(matches))), strengths)


def test_recent_weeks_count_more_than_old_ones(monkeypatch):
    monkeypatch.setattr(app, "ODDS_SQUAD_WEIGHT", 0)
    monkeypatch.setattr(app, "ODDS_HALF_LIFE", None)
    flat = estimates_for(SWING_WEEKS)["estimates"]
    assert flat[100]["form"] == pytest.approx(flat[101]["form"])           # the same total: no difference
    monkeypatch.setattr(app, "ODDS_HALF_LIFE", 4)
    recent = estimates_for(SWING_WEEKS)["estimates"]
    assert recent[100]["form"] > flat[100]["form"] > recent[101]["form"]   # improving is up, slumping is down
    assert recent[100]["average"] > recent[101]["average"]
    # a week 4 gameweeks old counts half as much as the latest: weights 1, 0.84, 0.71, 0.59 for the last four
    weights = [0.5 ** (k / 4) for k in (3, 2, 1, 0)]
    mean = sum(w * v for w, v in zip(weights, [20, 20, 60, 60])) / sum(weights)
    league = (160 + 160 + 120 + 120) / 16
    assert recent[100]["form"] == pytest.approx((mean * sum(weights) + 5 * league) / (sum(weights) + 5))


def test_a_squad_rating_moves_the_expected_score_and_a_missing_one_falls_back():
    plain = estimates_for(ODDS_WEEKS)
    assert plain["weights"] == {"form": 0.9, "squad": 0, "table": 0.1}
    assert all(e["squad"] is None for e in plain["estimates"].values())
    rated = estimates_for(ODDS_WEEKS, {100: 80, 101: 60, 102: 70, 103: 70})
    assert rated["weights"] == {"form": 0.7, "squad": 0.2, "table": 0.1}
    top = rated["estimates"][100]
    assert top["squad"] == pytest.approx(rated["league_average"] + 0.3 * 10)      # 10 above the average squad
    assert rated["estimates"][101]["squad"] == pytest.approx(rated["league_average"] - 0.3 * 10)
    assert top["average"] == pytest.approx(0.7 * top["form"] + 0.2 * top["squad"] + 0.1 * top["table"])
    # Team 3 scores less than the rest but has the best squad here: the squad share lifts their expected
    # score, and it pulls down a manager who scores far more than their squad suggests (Team 0) a little
    best = estimates_for(ODDS_WEEKS, {100: 70, 101: 70, 102: 70, 103: 90})["estimates"][103]
    expected = plain["estimates"][103]["average"] + 0.2 * (best["squad"] - best["form"])
    assert best["average"] == pytest.approx(expected)
    assert best["average"] > plain["estimates"][103]["average"]
    assert rated["estimates"][100]["average"] < plain["estimates"][100]["average"]
    # a manager nobody rated gets form and table only, and the squad share goes to their form
    partial = estimates_for(ODDS_WEEKS, {100: 80, 101: 60, 102: 70})["estimates"][103]
    assert partial["squad"] is None
    assert partial["average"] == pytest.approx(0.9 * partial["form"] + 0.1 * partial["table"])


def test_a_squad_rating_of_zero_or_a_lone_rating_is_ignored():
    assert estimates_for(ODDS_WEEKS, {100: 0, 101: 60, 102: 0, 103: 0})["weights"]["squad"] == 0  # one rating
    assert estimates_for(ODDS_WEEKS, {100: 0, 101: 60, 102: 70, 103: 70})["estimates"][100]["squad"] is None
    assert estimates_for(ODDS_WEEKS, {999: 70, 998: 60})["weights"]["squad"] == 0     # not in this league


# Teams 0 and 1 score 50 every week, so their form is the same; Team 0 faces weak opponents and wins every
# time, Team 1 faces strong ones and loses every time
TABLE_WEEKS = [m for gw in (1, 2, 3) for m in (match(gw, 10, 50, 12, 40), match(gw, 11, 50, 13, 60))]


def test_league_results_count_a_little_towards_the_expected_score(monkeypatch):
    got = estimates_for(TABLE_WEEKS)["estimates"]
    assert got[100]["form"] == pytest.approx(got[101]["form"])
    assert got[100]["table"] > got[101]["table"]
    assert got[100]["average"] > got[101]["average"]
    gap = got[100]["table"] - got[101]["table"]
    assert got[100]["average"] - got[101]["average"] == pytest.approx(0.1 * gap)
    monkeypatch.setattr(app, "ODDS_TABLE_WEIGHT", 0)
    assert estimates_for(TABLE_WEEKS)["estimates"][100]["average"] == pytest.approx(
        estimates_for(TABLE_WEEKS)["estimates"][101]["average"])


def test_a_manager_who_has_won_half_their_matches_is_expected_to_score_the_league_average():
    weeks = [match(1, 10, 60, 11, 40), match(1, 12, 50, 13, 50),
             match(2, 10, 30, 11, 70), match(2, 12, 50, 13, 50),
             match(3, 10, 60, 11, 40), match(3, 12, 50, 13, 50)]
    got = estimates_for(weeks)
    # Team 0 won, then lost, then won again; Team 2 and 3 only drew: a win rate of exactly half for the draws
    assert got["estimates"][102]["table"] == pytest.approx(got["league_average"])
    assert got["estimates"][100]["table"] > got["league_average"]


def test_the_odds_say_which_shares_they_used_and_why_for_every_manager():
    odds = app.season_odds(recap_league(ODDS_WEEKS + TO_PLAY), {100: 80, 101: 60, 102: 70, 103: 70}, runs=300)
    assert odds["weights"] == {"form": 0.7, "squad": 0.2, "table": 0.1}
    by_team = {m["entry_id"]: m for m in odds["managers"]}
    assert "Squad strength 80, 1st of 4." in by_team[100]["why"]["lines"]
    assert "Squad strength 60, 4th of 4." in by_team[101]["why"]["lines"]
    assert all(m["why"]["lines"][-1].startswith("The odds assume they score ") for m in odds["managers"])
    plain = app.season_odds(recap_league(ODDS_WEEKS + TO_PLAY), runs=300)
    assert plain["weights"]["squad"] == 0
    assert not any(line.startswith("Squad strength") for m in plain["managers"] for line in m["why"]["lines"])


SLUMP = [48, 40, 38, 40, 42, 19, 18, 24, 25]


def why_of(scores, strength=None, place=None, count=0, points=15, deserved=15.0):
    return app.odds_why(scores, 35.2, strength, place, count, points, deserved, 38.9)


def test_the_why_says_when_a_manager_is_slumping_or_rising():
    slump = why_of(SLUMP)
    assert slump["trend"] == "slumping" and (slump["recent"], slump["before"]) == (21.5, 41.6)
    assert slump["lines"][0] == (
        "Scoring 21.5 a week over the last 4 weeks, against 41.6 in the 5 before: slumping.")
    rising = why_of(SLUMP[::-1])     # the same weeks reversed: 25, 24, 18, 19, 42, then 40, 38, 40, 48
    assert rising["trend"] == "rising"
    assert rising["lines"][0] == (
        "Scoring 41.5 a week over the last 4 weeks, against 25.6 in the 5 before: rising.")


def test_a_small_change_in_form_is_not_called_rising_or_slumping():
    steady = why_of([40, 42, 38, 40, 41, 43, 39, 40])
    assert steady["trend"] is None and steady["lines"][0].endswith("in the 4 before.")
    assert why_of([40, 42, 38, 40, 41])["lines"][0] == "Scoring 40.2 a week so far."  # too few weeks
    assert why_of([40, 42, 38, 40, 41])["recent"] is None
    assert why_of([])["lines"][0] == "No scores yet."


def test_the_why_compares_league_points_with_what_the_scores_deserve():
    assert why_of(SLUMP, points=15, deserved=9.8)["lines"][-2] == (
        "15 league points; their scores deserve 9.8: lucky so far.")
    assert why_of(SLUMP, points=12, deserved=15.1)["lines"][-2] == (
        "12 league points; their scores deserve 15.1: unlucky so far.")
    assert why_of(SLUMP, points=15, deserved=14.2)["lines"][-2] == (
        "15 league points; their scores deserve 14.2, about what they deserve.")
    assert why_of(SLUMP, points=15, deserved=9.8)["luck"] == 5.2


def test_the_why_names_the_squad_and_what_the_odds_assume():
    lines = why_of(SLUMP, strength=62.4, place=11, count=12)["lines"]
    assert lines[1] == "Squad strength 62, 11th of 12."
    assert lines[-1] == "The odds assume they score 35.2 a week from here (the league average is 38.9)."
    assert not any(line.startswith("Squad") for line in why_of(SLUMP)["lines"])


@pytest.mark.parametrize("p, text", [(0.714, "71%"), (0.004, "under 1%"), (0.009, "under 1%"), (0, "0%"),
                                     (0.01, "1%"), (1, "100%")])
def test_a_chance_is_written_as_a_whole_percent(p, text):
    assert app.percent_text(p) == text


def test_squad_strengths_are_the_managers_who_have_an_eleven(monkeypatch):
    seen = []

    def fake_league(league_id, view):
        seen.append((league_id, view))
        return {"managers": [{"entry_id": 100, "strength": 70.5, "best_xi": [1, 2]},
                             {"entry_id": 101, "strength": 0, "best_xi": []}]}

    monkeypatch.setattr(app, "load_league", fake_league)
    assert app.squad_strengths(123) == {100: 70.5}
    assert seen == [(123, "season")]                          # the longer "until the break" rating


def test_squad_strengths_are_none_when_the_league_cannot_be_loaded(monkeypatch):
    def down(league_id, view):
        raise app.FplError("down", 502)

    monkeypatch.setattr(app, "load_league", down)
    assert app.squad_strengths(123) is None


def test_the_odds_are_worked_out_once_for_the_same_results_and_day(monkeypatch):
    monkeypatch.setattr(app, "_odds", {})
    loads, calls = [], []
    monkeypatch.setattr(app, "squad_strengths", lambda league_id: loads.append(league_id) or {100: 70})

    def fake_odds(details, strengths=None, runs=None):
        calls.append(strengths)
        return {"x": 1}

    monkeypatch.setattr(app, "season_odds", fake_odds)
    league, monday, tuesday = recap_league(ODDS_WEEKS + TO_PLAY), date(2026, 11, 9), date(2026, 11, 10)
    assert app.get_odds(123, league, monday) == app.get_odds(123, league, monday) == {"x": 1}
    assert calls == [{100: 70}] and loads == [123]                  # the squads went in, and were loaded once
    app.get_odds(123, league, tuesday)                              # a new day: the squads have changed
    assert len(calls) == 2
    played = recap_league(ODDS_WEEKS + [match(4, 10, 60, 11, 40)] + TO_PLAY[1:])      # a new result
    app.get_odds(123, played, tuesday)
    assert len(calls) == 3
    app.get_odds(456, league, tuesday)                              # another league
    assert len(calls) == 4


def test_the_squads_are_not_loaded_until_there_are_odds_to_work_out(monkeypatch):
    monkeypatch.setattr(app, "_odds", {})
    loads = []
    monkeypatch.setattr(app, "squad_strengths", lambda league_id: loads.append(league_id) or {})
    monkeypatch.setattr(app, "season_odds", lambda details, strengths=None, runs=None: None)
    app.get_odds(123, recap_league(ODDS_WEEKS[:4]))                 # two gameweeks: too early
    app.get_odds(124, {"league_entries": [{"id": 10, "entry_id": 100}]})      # no matches at all
    assert loads == []


def test_only_the_latest_odds_are_kept(monkeypatch):
    monkeypatch.setattr(app, "_odds", {})
    monkeypatch.setattr(app, "ODDS_KEPT", 2)
    monkeypatch.setattr(app, "squad_strengths", lambda league_id: None)
    monkeypatch.setattr(app, "season_odds", lambda details, strengths=None, runs=None: None)
    for league_id in (1, 2, 3):
        app.get_odds(league_id, recap_league(ODDS_WEEKS))
    assert [key[0] for key in app._odds] == [2, 3]


def odds_world(monkeypatch, strengths):
    """The odds route over the ODDS_WEEKS league, with few runs and the given squads (None: can't load)."""
    monkeypatch.setattr(app, "_odds", {})
    monkeypatch.setattr(app, "ODDS_RUNS", 200)
    monkeypatch.setattr(app, "squad_strengths", lambda league_id: strengths)
    fake = app.get_json
    monkeypatch.setattr(app, "get_json", lambda url: recap_league(ODDS_WEEKS + TO_PLAY)
                        if url.endswith("/league/123/details") else fake(url))


def test_odds_route_returns_the_odds_with_the_squads_in_them(fake_api, monkeypatch):
    odds_world(monkeypatch, {100: 80, 101: 60, 102: 70, 103: 70})
    data = app.app.test_client().get("/api/league/123/odds").get_json()
    assert data["league_name"] == "Recap League" and data["odds"]["runs"] == 200
    assert data["odds"]["managers"][0]["team"] == "Team 0"
    assert data["odds"]["weights"] == {"form": 0.7, "squad": 0.2, "table": 0.1}
    assert "why" in data["odds"]["managers"][0]


def test_odds_route_still_works_when_the_squads_cannot_be_loaded(fake_api, monkeypatch):
    odds_world(monkeypatch, None)
    odds = app.app.test_client().get("/api/league/123/odds").get_json()["odds"]
    assert odds["weights"] == {"form": 0.9, "squad": 0, "table": 0.1} and len(odds["managers"]) == 4


def test_odds_route_is_empty_for_a_league_without_matches(fake_api, monkeypatch):
    monkeypatch.setattr(app, "_odds", {})
    monkeypatch.setattr(app, "squad_strengths", lambda league_id: {})
    assert app.app.test_client().get("/api/league/123/odds").get_json()["odds"] is None


def test_odds_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/odds")
    assert res.status_code == 400 and "No Draft league found" in res.get_json()["error"]


# ---------------------------------------------------------------- moves (is being active paying off?)

MOVE_TEAMS = {100: {"team": "Team A", "manager": "Sam Lee"}, 101: {"team": "Team B", "manager": "Kim Ray"},
              102: {"team": "Team C", "manager": "Al Poe"}, 103: {"team": "Team D", "manager": "Jo Fox"}}


def league_moves_for(claims=(), trades=(), points=None, finished=(1, 2, 3, 4, 5, 6)):
    return app.league_moves(list(trades), list(claims), DEAL_NAMES, MOVE_TEAMS, DEAL_DEADLINES,
                            list(finished), points or {})


def manager_row(moves, entry):
    return next(m for m in moves["managers"] if m["entry_id"] == entry)


def test_there_are_no_moves_before_a_gameweek_has_finished():
    assert league_moves_for(finished=()) is None


def test_a_claim_is_judged_on_the_new_player_against_the_dropped_one_from_the_next_gameweek():
    claim = make_claim(1, got=5, gave=6, after=3)                                   # counts from GW4
    points = {3: {5: 99, 6: 99}, 4: {5: 10, 6: 2}, 5: {5: 4, 6: 0}, 6: {5: 6, 6: 1}}  # GW3: before the claim
    moves = league_moves_for([claim], points=points)
    a = manager_row(moves, 100)
    assert (a["moves"], a["claims"], a["net"], a["per_move"]) == (1, 1, 17, 17.0)          # 20 against 3
    assert a["best"]["text"] == "Star5 for Star6" and a["best"]["swing"] == 17
    assert (moves["claims"], moves["trades"], moves["waiting"], moves["average_claim"]) == (1, 0, 0, 17.0)


def test_a_claim_for_a_gameweek_that_has_not_finished_is_only_waiting():
    moves = league_moves_for([make_claim(1, got=5, gave=6, after=6)], points={6: {5: 9}})
    assert (moves["claims"], moves["waiting"]) == (0, 1)
    assert moves["headline"] == "No claims have been judged yet. 1 more is waiting for GW7 to finish."
    after_the_season = league_moves_for([make_claim(1, got=5, gave=6, after=9)], points={6: {5: 9}})
    assert (after_the_season["claims"], after_the_season["waiting"]) == (0, 0)  # no gameweek left to count


def test_a_player_dropped_again_stops_counting_for_the_claim_that_brought_him():
    first = make_claim(1, got=5, gave=6, after=2)                 # Star5 counts from GW3...
    second = make_claim(2, got=7, gave=5, after=4)                # ...until GW5, when he is dropped for Star7
    points = {3: {5: 8, 6: 1}, 4: {5: 2, 6: 1}, 5: {5: 20, 6: 0, 7: 3}, 6: {5: 20, 6: 0, 7: 3}}
    a = manager_row(league_moves_for([first, second], points=points), 100)
    # claim 1: GW3-4, 10 against 2 = +8 (his GW5 and GW6 points are not counted).
    # claim 2: GW5-6, 6 against 40 = -34
    assert (a["moves"], a["net"]) == (2, -26)
    assert (a["best"]["swing"], a["worst"]["swing"]) == (8, -34)


def test_a_claim_dropped_before_it_ever_counted_is_not_judged():
    first = make_claim(1, got=5, gave=6, after=2)
    later = (DEAL_DEADLINES[2] + timedelta(hours=2)).isoformat()      # the same gameweek, a little afterwards
    again = {**make_claim(2, got=7, gave=5, after=2), "added": later}
    points = {3: {5: 8, 6: 1, 7: 4}, 4: {5: 2, 6: 1, 7: 4}}
    a = manager_row(league_moves_for([first, again], points=points), 100)
    assert (a["moves"], a["net"]) == (1, -2)     # only the second claim counts: Star7's 8 points v Star5's 10


def test_denied_claims_and_other_kinds_of_row_are_ignored():
    rows = [make_claim(1, got=5, gave=6, after=2, result="do"),
            make_claim(2, got=5, gave=6, after=2, result="di"),
            make_claim(3, got=5, gave=6, after=2, kind="x"), make_claim(4, got=5, gave=6, after=2, entry=999)]
    moves = league_moves_for(rows, points={3: {5: 8}})
    assert (moves["claims"], moves["waiting"]) == (0, 0)


def test_gameweeks_whose_points_could_not_be_read_are_left_out():
    claim = make_claim(1, got=5, gave=6, after=2)  # GW3-6, but only GW5 was readable
    assert manager_row(league_moves_for([claim], points={5: {5: 6, 6: 1}}), 100)["net"] == 5
    assert league_moves_for([claim], points={})["claims"] == 0                 # nothing readable: not judged


def test_a_trade_counts_for_each_side_as_a_move():
    points = {5: {1: 10, 2: 6, 3: 1, 4: 0}, 6: {1: 8, 2: 2, 3: 0, 4: 2}}
    moves = league_moves_for(trades=[make_trade()], points=points)
    a, b = manager_row(moves, 100), manager_row(moves, 101)
    assert (a["trades"], a["claims"], a["net"], a["best"]["text"]) == (1, 0, 23, "trade with Team B")
    assert (b["net"], b["worst"]["text"]) == (-23, "trade with Team A")
    assert (moves["trades"], moves["claims"], moves["average_claim"]) == (1, 0, None)
    waiting = league_moves_for(trades=[make_trade(after=6)], points=points)
    assert waiting["trades"] == 0 and manager_row(waiting, 100)["moves"] == 0     # not played yet: not judged


def test_managers_are_ranked_by_net_points_and_those_with_no_moves_come_last():
    claims = [make_claim(1, got=5, gave=6, after=2, entry=101),
              make_claim(2, got=7, gave=8, after=2, entry=100)]
    points = {3: {5: 9, 6: 0, 7: 2, 8: 1}}
    moves = league_moves_for(claims, points=points)
    assert [(m["team"], m["net"]) for m in moves["managers"]] == [
        ("Team B", 9), ("Team A", 1), ("Team C", 0), ("Team D", 0)]
    assert [m["per_move"] for m in moves["managers"]] == [9.0, 1.0, None, None]
    assert moves["managers"][2]["best"] is None


def test_the_headline_says_whether_being_active_has_paid():
    def headline(got_points, gave_points):
        claim = make_claim(1, got=5, gave=6, after=2)
        return league_moves_for([claim], points={3: {5: got_points, 6: gave_points}})["headline"]

    assert headline(20, 3) == ("1 claim judged so far. On average a claim has beaten the player dropped "
                               "for it by 17 points, so being active has paid.")
    assert "about as well as the player dropped for it (0)" in headline(3, 3)
    assert "fallen 20 points short of the player dropped for it, so being active hasn't paid" in (
        headline(0, 20))


def test_the_best_and_worst_single_moves_are_listed_and_the_worst_only_if_they_lost_points():
    claims = [make_claim(n, got=10 + n, gave=20 + n, after=2) for n in (1, 2, 3, 4)]
    points = {3: {11: 30, 12: 10, 13: 5, 14: 0, 21: 0, 22: 0, 23: 0, 24: 7}}
    moves = league_moves_for(claims, points=points)
    assert [m["swing"] for m in moves["best"]] == [30, 10, 5]                 # at most three, biggest first
    assert [m["swing"] for m in moves["worst"]] == [-7]                      # only a move that lost points
    assert league_moves_for([make_claim(1, got=5, gave=6, after=2)], points={3: {5: 4, 6: 1}})["worst"] == []


def test_the_lines_praise_the_top_move_maker_and_poke_the_idle(first_wording):
    moves = league_moves_for([make_claim(1, got=5, gave=6, after=2)], points={3: {5: 17, 6: 0}})
    assert [(line["tone"], line["text"]) for line in moves["lines"]] == [
        ("praise", "**Team A** lead the waiver wire: 1 move, 17 points ahead of the players they let go. "
                   "It's basically a second job."),
        ("roast", "**Team B**, **Team C** and **Team D** haven't touched the waiver wire all season. "
                  "It's open, you know.")]


def busy_league():
    """Team A: one big claim (the top). B: three good ones (the sharpest). C: four poor ones (the busiest,
    in the red). D: none. Every claim counts from GW3."""
    claims = [make_claim(1, got=21, gave=22, entry=100, after=2)]
    claims += [make_claim(1 + n, got=30 + n, gave=40 + n, entry=101, after=2) for n in (1, 2, 3)]
    claims += [make_claim(4 + n, got=50 + n, gave=60 + n, entry=102, after=2) for n in (1, 2, 3, 4)]
    points = {21: 40}
    points.update({30 + n: 10 for n in (1, 2, 3)})
    points.update({60 + n: 5 for n in (1, 2, 3, 4)})
    return league_moves_for(claims, points={3: points}, finished=(1, 2, 3))


@pytest.mark.parametrize("index", [0, 1, 2])
def test_every_wording_of_every_moves_line_fills_in(monkeypatch, index):
    used = set()

    def pick(options, kind, gw):
        used.add(kind)
        return options[index]

    monkeypatch.setattr(app, "banter_wording", pick)
    moves = busy_league()
    assert used == set(app.MOVE_LINES)                    # top, sharp, busy, red and idle all appeared
    assert [line["tone"] for line in moves["lines"]] == ["praise", "praise", "roast", "roast", "roast"]
    assert not any("{" in line["text"] or "}" in line["text"] for line in moves["lines"])


def test_the_busiest_manager_is_only_poked_when_they_have_done_worse_than_the_average_claim():
    moves = busy_league()
    assert (manager_row(moves, 102)["moves"], manager_row(moves, 102)["net"]) == (4, -20)
    assert moves["average_claim"] == 6.2                                         # (40 + 30 - 20) / 8, rounded
    # Team C had 4 moves at -5 each, so it is both the busiest and the one in the red
    kinds = [line["text"] for line in moves["lines"]]
    assert sum("**Team C**" in text for text in kinds) == 2                     # a line for each


def test_every_kind_of_moves_line_has_three_wordings_that_only_use_their_fields():
    fields = {"moves_top": {"t", "moves", "net"}, "moves_sharp": {"t", "moves", "per"},
              "moves_busy": {"t", "moves", "net"}, "moves_red": {"t", "moves", "net"}, "moves_idle": {"t"}}
    assert set(app.MOVE_LINES) == set(fields)
    for kind, options in app.MOVE_LINES.items():
        assert len(options) == 3 and len(set(options)) == 3
        for text in options:
            assert set(re.findall(r"{(\w+)}", text)) <= fields[kind], (kind, text)


def test_moves_word_uses_the_right_unit():
    assert [app.moves_word(n) for n in (0, 1, 7)] == ["0 moves", "1 move", "7 moves"]


# the moves route (deals_world has Team A and Team B, GW1-6 finished and one trade counting from GW5)

def get_moves(league=123):
    return app.app.test_client().get(f"/api/league/{league}/moves").get_json()


def live_gameweeks(world):
    return sorted(url.split("/event/")[1].split("/")[0] for url in world["calls"] if "/live" in url)


def test_moves_route_judges_claims_and_the_trade_from_the_live_feeds(deals_world):
    deals_world["transactions"] = [make_claim(1, got=5, gave=6, after=1)]                  # counts from GW2
    deals_world["points"].update({2: {5: 7, 6: 1}, 3: {5: 5, 6: 0}, 4: {5: 3, 6: 2}})
    data = get_moves()
    assert data["league_name"] == "Deal League"
    a = manager_row(data["moves"], 100)
    assert (a["claims"], a["trades"], a["net"]) == (1, 1, 12 + 23)         # claim: 15 against 3; trade: +23
    assert live_gameweeks(deals_world) == ["2", "3", "4", "5", "6"]       # from the first move on, not GW1


def test_moves_route_asks_for_nothing_when_nobody_has_made_a_move(deals_world):
    deals_world["trades"] = []
    data = get_moves()
    assert live_gameweeks(deals_world) == []
    assert data["moves"]["claims"] == 0 and all(m["moves"] == 0 for m in data["moves"]["managers"])


def test_moves_route_is_empty_before_a_gameweek_has_finished(deals_world):
    deals_world["finished"] = []
    assert get_moves()["moves"] is None and live_gameweeks(deals_world) == []


def test_moves_route_still_judges_the_trade_when_the_claims_feed_is_down(deals_world):
    deals_world["down"] = {"transactions"}
    assert manager_row(get_moves()["moves"], 100)["net"] == 23


def test_moves_route_gives_a_friendly_error(monkeypatch):
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/moves")
    assert res.status_code == 400 and "No Draft league found" in res.get_json()["error"]


# ---------------------------------------------------------------- the hub page: where everything went

def test_the_scatter_moved_to_banter_and_the_head_to_head_grid_to_rivalries():
    page = hub_page()
    assert page.index('id="panel-banter"') < page.index('id="scatter"') < page.index('id="panel-recap"')
    assert page.index('id="panel-rivalries"') < page.index('id="h2hTable"') < page.index('id="panel-team"')
    charts = page[page.index('id="panel-charts"'):page.index('id="panel-rivalries"')]
    assert 'id="scatter"' not in charts and 'id="h2hTable"' not in charts
    assert "function renderScatter(ms)" in page and "renderScatter(b.scoring)" in page
    assert "GRID = data.grid" in page and "function renderHeadToHead" in page


def test_the_hub_has_an_odds_choice_and_a_trades_and_moves_choice():
    page = hub_page()
    assert 'data-tab="odds"' in page and 'id="panel-odds"' in page and "function renderOdds" in page
    assert '"deals", "odds", "charts"' in page and "odds: null" in page          # a tab like the others
    assert "Trades and moves" in page and 'id="activitySection"' in page and "function fillActivity" in page
    assert page.index('id="activitySection"') < page.index('id="dealsBody"')       # above the trade tracker
    assert "/moves`" in page and "/odds" not in page.split("function renderOdds")[0].split("<script>")[0]


def test_the_match_up_cards_draw_the_pairings_record():
    page = hub_page()
    assert "c.record" in page and "c.record.line" in page


def test_the_odds_page_opens_a_why_under_each_manager_and_adds_their_best_move():
    page = hub_page()
    assert 'class="orow' in page and "function oddsWhy" in page and "oddsOpen" in page
    assert 'id="oddsNote"' in page and "Best move so far" in page
    assert "const w = o.weights" in page                          # the note says which shares the server used
    assert page.count("askMoves()") >= 3      # Trades and moves and the odds notes share one request
    assert 'class="trend up"' in page and 'class="trend down"' in page


def test_the_tests_cannot_reach_the_real_site():
    with pytest.raises(AssertionError, match="real site"):
        requests.get("https://draft.premierleague.com/api/bootstrap-static")


# ---------------------------------------------------------------- odds over time

# ODDS_WEEKS plus GW4 finished (Team 0 beats 1, Team 2 beats 3); GW5 and GW6 still to play
FOUR_WEEKS = ODDS_WEEKS + [match(4, 10, 60, 11, 40), match(4, 12, 70, 13, 20)] + TO_PLAY[2:]
# ... and the same with GW5 played too (only GW6 to go)
FIVE_WEEKS = (ODDS_WEEKS + [match(4, 10, 60, 11, 40), match(4, 12, 70, 13, 20),
                            match(5, 10, 55, 12, 40), match(5, 11, 45, 13, 44)] + TO_PLAY[4:])


def test_odds_as_of_a_gameweek_ignore_the_later_results():
    league = recap_league(FOUR_WEEKS)
    then = app.odds_as_of(league, 3, 200)
    assert (then["gws_played"], then["gws_left"]) == (3, 3)  # GW4, GW5 and GW6 still to play
    assert {m["entry_id"]: m["league_points"] for m in then["managers"]} == {100: 9, 101: 3, 102: 3, 103: 3}
    now = app.odds_as_of(league, 4, 200)
    assert (now["gws_played"], now["gws_left"]) == (4, 2)
    assert {m["entry_id"]: m["league_points"] for m in now["managers"]} == {100: 12, 101: 3, 102: 6, 103: 3}
    assert all(m["finished"] for m in league["matches"][:8])  # the real details are not changed
    assert then["weights"]["squad"] == 0  # no squads: they weren't kept


def test_the_history_has_the_odds_after_each_gameweek_from_the_third(monkeypatch):
    monkeypatch.setattr(app, "_odds_past", {})
    assert app.odds_history(123, recap_league(ODDS_WEEKS[:4] + TO_PLAY), 100) is None      # two gameweeks
    history = app.odds_history(123, recap_league(FOUR_WEEKS), 300)
    assert (history["runs"], history["gws"]) == (300, [3, 4])
    assert [m["entry_id"] for m in history["managers"]] == [100, 102, 101, 103]  # table order now
    assert history["managers"][0]["team"] == "Team 0"
    for i in range(2):  # every gameweek's chances add up across the league
        assert sum(m["win"][i] for m in history["managers"]) == pytest.approx(1, abs=0.002)
        assert sum(m["top3"][i] for m in history["managers"]) == pytest.approx(3, abs=0.002)
        assert sum(m["last"][i] for m in history["managers"]) == pytest.approx(1, abs=0.002)
    assert all(len(m[k]) == 2 for m in history["managers"] for k in ("win", "top3", "last"))


def test_past_gameweeks_are_only_simulated_once(monkeypatch):
    monkeypatch.setattr(app, "_odds_past", {})
    real, calls = app.odds_as_of, []

    def counted(details, gw, runs):
        calls.append(gw)
        return real(details, gw, runs)

    monkeypatch.setattr(app, "odds_as_of", counted)
    first = app.odds_history(123, recap_league(FOUR_WEEKS), 100)
    assert calls == [3, 4]
    assert app.odds_history(123, recap_league(FOUR_WEEKS), 100) == first and calls == [3, 4]  # nothing again
    app.odds_history(123, recap_league(FIVE_WEEKS), 100)  # a new gameweek
    assert calls == [3, 4, 5]
    app.odds_history(456, recap_league(FOUR_WEEKS), 100)  # another league
    assert calls == [3, 4, 5, 3, 4]


def test_the_history_is_the_same_every_time(monkeypatch):
    monkeypatch.setattr(app, "_odds_past", {})
    first = app.odds_history(123, recap_league(FOUR_WEEKS), 100)
    monkeypatch.setattr(app, "_odds_past", {})
    assert app.odds_history(123, recap_league(FOUR_WEEKS), 100) == first


def test_only_the_latest_past_gameweeks_are_kept(monkeypatch):
    monkeypatch.setattr(app, "_odds_past", {})
    monkeypatch.setattr(app, "ODDS_PAST_KEPT", 1)
    app.odds_history(123, recap_league(FOUR_WEEKS), 50)
    assert list(app._odds_past) == [(123, 4, 50)]


def test_odds_history_route_returns_the_history(fake_api, monkeypatch):
    monkeypatch.setattr(app, "_odds_past", {})
    monkeypatch.setattr(app, "ODDS_HISTORY_RUNS", 100)
    fake = app.get_json
    monkeypatch.setattr(app, "get_json", lambda url: recap_league(FOUR_WEEKS)
                        if url.endswith("/league/123/details") else fake(url))
    data = app.app.test_client().get("/api/league/123/odds-history").get_json()
    assert data["league_name"] == "Recap League" and data["history"]["gws"] == [3, 4]
    assert data["history"]["runs"] == 100 and len(data["history"]["managers"]) == 4


def test_odds_history_route_is_empty_without_enough_results_and_gives_a_friendly_error(fake_api, monkeypatch):
    assert app.app.test_client().get("/api/league/123/odds-history").get_json()["history"] is None
    def not_found(url):
        raise requests.HTTPError(response=type("R", (), {"status_code": 404})())

    monkeypatch.setattr(app, "get_json", not_found)
    res = app.app.test_client().get("/api/league/999/odds-history")
    assert res.status_code == 400 and "No Draft league found" in res.get_json()["error"]


# ---------------------------------------------------------------- who owns which club

def club_player(pid, club, owner, name=None, score=50, games=()):
    """A player for the club tests: `games` are the gameweeks his club plays."""
    return {"id": pid, "name": name or f"P{pid}", "team": club, "owner": owner, "score": score,
            "fixtures": [{"gw": gw, "opp": "XXX", "home": True, "difficulty": 3} for gw in games]}


CLUB_MANAGERS = [{"entry_id": 100, "team_name": "Team A", "manager": "Sam"},
                 {"entry_id": 101, "team_name": "Team B", "manager": "Kim"},
                 {"entry_id": 102, "team_name": "Team C", "manager": "Al"}]


def club_squads(a_liverpool=4):
    """Liverpool have no game in GW11, Man City play twice and Arsenal once. Team A hold most Liverpool."""
    liv, mci, ars = [12], [11, 11], [11]
    players = [club_player(1, "LIV", 100, "Salah", 90, liv), club_player(2, "LIV", 100, "Robertson", 80, liv),
               club_player(3, "LIV", 100, "Gakpo", 70, liv), club_player(4, "LIV", 100, "Jota", 60, liv),
               club_player(5, "LIV", 101, "Alisson", 55, liv), club_player(6, "LIV", None, "Free", 40, liv),
               club_player(7, "MCI", 101, "Haaland", 95, mci), club_player(8, "MCI", 101, "Foden", 85, mci),
               club_player(9, "MCI", 101, "Rodri", 75, mci), club_player(10, "MCI", 102, "Dias", 65, mci),
               club_player(11, "ARS", 100, "Saka", 88, ars), club_player(12, "ARS", 102, "Rice", 78, ars),
               club_player(13, "ARS", 102, "Saliba", 68, ars)]
    for p in players[a_liverpool:4]:          # take some of Team A's Liverpool players away
        p["owner"] = None
    return players


def test_each_squads_clubs_are_counted_and_the_most_all_in_squad_comes_first():
    shown = app.club_exposure(club_squads(), CLUB_MANAGERS, 11)
    assert shown["owned"] == 12  # the free agent isn't counted
    assert [c["club"] for c in shown["clubs"]] == ["LIV", "MCI", "ARS"]            # most owned first
    assert [c["owned"] for c in shown["clubs"]] == [5, 4, 3]
    assert [m["team"] for m in shown["managers"]] == ["Team A", "Team B", "Team C"]  # 4, 3, 2 from one club
    a, b = shown["managers"][0], shown["managers"][1]
    assert (a["total"], a["counts"], a["top_club"], a["top"]) == (5, {"LIV": 4, "ARS": 1}, "LIV", 4)
    assert a["players"]["LIV"] == ["Salah", "Robertson", "Gakpo", "Jota"]           # best rated first
    assert (b["counts"], b["top_club"], b["top"]) == ({"LIV": 1, "MCI": 3}, "MCI", 3)


def test_a_club_with_no_game_or_two_games_next_gameweek_is_flagged():
    clubs = {c["club"]: c for c in app.club_exposure(club_squads(), CLUB_MANAGERS, 11)["clubs"]}
    assert (clubs["LIV"]["games"], clubs["LIV"]["flag"]) == (0, "blank")
    assert (clubs["MCI"]["games"], clubs["MCI"]["flag"]) == (2, "double")
    assert (clubs["ARS"]["games"], clubs["ARS"]["flag"]) == (1, None)


def test_no_club_is_called_blank_when_the_fixtures_are_not_known():
    players = [club_player(1, "LIV", 100), club_player(2, "ARS", 101)]            # nobody has any fixtures
    shown = app.club_exposure(players, CLUB_MANAGERS, 11)
    assert shown["known"] is False
    assert all(c["flag"] is None and c["games"] is None for c in shown["clubs"])
    assert shown["lines"] and not any("GW" in line["text"] for line in shown["lines"])  # no blank/double line


def test_the_lines_say_who_a_blank_or_double_hits_and_who_is_all_in(first_wording):
    texts = [line["text"] for line in app.club_exposure(club_squads(), CLUB_MANAGERS, 11)["lines"]]
    assert texts == [
        "LIV don't play in GW11: **Team A** (4), **Team B** (1) have players with nowhere to go.",
        "MCI play twice in GW11: good news for **Team B** (3), **Team C** (1).",
        "**Team A** have 4 LIV players. If LIV have a bad week, so do they.",
        "LIV are the league's favourite club: 5 of the 12 owned players."]


def test_when_nobody_is_all_in_the_banter_says_so(first_wording):
    texts = [line["text"] for line in app.club_exposure(club_squads(3), CLUB_MANAGERS, 11)["lines"]]
    assert "Nobody has more than 3 players from one club. A very sensible league." in texts
    assert not any("all-in" in t for t in texts)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_every_wording_of_every_club_line_fills_in(monkeypatch, index):
    used = set()

    def pick(options, kind, gw):
        used.add(kind)
        return options[index]

    monkeypatch.setattr(app, "banter_wording", pick)
    texts = [line["text"] for squads in (club_squads(), club_squads(3))
             for line in app.club_exposure(squads, CLUB_MANAGERS, 11)["lines"]]
    assert used == set(app.CLUB_LINES) and texts
    assert not any("{" in t or "}" in t for t in texts)


def test_every_kind_of_club_line_has_three_wordings_that_only_use_their_fields():
    fields = {"clubs_all_in": {"t", "n", "club"}, "clubs_spread": {"n"}, "clubs_most": {"club", "n", "total"},
              "clubs_blank": {"club", "gw", "who"}, "clubs_double": {"club", "gw", "who"}}
    assert set(app.CLUB_LINES) == set(fields)
    for kind, options in app.CLUB_LINES.items():
        assert len(options) == 3 and len(set(options)) == 3
        for text in options:
            assert set(re.findall(r"{(\w+)}", text)) <= fields[kind], (kind, text)


def test_a_manager_with_no_players_is_listed_last_and_does_not_break_it():
    managers = CLUB_MANAGERS + [{"entry_id": 103, "team_name": "Team D", "manager": "Jo"}]
    shown = app.club_exposure(club_squads(), managers, 11)
    empty = shown["managers"][-1]
    assert (empty["team"], empty["total"], empty["counts"], empty["top"], empty["top_club"]) == (
        "Team D", 0, {}, 0, None)
    assert app.club_exposure([], [], 11)["lines"] == []                    # nothing at all: nothing to say


def test_clubs_route_returns_the_exposure_from_the_week_view(monkeypatch):
    seen = []

    def fake_league(league_id, view):
        seen.append((league_id, view))
        return {"league_name": "Club League", "next_gw": 11, "managers": CLUB_MANAGERS,
                "players": club_squads()}

    monkeypatch.setattr(app, "load_league", fake_league)
    data = app.app.test_client().get("/api/league/123/clubs").get_json()
    assert data["league_name"] == "Club League" and data["clubs"]["gw"] == 11 and seen == [(123, "week")]
    assert data["clubs"]["managers"][0]["players"]["LIV"][0] == "Salah"


def test_clubs_route_gives_a_friendly_error(monkeypatch):
    def down(league_id, view):
        raise app.FplError("The Draft site is having a moment.", 502)

    monkeypatch.setattr(app, "load_league", down)
    res = app.app.test_client().get("/api/league/123/clubs")
    assert res.status_code == 502 and "having a moment" in res.get_json()["error"]


def test_the_odds_page_draws_how_the_odds_moved_under_the_grid():
    page = hub_page()
    assert 'id="oddsMoved"' in page and "function renderMoved" in page and "/odds-history`" in page
    assert page.index('id="oddsTable"') < page.index('id="oddsMoved"') < page.index('id="panel-charts"')
    assert all(f'data-metric="{metric}"' in page for metric in ("win", "top3", "last"))  # the three chances
    assert "if (ohFor !== leagueId) loadMoved();" in page          # asked for once, after the grid


def test_the_charts_page_has_a_club_grid_under_the_weekly_scores():
    page = hub_page()
    assert 'id="clubsTable"' in page and "function renderClubs" in page and "/clubs`" in page
    assert page.index('id="spread"') < page.index('id="clubsSection"') < page.index('id="squadsSection"')
    assert page.index('id="squadsSection"') < page.index('id="panel-rivalries"')
    assert "loadClubs();" in page  # it loads on its own, like the squad strength


# ---------------------------------------------------------------- side battles

BATTLE_TEAMS = {100: "Team A", 101: "Team B", 102: "Team C", 103: "Team D"}
BATTLE_FIXTURES = [{"home": "ARS", "away": "NEW", "kickoff": "2026-11-14T12:30:00+00:00"},
                   {"home": "LIV", "away": "MUN", "kickoff": "2026-11-14T15:00:00+00:00"}]
# Team A has 3 Arsenal and Team B 2 Newcastle: a side battle in ARS v NEW. Liverpool and Man Utd are owned
# on one side only or one player at a time
BATTLE_OWNED = {"ARS": {100: ["Saka", "Rice", "Saliba"], 102: ["Gabriel"]},
                "NEW": {101: ["Isak", "Gordon"], 103: ["Pope"]},
                "LIV": {100: ["Salah"], 101: ["Alisson"]},
                "MUN": {102: ["Bruno", "Onana"], 103: ["Dalot", "Mount"]}}


def battles_for(pairs, fixtures=BATTLE_FIXTURES, owned=BATTLE_OWNED, gw=11):
    return app.side_battles(gw, fixtures, owned, pairs, BATTLE_TEAMS)


def test_two_managers_on_opposite_sides_of_a_fixture_with_two_players_each_have_a_side_battle(first_wording):
    shown = battles_for([(100, 101), (102, 103)])
    first, second = shown["matches"]
    assert (first["battles"], first["biggest"]) == (1, {
        "home": "ARS", "away": "NEW", "a_club": "ARS", "a_n": 3, "b_club": "NEW", "b_n": 2})
    assert first["line"] == "Side battle: **Team A**'s 3 ARS v **Team B**'s 2 NEW in ARS v NEW."
    # Teams C and D own 1 Arsenal and 1 Newcastle player, and both own Man Utd: no battle
    assert (second["battles"], second["biggest"], second["line"]) == (0, None, None)
    assert (first["a_team"], first["b_team"]) == ("Team A", "Team B")


def test_the_battle_reads_the_same_whichever_manager_is_first():
    flipped = battles_for([(101, 100)])["matches"][0]
    assert flipped["biggest"] == {
        "home": "ARS", "away": "NEW", "a_club": "NEW", "a_n": 2, "b_club": "ARS", "b_n": 3}
    assert "**Team B**" in flipped["line"] and "**Team A**" in flipped["line"]


def test_a_player_each_is_not_enough_unless_the_bar_is_lowered(monkeypatch):
    assert battles_for([(102, 103)])["matches"][0]["battles"] == 0              # one Arsenal v one Newcastle
    monkeypatch.setattr(app, "BATTLE_MIN", 1)
    assert battles_for([(102, 103)])["matches"][0]["battles"] == 1


def test_each_fixture_lists_who_is_on_each_side_and_which_pairs_are_fighting_there():
    arsenal_newcastle, liverpool_united = battles_for([(100, 101)])["fixtures"]
    assert [(o["team"], o["n"]) for o in arsenal_newcastle["home_owners"]] == [("Team A", 3), ("Team C", 1)]
    assert arsenal_newcastle["home_owners"][0]["players"] == ["Saka", "Rice", "Saliba"]
    assert [(o["team"], o["n"]) for o in arsenal_newcastle["away_owners"]] == [("Team B", 2), ("Team D", 1)]
    assert arsenal_newcastle["battles"] == [{"a": "Team A", "b": "Team B"}]
    assert liverpool_united["battles"] == []
    assert arsenal_newcastle["kickoff"] == "2026-11-14T12:30:00+00:00"  # fixture fields are kept


def test_the_biggest_battle_has_the_most_players_and_a_double_gameweek_counts_twice(first_wording):
    owned = {**BATTLE_OWNED, "LIV": {100: ["Salah", "Jota"], 101: ["Alisson"]},
             "MUN": {101: ["Bruno", "Dalot"]}}
    fixtures = BATTLE_FIXTURES + [{"home": "ARS", "away": "NEW", "kickoff": "2026-11-17T19:45:00+00:00"}]
    match = battles_for([(100, 101)], fixtures, owned)["matches"][0]
    assert match["battles"] == 3                                  # ARS v NEW twice, and Liverpool v Man Utd
    assert (match["biggest"]["a_n"], match["biggest"]["b_n"]) == (3, 2)        # not the 2 v 2 at Anfield


def test_a_match_with_no_manager_or_no_owners_makes_no_fuss():
    assert battles_for([])["matches"] == []
    empty = battles_for([(100, 101)], owned={})
    assert empty["matches"][0]["battles"] == 0 and empty["fixtures"][0]["home_owners"] == []
    assert battles_for([(100, 999)])["matches"][0]["b_team"] == "Team 999"  # a manager the names don't know


@pytest.mark.parametrize("index", [0, 1, 2])
def test_every_wording_of_the_side_battle_line_fills_in(monkeypatch, index):
    monkeypatch.setattr(app, "banter_wording", lambda options, kind, gw: options[index])
    line = battles_for([(100, 101)])["matches"][0]["line"]
    assert "**Team A**" in line and "ARS" in line and "NEW" in line and "{" not in line


def test_the_battle_wordings_only_use_their_fields():
    options = app.BATTLE_LINES["battle"]
    assert len(options) == 3 and len(set(options)) == 3
    fields = {"a", "na", "ca", "b", "nb", "cb", "home", "away"}
    assert all(set(re.findall(r"{(\w+)}", text)) <= fields for text in options)


# the battles route over the recap world: GW3 is the first with unfinished matches (Team 0 v Team 1 and
# Team 2 v Team 3), ARS v CHE and LIV v MCI are the fixtures, and each team starts two players (one at each
# of two clubs)

def get_battles(league=123):
    return app.app.test_client().get(f"/api/league/{league}/battles").get_json()


def test_the_battles_route_reads_the_gameweek_the_cards_are_for(recap_world):
    shown = get_battles()["battles"]
    assert shown["gw"] == 3
    assert [(f["home"], f["away"]) for f in shown["fixtures"]] == [
        ("ARS", "CHE"), ("LIV", "MCI")]                                                        # by kickoff
    first = shown["fixtures"][0]
    assert [(o["team"], o["players"]) for o in first["home_owners"]] == [
        ("Team 0", ["Saka"]), ("Team 2", ["Player5"])]
    assert [(m["a_team"], m["b_team"], m["battles"]) for m in shown["matches"]] == [
        ("Team 0", "Team 1", 0), ("Team 2", "Team 3", 0)]          # one player each: not enough


def test_the_battles_route_finds_side_battles_when_the_bar_is_one_player(recap_world, monkeypatch):
    monkeypatch.setattr(app, "BATTLE_MIN", 1)
    first, second = get_battles()["battles"]["matches"]
    assert first["battles"] == 2 and second["battles"] == 2          # ARS v CHE and LIV v MCI, for both pairs
    assert first["line"].startswith("Side battle: **Team 0**") or "**Team 0**" in first["line"]


def test_the_battles_route_has_nothing_when_every_match_is_finished_or_the_fixtures_are_down(recap_world):
    recap_world["fixtures"] = None
    assert get_battles()["battles"] is None
    recap_world["fixtures"] = [{"event": 3, "team_h": 1, "team_a": 2, "kickoff_time": "2026-10-10T11:30:00Z",
                                "started": False, "finished": False}]
    recap_world["matches"] = RECAP_WEEKS[:4]                         # nothing left to play
    assert get_battles()["battles"] is None


def test_the_battles_route_gives_an_error_when_the_owners_cannot_be_read(recap_world):
    recap_world["owners"] = False
    res = app.app.test_client().get("/api/league/123/battles")
    assert res.status_code >= 400 and "error" in res.get_json()


# ---------------------------------------------------------------- the club market

MARKET_CLUBS = {1: "BHA", 2: "BHA", 3: "ARS", 4: "ARS", 5: "MUN", 6: "LIV"}


def claim_row(got, gave, kind="w", result="a"):
    return {"kind": kind, "result": result, "element_in": got, "element_out": gave}


def test_the_market_counts_players_claimed_and_dropped_for_each_club(first_wording):
    rows = [claim_row(1, 3), claim_row(2, 4), claim_row(1, 3), claim_row(5, 3, kind="f")]
    market = app.club_market(rows, MARKET_CLUBS, 10)
    assert market["claims"] == 4
    assert market["clubs"] == [{"club": "BHA", "bought": 3, "sold": 0, "net": 3},
                               {"club": "MUN", "bought": 1, "sold": 0, "net": 1},
                               {"club": "ARS", "bought": 0, "sold": 4, "net": -4}]       # most bought first
    assert [line["text"] for line in market["lines"]] == [
        "The league is buying BHA: 3 claimed and 0 dropped.",
        "The league is dumping ARS: 4 dropped and only 0 claimed."]


def test_denied_claims_trades_and_unknown_players_are_not_counted(first_wording):
    rows = [claim_row(1, 3, result="do"), claim_row(1, 3, result="di"), claim_row(1, 3, kind="t"),
            claim_row(99, 98), claim_row(6, 5)]
    market = app.club_market(rows, MARKET_CLUBS, 10)
    assert market["claims"] == 2                  # the unknown players' claim still counts as a claim...
    assert market["clubs"] == [{"club": "LIV", "bought": 1, "sold": 0, "net": 1},
                               {"club": "MUN", "bought": 0, "sold": 1, "net": -1}]     # ...but moves no club
    assert [line["text"] for line in market["lines"]][0].startswith("No club has moved by more than 1")


def test_clubs_with_the_same_net_are_ordered_by_buying_then_name():
    rows = [claim_row(1, 5), claim_row(3, 5), claim_row(6, 6)]  # BHA +1, ARS +1, LIV 0, MUN -2
    clubs = app.club_market(rows, MARKET_CLUBS, 10)["clubs"]
    assert [c["club"] for c in clubs] == ["ARS", "BHA", "LIV", "MUN"]


def test_no_claims_means_no_market():
    market = app.club_market([], MARKET_CLUBS, 10)
    assert market == {"claims": 0, "clubs": [], "lines": []}


@pytest.mark.parametrize("index", [0, 1, 2])
def test_every_wording_of_every_market_line_fills_in(monkeypatch, index):
    used = set()

    def pick(options, kind, gw):
        used.add(kind)
        return options[index]

    monkeypatch.setattr(app, "banter_wording", pick)
    busy = [claim_row(1, 3), claim_row(2, 4), claim_row(1, 3), claim_row(2, 4)]
    quiet = [claim_row(1, 3)]
    texts = [line["text"] for rows in (busy, quiet)
             for line in app.club_market(rows, MARKET_CLUBS, 10)["lines"]]
    assert used == set(app.MARKET_LINES) and texts and not any("{" in t for t in texts)


def test_every_kind_of_market_line_has_three_wordings_that_only_use_their_fields():
    fields = {"market_buy": {"club", "bought", "sold"}, "market_sell": {"club", "bought", "sold"},
              "market_quiet": {"n"}}
    assert set(app.MARKET_LINES) == set(fields)
    for kind, options in app.MARKET_LINES.items():
        assert len(options) == 3 and len(set(options)) == 3
        assert all(set(re.findall(r"{(\w+)}", text)) <= fields[kind] for text in options)


def test_the_market_route_counts_the_leagues_claims_by_club(deals_world):
    # in the deals world the odd players play for ARS and the even ones for CHE
    deals_world["transactions"] = [make_claim(1, got=5, gave=6, after=1),
                                   make_claim(2, got=7, gave=8, after=1)]
    data = app.app.test_client().get("/api/league/123/market").get_json()
    assert data["league_name"] == "Deal League"
    assert data["market"]["claims"] == 2
    assert {c["club"]: (c["bought"], c["sold"]) for c in data["market"]["clubs"]} == {
        "ARS": (2, 0), "CHE": (0, 2)}
    assert [call for call in deals_world["calls"] if "/live" in call] == []        # it asks for no points


def test_the_market_route_gives_an_error_when_the_claims_cannot_be_read(deals_world):
    deals_world["down"] = {"transactions"}
    res = app.app.test_client().get("/api/league/123/market")
    assert res.status_code >= 400 and "error" in res.get_json()


# ---------------------------------------------------------------- the pages for them

def test_banter_has_a_whos_cheering_for_whom_section_with_the_side_battles():
    page = hub_page()
    assert 'id="battlesSection"' in page and "function renderBattles" in page and "/battles`" in page
    banter = page[page.index('id="panel-banter"'):page.index('id="panel-charts"')]
    assert 'id="battlesSection"' in banter and 'id="fixtureList"' in banter
    assert "refreshBattles()" in page                       # asked for with the cards, as a bonus
    assert "BATTLES = null" in page                         # forgotten when another league is opened
    # your own team is marked "me", not "mine": .mine is the style of the Your matches card
    assert '.fxsides .own.me' in page and '"mine" : ""}" title=' not in page


def test_side_battles_show_on_the_pinned_cards_and_in_the_next_up_card():
    page = hub_page()
    assert "function battleFor" in page and "battleLine(c)" in page
    assert "battleFor(myId, next.opponent, next.gw)" in page
    assert page.count("clash?.line") == 1                   # drawn under the record in Your matches


def test_the_charts_page_has_the_club_market_under_the_club_grid():
    page = hub_page()
    assert 'id="marketBlock"' in page and "function renderMarket" in page and "/market`" in page
    assert page.index('id="clubsPicked"') < page.index('id="marketBlock"') < page.index('id="squadsSection"')
    assert "loadMarket();" in page and "MARKET = null" in page


# ---------------------------------------------------------------- squad DNA and where the points are

def test_each_squad_has_its_dna_the_biggest_stack_first():
    a, b, c = app.club_exposure(club_squads(), CLUB_MANAGERS, 11)["managers"]
    assert (a["stacks"], a["clubs_n"]) == ([{"club": "LIV", "n": 4}, {"club": "ARS", "n": 1}], 2)
    assert (b["stacks"], b["clubs_n"]) == ([{"club": "MCI", "n": 3}, {"club": "LIV", "n": 1}], 2)
    assert (c["stacks"], c["clubs_n"]) == ([{"club": "ARS", "n": 2}, {"club": "MCI", "n": 1}], 2)
    assert all(sum(s["n"] for s in m["stacks"]) == m["total"] for m in (a, b, c))      # the whole squad


def test_stacks_of_the_same_size_go_by_club_code_and_an_empty_squad_has_none():
    squad = [club_player(1, "ZZZ", 100), club_player(2, "ZZZ", 100), club_player(3, "AAA", 100),
             club_player(4, "AAA", 100), club_player(5, "MMM", 100)]
    managers = CLUB_MANAGERS[:1] + [{"entry_id": 103, "team_name": "Team D", "manager": "Jo"}]
    mine, empty = app.club_exposure(squad, managers, 11)["managers"]
    assert [s["club"] for s in mine["stacks"]] == ["AAA", "ZZZ", "MMM"] and mine["clubs_n"] == 3
    assert (empty["stacks"], empty["clubs_n"]) == ([], 0)


POINTS_SHORT = {1: "ARS", 2: "BHA", 3: "CRY"}


def points_player(pid, club, points, name=None):
    return {"id": pid, "team": club, "web_name": name or f"P{pid}", "total_points": points}


def points_world():
    """
    ARS: four owned players (50, 40, 30, 20) and two free agents on 10 each. BHA: five owned on 20 each and
    one free agent on 60. CRY: one owned player on 90 and one free agent who has scored nothing. A player of a
    club the list doesn't know is ignored.
    """
    elements = [points_player(i, 1, p) for i, p in zip(range(1, 5), (50, 40, 30, 20))]
    elements += [points_player(5, 1, 10, "Fred"), points_player(6, 1, 10, "Abel")]
    elements += [points_player(i, 2, 20) for i in range(10, 15)] + [points_player(15, 2, 60, "Gem")]
    elements += [points_player(20, 3, 90), points_player(21, 3, 0, "Dud"), points_player(99, 9, 500)]
    owner_of = {i: 100 for i in (1, 2, 3, 4, 10, 11, 12, 13, 14, 20)}
    return elements, owner_of


def get_points(elements=None, owner_of=None, week=10):
    world = points_world()
    return app.club_points(world[0] if elements is None else elements,
                           world[1] if owner_of is None else owner_of, POINTS_SHORT, week)


def test_each_clubs_points_are_split_into_owned_and_on_the_wire(first_wording):
    shown = get_points()
    clubs = {c["club"]: c for c in shown["clubs"]}
    assert shown["owned"] == 10
    assert (clubs["ARS"]["owned"], clubs["ARS"]["owned_points"], clubs["ARS"]["wire_points"]) == (4, 140, 20)
    assert (clubs["BHA"]["owned"], clubs["BHA"]["owned_points"], clubs["BHA"]["wire_points"]) == (5, 100, 60)
    assert [clubs[c]["per_player"] for c in ("ARS", "BHA", "CRY")] == [35.0, 20.0, 90.0]
    assert "9" not in clubs and len(clubs) == 3                  # the unknown club's player is ignored


def test_the_best_free_agent_is_the_top_scorer_ties_by_name_and_none_if_nobody_scored():
    clubs = {c["club"]: c for c in get_points()["clubs"]}
    assert clubs["BHA"]["best_wire"] == {"name": "Gem", "points": 60}
    assert clubs["ARS"]["best_wire"] == {"name": "Abel", "points": 10}        # tied on 10 with Fred: by name
    assert clubs["CRY"]["best_wire"] is None                                  # the only free agent scored 0


def test_clubs_come_with_the_most_points_on_the_wire_first_and_ties_by_code():
    assert [c["club"] for c in get_points()["clubs"]] == ["BHA", "ARS", "CRY"]
    even = [points_player(1, 2, 10), points_player(2, 1, 10), points_player(3, 3, 10)]
    assert [c["club"] for c in get_points(even, {})["clubs"]] == ["ARS", "BHA", "CRY"]


def test_a_club_with_no_owned_players_has_no_points_per_player():
    only_free = get_points([points_player(1, 1, 25, "Free")], {})
    assert only_free["clubs"][0]["per_player"] is None and only_free["clubs"][0]["owned"] == 0


def test_the_lines_name_the_wire_club_and_the_best_and_worst_to_own(first_wording):
    texts = [line["text"] for line in get_points()["lines"]]
    assert texts == ["BHA have the most points on the wire: 60, against 100 in squads.",
                     "ARS pay best for the league: 35 points per owned player.",
                     "BHA pay worst: only 20 points per owned player."]    # CRY's 90 is one player: unranked


def test_no_best_or_worst_line_without_two_clubs_with_enough_owned_players(first_wording, monkeypatch):
    monkeypatch.setattr(app, "CLUB_POINTS_MIN_OWNED", 5)           # only BHA has five
    assert [line["text"] for line in get_points()["lines"]] == [
        "BHA have the most points on the wire: 60, against 100 in squads."]
    monkeypatch.setattr(app, "CLUB_POINTS_MIN_OWNED", 1)           # all three count
    assert [line["text"] for line in get_points()["lines"]][1:] == [
        "CRY pay best for the league: 90 points per owned player.",
        "BHA pay worst: only 20 points per owned player."]


def test_there_is_no_wire_line_when_nobody_is_on_the_wire_and_nothing_at_all_for_no_players(monkeypatch):
    owned_all = [points_player(i, 1 + i % 2, 10) for i in range(1, 9)]
    shown = get_points(owned_all, {p["id"]: 100 for p in owned_all})
    assert not any("wire" in line["text"] for line in shown["lines"])
    assert get_points([], {}) == {"owned": 0, "clubs": [], "lines": []}


@pytest.mark.parametrize("index", [0, 1, 2])
def test_every_wording_of_every_points_line_fills_in(monkeypatch, index):
    used = set()

    def pick(options, kind, gw):
        used.add(kind)
        return options[index]

    monkeypatch.setattr(app, "banter_wording", pick)
    texts = [line["text"] for line in get_points()["lines"]]
    assert used == set(app.POINTS_LINES) and len(texts) == 3
    assert not any("{" in t or "}" in t for t in texts)


def test_every_kind_of_points_line_has_three_wordings_that_only_use_their_fields():
    fields = {"points_wire": {"club", "wire", "owned"}, "points_best": {"club", "per"},
              "points_worst": {"club", "per"}}
    assert set(app.POINTS_LINES) == set(fields)
    for kind, options in app.POINTS_LINES.items():
        assert len(options) == 3 and len(set(options)) == 3
        assert all(set(re.findall(r"{(\w+)}", text)) <= fields[kind] for text in options)


def test_the_club_points_route_adds_up_the_leagues_players_by_club(deals_world):
    # in the deals world the odd players play for ARS and the even ones for CHE; 1-4 are owned, 5-8 are free
    # agents, and everyone has 30 points
    data = app.app.test_client().get("/api/league/123/club-points").get_json()
    shown = data["points"]
    assert data["league_id"] == 123 and shown["owned"] == 4
    assert [(c["club"], c["owned"], c["owned_points"], c["wire_points"]) for c in shown["clubs"]] == [
        ("ARS", 2, 60, 60), ("CHE", 2, 60, 60)]
    assert shown["clubs"][0]["best_wire"] == {"name": "Star5", "points": 30}
    assert shown["lines"][0]["text"].count("60") == 2 and "ARS" in shown["lines"][0]["text"]
    assert [call for call in deals_world["calls"] if "/live" in call or "/transactions" in call] == []


def test_the_club_points_route_gives_an_error_when_the_owners_cannot_be_read(deals_world):
    deals_world["down"] = {"owners"}
    res = app.app.test_client().get("/api/league/123/club-points")
    assert res.status_code >= 400 and "error" in res.get_json()


def test_the_club_section_opens_on_squad_dna_with_the_grid_behind_a_switch():
    page = hub_page()
    assert 'id="clubsDna"' in page and 'id="clubsView"' in page and "function showClubs" in page
    assert page.index('id="clubsView"') < page.index('id="clubsDna"') < page.index('id="clubsGrid"')
    assert page.index('id="clubsGrid"') < page.index('id="clubsPicked"') < page.index('id="pointsBlock"')
    assert 'data-clubs="dna" aria-pressed="true"' in page            # Squad DNA is the first view
    assert 'class="tablewrap hidden" id="clubsGrid"' in page         # and the grid starts hidden
    assert "function pickClub" in page and '$("clubsDna").addEventListener("click"' in page


def test_the_charts_page_has_where_the_points_are_between_the_club_picker_and_the_market():
    page = hub_page()
    assert 'id="pointsBlock"' in page and "function renderWire" in page and "/club-points`" in page
    assert page.index('id="clubsPicked"') < page.index('id="pointsBlock"') < page.index('id="marketBlock"')
    assert "loadWire();" in page and "WIRE = null" in page  # asked for with the grid, reset on switch
    assert "WIRE_SHOWN" in page and 'id="pointsMore"' in page          # the first clubs, then "Show all"


def test_squads_with_the_same_biggest_stack_go_by_fewest_clubs_then_name():
    wide = [club_player(n, c, 100) for n, c in enumerate(("AAA", "AAA", "BBB", "CCC"), 1)]      # 3 clubs
    narrow = [club_player(n, c, 101) for n, c in enumerate(("AAA", "AAA", "BBB"), 10)]         # 2 clubs
    twin = [club_player(n, c, 102) for n, c in enumerate(("DDD", "DDD", "EEE"), 20)]           # 2 clubs
    shown = app.club_exposure(wide + narrow + twin, CLUB_MANAGERS, 11)
    assert [m["team"] for m in shown["managers"]] == ["Team B", "Team C", "Team A"]    # all have a stack of 2
