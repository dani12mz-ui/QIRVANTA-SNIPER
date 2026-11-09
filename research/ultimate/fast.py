"""Reconcile actual campaign outcomes and isolate causal FAST hypotheses."""
from collections import Counter, defaultdict
from dataclasses import replace
from math import isclose
from statistics import mean

from research.ultimate.replay import Setup
from research.validation.native_report import excursion_bounds, sessions
from research.validation.statistics import block_bootstrap, cash_metrics, quantile


def drawdown_duration(times_cash):
    cash = peak = 0.
    peak_at = None
    max_ms = 0
    underwater = False
    for stamp, delta in sorted(times_cash, key=lambda item: item[0]):
        if peak_at is None:
            peak_at = stamp
        cash += delta
        if underwater or cash < peak:
            max_ms = max(max_ms, stamp - peak_at)
        underwater = cash < peak
        if cash >= peak:
            peak = cash
            peak_at = stamp
    return max_ms / 3_600_000


def performance(outcomes):
    ordered = sorted(outcomes, key=lambda x: (x["exit_time"], x["id"]))
    cash = [x["net_cash"] for x in ordered]
    wins, losses = [x for x in cash if x > 0], [x for x in cash if x < 0]
    rates = [x["net_r"] for x in ordered if x["net_r"] is not None]
    hits = Counter(reason for x in ordered for reason in set(x["exit_reasons"])
                   if reason in {"TP1", "TP2", "TP3"})
    return {
        **cash_metrics(cash),
        "avg_win_cash": mean(wins) if wins else None,
        "avg_loss_cash": mean(losses) if losses else None,
        "campaign_close_drawdown_hours": drawdown_duration(
            [(x["exit_time"], x["net_cash"]) for x in ordered]),
        "tp_campaigns": {key: hits[key] for key in ("TP1", "TP2", "TP3")},
        "net_r_mean": block_bootstrap(rates, repetitions=1000),
        "unscored_r": len(ordered) - len(rates),
    }


def verify_native(audit):
    if audit["status"] != "PASS_OBSERVED_JOURNAL" or audit["issues"]:
        raise ValueError("native source did not reconcile")
    campaigns = audit["campaigns"]
    ids, exit_ids = set(), set()
    outputs = []
    for campaign in campaigns:
        identifier = campaign["plan_id"]
        if identifier is None or identifier in ids or campaign["state"] != "COMPLETE":
            raise ValueError("expected complete campaigns with unique plan IDs")
        ids.add(identifier)
        entries, exits = campaign["actual_entry"], campaign["exit_rows"]
        quantity = sum(abs(row["size"]) for row in exits)
        fees = sum(row["commission"] for row in exits)
        if not isclose(quantity, campaign["submitted_quantity"], abs_tol=1e-8):
            raise ValueError("native campaign quantities differ")
        gross = 0.
        for row in exits:
            if row["trade_index"] in exit_ids:
                raise ValueError("duplicate native exit row")
            exit_ids.add(row["trade_index"])
            gross += (row["exit_price"] - entries) * row["size"] * audit["run"]["pointvalue"]
        net = gross - fees
        if not (isclose(net, campaign["realized_net_cash"], abs_tol=1e-5)
                and isclose(fees, campaign["realized_commission_cash"], abs_tol=1e-8)):
            raise ValueError("native cash reconciliation differs")
        outputs.append({
            "id": identifier, "family": campaign["family"], "side": campaign["side"],
            "exit_time": campaign["last_exit_time"], "net_cash": campaign["realized_net_cash"],
            "net_r": campaign["net_r"], "fees": fees, "gross": gross, "parts": len(exits),
            "exit_reasons": [x["exit_comment"] for x in exits],
        })
    if len(exit_ids) != audit["coverage"]["closed_rows_observed"]:
        raise ValueError("native exit coverage differs")
    return outputs


