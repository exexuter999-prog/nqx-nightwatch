#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Conditional challenge arithmetic for R11-D.

This tool reports arithmetic sensitivity only.  It never labels a scenario as
an empirical pass probability unless a separately frozen ledger is supplied
and the minimum evidence gate is met.
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys

POINT_VALUE = 2.0
DEFAULT_QTY = 2


def scenario(args):
    risk_dollars = float(args.risk_dollars)
    qty = int(args.qty)
    target_distance = float(args.target_distance)
    stop_distance = float(args.stop_distance)
    fees = float(args.fees)
    slippage = float(args.slippage)
    remaining = float(args.remaining_profit)
    target_value = target_distance * qty * POINT_VALUE
    required_qty = math.ceil((remaining + fees) / (target_distance * POINT_VALUE)) if target_distance > 0 else None
    planned_loss = (stop_distance + slippage) * qty * POINT_VALUE + fees
    win_r = float(args.avg_win_r)
    loss_r = float(args.avg_loss_r)
    wr = float(args.wr)
    expectancy_r = wr * win_r - (1.0 - wr) * loss_r
    expected_trades = (remaining / (expectancy_r * risk_dollars)) if expectancy_r > 0 and risk_dollars > 0 else None
    feasible = bool(target_value > 0 and planned_loss <= risk_dollars and stop_distance * qty * POINT_VALUE <= float(args.max_drawdown))
    return {
        "mode": "scenario",
        "status": "COMPLIANT" if feasible else "NOT_FEASIBLE",
        "risk_dollars": risk_dollars,
        "qty": qty,
        "required_qty_for_remaining_profit": required_qty,
        "target_distance_pt": target_distance,
        "stop_distance_pt": stop_distance,
        "fees": fees,
        "slippage_pt": slippage,
        "planned_loss_dollars": round(planned_loss, 2),
        "pass_target": remaining,
        "target_value_per_trade": round(target_value, 2),
        "win_rate_input": wr,
        "avg_win_r": win_r,
        "avg_loss_r": loss_r,
        "expectancy_r": round(expectancy_r, 4),
        "expected_trades_conditional": round(expected_trades, 2) if expected_trades is not None else None,
        # No empirical sample is being claimed by --scenario.
        "conditional_pass_rate": None,
        "conditional_breach_rate": None,
        "evidence": "UNVERIFIED_INPUT",
    }


def empirical(args):
    if not args.ledger:
        return {"mode": "empirical", "status": "UNVERIFIED_INPUT", "reason": "--ledger is required"}
    try:
        with open(args.ledger, "r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        return {"mode": "empirical", "status": "UNVERIFIED_INPUT", "reason": str(exc)}
    rows = [row for row in rows if row.get("outcome") in {"WIN", "LOSS", "BREAKEVEN"}]
    years = {str(row.get("decision_id", ""))[:4] for row in rows if row.get("decision_id")}
    if len(rows) < 30 or len(years) < 2:
        return {"mode": "empirical", "status": "UNVERIFIED_INPUT", "count": len(rows),
                "calendar_years": sorted(years), "reason": "requires OOS N>=30 and two calendar years"}
    wins = sum(row.get("outcome") == "WIN" for row in rows)
    rates = wins / len(rows)
    r_values = []
    for row in rows:
        try:
            r_values.append(float(row["r_multiple"]))
        except (KeyError, TypeError, ValueError):
            pass
    return {"mode": "empirical", "status": "UNVERIFIED_INPUT",
            "count": len(rows), "calendar_years": sorted(years),
            "observed_win_rate": round(rates, 4),
            "observed_mean_r": round(statistics.mean(r_values), 4) if r_values else None,
            "reason": "calibration and trailing-drawdown path validation remain separate gates"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="store_true")
    parser.add_argument("--empirical", action="store_true")
    parser.add_argument("--wr", type=float, default=0.50)
    parser.add_argument("--avg-win-r", type=float, default=2.0)
    parser.add_argument("--avg-loss-r", type=float, default=1.0)
    parser.add_argument("--risk-dollars", type=float, default=240.0)
    parser.add_argument("--remaining-profit", type=float, default=9000.0)
    parser.add_argument("--target-distance", type=float, default=108.0)
    parser.add_argument("--stop-distance", type=float, default=60.0)
    parser.add_argument("--qty", type=int, default=DEFAULT_QTY)
    parser.add_argument("--fees", type=float, default=2.0)
    parser.add_argument("--slippage", type=float, default=2.0)
    parser.add_argument("--max-drawdown", type=float, default=4000.0)
    parser.add_argument("--ledger")
    args = parser.parse_args(argv)
    if args.empirical:
        out = empirical(args)
    elif args.scenario:
        out = scenario(args)
    else:
        out = {"status": "UNVERIFIED_INPUT", "reason": "choose --scenario or --empirical"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
