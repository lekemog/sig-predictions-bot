# Basket entry proof gate — 11 October 2026

The settlement audit found missing proof, not a demonstrated broken basket. New entries in Nebraska Senate, Montana Senate, Kansas Senate, PA Governor and Alaska Governor require a reviewed manifest. No manifest has been approved for these five. Existing holdings, matched exits, reconciliation, execution ownership and all ceilings remain unchanged. The other seven baskets retain their existing validation; this change does not certify their floors.

## Manifest (basket.entry_proof, schema_version 1)

- `reviewer`: identified reviewer; `reviewed_at` and `expires_at`: timezone-aware ISO timestamps, lifetime at most 72 hours.
- `contract_hash`: `contract_proof_hash(basket, validate_contracts(api, basket))`; binds all current returned contract definitions and configured market/exchange/party legs. This is an API metadata fingerprint, not a hash of rules the API never returned.
- `rules_review`: full reviewed `text`, its UTF-8 SHA256 in `sha256`, HTTPS `source_url`, `complete: true`, `other_matches_zero: true`. This requires sourced settlement interpretation, including affiliation, independent/nonpartisan, endorsements and write-ins. It is a reviewed attestation, not automated legal inference. If those meanings are uncertain, do not approve it.
- `certified_candidates`: exhaustive stable person IDs from official certification. One person must retain the SAME ID across labels. A party category containing three people needs three separate rows.
- `candidate_rows`: person ID -> list of matching exchange ID strings. Include every certified person and explicit `write_in_other: []`. Empty rows for candidates matching no legs are valid. Unknown winners must be demonstrated to match zero by the reviewed rules; do not lump known write-ins into the wildcard.
- `enumeration_complete: true`: reviewer attests the full eligible winner enumeration, including listed/qualified write-ins.
- `sources`: nonempty list of fetched official certification artifacts, each with `tier: A`, `official: true`, `fetched: true`, `kind: certification`, HTTPS `url`, exact lowercase artifact `sha256`, `source_date` YYYY-MM-DD, timezone-aware `fetched_at`, and `candidate_ids`. Source coverage must exactly equal the certified enumeration; fetches must be within 72 hours and no later than review. B-tier county/sample/indexed evidence cannot unlock entries. Archive the actual fetched artifact alongside the research record before approving.

`max_simultaneous_yes(candidate_rows, exchange_ids)` counts matched legs for every possible winner. The n−1 floor requires the maximum to be at most one. Structural validation cannot establish the truth of source-tier/completeness/mapping attestations; research must do that. Re-fetch and re-review after expiry, a replacement/withdrawal or changed API definitions. Full remote rule revisions not reflected in API metadata are bounded only by the review expiry and monitoring.

## Nebraska first

Configured exact leg labels are Democratic Party (market279/exchange968), Nonpartisan (market280/exchange969), Republican Party (market281/exchange970). The market280 title says Independent, while winnerName says Nonpartisan. The prior official candidate audit recorded Ricketts Republican and Osborn By Petition, plus three other party candidates and no Democratic nominee. That does not establish how SIG maps By Petition or political endorsement/caucus affiliation to these leg labels. Nebraska remains blocked until full platform settlement rules resolve that mapping and complete current certification is reviewed. No invented person-to-leg matrix is deployed.

## Validation

12 regression tests cover overlapping labels, multi-person categories, incomplete enumeration, stale/future/overlong proofs, contract/leg changes, B-tier sources, absent rules, artifact digests, source coverage/freshness, malformed and unknown rows, all five quarantines, renamed baskets, preserved profitable matched exits, held screening, and pre-journal expiry rejection with zero writes. Existing execution tests cover recovery and idempotency. No live or dry-run workflow was dispatched for this patch.
