"""The optimizer (Optimizer.py) and the Analytics tab (webapp/analytics.py)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import Accounts
import AIWeights
import Betting
import Optimizer
import Tracker
from EVCalculator import LLMPrediction, consensus_probability
from webapp.main import app, get_db

RULES = {**Betting.DEFAULTS, "mode": "paper", "segments": ["preseason", "regular", "postseason"]}


def game(i, day, outcome, preds, yes_bid=0.48, yes_ask=0.50, league="NBA"):
    return {"ticker": f"KXNBAGAME-26OCT{day:02d}G{i:03d}-AAA", "game_date": f"2026-10-{day:02d}", "league": league,
            "segment": "regular", "yes_bid": yes_bid, "yes_ask": yes_ask, "market_prob": (yes_bid + yes_ask) / 2,
            "outcome": outcome, "tied": False, "preds": preds}


def informative_games(days=8, per_day=6):
    """'sharp' knows the result (80/20), 'noisy' says the opposite half the time; equal confidence."""
    games = []
    for d in range(days):
        for i in range(per_day):
            outcome = (i + d) % 2
            sharp = 0.8 if outcome else 0.2
            noisy = 0.7 if i % 4 < 2 else 0.3
            games.append(game(d * per_day + i, 5 + d, outcome, [("sharp", sharp, 0.9), ("noisy", noisy, 0.9)]))
    return games


def edge_games(days=8):
    """Each day: three 25-point-edge YES bets that win, and three 3-point-edge ones that lose."""
    games = []
    for d in range(days):
        for i in range(3):
            games.append(game(d * 10 + i, 5 + d, 1, [("a", 0.75, 1.0), ("b", 0.75, 1.0)]))
        for i in range(3, 6):
            games.append(game(d * 10 + i, 5 + d, 0, [("a", 0.53, 1.0), ("b", 0.53, 1.0)]))
    return games


class ConsensusTests(unittest.TestCase):
    def test_matches_the_site_wide_formula(self):
        preds = [("openai", 0.7, 0.8), ("claude", 0.4, 0.5), ("gemini", 0.55, 0.9)]
        weights = {"openai": 1.5, "claude": 0.5, "gemini": 1.0}
        expected = consensus_probability([LLMPrediction(n.capitalize(), p, c) for n, p, c in preds],
                                         {n.capitalize(): w for n, w in weights.items()})
        self.assertAlmostEqual(Optimizer.consensus(preds, weights), expected)


class WeightTests(unittest.TestCase):
    def test_fitting_moves_weight_to_the_ai_that_knows(self):
        w = Optimizer.fit_weights(informative_games(), ["noisy", "sharp"], lam=0.0)
        self.assertGreater(w["sharp"], 1.5)
        self.assertAlmostEqual(sum(w.values()) / len(w), 1.0)  # weights keep averaging 1.0

    def test_strong_shrinkage_stays_near_equal(self):
        w = Optimizer.fit_weights(informative_games(), ["noisy", "sharp"], lam=10.0)
        self.assertLess(abs(w["sharp"] - 1.0), 0.2)

    def test_optimize_weights_reports_held_out_scores(self):
        result = Optimizer.optimize_weights(informative_games(), {"sharp": 1.0, "noisy": 1.0})
        self.assertGreater(result["suggested"]["sharp"], result["suggested"]["noisy"])
        self.assertLess(result["cv_brier"]["suggested"], result["cv_brier"]["current"])
        for value in result["suggested"].values():
            self.assertAlmostEqual(value / Optimizer.WEIGHT_STEP, round(value / Optimizer.WEIGHT_STEP))

    def test_no_signal_keeps_equal_weights(self):
        games = [game(i, 5 + i % 6, i % 2, [("a", 0.5, 1.0), ("b", 0.5, 1.0)]) for i in range(30)]
        self.assertEqual(Optimizer.optimize_weights(games, {"a": 1.0, "b": 1.0})["suggested"], {"a": 1.0, "b": 1.0})


class RuleTests(unittest.TestCase):
    def test_finds_the_edge_threshold_that_drops_the_losers(self):
        found = Optimizer.best_rules(Optimizer._with_consensus(edge_games(), {}), RULES, 2)
        rules, bets, _ = found
        self.assertGreater(rules["min_edge"], 0.03)
        self.assertTrue(all(b["status"] == "won" for b in bets))

    def test_too_few_bets_never_win(self):
        bets = [{"pnl": 5.0, "cost": 5.0, "fee": 0.1, "status": "won"}] * 3
        self.assertIsNone(Optimizer.score(bets, min_bets=5))
        self.assertIsNotNone(Optimizer.score(bets * 2, min_bets=5))

    def test_optimize_rules_includes_the_honest_checks(self):
        result = Optimizer.optimize_rules(edge_games(), {}, {**RULES, "min_edge": 0.0})
        self.assertGreater(result["suggested"]["roi"], result["current"]["roi"])
        wf = result["walk_forward"]
        self.assertEqual(len(wf["days"]), 5)  # 8 days, the first 3 only train
        self.assertGreater(wf["tuned"]["pnl"], wf["current"]["pnl"])  # here tuning genuinely helps
        low, high = result["suggested"]["interval"]
        self.assertLessEqual(low, high)

    def test_the_users_stake_and_cap_are_kept(self):
        rules = {**RULES, "stake_usd": 12.0, "max_daily_usd": 30.0}
        found = Optimizer.best_rules(Optimizer._with_consensus(edge_games(), {}), rules, 2)
        self.assertEqual((found[0]["stake_usd"], found[0]["max_daily_usd"]), (12.0, 30.0))


class ConfidenceRuleTests(unittest.TestCase):
    def confidence_games(self, days=8):
        """Each day: three confident (90%) 25-point-edge winners, and three unsure (30%) 25-point-edge losers."""
        games = []
        for d in range(days):
            for i in range(3):
                games.append(game(d * 10 + i, 5 + d, 1, [("a", 0.75, 0.9), ("b", 0.75, 0.9)]))
            for i in range(3, 6):
                games.append(game(d * 10 + i, 5 + d, 0, [("a", 0.75, 0.3), ("b", 0.75, 0.3)]))
        return games

    def test_the_grid_comes_from_the_data(self):
        grid = Optimizer.confidence_grid(Optimizer._with_consensus(self.confidence_games(), {}))
        self.assertEqual(grid[0], 0.0)
        self.assertIn(0.9, grid)
        self.assertTrue(all(0 <= v <= 1 for v in grid))

    def test_finds_the_confidence_minimum_that_drops_the_unsure_losers(self):
        rules, bets, _ = Optimizer.best_rules(Optimizer._with_consensus(self.confidence_games(), {}), RULES, 2)
        self.assertGreater(rules["min_confidence"], 0.3)
        self.assertTrue(all(b["status"] == "won" for b in bets))

    def test_the_suggestion_includes_it(self):
        result = Optimizer.optimize_rules(self.confidence_games(), {}, RULES)
        self.assertGreater(result["suggested"]["rules"]["min_confidence"], 0.3)

    def test_returns_grouped_by_confidence(self):
        rows = {r["label"]: r for r in Optimizer.confidence_returns(self.confidence_games(), {})}
        self.assertEqual(set(rows), {"60%+", "30-40%"})
        self.assertGreater(rows["60%+"]["roi"], 0)   # the confident calls won
        self.assertLess(rows["30-40%"]["roi"], 0)    # the unsure ones lost
        self.assertAlmostEqual(rows["60%+"]["ai_confidence"], 0.9)

    def test_weights_shift_the_confidence(self):
        preds = [("a", 0.6, 0.9), ("b", 0.6, 0.1)]
        self.assertAlmostEqual(Optimizer.confidence(preds, {}), 0.5)
        self.assertAlmostEqual(Optimizer.confidence(preds, {"a": 1.0, "b": 0.0}), 0.9)


class DiagnosticTests(unittest.TestCase):
    def test_calibration_buckets(self):
        rows = {(r["low"], r["high"]): r for r in Optimizer.calibration(informative_games(), {"sharp": 1, "noisy": 0})}
        self.assertEqual(rows[(0.8, 0.9)]["ai"]["actual"], 1.0)   # sharp's 80% calls all came true
        self.assertEqual(rows[(0.2, 0.3)]["ai"]["actual"], 0.0)

    def test_edge_returns_rise_with_the_edge_here(self):
        rows = {r["label"]: r for r in Optimizer.edge_returns(edge_games(), {})}
        self.assertLess(rows["0-5 pts"]["roi"], 0)
        self.assertGreater(rows["15+ pts"]["roi"], 0)


def _seed_games(db_path, games):
    conn = Tracker.connect(db_path)
    try:
        for g in games:
            for llm, p, c in g["preds"]:
                conn.execute(
                    "INSERT INTO predictions (created_at, ticker, league, team, opponent, game_date, yes_bid, yes_ask, "
                    " llm, predicted_prob, confidence, market_prob, outcome, tied) "
                    "VALUES ('t', ?, 'NBA', 'Home', 'Away', ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (g["ticker"], g["game_date"], g["yes_bid"], g["yes_ask"], {"a": "OpenAI", "b": "Claude"}[llm],
                     p, c, g["market_prob"], g["outcome"]))
        conn.commit()
    finally:
        conn.close()


class AnalyticsPageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "predictions.db")

        def override_get_db():
            conn = Tracker.connect(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        for patcher in (mock.patch.object(AIWeights, "get_weight", return_value=1.0), mock.patch.dict(os.environ)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("USERNAME", "PASSWORD", "SESSION_SECRET"):
            os.environ.pop(name, None)
        conn = Tracker.connect(self.db_path)
        try:
            self.uid = Accounts.create_user(conn, "pat", "pat-password")["id"]
            Accounts.set_setting(conn, self.uid, "betting", {**RULES, "min_edge": 0.0})
        finally:
            conn.close()
        self.client = TestClient(app)

    def sign_in(self):
        self.client.post("/login", data={"username": "pat", "password": "pat-password", "next": "/"})

    def setting(self, name):
        conn = Tracker.connect(self.db_path)
        try:
            return Accounts.get_setting(conn, self.uid, name)
        finally:
            conn.close()

    def test_guests_are_kept_out(self):
        self.client.post("/guest")
        self.assertEqual(self.client.get("/analytics").status_code, 403)
        self.assertEqual(self.client.post("/analytics/run").status_code, 403)

    def test_too_little_data_says_so_and_refuses_to_run(self):
        _seed_games(self.db_path, edge_games(days=2))
        self.sign_in()
        page = self.client.get("/analytics").text
        self.assertIn('href="/analytics" class="active"', page)
        self.assertIn("Needs at least", page)
        self.assertEqual(self.client.post("/analytics/run").status_code, 400)

    def test_the_page_shows_confidence_tables(self):
        _seed_games(self.db_path, edge_games())
        self.sign_in()
        page = self.client.get("/analytics").text
        self.assertIn("Does confidence pay?", page)
        self.assertIn("Fair odds model", page)
        self.assertIn("<th>AI confidence</th>", page)

    def test_run_then_apply_rules(self):
        _seed_games(self.db_path, edge_games())
        self.sign_in()
        r = self.client.post("/analytics/run")
        self.assertEqual(r.status_code, 200)  # followed to the page
        self.assertIn("Optimization finished.", r.text)
        self.assertIn("the honest check", r.text)
        run = self.setting("analytics_run")
        suggested = run["rules"]["suggested"]["rules"]
        r = self.client.post("/analytics/apply", data={"which": "rules"})
        self.assertIn("Applied the suggested betting rules.", r.text)
        rules = self.setting("betting")
        self.assertEqual({k: rules[k] for k in suggested}, suggested)
        self.assertEqual(rules["mode"], "paper")  # applying never changes the mode

    def test_applying_weights(self):
        _seed_games(self.db_path, [{**g, "preds": [("a", 0.8 if g["outcome"] else 0.2, 0.9), ("b", 0.5, 0.9)]}
                                   for g in edge_games()])
        self.sign_in()
        self.client.post("/analytics/run")
        self.client.post("/analytics/apply", data={"which": "weights"})
        weights = self.setting("ai_weights")
        self.assertGreater(weights["openai"], weights["claude"])  # "a" was seeded as OpenAI, the one that knew

    def test_a_run_saved_before_the_confidence_rule_still_shows(self):
        _seed_games(self.db_path, edge_games())
        self.sign_in()
        self.client.post("/analytics/run")
        conn = Tracker.connect(self.db_path)
        try:
            run = Accounts.get_setting(conn, self.uid, "analytics_run")
            run["rules"]["current_rules"].pop("min_confidence")
            run["rules"]["suggested"]["rules"].pop("min_confidence")
            Accounts.set_setting(conn, self.uid, "analytics_run", run)
        finally:
            conn.close()
        self.assertEqual(self.client.get("/analytics").status_code, 200)

    def test_applying_before_running_is_refused(self):
        self.sign_in()
        r = self.client.post("/analytics/apply", data={"which": "both"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Run the optimizer first", r.text)

    def test_a_run_goes_stale_when_new_games_settle(self):
        _seed_games(self.db_path, edge_games())
        self.sign_in()
        self.client.post("/analytics/run")
        _seed_games(self.db_path, [game(999, 20, 1, [("a", 0.6, 1.0), ("b", 0.6, 1.0)])])
        self.assertIn("New games have settled since", self.client.get("/analytics").text)


    def regular_season_games(self):
        """edge_games() moved to Oct 21-28, inside the shipped leagues.json's NBA regular season (it starts Oct 20)."""
        return [{**g, "ticker": g["ticker"].replace("26OCT", "26OCR"),
                 "game_date": f"2026-10-{int(g['game_date'][-2:]) + 16:02d}"} for g in edge_games()]

    def test_load_games_keeps_only_the_parts_of_the_season_asked_for(self):
        _seed_games(self.db_path, edge_games() + self.regular_season_games())
        conn = Tracker.connect(self.db_path)
        try:
            self.assertEqual({g["segment"] for g in Optimizer.load_games(conn, ["regular"])}, {"regular"})
            self.assertEqual(len(Optimizer.load_games(conn, ["preseason"])), len(edge_games()))
            self.assertEqual(Optimizer.segment_counts(conn), {"preseason": 48, "regular": 48})
        finally:
            conn.close()

    def test_by_default_it_covers_the_parts_the_user_bets_on(self):
        _seed_games(self.db_path, edge_games())  # all preseason
        conn = Tracker.connect(self.db_path)
        try:
            Accounts.set_setting(conn, self.uid, "betting", {**RULES, "segments": ["regular", "postseason"]})
        finally:
            conn.close()
        self.sign_in()
        page = self.client.get("/analytics").text
        self.assertIn("Needs at least", page)  # no regular-season games yet
        self.assertNotIn('name="segment" value="preseason" checked', page)
        page = self.client.get("/analytics?segment=preseason").text
        self.assertNotIn("Needs at least", page)

    def test_a_run_uses_and_remembers_the_filter(self):
        _seed_games(self.db_path, edge_games() + self.regular_season_games())
        self.sign_in()
        r = self.client.post("/analytics/run", data={"segment": ["regular"]}, follow_redirects=False)
        self.assertIn("segment=regular", r.headers["location"])
        run = self.setting("analytics_run")
        self.assertEqual((run["segments"], run["games"]), (["regular"], 48))
        self.assertIn("That's not the parts of the season ticked above",
                      self.client.get("/analytics?segment=preseason&segment=regular").text)
        self.assertNotIn("That's not the parts of the season ticked above",
                         self.client.get("/analytics?segment=regular").text)
        r = self.client.post("/analytics/apply", data={"which": "rules"}, follow_redirects=False)
        self.assertIn("segment=regular", r.headers["location"])  # back to the view the run was for


if __name__ == "__main__":
    unittest.main()
