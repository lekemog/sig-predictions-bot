# Free scheduler alternative (7 October 2026)

Render is not required. Do not add a card or apply its paid Blueprint.
Google Apps Script is available with a personal Google account. The script
only asks the existing GitHub workflow to run; GitHub owns all SIG execution.
This is periodic oversight/triggering, not an always-on market feed or a new
validated strategy. There is no promise of exact timer delivery.

`free_dispatch.js` can be pasted into a single private Apps Script project's
`Code.gs`. Keep this project unshared. Never create a second trigger project.
Do not delete `LAST_DISPATCH`: it is the durable unknown-request journal.
The script lock protects simultaneous invocations in that project. GitHub's
shared concurrency group and order journal remain unchanged.

## Owner-only setup

1. Open https://script.google.com and create one standalone project named
   `SIG GitHub sweep trigger`. Paste `free_dispatch.js` into `Code.gs` and save.
2. Create a fine-grained GitHub token restricted to
   `lekemog/sig-predictions-bot`, Actions read/write and Contents read,
   expiring after 4 November's 17:00 UTC cutoff. Enter it directly in the
   script's Project Settings > Script Properties as `GITHUB_DISPATCH_TOKEN`.
   Do not put it in chat or source. Do not move `SIG_API_KEY` out of GitHub.
3. Run `installDryRunTrigger` once and approve its external-request and
   timer permissions. No Gmail, contacts, calendar or portfolio access is
   needed. It defaults to dry-run, checks every ten minutes, and requests
   at most one sweep per hour. It never runs the bot inside Google.
4. Verify an accepted correlated dry-run, successful hosted screening and
   checkpoint. Only then the owner changes the script property
   `DISPATCH_MODE` to `live`. This final prize-based trading activation is a
   human action. A successful POST alone is not successful live management.

`RUN_ENABLED=false` disables future requests. Every tick also checks the
GitHub workflow enabled state and the portfolio kill switch. Requests stop
at 2026-11-04T17:00:00Z. An ambiguous dispatch stops future POSTs until an
exact GitHub run title confirms acceptance; it is never blindly retried.

## Free usage limits

Google consumer accounts currently include 20,000 URL fetch calls and 90
minutes of trigger runtime per day. At 144 ten-minute checks per day, this
script uses roughly 1,008 status reads plus dispatch/correlation reads in an
idle day, well below the fetch quota. Slow external calls can consume the
runtime quota. Google provides no exact scheduling guarantee.

Private GitHub Actions still consumes the account's included minutes. Keep
paid Actions overage blocked and inspect remaining included usage before
activation. Do not enable paid plans, spending or make the repository public.
An hourly trigger with two-minute sweeps is approximately 1,344 runner
minutes over 28 days, before other workflows and GitHub's native schedule.
The approved native cron stays unchanged; if it resumes reliably it may
consume additional minutes. A free quota being exhausted must stop execution,
not result in a bill. The account's remaining allowance has not been verified.

This alternative does not activate the staged increased limits or political
turnover models, and has not been deployed or authorized in Google yet.

## Validation

`node --test test_free_dispatch.cjs` tests crash/persistence failure, unknown
POST non-replay, exact correlation recovery, busy jobs, invalid checkpoint,
heartbeat mode, cooldown, redirect guard and tournament cutoff. No real
credentials or network requests are used by these regression tests.

Official references:
- https://workspace.google.com/products/apps-script/
- https://developers.google.com/apps-script/guides/services/quotas
- https://developers.google.com/apps-script/reference/lock/lock-service
- https://developers.google.com/apps-script/guides/triggers/installable
- https://docs.github.com/en/billing/concepts/product-billing/github-actions
