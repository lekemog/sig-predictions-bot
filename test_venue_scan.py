import unittest
from venue_scan import discover, scan, tokens


class CollectorTests(unittest.TestCase):
    def test_kalshi_pages_and_missing_cursor(self):
        pages = iter([{"markets": [{"ticker": "A"}], "cursor": "next"}, {"markets": [], "cursor": ""}])
        rows, complete, _ = discover("kalshi", lambda _: next(pages))
        self.assertEqual(len(rows), 1)
        self.assertTrue(complete)
        with self.assertRaises(ValueError):
            discover("kalshi", lambda _: {"markets": []})

    def test_poly_duplicate_fails_closed(self):
        rows, complete, reason = discover("polymarket", lambda _: [{"id": "1"}])
        self.assertFalse(complete)
        self.assertIn("Duplicate", reason)

    def test_page_budget_not_complete(self):
        _, complete, _ = discover("polymarket", lambda _: [{"id": "1"}], max_pages=1)
        self.assertFalse(complete)

    def test_two_venues_books_no_execution(self):
        def fake(url):
            if "/orderbook" in url or "/book?" in url:
                return {"bids": [], "asks": []}
            if "kalshi" in url:
                return {"markets": [{"ticker": "SENATE26", "title": "Senate 2026"}], "cursor": ""}
            if "offset=0" in url:
                return [{"id": "1", "question": "Senate 2026", "clobTokenIds": '["a","b"]'}]
            return []
        result = scan(fake)
        self.assertEqual(result["book_count"], 3)
        self.assertTrue(result["book_coverage_complete"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["comparison_status"], "UNKNOWN")
        self.assertTrue(all(not b["quote_timestamp_verified"] for b in result["books"]))

    def test_bad_tokens(self):
        with self.assertRaises(ValueError):
            tokens({"clobTokenIds": "{}"})


if __name__ == "__main__":
    unittest.main()
