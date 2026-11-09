"""Independent fail-closed sizing and quote-driven paper management."""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import isclose, isfinite
from typing import Callable

from research.risk.calculator import BrokerSpec, size_position


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    at_ms: int
    server: str | None = None


@dataclass(frozen=True)
class TradePlan:
    plan_id: str
    symbol: str
    side: int
    entry: float
    stop: float
    targets: tuple[float, float, float]
    issued_at_ms: int
    expires_at_ms: int
    target_weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)


@dataclass(frozen=True)
class RiskState:
    equity: float
    free_margin: float
    account_currency: str
    open_risk_cash: float
    day_loss_cash: float
    week_loss_cash: float
    open_positions: int
    cooldown_until_ms: int = 0


@dataclass(frozen=True)
class RiskPolicy:
    max_spread: float
    max_entry_deviation: float
    risk_fraction: float = .005
    portfolio_risk_fraction: float = .01
    daily_loss_limit_fraction: float = .02
    weekly_loss_limit_fraction: float = .04
    max_positions: int = 1
    max_loss_cash: float | None = None
    max_free_margin_fraction: float = .5
    min_net_reward_r: float = 1.
    max_quote_age_ms: int = 5000
    max_spec_age_ms: int = 86400000
    slippage_ticks_each_way: int = 2


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    lots: float | None = None
    estimated_loss_cash: float | None = None
    estimated_margin_cash: float | None = None
    net_reward_r: float | None = None
    execution_reference: float | None = None
    target_lots: tuple[float, ...] = ()
    orders_enabled: bool = field(default=False, init=False)


def number(value):
    return type(value) in (int, float) and abs(value) <= 1e100 and isfinite(value)


