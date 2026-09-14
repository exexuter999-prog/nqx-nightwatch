"""Bootstrap Profit Factor confidence interval from a TradingView List of Trades CSV.

Usage:
    python bootstrap_pf.py <csv_path> [--profit-col "Profit"] [--n-resamples 10000] [--seed 20260718]

Design reference: project/indicator/NQX_EVIDENCE_BASED_STRATEGY_UPGRADE_BLUEPRINT.md J-5/J-6
and project/strategy/GPT_STRATEGY_UPGRADE_DIRECTIVE.md 5-4.

Dependencies: numpy, pandas only (per directive 5-4). Neither is installed in
the harness environment this file was authored in — install with
`pip install numpy pandas` before running. This script has NOT been executed
against a real TradingView export in this session (no Strategy Tester run has
happened yet); do not treat its console output as a verified number until it
has actually been run against a real CSV (out-of-scope tag [BOOT] only applies
post-execution).

Output: PF point estimate, 95% CI (2.5/97.5 percentile), trade count, mean
trade P&L, and mean P&L / $3.00 (baseline MNQ round-trip cost, J-3), as both
JSON and stdout text. The random seed is fixed for reproducibility.
"""

import argparse
import json
import sys

import numpy as np
import pandas as pd

BASELINE_ROUNDTRIP_COST_USD = 3.00


def compute_profit_factor(pnl):
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    if losses == 0:
        return np.inf if gains > 0 else np.nan
    return gains / losses


def bootstrap_pf(pnl, n_resamples, seed):
    rng = np.random.default_rng(seed)
    n = len(pnl)
    pf_samples = np.empty(n_resamples)
    for i in range(n_resamples):
        resample = rng.choice(pnl, size=n, replace=True)
        pf_samples[i] = compute_profit_factor(resample)
    finite = pf_samples[np.isfinite(pf_samples)]
    return pf_samples, finite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument("--profit-col", default="Profit", help="Column name holding per-trade P&L. "
                         "TradingView exports vary ('Profit', 'Profit USD', 'Net P&L'); pass the real header.")
    parser.add_argument("--n-resamples", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260718)
    args = parser.parse_args()

    df = pd.read_csv(args.csv_path)
    if args.profit_col not in df.columns:
        candidates = [c for c in df.columns if "profit" in c.lower() or "p&l" in c.lower() or "pnl" in c.lower()]
        sys.stderr.write(
            f"Column '{args.profit_col}' not found. Candidates in this file: {candidates}\n"
        )
        sys.exit(1)

    pnl = df[args.profit_col].astype(float).to_numpy()
    n_trades = len(pnl)
    if n_trades == 0:
        sys.stderr.write("No trades in CSV — nothing to bootstrap.\n")
        sys.exit(1)

    point_pf = compute_profit_factor(pnl)
    pf_samples, finite_samples = bootstrap_pf(pnl, args.n_resamples, args.seed)
    ci_low, ci_high = (np.percentile(finite_samples, [2.5, 97.5]) if len(finite_samples) > 0 else (np.nan, np.nan))

    mean_trade_pnl = float(np.mean(pnl))
    mean_over_cost = mean_trade_pnl / BASELINE_ROUNDTRIP_COST_USD

    result = {
        "n_trades": int(n_trades),
        "pf_point_estimate": None if not np.isfinite(point_pf) else float(point_pf),
        "pf_95ci_low": None if np.isnan(ci_low) else float(ci_low),
        "pf_95ci_high": None if np.isnan(ci_high) else float(ci_high),
        "n_resamples": args.n_resamples,
        "seed": args.seed,
        "mean_trade_pnl_usd": mean_trade_pnl,
        "mean_trade_pnl_over_baseline_cost": mean_over_cost,
        "baseline_roundtrip_cost_usd": BASELINE_ROUNDTRIP_COST_USD,
        "finite_resample_fraction": len(finite_samples) / args.n_resamples,
    }

    print(json.dumps(result, indent=2))
    print(
        f"\n[BOOT] PF point={result['pf_point_estimate']} "
        f"95%CI=[{result['pf_95ci_low']}, {result['pf_95ci_high']}] "
        f"N={n_trades} mean_pnl=${mean_trade_pnl:.2f} "
        f"mean/cost={mean_over_cost:.2f}x"
    )
    print(
        "PAST PERFORMANCE IS NOT A GUARANTEE OF FUTURE RESULTS. "
        "This output alone does not satisfy J-5 (requires IS+OOS+robustness+STRESS)."
    )


if __name__ == "__main__":
    main()
