"""
Tests for Draft Scout. They use fake data, so they never call the real
FPL servers and always give the same result.

Run with:  pytest -v
"""
import json
from datetime import datetime, timedelta, timezone

import anthropic
import pytest
import requests

import app
import check_league

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


def test_cards_are_left_out_when_there_are_too_few_matches():
    banter = app.league_banter(banter_league(BANTER_WEEKS))   # GW3 has two matches
    assert banter["hot_match"] is not None
    assert [b["title"] for b in banter["battles"]] == ["Battle for 3rd"]


def test_no_battles_when_nothing_is_left_to_play():
    banter = app.league_banter(banter_league(BANTER_WEEKS[:4]))
    assert banter["hot_match"] is None and banter["battles"] == []


def facts_by_kind(matches):
    return {f["kind"]: f["text"] for f in app.league_banter(banter_league(matches))["facts"]}


def test_facts_are_capped_and_not_repeated(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 4)
    kinds = [f["kind"] for f in app.league_banter(banter_league(BANTER_WEEKS))["facts"]]
    assert len(kinds) == 4
    assert len(kinds) == len(set(kinds))


def test_harsh_fact_names_the_highest_losing_score(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    assert facts_by_kind(BANTER_WEEKS)["harsh"] == (
        "**Team 2** scored 69 in GW2 and still lost to **Team 0** (70). Harsh.")


def test_each_fact_reads_from_the_numbers(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    facts = facts_by_kind(BANTER_WEEKS)
    assert facts["thrashing"].startswith(
        "Biggest beating so far: **Team 2** 90-50 **Team 3** in GW1, a 40-point gap")
    assert facts["closest"] == "Closest match so far: **Team 0** edged **Team 2** 70-69 in GW2."
    assert facts["low"].startswith("Lowest score so far: **Team 4** managed 20 in GW2")
    assert facts["high"] == "Best week so far: **Team 2** put up 90 in GW1."


def test_luck_fact_finds_the_manager_whose_table_place_lags_their_points(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    monkeypatch.setattr(app, "LUCK_FACT_MIN", 99)   # no "play everyone" facts (they name Team 2 too)
    # Team 2 scores 200 (the most) but loses both games by a point, so it sits 4th in the table
    weeks = [match(1, 12, 100, 13, 101), match(1, 10, 10, 11, 5),
             match(2, 12, 100, 14, 101), match(2, 10, 10, 13, 5)]
    assert facts_by_kind(weeks)["luck"] == (
        "**Team 2** are 1st for points scored but only 4th in the table. The fixture list has not been kind.")


def test_a_winning_run_is_called_out(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    weeks = [match(gw, 10, 60, 11 + gw, 40) for gw in (1, 2, 3)]
    assert facts_by_kind(weeks)["streak"] == "**Team 0** have won 3 in a row. Somebody stop them."


def test_a_losing_run_is_teased_kindly(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    weeks = [match(1, 10, 60, 11, 40), match(2, 12, 60, 11, 40), match(3, 13, 60, 11, 40)]
    assert facts_by_kind(weeks)["streak"] == "**Team 1** have lost 3 in a row. A hug may be needed."


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


@pytest.mark.parametrize("n,text", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
                                    (11, "11th"), (12, "12th"), (21, "21st")])
def test_ordinal(n, text):
    assert app.ordinal(n) == text


# ---------------------------------------------------------------- trade and banter pages

@pytest.mark.parametrize("path,text", [("/trade", b"Trade analyzer"), ("/league", b"League hub")])
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


def test_luck_facts_name_the_luckiest_and_unluckiest(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    # 100 scores well but meets 101, the top scorer, both weeks; 103 scores poorly but meets the bottom team
    weeks = [match(1, 10, 60, 11, 65), match(1, 12, 10, 13, 20),
             match(2, 10, 58, 11, 70), match(2, 12, 10, 13, 15)]
    facts = {f["kind"]: f["text"] for f in app.league_banter(h2h(weeks))["facts"]}
    assert facts["lucky"] == ("**Team 103** have 6 league points, but their scores only deserved 2. "
                              "Don't ask questions.")
    assert facts["unlucky"] == ("**Team 100** have 0 league points, but their scores deserved 4. "
                                "Robbed by the fixture list.")


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
    def player(pid, name, pos, score, owner):
        return {"id": pid, "name": name, "pos": pos, "team": "ARS", "score": score, "owner": owner}

    return {
        "view": "week", "windows": {"week": {"from": 7, "to": 11}, "season": {"from": 7, "to": 13}},
        "players": [player(1, "Raya", "GKP", 70.0, 100), player(2, "Saka", "MID", 80.0, 100),
                    player(3, "Gabriel", "DEF", 60.0, 100), player(4, "Haaland", "FWD", 90.0, 100),
                    player(5, "Odegaard", "MID", 55.0, 100), player(6, "Bench", "DEF", 30.0, 100),
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
    assert weaker["best_xi"][0] == {"name": "Raya", "pos": "GKP", "club": "ARS", "rating": 70.0}
    assert [p["name"] for p in weaker["bench"]] == ["Bench"]
    assert (out["view"], out["windows"]["week"]) == ("week", {"from": 7, "to": 11})


def test_squads_leave_out_players_nobody_owns_and_cope_with_an_empty_squad():
    out = app.league_squads(squad_data())
    everyone = [p["name"] for m in out["managers"] for p in m["best_xi"] + m["bench"]]
    assert "Free" not in everyone
    empty = out["managers"][2]
    assert empty["best_xi"] == [] and empty["bench"] == []


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
