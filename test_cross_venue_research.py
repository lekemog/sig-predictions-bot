import unittest
from cross_venue_research import collect_kalshi, compare_pairs, midpoint, model_three_venues


class ResearchTests(unittest.TestCase):
    def quote(self, venue="sig"):
        return dict(venue=venue, kind="book", bid=.4, ask=.42,
                    bid_size=10, ask_size=20, quote_at="2026-10-13T15:00:00Z",
                    quote_timestamp_verified=True)

    def pair(self):
        return dict(race="TX2026", outcome="Democratic",
                    settlement_match="reviewed_exact", sig=self.quote(),
                    reference=self.quote("kalshi"))

    def test_midpoint(self):
        self.assertEqual(float(midpoint(self.quote())), .41)

    def test_comparison_has_no_live_authority(self):
        r = compare_pairs([self.pair()])
        self.assertEqual(r["mae_pp"], 0)
        self.assertFalse(r["execution_authorized"])
        self.assertIsNone(r["calibrated_interval"])

    def test_reject_fill_display_missing_size_crossed(self):
        for change in [dict(kind="fill"), dict(kind="display"),
                       dict(bid_size=0), dict(bid=.6)]:
            q = self.quote(); q.update(change)
            with self.assertRaises(ValueError): midpoint(q)

    def test_reject_unreviewed_or_unknown_time(self):
        p = self.pair(); p["settlement_match"] = "title_only"
        with self.assertRaises(ValueError): compare_pairs([p])
        p = self.pair(); p["reference"]["quote_timestamp_verified"] = False
        with self.assertRaises(ValueError): compare_pairs([p])

    def test_reject_time_skew(self):
        p = self.pair(); p["reference"]["quote_at"] = "2026-10-13T15:02:00Z"
        with self.assertRaises(ValueError): compare_pairs([p])

    def test_races_not_outcomes(self):
        self.assertEqual(compare_pairs([self.pair(), self.pair()])["race_count"], 1)

    def test_get_pagination(self):
        calls = []
        def get(url):
            calls.append(url)
            return {"markets": [{"ticker": str(len(calls))}],
                    "cursor": "next" if len(calls) == 1 else ""}
        rows = collect_kalshi(["SENATETX"], get)
        self.assertEqual(len(rows), 2)
        self.assertIn("cursor=next", calls[1])
        self.assertTrue(all(not r["execution_authorized"] for r in rows))

    def test_incomplete_pagination_fails(self):
        with self.assertRaises(ValueError):
            collect_kalshi(["S"], lambda u: {"markets": []})
        with self.assertRaises(ValueError):
            collect_kalshi(["S"], lambda u: {"markets": [], "cursor": "loop"})

    def triple(self):
        return dict(race="TX2026", outcome="Democratic",
                    settlement_match="reviewed_exact",
                    quotes={v:self.quote(v) for v in ("sig", "polymarket", "kalshi")})

    def test_three_pairwise_comparisons(self):
        r=model_three_venues([self.triple()], "2026-10-13T15:01:00Z")
        self.assertEqual(len(r["rows"][0]["pairwise"]), 3)
        self.assertEqual(r["eligible_race_count"], 1)
        self.assertFalse(r["execution_authorized"])
        self.assertIsNone(r["calibrated_probability"])

    def test_missing_venue_not_imputed(self):
        m=self.triple(); del m["quotes"]["kalshi"]
        r=model_three_venues([m], "2026-10-13T15:01:00Z")
        self.assertEqual(r["eligible_outcome_count"], 0)
        self.assertEqual(r["rows"][0]["pairwise"], [])

    def test_stale_books_unknown(self):
        r=model_three_venues([self.triple()], "2026-10-13T15:06:00Z")
        self.assertEqual(r["eligible_outcome_count"], 0)

    def test_three_venue_skew_unknown(self):
        m=self.triple(); m["quotes"]["kalshi"]["quote_at"]="2026-10-13T15:02:00Z"
        r=model_three_venues([m], "2026-10-13T15:03:00Z")
        self.assertEqual(r["eligible_outcome_count"], 0)

    def test_sworn_in_vs_elected_not_exact(self):
        m=self.triple(); m["settlement_match"]="different_resolution"
        r=model_three_venues([m], "2026-10-13T15:01:00Z")
        self.assertIsNone(r["rows"][0]["real_money_envelope"])

    def test_spread_envelope_is_not_confidence_interval(self):
        m=self.triple()
        m["quotes"]["kalshi"].update(bid=.5,ask=.52)
        r=model_three_venues([m], "2026-10-13T15:01:00Z")
        self.assertEqual(r["rows"][0]["real_money_envelope"], [.4,.52])
        self.assertIsNone(r["calibrated_error_interval"])

    def test_reference_excludes_sig_and_double_counting(self):
        m=self.triple(); m["quotes"]["sig"].update(bid=.1,ask=.2)
        r=model_three_venues([m], "2026-10-13T15:01:00Z")
        ref=r["rows"][0]["reference_probability_range"]
        self.assertEqual(ref["range"], [.4,.42])
        self.assertFalse(ref["sig_included"])
        self.assertFalse(ref["calibrated"])
        self.assertIsNone(ref["evidence_adjustment"])
        self.assertEqual(ref["evidence_family"], "real_money_market_prices")


if __name__ == "__main__":
    unittest.main()