def campaign_context(campaigns, bars, features, native_outcomes):
    rows = []
    outcome_by_id = {row["id"]: row for row in native_outcomes}
    for campaign in campaigns:
        m = campaign["submitted_metadata"]
        i = m["submitted_bar"]
        bar = bars[i]
        f = features[i]
        if bar["time_close"] != m["submitted_time"] or not isclose(bar["close"], m["reference"], abs_tol=.00051):
            raise ValueError("native signal/bar mapping differs")
        side = campaign["side"]
        recent = bars[max(0, i - 4):i]
        if len(recent) != 4:
            raise ValueError("missing FAST lookback")
        boundary = (max(x["high"] for x in recent) if side == 1 else min(x["low"] for x in recent))
        ref, stop, atr = m["reference"], m["initial_sl"], m["atr"]
        risk = (ref - stop) * side
        ext = (ref - boundary) * side / atr
        # The baseline FAST definition is close across the preceding 4-bar extreme.
        if campaign["family"] == 2 and (ext <= 0 or (bars[i - 1]["close"] - boundary) * side > .00051):
            raise ValueError("native FAST boundary could not be reconstructed")
        ratio = (bar["close"] - bar["open"]) * side / f["range"] if f["range"] > 0 else None
        obstacle = f["buy_obstacle" if side == 1 else "sell_obstacle"]
        room = (obstacle - ref) * side / risk if obstacle is not None and risk > 0 else None
        entry_i = campaign["entry_bar_index"]
        last_i = max(fill["exit_bar_index"] for fill in campaign["exit_rows"])
        first_three = bars[entry_i:min(last_i, entry_i + 3)]
        failed_bar = next((k for k, item in enumerate(first_three, 1)
                           if (item["close"] - boundary) * side <= 0), None)
        net = outcome_by_id[campaign["plan_id"]]
        levels = {
            "planned_stop_atr": risk / atr,
            "target_rr_planned": [(m[f"tp{j}"] - ref) * side / risk for j in (1, 2, 3)],
            "entry_deviation_signed_atr": (campaign["actual_entry"] - ref) * side / atr,
        }
        setup = Setup(net["id"], i, side, ref, stop, atr, boundary, obstacle)
        row = {
            **net, **levels, "signal_at": m["submitted_time"], "entry_at": campaign["entry_time"],
            "entry_delay_bars": entry_i - i,
            "entry_delay_from_confirmation_ms": campaign["entry_time"] - m["submitted_time"],
            "regime": f["regime"], "native_regime": campaign["regime"],
            "native_htf_relation": campaign["trend_relation"], "confirmed_h1_bias": f["h1"]["bias"],
            "confirmed_h4_bias": f["h4"]["bias"],
            "session": sessions(m["submitted_time"]), "native_efficiency": m["efficiency"],
            "adx": f["adx"], "atr_ratio": f["atr_ratio"], "directional_rsi": (f["rsi"] - 50) * side,
            "directional_body_fraction": ratio, "four_bar_signed_extension_atr": ext,
            "obstacle_room_r": room,
            "first_3_full_holding_closes": len(first_three),
            "first_failed_close_bar": failed_bar,
            "failed_followthrough": ("OBSERVED" if failed_bar is not None else
                                     "NO_FAILURE_WITHIN_3" if len(first_three) == 3 else "CENSORED"),
            "excursion": (excursion_bounds(campaign, bars) if campaign["initial_risk_cash"] else
                          {"status": "INVALID_ACTUAL_INITIAL_RISK"}),
            "setup": setup,
        }
        rows.append(row)
    return rows