def millis(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def grid_aligned(price, step):
    if not number(price) or not number(step) or step <= 0:
        return False
    steps = price / step
    return number(steps) and abs(steps - round(steps)) <= 1e-7


def _volumes(units, minimum_units, weights):
    """Allocate three executable lots, preserving their exact total in step units."""
    weight = [Decimal(str(x)) for x in weights]
    unallocated = units - 3 * minimum_units
    expected = [Decimal(unallocated) * x / sum(weight) for x in weight]
    floors = [int(x) for x in expected]
    remainder = unallocated - sum(floors)
    for i in sorted(range(3), key=lambda i: (floors[i] - expected[i], i))[:remainder]:
        floors[i] += 1
    return tuple(x + minimum_units for x in floors)


def _policy_valid(policy):
    positives = (policy.max_spread, policy.risk_fraction, policy.portfolio_risk_fraction,
                 policy.daily_loss_limit_fraction, policy.weekly_loss_limit_fraction,
                 policy.max_free_margin_fraction, policy.min_net_reward_r)
    integers = (policy.max_positions, policy.max_quote_age_ms, policy.max_spec_age_ms,
                policy.slippage_ticks_each_way)
    return (all(number(x) and x > 0 for x in positives)
            and number(policy.max_entry_deviation) and policy.max_entry_deviation >= 0
            and all(millis(x) for x in integers) and policy.max_positions > 0
            and policy.risk_fraction <= policy.portfolio_risk_fraction < 1
            and policy.daily_loss_limit_fraction <= policy.weekly_loss_limit_fraction < 1
            and policy.max_free_margin_fraction <= 1
            and (policy.max_loss_cash is None or
                 number(policy.max_loss_cash) and policy.max_loss_cash > 0))


def assess_plan(plan: TradePlan, quote: Quote, spec: BrokerSpec, state: RiskState,
                policy: RiskPolicy, *, now_ms: int,
                margin_for_lots: Callable[[float], float] | None = None,
                tick_value_profit_account_ccy: float | None = None,
                manual_pause: bool = False, news_blocked: bool = False,
                session_open: bool | None = None) -> RiskDecision:
    """Size a quote-bound local plan. Margin probes must be read-only or paper assumptions.

    Open/reserved risk is counted in portfolio and loss-window headroom. The caller
    must supply account-currency state and synchronized, independently verified
    symbol metadata; the function never substitutes a generic broker contract.
    """
    def veto(reason):
        return RiskDecision(False, reason)

    if not isinstance(plan, TradePlan) or not isinstance(quote, Quote) or not isinstance(state, RiskState):
        return veto('INVALID_PLAN_QUOTE_OR_RISK_STATE')
    if not isinstance(policy, RiskPolicy) or not _policy_valid(policy):
        return veto('INVALID_RISK_POLICY')
    if type(manual_pause) is not bool or type(news_blocked) is not bool:
        return veto('PAUSE_STATE_UNAVAILABLE')
    if manual_pause or news_blocked:
        return veto('MANUAL_PAUSE' if manual_pause else 'NEWS_RISK_BLOCKED')
    if session_open is not True:
        return veto('SESSION_CLOSED' if session_open is False else 'SESSION_UNVERIFIED')
    if not all(millis(x) for x in (now_ms, plan.issued_at_ms, plan.expires_at_ms, quote.at_ms)):
        return veto('INVALID_TIMESTAMPS')
    if not plan.issued_at_ms <= now_ms < plan.expires_at_ms:
        return veto('PLAN_STALE_OR_FUTURE')
    if not isinstance(plan.plan_id, str) or not plan.plan_id.strip() or type(plan.side) is not int or plan.side not in (-1, 1):
        return veto('INVALID_PLAN_ID_OR_SIDE')
    if not isinstance(spec, BrokerSpec) or not spec.server or quote.server != spec.server:
        return veto('UNVERIFIED_QUOTE_SERVER')
    if not plan.symbol or not plan.symbol == quote.symbol == spec.symbol:
        return veto('SYMBOL_MISMATCH')
    if not state.account_currency or state.account_currency != spec.account_currency:
        return veto('ACCOUNT_CURRENCY_MISMATCH')
    state_cash = (state.equity, state.free_margin, state.open_risk_cash,
                  state.day_loss_cash, state.week_loss_cash)
    if (not all(number(x) and x >= 0 for x in state_cash) or state.equity <= 0
            or not millis(state.open_positions) or not millis(state.cooldown_until_ms)):
        return veto('INVALID_ACCOUNT_RISK_STATE')
    if now_ms < state.cooldown_until_ms:
        return veto('RISK_COOLDOWN')
    if state.open_positions >= policy.max_positions:
        return veto('POSITION_COUNT_LIMIT')
    if not 0 <= now_ms - quote.at_ms <= policy.max_quote_age_ms:
        return veto('STALE_OR_FUTURE_BID_ASK')
    if not all(number(x) and x > 0 for x in (quote.bid, quote.ask, plan.entry, plan.stop)) or quote.ask < quote.bid:
        return veto('INVALID_BID_ASK_OR_PLAN_PRICE')
    if Decimal(str(quote.ask)) - Decimal(str(quote.bid)) > Decimal(str(policy.max_spread)):
        return veto('SPREAD_LIMIT')
    side = plan.side
    entry = quote.ask if side == 1 else quote.bid
    close_quote = quote.bid if side == 1 else quote.ask
    if abs(Decimal(str(entry)) - Decimal(str(plan.entry))) > Decimal(str(policy.max_entry_deviation)):
        return veto('ENTRY_DEVIATION_LIMIT')
    if ((plan.entry - plan.stop) * side <= 0 or not isinstance(plan.targets, (tuple, list)) or len(plan.targets) != 3
            or not all(number(x) and x > 0 for x in plan.targets)
            or not all((b-a) * side > 0 for a, b in zip((plan.entry, *plan.targets), plan.targets))
            or not isinstance(plan.target_weights, (tuple, list)) or len(plan.target_weights) != 3
            or not all(number(x) and x > 0 for x in plan.target_weights)
            or not isclose(sum(plan.target_weights), 1., rel_tol=0., abs_tol=1e-12)):
        return veto('INVALID_STOP_TARGET_OR_PARTIAL_GEOMETRY')

    weekly_room = state.equity * policy.weekly_loss_limit_fraction - state.week_loss_cash - state.open_risk_cash
    if weekly_room <= 0:
        return veto('WEEKLY_LOSS_LIMIT')
    try:
        sizing = size_position(
            spec, side=side, bid=quote.bid, ask=quote.ask, quote_at_ms=quote.at_ms,
            now_ms=now_ms, stop=plan.stop, equity=state.equity,
            risk_fraction=policy.risk_fraction, existing_risk_cash=state.open_risk_cash,
            portfolio_risk_fraction=policy.portfolio_risk_fraction,
            realized_daily_loss=state.day_loss_cash + state.open_risk_cash,
            daily_loss_limit_fraction=policy.daily_loss_limit_fraction,
            slippage_ticks_each_way=policy.slippage_ticks_each_way,
            max_quote_age_ms=policy.max_quote_age_ms, max_spec_age_ms=policy.max_spec_age_ms,
        )
    except (ArithmeticError, ValueError, TypeError):
        return veto('INVALID_SIZING_INPUT')
    if sizing['status'] != 'CALCULATION_ONLY':
        return veto(sizing['reason'])
    loss_per_lot = sizing['estimated_loss_per_lot']
    if not number(loss_per_lot) or loss_per_lot <= 0:
        return veto('INVALID_CASH_RISK')
    if not number(tick_value_profit_account_ccy) or tick_value_profit_account_ccy <= 0:
        return veto('PROFIT_TICK_VALUE_UNVERIFIED')
    broker_distance = max(spec.stops_level_points, spec.freeze_level_points) * spec.point
    slip_price = policy.slippage_ticks_each_way * spec.tick_size
    if any(not grid_aligned(tp, spec.tick_size) or (tp-entry)*side <= 2*slip_price
           or (tp-close_quote)*side + 1e-12 < broker_distance for tp in plan.targets):
        return veto('TARGET_OFF_GRID_OR_NOT_EXECUTABLE')
    step = Decimal(str(spec.volume_step))
    min_units = int((Decimal(str(spec.volume_min)) / step).to_integral_value(rounding=ROUND_CEILING))
    budget = min(sizing['risk_budget_cash'], weekly_room,
                 policy.max_loss_cash if policy.max_loss_cash is not None else weekly_room)
    raw_units = Decimal(str(min(sizing['lots'], budget / loss_per_lot))) / step
    high = int(raw_units.to_integral_value(rounding=ROUND_FLOOR))
    low = 3 * min_units
    if high < low:
        return veto('RISK_BELOW_MINIMUM_THREE_PARTIALS')
    if not callable(margin_for_lots) or state.free_margin <= 0:
        return veto('MARGIN_UNAVAILABLE' if state.free_margin > 0 else 'NO_FREE_MARGIN')
    margin_budget = state.free_margin * policy.max_free_margin_fraction
    # Probe only grid volumes, and retain only a quantity whose own probe passed.
    # The maximum feasible volume is guaranteed only for monotone margin estimates.
    chosen = None
    try:
        margin = margin_for_lots(float(step * high))
        if not number(margin) or margin <= 0:
            return veto('MARGIN_UNVERIFIED')
        if margin <= margin_budget:
            chosen = (high, margin)
        else:
            high -= 1
            while low <= high:
                mid = (low + high) // 2
                margin = margin_for_lots(float(step * mid))
                if not number(margin) or margin <= 0:
                    return veto('MARGIN_UNVERIFIED')
                if margin <= margin_budget:
                    chosen = (mid, margin)
                    low = mid + 1
                else:
                    high = mid - 1
    except Exception:
        return veto('MARGIN_PROVIDER_ERROR')
    if chosen is None:
        return veto('MARGIN_LIMIT')
    units, margin = chosen
    lots = float(step * units)
    cash_risk = lots * loss_per_lot
    if not number(cash_risk) or cash_risk > budget:
        return veto('CASH_RISK_AFTER_ROUNDING')
    allocations = _volumes(units, min_units, plan.target_weights)
    target_lots = tuple(float(step * n) for n in allocations)
    tp_net = [((target-entry)*side - 2*slip_price) / spec.tick_size
              * tick_value_profit_account_ccy - spec.commission_round_trip_per_lot
              for target in plan.targets]
    net_r = sum(n * reward for n, reward in zip(target_lots, tp_net)) / cash_risk
    if not number(net_r) or tp_net[0] <= 0 or net_r < policy.min_net_reward_r:
        return veto('NET_REWARD_RISK_LIMIT')
    return RiskDecision(True, 'LOCAL_PLAN_VALIDATED', lots, cash_risk, margin,
                        net_r, entry, target_lots)


@dataclass(frozen=True)
class ManagementPolicy:
    max_holding_ms: int
    break_even_after_target: int = 1
    profit_lock_r: float = 0.
    atr_trail_multiplier: float | None = None


@dataclass(frozen=True)
class ManagementDecision:
    action: str
    exit_price: float | None = None
    next_stop: float | None = None
    targets_hit: tuple[int, ...] = ()


def manage_quote(plan: TradePlan, quote: Quote, *, fill_price: float, fill_at_ms: int,
                 current_stop: float, next_target: int, previous_quote_at_ms: int,
                 tick_size: float, policy: ManagementPolicy, now_ms: int,
                 max_quote_age_ms: int, expected_server: str, exit_slippage_ticks: int = 0,
                 atr: float | None = None, invalidation_price: float | None = None,
                 volatility_shock: bool = False) -> ManagementDecision:
    """Preview a paper exit or tighter stop. Existing fills and state stay immutable.

    One fresh quote can hit several TP levels. A stop takes precedence and fills
    at the observed executable side plus adverse configured slippage. Stop changes
    affect later observations only. No price is imputed across a missing quote.
    """
    if (not isinstance(plan, TradePlan) or not isinstance(quote, Quote)
            or not isinstance(policy, ManagementPolicy) or not isinstance(plan.targets, (tuple, list))):
        return ManagementDecision('DATA_UNAVAILABLE')
    prices = (quote.bid, quote.ask, fill_price, current_stop, plan.stop, tick_size, *plan.targets)
    times = (quote.at_ms, fill_at_ms, previous_quote_at_ms, now_ms,
             max_quote_age_ms, policy.max_holding_ms, exit_slippage_ticks)
    if (not all(millis(x) for x in times) or policy.max_holding_ms == 0
            or not all(number(x) and x > 0 for x in prices)
            or quote.symbol != plan.symbol or quote.ask < quote.bid
            or not isinstance(expected_server, str) or not expected_server or quote.server != expected_server
            or not fill_at_ms <= previous_quote_at_ms < quote.at_ms <= now_ms
            or now_ms - quote.at_ms > max_quote_age_ms
            or type(plan.side) is not int or plan.side not in (-1, 1)
            or (fill_price-plan.stop)*plan.side <= 0
            or (current_stop-plan.stop)*plan.side < 0 or not grid_aligned(current_stop, tick_size)
            or type(next_target) is not int or not 0 <= next_target < 3
            or len(plan.targets) != 3 or type(volatility_shock) is not bool
            or not all((b-a)*plan.side > 0 for a, b in zip((fill_price, *plan.targets), plan.targets))
            or type(policy.break_even_after_target) is not int or not 1 <= policy.break_even_after_target <= 3
            or not number(policy.profit_lock_r) or policy.profit_lock_r < 0
            or policy.atr_trail_multiplier is not None and
               (not number(policy.atr_trail_multiplier) or policy.atr_trail_multiplier <= 0)
            or invalidation_price is not None and
               (not number(invalidation_price) or invalidation_price <= 0)):
        return ManagementDecision('DATA_UNAVAILABLE')
    side = plan.side
    mark = quote.bid if side == 1 else quote.ask
    exit_price = mark - side * exit_slippage_ticks * tick_size
    if not number(exit_price) or exit_price <= 0:
        return ManagementDecision('DATA_UNAVAILABLE')
    if (mark-current_stop)*side <= 0:
        return ManagementDecision('STOP', exit_price)
    if invalidation_price is not None and (mark-invalidation_price)*side <= 0:
        return ManagementDecision('STRUCTURAL_INVALIDATION', exit_price)
    if volatility_shock:
        return ManagementDecision('VOLATILITY_SHOCK', exit_price)
    if quote.at_ms-fill_at_ms >= policy.max_holding_ms:
        return ManagementDecision('TIME_STOP', exit_price)
    hits = tuple(i for i in range(next_target, 3) if (mark-plan.targets[i])*side >= 0)
    if 2 in hits:
        return ManagementDecision('TARGETS', targets_hit=hits)
    levels = [current_stop]
    if max((next_target, *(i+1 for i in hits))) >= policy.break_even_after_target:
        levels.append(fill_price + side * policy.profit_lock_r * abs(fill_price-plan.stop))
    if (policy.atr_trail_multiplier is not None and number(atr) and atr > 0):
        levels.append(mark - side * atr * policy.atr_trail_multiplier)
    stop = max(levels, key=lambda price: price*side)
    step = Decimal(str(tick_size))
    # A tightened long SL rounds down and a short SL rounds up.
    direction = ROUND_FLOOR if side == 1 else ROUND_CEILING
    stop = float((Decimal(str(stop))/step).to_integral_value(rounding=direction)*step)
    tightened = stop if (stop-current_stop)*side > 0 and (mark-stop)*side > 0 else None
    return ManagementDecision('TARGETS' if hits else 'HOLD', next_stop=tightened, targets_hit=hits)
