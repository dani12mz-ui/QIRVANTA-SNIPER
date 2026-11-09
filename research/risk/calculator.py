"""Offline linear-CFD sizing only. Returns a calculation, never sends orders.

An account/server-specific verified specification and current executable quote
are mandatory. Generic website contract examples cannot set verified=True.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
import math


@dataclass(frozen=True)
class BrokerSpec:
    symbol: str
    server: str
    account_currency: str
    verified: bool
    evidence: str
    verified_at_ms: int
    contract_size: float
    point: float
    tick_size: float
    tick_value_loss_account_ccy: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: float
    freeze_level_points: float
    commission_round_trip_per_lot: float
    currency_conversion_verified: bool
    calculation_mode: str = 'LINEAR_CFD'


def size_position(spec, *, side, bid, ask, quote_at_ms, now_ms, stop, equity,
                  risk_fraction=.005, existing_risk_cash=0, portfolio_risk_fraction=.01,
                  realized_daily_loss=0, daily_loss_limit_fraction=.02,
                  slippage_ticks_each_way=2, max_quote_age_ms=5000,
                  max_spec_age_ms=86400000, manual_news_pause=False):
    """Stops use closing bid for long / ask for short, entry uses opposite side.

    Loss includes observed spread via entry-to-stop distance plus a configured
    adverse entry/exit tick allowance and round-trip commission. Swaps and gap
    tail losses are unbounded here; therefore this is not guaranteed loss.
    """
    def blocked(reason):
        return {'status': 'BLOCKED', 'reason': reason, 'lots': None, 'orders_enabled': False}
    if not isinstance(spec, BrokerSpec) or spec.verified is not True or not spec.evidence or not spec.server or not spec.symbol or not spec.account_currency or spec.currency_conversion_verified is not True:
        return blocked('UNVERIFIED_ACCOUNT_SYMBOL_SPECIFICATION')
    if spec.calculation_mode != 'LINEAR_CFD':
        return blocked('UNSUPPORTED_PROFIT_CALCULATION_MODE')
    timestamps=(now_ms,quote_at_ms,spec.verified_at_ms,max_quote_age_ms,max_spec_age_ms)
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<0 for x in timestamps):
        return blocked('INVALID_TIMESTAMP_OR_AGE_LIMIT')
    if not 0 <= now_ms-spec.verified_at_ms <= max_spec_age_ms:
        return blocked('STALE_OR_FUTURE_SPECIFICATION')
    if not 0 <= now_ms-quote_at_ms <= max_quote_age_ms:
        return blocked('STALE_OR_FUTURE_BID_ASK')
    if manual_news_pause:
        return blocked('MANUAL_NEWS_PAUSE')
    positive = (spec.contract_size,spec.point,spec.tick_size,spec.tick_value_loss_account_ccy,spec.volume_min,spec.volume_max,spec.volume_step,bid,ask,stop,equity)
    nonnegative = (spec.stops_level_points,spec.freeze_level_points,spec.commission_round_trip_per_lot,existing_risk_cash,realized_daily_loss,slippage_ticks_each_way)
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x <= 0 for x in positive) or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x < 0 for x in nonnegative):
        return blocked('INVALID_OR_MISSING_NUMERIC_INPUT')
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in (risk_fraction,portfolio_risk_fraction,daily_loss_limit_fraction)):
        return blocked('INVALID_GEOMETRY_OR_RISK_LIMIT')
    if side not in (-1,1) or ask < bid or spec.volume_max < spec.volume_min or not 0 < risk_fraction <= portfolio_risk_fraction < 1 or not 0 < daily_loss_limit_fraction < 1:
        return blocked('INVALID_GEOMETRY_OR_RISK_LIMIT')
    if realized_daily_loss >= equity*daily_loss_limit_fraction:
        return blocked('DAILY_LOSS_LIMIT')
    entry = ask if side == 1 else bid
    closing_quote = bid if side == 1 else ask
    minimum_distance = max(spec.stops_level_points,spec.freeze_level_points)*spec.point
    if (closing_quote-stop)*side <= 0 or (closing_quote-stop)*side + 1e-12 < minimum_distance:
        return blocked('STOP_SIDE_OR_BROKER_DISTANCE')
    # SL must align to actual tick grid, not the displayed point or decimal count.
    ticks = stop/spec.tick_size
    if abs(ticks-round(ticks)) > 1e-7:
        return blocked('STOP_OFF_TICK_GRID')
    risk_budget = min(equity*risk_fraction, equity*portfolio_risk_fraction-existing_risk_cash,
                      equity*daily_loss_limit_fraction-realized_daily_loss)
    if risk_budget <= 0:
        return blocked('PORTFOLIO_RISK_LIMIT')
    price_loss = (entry-stop)*side/spec.tick_size*spec.tick_value_loss_account_ccy
    allowance = 2*slippage_ticks_each_way*spec.tick_value_loss_account_ccy
    loss_per_lot = price_loss + allowance + spec.commission_round_trip_per_lot
    raw_lots = min(spec.volume_max,risk_budget/loss_per_lot)
    # MT5 volume must be an integer multiple of step (zero-anchored), not
    # min+n*step. A non-grid minimum does not authorize a non-grid volume.
    if raw_lots < spec.volume_min:
        return blocked('RISK_BELOW_MINIMUM_LOT')
    step = Decimal(str(spec.volume_step))
    lots = (Decimal(str(raw_lots))/step).to_integral_value(rounding=ROUND_FLOOR)*step
    lots = float(lots)
    if lots < spec.volume_min:
        return blocked('RISK_BELOW_MINIMUM_LOT_ON_STEP_GRID')
    return {'status': 'CALCULATION_ONLY', 'symbol': spec.symbol, 'lots': lots,
            'execution_reference': entry, 'spread': ask-bid, 'stop': stop,
            'risk_budget_cash': risk_budget, 'estimated_loss_per_lot': loss_per_lot,
            'estimated_loss_cash': lots*loss_per_lot, 'orders_enabled': False,
            'limitations': ['gap and liquidity loss can exceed estimate', 'overnight swap not included', 'margin/filling permission must be independently checked before any manual trade'],
            'source': spec.evidence}
