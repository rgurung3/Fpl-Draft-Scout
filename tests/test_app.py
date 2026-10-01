"""
Tests for Draft Scout. They use fake data, so they never call the real
FPL servers and always give the same result.

Run with:  pytest -v
"""
from datetime import timedelta

import pytest
import requests

import app
import check_league

# ---------------------------------------------------------------- fake data

def make_player(pid, team=1, pos=3, form="5.0", ppg="5.0", minutes=540,
                xgi="2.0", status="a", chance=None, creativity="20.0", xgc="6.0", dc=30):
    return {
        "id": pid, "web_name": f"Player{pid}", "team": team, "element_type": pos,
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
            return {"history": [{"event": 4, "minutes": 26}, {"event": 5, "minutes": 71},
                                {"event": 5, "minutes": 10}]}
        if "bootstrap" in url:
            return {"teams": TEAMS}
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


def test_facts_are_capped_and_not_repeated():
    kinds = [f["kind"] for f in app.league_banter(banter_league(BANTER_WEEKS))["facts"]]
    assert len(kinds) == app.FACT_COUNT
    assert len(kinds) == len(set(kinds))


def test_harsh_fact_names_the_highest_losing_score(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    assert facts_by_kind(BANTER_WEEKS)["harsh"] == (
        "Team 2 scored 69 in GW2 and still lost to Team 0 (70). Harsh.")


def test_each_fact_reads_from_the_numbers(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    facts = facts_by_kind(BANTER_WEEKS)
    assert facts["thrashing"].startswith("Biggest beating so far: Team 2 90-50 Team 3 in GW1, a 40-point gap")
    assert facts["closest"] == "Closest match so far: Team 0 edged Team 2 70-69 in GW2."
    assert facts["low"].startswith("Lowest score so far: Team 4 managed 20 in GW2")
    assert facts["high"] == "Best week so far: Team 2 put up 90 in GW1."


def test_luck_fact_finds_the_manager_whose_table_place_lags_their_points(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    # Team 2 scores 200 (the most) but loses both games by a point, so it sits 4th in the table
    weeks = [match(1, 12, 100, 13, 101), match(1, 10, 10, 11, 5),
             match(2, 12, 100, 14, 101), match(2, 10, 10, 13, 5)]
    assert facts_by_kind(weeks)["luck"] == (
        "Team 2 are 1st for points scored but only 4th in the table. The fixture list has not been kind.")


def test_a_winning_run_is_called_out(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    weeks = [match(gw, 10, 60, 11 + gw, 40) for gw in (1, 2, 3)]
    assert facts_by_kind(weeks)["streak"] == "Team 0 have won 3 in a row. Somebody stop them."


def test_a_losing_run_is_teased_kindly(monkeypatch):
    monkeypatch.setattr(app, "FACT_COUNT", 20)
    weeks = [match(1, 10, 60, 11, 40), match(2, 12, 60, 11, 40), match(3, 13, 60, 11, 40)]
    assert facts_by_kind(weeks)["streak"] == "Team 1 have lost 3 in a row. A hug may be needed."


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


def test_league_hub_offers_the_three_choices():
    res = app.app.test_client().get("/league?league=123")
    assert res.status_code == 200
    for choice in (b'data-tab="banter"', b'data-tab="charts"', b'data-tab="team"'):
        assert choice in res.data
    res.close()


@pytest.mark.parametrize("old,tab", [("/banter", "banter"), ("/charts", "charts")])
def test_old_page_links_open_the_hub_on_that_tab(old, tab):
    res = app.app.test_client().get(old + "?league=123&team=100&junk=1")
    assert res.status_code == 302
    assert res.headers["Location"] == f"/league?league=123&team=100&tab={tab}"   # junk is dropped


def test_old_page_link_without_a_league_still_opens_the_hub():
    res = app.app.test_client().get("/charts")
    assert res.headers["Location"] == "/league?tab=charts"
