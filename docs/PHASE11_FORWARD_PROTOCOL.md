# Phase 11 preregistered forward paper

Before the first eligible observation, registration writes a canonical manifest and immutable protocol to each wallet journal. UTC timestamps fix the history cutoff, start and end. The manifest pins a Git source reference, executable file hash, exact data source and dataset identifier, symbol and timeframe, strategy roster, broker contract source, virtual initial cash, parameters, costs, execution assumptions and risk thresholds. The implementation hash detects drift between registration and execution. A new model version requires a new roster and future interval.

Prospective registration uses the system clock before the start. The foreground is the trusted ingestion boundary for actual MT5 observations. Historical replay and synthetic processing use separate evidence classes and can exercise the same engine. They cannot qualify as forward evidence. Historical bars can provide causal warmup at a current quote and cannot create prior entries.

## Predeclared decision rules

- The primary metric is mean net R per complete campaign, with R computed from initial modeled cash risk including costs.
- Require a minimum of 100 complete campaigns, profit factor of at least 1.10, mean net R of at least 0.10 and maximum observed-equity drawdown of at most 10% of initial cash. Thresholds may be tightened before registration.
- Report a 95% Wilson win-rate interval and a seeded circular block-bootstrap 95% net-R interval. Require the lower net-R bound to exceed zero before continuing paper research. The bootstrap assumes the ordered campaign process is approximately stationary within the period.
- Insufficient campaigns, an unfinished window, an unresolved campaign, uncertain execution path or operator pause can produce INSUFFICIENT_FORWARD_EVIDENCE. A breached drawdown criterion or mature loss result can produce REJECT. CONTINUE_PAPER_TEST is a research outcome.
- Preserve missed opportunities, losses, partials, gap-affected outcomes, drawdown duration, paid costs and exact exit reasons. Each strategy has independent inference and statistics. Sample intervals provide no cross-strategy multiple-testing correction or selection proof.

With no eligible observations or campaigns, the report shows zero complete campaigns, undefined return statistics where appropriate and insufficient forward evidence. All statuses preserve the local-paper-only boundary.
