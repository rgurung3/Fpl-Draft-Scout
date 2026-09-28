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


def test_season_window_covers_six_gameweeks(monkeypatch):
    fixtures = [{"event": gw, "team_h": 1, "team_a": 2, "team_h_difficulty": 2,
                 "team_a_difficulty": 4} for gw in (7, 9, 12, 13)]
    monkeypatch.setattr(app, "get_json", lambda url: {"teams": TEAMS} if "bootstrap" in url else fixtures)
    week = app.upcoming_fixtures(TEAMS, 7)
    season = app.upcoming_fixtures(TEAMS, 7, app.SEASON_LOOKAHEAD)
    assert [f["gw"] for f in week[1]] == [7, 9]            # GW7-9
    assert [f["gw"] for f in season[1]] == [7, 9, 12]      # GW7-12


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


def test_new_stats_need_180_minutes():
    cameo = make_player(1, pos=2, minutes=90, creativity="50.0", xgc="0.0", dc=40)
    breakdown = app.score_players([cameo], {}, current_gw=6, view="season")[1]["breakdown"]
    assert breakdown["crea"] == breakdown["cs"] == breakdown["dc"] == 0


def test_forwards_only_change_through_fixtures_and_fitness():
    forwards = [make_player(i, pos=4, form=str(i), creativity=str(10 * i)) for i in range(1, 8)]
    week = app.score_players(forwards, {}, current_gw=6)
    season = app.score_players(forwards, {}, current_gw=6, view="season")
    assert all(week[i]["score"] == season[i]["score"] for i in week)


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
    assert season["lookahead"] == app.SEASON_LOOKAHEAD
    assert all(p["score"] == p["season_score"] for p in season["players"])

    week = client.get("/api/team/100").get_json()
    assert week["view"] == "week"
    assert week["lookahead"] == app.LOOKAHEAD
    assert all(p["score"] == p["week_score"] for p in week["players"])


def test_unknown_view_falls_back_to_week(fake_api):
    data = app.app.test_client().get("/api/team/100?view=banana").get_json()
    assert data["view"] == "week"


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
def test_drop_injury_doubt_is_a_reason(chance, text):
    drop = rated(1, "DEF", 20, owner=100, chance=chance, breakdown=piece(form=40, avail=-20))
    claim = rated(2, "DEF", 40, breakdown=piece(form=40))
    assert app.swap_reasons(drop, claim)["reasons"] == [{"text": text, "points": 20}]


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
