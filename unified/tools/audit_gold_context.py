"""Audit matching GOLD PRO native market-context CSV exports."""

import argparse
from collections import deque
import csv
from dataclasses import dataclass
import hashlib
import json
from math import isclose
from pathlib import Path

from unified.tools.gold_market_data import (
    Bar, HtfContext, HTF_WINDOW, ObservedRange, RollingFingerprint, SessionRanges,
    finite_emas, htf_side,
)


@dataclass(frozen=True)
class ContextRow:
    symbol: str
    frame: str
    tick: float
    bar: Bar
    fingerprint: tuple
    ready: bool
    session: str
    session_range: ObservedRange | None
    trading_day: int
    daily_ohl: tuple
    day_complete: bool
    htf: tuple[HtfContext, HtfContext, HtfContext]

    @classmethod
    def parse(cls, message):
        fields = message.split("|")
        if fields[:2] != ["GOLD_CTX", "1"] or len(fields) != 42:
            raise ValueError("Expected GOLD_CTX|1 with 42 fields")
        symbol, frame, tick = fields[2:5]
        if symbol != "OANDA:XAUUSD" or frame not in ("5", "15", "30"):
            raise ValueError(f"Unexpected market {symbol} {frame}")
        bar = Bar(int(fields[5]), int(fields[6]), *map(float, fields[7:11]))
        fp = tuple(nullable(value, int) for value in fields[11:13])
        high, low = (nullable(value, float) for value in fields[15:17])
        observed = None if high is None else ObservedRange(high, low, int(fields[17]), boolean(fields[18]))
        if observed is None and fields[15:19] != ["-", "-", "-", "0"]:
            raise ValueError(f"Inconsistent missing session at {bar.open_at}")
        groups = []
        for index in (24, 30, 36):
            opened, closed, price, fast, slow, side = fields[index:index + 6]
            groups.append(HtfContext(
                nullable(opened, int), nullable(closed, int), nullable(price, float),
                nullable(fast, float), nullable(slow, float), nullable(side, int),
            ))
        return cls(
            symbol, frame, float(tick), bar, fp, boolean(fields[13]), fields[14], observed,
            int(fields[19]), tuple(map(float, fields[20:23])), boolean(fields[23]), tuple(groups),
        )


def nullable(raw, convert):
    return None if raw == "-" else convert(raw)


def boolean(raw):
    if raw not in ("0", "1"):
        raise ValueError(f"Invalid boolean {raw}")
    return raw == "1"


def read_export(path):
    rows = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        for entry in csv.DictReader(source):
            message = entry.get("Message", "")
            if not message.startswith("GOLD_CTX|"):
                continue
            row = ContextRow.parse(message)
            stamp = row.bar.open_at
            if stamp in rows and row != rows[stamp]:
                raise ValueError(f"Conflicting records at {stamp} in {path}")
            rows[stamp] = row
    if not rows:
        raise ValueError(f"No GOLD_CTX records in {path}")
    return tuple(rows[stamp] for stamp in sorted(rows))


class _HtfAudit:
    def __init__(self, minutes):
        self.minutes = minutes
        self.current = None
        self.window = deque(maxlen=HTF_WINDOW)
        self.finite_checks = 0
        self.ready = 0

    def add(self, group, chart_open):
        opened, known, close, fast, slow, side = (
            group.open_at, group.known_at, group.close, group.ema21, group.ema55, group.side,
        )
        if known is None:
            if any(value is not None for value in (opened, close, fast, slow, side)) or self.current is not None:
                raise ValueError(f"Missing {self.minutes} timestamp for a previously present source at {chart_open}")
            return
        if opened is None or close is None or not opened < known <= chart_open:
            raise ValueError(f"Future or malformed {self.minutes} source at {chart_open}")
        if (fast is None) != (slow is None):
            raise ValueError(f"Partial {self.minutes} EMA warmup at {chart_open}")
        if side != htf_side(opened, known, close, fast, slow, chart_open, self.minutes):
            raise ValueError(f"Invalid {self.minutes} context direction or readiness at {chart_open}")
        self.ready += int(side is not None)
        stable = (opened, known, close, fast, slow)
        if self.current is not None:
            if known < self.current[1] or known == self.current[1] and stable != self.current:
                raise ValueError(f"Revised {self.minutes} source at {chart_open}")
        if stable != self.current:
            self.current = stable
            self.window.append(close)
            predicted = finite_emas(self.window)
            if predicted[0] is not None:
                if fast is None or not all(isclose(a, b, rel_tol=0, abs_tol=1e-8)
                                           for a, b in zip(predicted, (fast, slow))):
                    raise ValueError(f"Incorrect finite {self.minutes} EMA at {chart_open}")
                self.finite_checks += 1


