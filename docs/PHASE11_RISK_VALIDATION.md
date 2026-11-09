# Phase 11 hard risk validation

The local guardian reuses Phase 10 sizing and hard risk controls. Admission and pending execution require a usable closed bar, an explicitly open session, current executable bid and ask, calendar coverage and a current broker contract match for prospective paper.

| Gate | Required evidence |
| --- | --- |
| Source identity | Registered source, server and exact broker symbol. |
| Price inputs | Both sides on the frozen tick grid, valid spread, quote age at most five seconds and bounded deviation from the plan reference. |
| Contract | Fresh read-only symbol snapshot for prospective paper, matching tick values, volume grid, contract size, stops and freeze levels. Account and broker profit currencies must match the virtual wallet. Market bracket eligibility must permit the side. |
| Calendar | Exact approved HTTPS snapshot source, publication available at observation time and required USD release coverage. Missing coverage, active events and operator pause prevent entries. |
| Stop and targets | Side-correct stop, minimum stop and freeze distances, ordered grid-aligned targets, and positive target reward after modeled costs. |
| Exposure | One pending or open campaign per wallet. A local cash loss cap and tighter limits can be registered. |
| Sizing | Three executable minimum lots, lot-step rounding, signed stop risk, modeled margin and reserves charged against headroom. |
| Loss budgets | Default maximum portfolio risk of 1% of virtual equity, daily loss allowance 2%, weekly loss allowance 4% and margin use up to 50% of modeled free margin. |

Missing analysis, unavailable news or a manual pause can veto entries while existing positions remain eligible for protection on later observed executable quotes in a known-open session. Stop changes apply only on later observations. Recovery records a gap before any resulting fill.

Each wallet is an independent hypothetical experiment. The roster implements no aggregate multi-symbol wallet with correlated exposure or shared capital. Such a portfolio requires a separate registered experiment. Frozen fees and margin represent local modeling assumptions. Broker financing, actual market impact, order queue and margin for a future order have no execution evidence here.
