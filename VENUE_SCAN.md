Public Kalshi and Polymarket research collection
==============================================

GitHub Actions requests a scan every ten minutes, at minutes 3/13/23/33/43/53.
GitHub cron may delay or skip runs; this is a requested cadence, not a guaranteed
ten-minute heartbeat. The workflow has no SIG secret or trading adapter, does
not write portfolio checkpoints, and has a separate research concurrency group.

The collector paginates open Kalshi and active, unclosed Polymarket catalogs,
preserves raw market metadata and resolution descriptions, and gathers public
order books for broad election research leads. Catalog discovery has per-venue
time/page bounds. Book sampling alternates venues, up to 300 books per run.
Incomplete discovery or book coverage is explicitly partial, never called a
full-universe price comparison. Failures are retained in the observation file.

Each run stores venue_observations.json as an Actions artifact for 21 days.
It contains source URLs, observation times, raw catalog prices and order books.
Quote timestamp verification remains false until schema and timing validation.
Catalog matches are leads only. Comparison/probability outputs remain UNKNOWN
without reviewed exact settlement relationships and synchronized SIG books.
This collector does not feed prices into live directional probabilities.

Collection stops at 2026-11-04T17:00:00Z. No manual workflow dispatch is exposed.
Tests: python -m unittest test_venue_scan test_cross_venue_research -v
