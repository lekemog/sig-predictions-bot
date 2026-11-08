# Three-venue market reference

Research-only, GET-only collector and comparison model in cross_venue_research.py.
No changes to execution, schedule, credentials or risk controls. Not imported by
the live trading executor. Run tests with:

    python -m unittest test_cross_venue_research -v

Supply manually reviewed exact settlement mappings and synchronized two-sided
SIG, Polymarket and Kalshi books. Missing quotes, size, verified quote times,
or exact settlement equivalence produce UNKNOWN. Default maximum age is five
minutes and maximum time skew is sixty seconds. These are data-quality gates,
not calibrated trading thresholds.

The reference_probability_range is the lowest bid to highest ask across Kalshi
and Polymarket. It is a descriptive market-implied reference, not a confidence
interval or fair-probability guarantee. Both venues are one correlated evidence
family. SIG prices are excluded from that range and compared against it.
No blending weights or news adjustments are invented. evidence_adjustment stays
null pending chronological out-of-sample validation. No directional trades can
be authorized by this module.

Kalshi initial collection, 13 October 2026 15:44:43 UTC: Texas, North Carolina
and Kansas 2026 party quotes were available; selected Montana series returned
no open markets. 2028 outcomes appeared in NC/KS series and must not be matched
to 2026 SIG races. Kalshi rules reference the party sworn in for the 2027 term;
SIG specifies election party winner with 4 November settlement. Full settlement
equivalence has not been established. Kalshi market updated_time is retained
as metadata, not assumed to be the quote timestamp.

Previous four-race asynchronous comparison and its suggested 7/10-point
thresholds or 95% interval are withdrawn. Real historical validation, complete
fresh books, fee verification and a validated directional adapter remain
required before live directional integration.
