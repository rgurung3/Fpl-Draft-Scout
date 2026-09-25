"""
Tests for Draft Scout. They use fake data, so they never call the real
FPL servers and always give the same result.

Run with:  pytest -v
"""
import pytest
import requests

import app
import check_league

# ---------------------------------------------------------------- fake data

def make_player(pid, team=1, pos=3, form="5.0", ppg="5.0", minutes=540,
                xgi="2.0", status="a", chance=None):
    return {
        "id": pid, "web_name": f"Player{pid}", "team": team, "element_type": pos,
        "form": form, "points_per_game": ppg, "total_points": 30,
        "minutes": minutes, "expected_goal_involvements": xgi,
        "status": status, "news": "", "chance_of_playing_next_round": chance,
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

    def fake_get_json(url):
        if "draft" in url and "bootstrap" in url:
            return boot
        if url.endswith("/details"):
            return details
        if url.endswith("/element-status"):
            return status
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


def test_low_minutes_players_get_no_xgi_boost():
    cameo = make_player(1, minutes=45, xgi="1.0")
    scores = app.score_players([cameo], {}, current_gw=6)
    assert scores[1]["xgi90"] == 0


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
