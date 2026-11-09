"""Audit supplied native fill records; this module does not simulate Pine fills.

The records must come from a native ledger (or an explicitly synthetic fixture).
No OHLC path, prospective gap rejection, or inferred fill is manufactured here.
"""
from dataclasses import dataclass, field
from math import isclose


@dataclass(frozen=True)
class NativeFill:
    index: int
    plan_id: str
    entry: float
    exit: float
    quantity: float
    commission: float
    entry_time: int
    exit_time: int
    processed_time: int
    exit_kind: str


@dataclass
class NativeCampaign:
    plan_id: str
    side: int
    initial_stop: float
    total_quantity: float
    pointvalue: float = 1.0
    minimum_tick: float = 0.001
    records: dict[int, NativeFill] = field(default_factory=dict)

    def observe(self, fills):
        """Append genuinely new native records; identical rereads are idempotent."""
        for fill in fills:
            if fill.plan_id != self.plan_id:
                raise ValueError('wrong plan identity')
            if fill.index in self.records:
                if self.records[fill.index] != fill:
                    raise ValueError('native record changed')
                continue
            if fill.quantity <= 0 or fill.commission < 0:
                raise ValueError('invalid quantity or commission')
            if not fill.entry_time <= fill.exit_time <= fill.processed_time:
                raise ValueError('backdated observation')
            if self.closed_quantity + fill.quantity > self.total_quantity + 1e-8:
                raise ValueError('closed quantity exceeds entry quantity')
            self.records[fill.index] = fill

    @property
    def closed_quantity(self):
        return sum(r.quantity for r in self.records.values())

    @property
    def remaining_quantity(self):
        return max(0.0, self.total_quantity - self.closed_quantity)

    def report(self):
        ordered = sorted(self.records.values(), key=lambda r: r.index)
        entry = ordered[0].entry if ordered else None
        risk = (entry - self.initial_stop) * self.side if entry is not None else None
        valid_risk = risk is not None and risk >= self.minimum_tick
        gross_cash = sum((r.exit - r.entry) * self.side * r.quantity * self.pointvalue for r in ordered)
        commission_cash = sum(r.commission for r in ordered)
        net_cash = gross_cash - commission_cash
        denominator = risk * self.total_quantity * self.pointvalue if valid_risk else None
        return {
            'plan_id': self.plan_id,
            'actual_entry': entry,
            'closed_quantity': self.closed_quantity,
            'remaining_quantity': self.remaining_quantity,
            'completed': isclose(self.closed_quantity, self.total_quantity, abs_tol=1e-8),
            'gross_cash': gross_cash,
            'commission_cash': commission_cash,
            'net_cash': net_cash,
            'gross_r': gross_cash / denominator if valid_risk else None,
            'cost_r': commission_cash / denominator if valid_risk else None,
            'net_r': net_cash / denominator if valid_risk else None,
            'exit_events': [r.exit_kind for r in ordered],
            'effective_times': [r.exit_time for r in ordered],
            'known_times': [r.processed_time for r in ordered],
        }


def admission_quantity_valid(unit_quantity, minimum_quantity):
    """Do not round up risk to get trades. Each of the three legs must be tradable."""
    return minimum_quantity is not None and minimum_quantity > 0 and unit_quantity + minimum_quantity * 1e-8 >= minimum_quantity

