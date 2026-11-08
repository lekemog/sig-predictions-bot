import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from decision_engine import evaluate, evaluate_basket, executable, forecast, timestamp, calibration, journal

ROOT = Path(__file__).resolve().parents[1]
NOW = timestamp("2026-10-06T12:00:10Z")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads((ROOT / "examples/synthetic_trade.json").read_text())

    def test_normal_probability_not_vote_share(self):
        p, _, _ = forecast(self.case["forecast"], NOW)
        self.assertAlmostEqual(p[0], 0.6305586598)
        self.assertAlmostEqual(p[1], 0.8413447461)

    def test_negative_margin_extrema(self):
        self.case["forecast"]["final_margin_pp"] = [-4, -2]
        p, _, _ = forecast(self.case["forecast"], NOW)
        self.assertAlmostEqual(p[0], 1 - 0.8413447461)
        self.assertAlmostEqual(p[1], 1 - 0.6305586598)

    def test_missing_validation_abstains(self):
        self.assertEqual(evaluate(self.case, NOW)["decision"], "INSUFFICIENT_EVIDENCE")

    def test_validated_candidate(self):
        self.case["forecast"]["validation_reference"] = "Synthetic test-only reference"
        self.assertEqual(evaluate(self.case, NOW)["decision"], "CANDIDATE")

    def test_no_side_reverses_bounds(self):
        self.case["trade"]["side"] = "no"
        r = evaluate(self.case, NOW)
        self.assertAlmostEqual(r["probability_range"][0], 0.1586552539)
        self.assertAlmostEqual(r["entry"]["vwap"], 0.46)

    def test_depth_vwap_and_shortfall(self):
        book = {"asks": [{"price":0.4,"quantity":50},{"price":0.6,"quantity":50}], "bids":[]}
        r = executable(book, "yes", "buy", 100)
        self.assertEqual(r["notional"], 50)
        self.assertFalse(executable(book, "yes", "buy", 101)["complete"])

    def test_round_trip_pays_spread_and_costs(self):
        self.assertAlmostEqual(evaluate(self.case, NOW)["immediate_round_trip_pnl"], -2)

    def test_stale_and_future_books_block(self):
        for t in ("2026-10-05T12:00:00Z", "2026-10-07T12:00:00Z"):
            self.case["book"]["asOf"]["at"] = t
            self.assertTrue(any("Order book is stale" in x for x in evaluate(self.case,NOW)["issues"]))

    def test_news_double_counting_blocks(self):
        event = dict(self.case["forecast"]["evidence"][0], likelihood_ratio=[1.1,1.2],
                     likelihood_justification="Synthetic", not_in_base_forecast=True)
        self.case["forecast"]["news_updates"] = [event]
        self.assertTrue(any("double-counts" in x for x in evaluate(self.case,NOW)["issues"]))

    def test_bayes_update(self):
        m = self.case["forecast"]
        m.update(kind="probability_range",yes_probability=[0.5,0.5])
        m["news_updates"] = [dict(m["evidence"][0], independence_group="new",likelihood_ratio=[2,2],
                                  likelihood_justification="Test only",not_in_base_forecast=True)]
        p,_,_ = forecast(m,NOW)
        self.assertAlmostEqual(p[0],2/3)

    def test_social_lead_blocks(self):
        self.case["forecast"]["evidence"][0]["verified"] = False
        self.assertTrue(any("Unverified" in x for x in evaluate(self.case,NOW)["issues"]))

    def test_risk_and_objective(self):
        self.case["risk"]["factor_exposure"] = 1999
        self.case["trade"]["objective"] = "quick_resale"
        reasons = evaluate(self.case,NOW)["issues"]
        self.assertTrue(any("factor risk cap" in x for x in reasons))
        self.assertTrue(any("short-term resale" in x for x in reasons))

    def test_basket_void_scenario_and_joint_probabilities(self):
        b = json.loads((ROOT / "examples/synthetic_basket.json").read_text())
        r = evaluate_basket(b,NOW)
        self.assertAlmostEqual(r["cost"],98)
        self.assertAlmostEqual(r["worst_case_profit"],0)
        self.assertAlmostEqual(r["expected_profit_range"][1],3)
        b["probability_scenarios"] = [[0.5,0.5,0.5,0]]
        with self.assertRaises(ValueError):evaluate_basket(b,NOW)

    def test_calibration_rejects_lookahead(self):
        row = {"probability":0.7,"outcome":1,"predicted_at":"2026-01-01T00:00:00Z","resolved_at":"2026-02-01T00:00:00Z"}
        self.assertAlmostEqual(calibration([row])["brier_score"],0.09)
        row["predicted_at"] = row["resolved_at"]
        with self.assertRaises(ValueError):calibration([row])

    def test_nonfinite_and_invalid_quantity(self):
        self.case["forecast"]["final_margin_pp"] = [float('nan'),4]
        with self.assertRaises(ValueError):evaluate(self.case,NOW)
        with self.assertRaises(ValueError):executable(self.case["book"],"yes","buy",1.5)

    def test_private_journal_deterministic_input_hash(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d)/"audit.sqlite")
            result = evaluate(self.case,NOW)
            self.assertEqual(journal(path,self.case,result),journal(path,copy.deepcopy(self.case),result))


if __name__ == "__main__":
    unittest.main()
