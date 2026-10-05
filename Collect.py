"""
Collect.py

Command-line workflow for accumulating LLM accuracy data.

    py Collect.py collect [N]        query the LLMs on up to N open NBA games and store the answers
    py Collect.py settle             fetch results for finished games from Kalshi
    py Collect.py weights            print each LLM's accuracy and its 0-1 weight
    py Collect.py export [out.xlsx]  dump every prediction + the summary above to Excel
    py Collect.py usage              print each AI's logged token usage/estimated spend
    py Collect.py costs [out.xlsx]   dump logged API token usage/spend to its own Excel file
"""

import sys

import CostExport
import CostTracker
import Export
import Tracker
from Kalshi import NBA_GAME_SERIES, get_all_markets
from Probability import analyze_market, one_market_per_event


def collect(limit: int = 5, db_path: str = Tracker.DEFAULT_DB) -> None:
    conn = Tracker.connect(db_path)
    already = {r[0] for r in conn.execute("SELECT DISTINCT event_ticker FROM predictions")}

    markets = [
        m for m in one_market_per_event(get_all_markets(market=NBA_GAME_SERIES))
        if m.get("event_ticker") not in already
    ]
    for market in markets[:limit]:
        try:
            analyze_market(market, verbose=False, db_conn=conn)
            print(f"recorded {market['ticker']}")
        except Exception as e:
            print(f"skipped {market['ticker']}: {e}")


def settle(db_path: str = Tracker.DEFAULT_DB) -> None:
    conn = Tracker.connect(db_path)
    settled = Tracker.settle_from_kalshi(conn)
    print(f"settled {len(settled)} market(s); {len(Tracker.unsettled_tickers(conn))} still open")


def show_weights(db_path: str = Tracker.DEFAULT_DB) -> None:
    conn = Tracker.connect(db_path)
    scores = Tracker.brier_scores(conn)
    weights = Tracker.compute_weights(conn)
    if not weights:
        print("no predictions recorded yet")
        return
    print(f"{'LLM':<10} {'settled':>8} {'brier':>8} {'market':>8} {'weight':>8}")
    for llm, weight in sorted(weights.items()):
        s = scores.get(llm)
        if s:
            print(f"{llm:<10} {s['n']:>8} {s['brier']:>8.4f} {s['market_brier']:>8.4f} {weight:>8.2f}")
        else:
            print(f"{llm:<10} {0:>8} {'-':>8} {'-':>8} {weight:>8.2f}")


def show_costs(db_path: str = Tracker.DEFAULT_DB) -> None:
    rows = CostTracker.usage_summary(db_path)
    if not rows:
        print("no API usage logged yet")
        return
    print(f"{'Provider':<10} {'calls':>6} {'in tok':>10} {'out tok':>10} {'cost $':>10} {'unpriced':>9}")
    for r in rows:
        cost = "unknown" if r["cost_usd"] is None else f"{r['cost_usd']:.4f}"
        print(f"{r['provider']:<10} {r['calls']:>6} {r['input_tokens']:>10} {r['output_tokens']:>10} "
              f"{cost:>10} {r['unpriced_calls']:>9}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if command == "collect":
        collect(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    elif command == "settle":
        settle()
    elif command == "weights":
        show_weights()
    elif command == "export":
        out = sys.argv[2] if len(sys.argv) > 2 else Export.DEFAULT_OUT
        path = Export.export_to_excel(out_path=out)
        print(f"wrote {path}")
    elif command == "usage":
        show_costs()
    elif command == "costs":
        out = sys.argv[2] if len(sys.argv) > 2 else CostExport.DEFAULT_OUT
        path = CostExport.export_to_excel(out_path=out)
        print(f"wrote {path}")
    else:
        sys.exit(__doc__)
