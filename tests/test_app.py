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

def rated(pid, pos, score, owner=None, chance=100):
    """A player as the page sees it, with only the fields the squad logic uses."""
    return {"id": pid, "pos": pos, "score": score, "owner": owner, "chance": chance}


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
    assert targets == [{"drop": 1, "claim": 2, "gain": 20, "backup": None}]


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


def test_team_endpoint_includes_targets_and_strength(fake_api):
    data = app.app.test_client().get("/api/team/100").get_json()
    assert "waiver_targets" in data
    me = data["managers"][0]
    assert me["best_xi"] == [1]          # the only player this team owns
    assert all("chance" in p for p in data["players"])


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


def test_checker_flags_my_team_missing_from_the_league(fake_api):
    data = app.app.test_client().get("/api/team/100").get_json()
    data["me"] = 777
    problems = [msg for ok, msg in check_league.check(data) if not ok]
    assert any("777" in msg for msg in problems)