def audit_rows(rows):
    tick = rows[0].tick
    fingerprints = RollingFingerprint(tick)
    sessions = SessionRanges()
    probes = [_HtfAudit(minutes) for minutes in (60, 240, 1440)]
    previous = None
    daily_known = False
    daily_values = ()
    fingerprint_checks = session_checks = daily_checks = ready = 0
    for row in rows:
        chart = row.bar
        if (row.symbol, row.frame, row.tick) != (rows[0].symbol, rows[0].frame, tick):
            raise ValueError("Mixed market exports")
        if previous is not None and (chart.open_at < previous.bar.close_at or row.trading_day < previous.trading_day):
            raise ValueError(f"Chronological reversal at {chart.open_at}")
        expected = fingerprints.add(chart)
        if expected is not None:
            if row.fingerprint != expected:
                raise ValueError(f"Chart fingerprints diverged at {chart.open_at}")
            fingerprint_checks += 1
        label, session_range = sessions.add(chart)
        if row.session != label:
            raise ValueError(f"Session assignment mismatch at {chart.open_at}")
        if session_range is None or session_range.complete_to_current:
            if row.session_range != session_range:
                raise ValueError(f"Session range mismatch at {chart.open_at}")
            session_checks += 1
        if row.trading_day > chart.open_at:
            raise ValueError(f"Future daily timestamp at {chart.open_at}")
        if previous is None or row.trading_day != previous.trading_day:
            daily_known = row.trading_day == chart.open_at
            daily_values = chart.open, chart.high, chart.low
        else:
            daily_known = daily_known and previous.bar.close_at == chart.open_at
            daily_values = daily_values[0], max(daily_values[1], chart.high), min(daily_values[2], chart.low)
        if daily_known:
            if not row.day_complete or row.daily_ohl != daily_values:
                raise ValueError(f"Daily range mismatch at {chart.open_at}")
            daily_checks += 1
        for probe, group in zip(probes, row.htf):
            probe.add(group, chart.open_at)
        if row.ready != all(group.side is not None for group in row.htf[:2]):
            raise ValueError(f"H1/H4 eligibility mismatch at {chart.open_at}")
        ready += int(row.ready)
        previous = row
    return {
        "bars": len(rows), "first_open_utc_ms": rows[0].bar.open_at,
        "last_close_utc_ms": rows[-1].bar.close_at, "eligible_bars": ready,
        "unknown_context_bars": len(rows) - ready, "fingerprints_recomputed": fingerprint_checks,
        "session_labels_checked": len(rows), "session_ranges_recomputed": session_checks,
        "daily_ranges_recomputed": daily_checks,
        "htf": {str(probe.minutes): {"ready_bars": probe.ready, "finite_ema_checks": probe.finite_checks}
                for probe in probes},
    }


def paired(strategy, indicator, minimum_bars):
    left, right = read_export(strategy), read_export(indicator)
    if len(left) < minimum_bars or len(right) < minimum_bars:
        raise ValueError(f"Need {minimum_bars} chart rows in each export")
    if left != right:
        for a, b in zip(left, right):
            if a != b:
                raise ValueError(f"Strategy and indicator context differ at {a.bar.open_at} / {b.bar.open_at}")
        raise ValueError("Strategy and indicator timestamp coverage differs")
    audit = audit_rows(left)
    if audit["eligible_bars"] == 0:
        raise ValueError("Matched exports contain no eligible H1/H4 context")
    for label, path in (("strategy_sha256", strategy), ("indicator_sha256", indicator)):
        audit[label] = hashlib.sha256(path.read_bytes()).hexdigest()
    return left[0].frame, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", type=Path, nargs="+", required=True)
    parser.add_argument("--indicator", type=Path, nargs="+", required=True)
    parser.add_argument("--min-bars", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if len(args.strategy) != len(args.indicator) or args.min_bars < 1:
        parser.error("Supply one strategy and indicator export per frame, and a positive minimum")
    frames = {}
    for strategy, indicator in zip(args.strategy, args.indicator):
        frame, audit = paired(strategy, indicator, args.min_bars)
        if frame in frames:
            raise ValueError(f"Duplicate export for M{frame}")
        frames[frame] = audit
    report = {
        "status": "VERIFIED_NATIVE_CONTEXT" if set(frames) == {"5", "15", "30"} else "VERIFIED_NATIVE_CONTEXT_SUBSET",
        "symbol": "OANDA:XAUUSD",
        "scope": "Equality of exported confirmed market context. Finite EMA counts require 110 visible source closes.",
        "frames": frames,
    }
    with args.out.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(report, output, indent=2, sort_keys=True)
        output.write("\n")
    print(f"Audited {sum(row['bars'] for row in frames.values())} paired native context rows")


if __name__ == "__main__":
    main()