def gate(row, bars, features, trial):
    setup = row["setup"]
    i, side = setup.index, setup.side
    f = features[i]
    swing = f["previous_swing_high" if side == 1 else "previous_swing_low"]
    directional_regime = "UPTREND" if side == 1 else "DOWNTREND"
    body_ok = row["directional_body_fraction"] is not None and row["directional_body_fraction"] >= .55
    room_ok = row["obstacle_room_r"] is not None and row["obstacle_room_r"] >= 1
    return {
        "F0": True,
        "F1": body_ok,
        "F3": swing is not None and (setup.reference - swing) * side > 0 and f["h1"]["bias"] == side,
        "F5": f["atr_ratio"] is not None and .8 <= f["atr_ratio"] <= 1.4,
        "F6": row["four_bar_signed_extension_atr"] <= .25,
        "F7": room_ok,
        "F8": f["regime"] == directional_regime,
        "F9": row["session"] in {"LONDON", "NEW_YORK", "LONDON+NEW_YORK"},
        "F10": row["directional_rsi"] >= 5 and (setup.reference - bars[i - 2]["close"]) * side >= .25 * setup.atr,
        "F12": body_ok and room_ok,
    }[trial]


def delayed_confirmation(bars, features, setup, trial):
    if trial not in {"F2", "F4"}:
        raise ValueError("unknown delay rule")
    max_wait = 1 if trial == "F2" else 4
    limit = min(len(bars), setup.index + 1 + max_wait)
    for i in range(setup.index + 1, limit):
        bar, f = bars[i], features[i]
        if bar["time"] != bars[i - 1]["time_close"]:
            return None, "GAP_BEFORE_CONFIRMATION"
        extreme = bar["low"] if setup.side == 1 else bar["high"]
        if (extreme - setup.stop) * setup.side <= 0:
            return None, "INITIAL_INVALIDATION_BEFORE_CONFIRMATION"
        distance = (bar["close"] - setup.boundary) * setup.side
        if distance <= 0:
            return None, "CLOSED_BACK_THROUGH_BREAKOUT"
        if trial == "F2":
            accepted = distance >= .10 * setup.atr
        else:
            accepted = abs(extreme - setup.boundary) <= .15 * setup.atr
        if accepted:
            if (bar["close"] - setup.stop) * setup.side < .001 or not f["atr"]:
                return None, "DELAYED_RISK_INVALID"
            return replace(setup, index=i, reference=bar["close"], atr=f["atr"],
                           obstacle=f["buy_obstacle" if setup.side == 1 else "sell_obstacle"]), "DELAYED_CONFIRMED"
    if limit < setup.index + 1 + max_wait:
        return None, "PENDING_AT_DATA_CUTOFF"
    return None, "TIMEOUT_OR_ACCEPTANCE_NOT_MET"


def loss_patterns(rows):
    bins = {
        "side": lambda x: str(x["side"]),
        "regime": lambda x: x["regime"],
        "native_htf_relation": lambda x: x["native_htf_relation"],
        "session": lambda x: x["session"],
        "failed_followthrough": lambda x: x["failed_followthrough"],
        "extended_at_fill": lambda x: "GT_0.20_ATR" if abs(x["entry_deviation_signed_atr"]) > .2 else "LE_0.20_ATR",
        "planned_structural_room": lambda x: "UNKNOWN" if x["obstacle_room_r"] is None else (
            "GE_1R" if x["obstacle_room_r"] >= 1 else "LT_1R"),
    }
    groups = {}
    for name, classify in bins.items():
        strata = defaultdict(list)
        for row in rows:
            strata[classify(row)].append(row)
        groups[name] = {key: performance(value) for key, value in sorted(strata.items())}
    for key in ("entry_deviation_signed_atr", "planned_stop_atr", "atr_ratio", "four_bar_signed_extension_atr",
                "directional_body_fraction", "obstacle_room_r"):
        groups[key] = {}
        for name, cohort in (("wins", [x for x in rows if x["net_cash"] > 0]),
                             ("losses", [x for x in rows if x["net_cash"] < 0])):
            observed = [x[key] for x in cohort if x[key] is not None]
            groups[key][name] = {"known": len(observed), "total": len(cohort),
                                 "p25": quantile(observed, .25), "median": quantile(observed, .5),
                                 "p75": quantile(observed, .75)}
    return groups
