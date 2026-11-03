"""Cost per run from a Swarm Lab CSV export, split into the leader (decision model) and the others (persuaders).

Usage:
    python scripts/run_costs.py analysis/package.csv [more.csv ...] [--prices analysis/prices.json] [--runs]

Prices come from a JSON file (copy scripts/prices.example.json to analysis/prices.json and fill it in).
Prices are USD per million tokens. A model is matched by its profile name in the CSV column leader_model.

How a side (leader or others) is priced:
- If the provider reported a cost (leader_cost_usd / others_cost_usd, e.g. OpenRouter), that cost is used.
- Otherwise tokens times prices. input_tokens already contains cache reads and cache writes (Anthropic),
  so plain input = input - cache_read - cache_write, and each kind gets its own price.
- Without cache columns (OpenAI, Gemini in this lab) all input is priced as plain input. That is an upper
  bound, marked "upper bound" in the output.
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_PRICES = Path("analysis/prices.json")
OVER_ESTIMATE = 0.30  # The plan allows at most about 30 % above the estimate.


def number(value):
    if value in (None, ""):
        return None
    return float(value)


def read_rows(paths):
    """Rows of all exports, once per run id (a run can appear in several exports)."""
    rows = {}
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                rows[row["id"]] = row
    return list(rows.values())


def side_cost(row, side, price):
    """Cost of one side of a run in USD (net), and how it was found: reported, tokens, upper bound or missing."""
    reported = number(row.get(f"{side}_cost_usd"))
    if reported is not None:
        return reported, "reported"
    tokens_in = number(row.get(f"{side}_input_tokens"))
    tokens_out = number(row.get(f"{side}_output_tokens"))
    if tokens_in is None and tokens_out is None:
        return None, "no tokens"
    if price is None or price.get("input") is None or price.get("output") is None:
        return None, "no price"
    read = number(row.get(f"{side}_cache_read_tokens"))
    write = number(row.get(f"{side}_cache_write_tokens"))
    plain = (tokens_in or 0) - (read or 0) - (write or 0)
    read_price = price["input"] if price.get("cache_read") is None else price["cache_read"]
    write_price = price["input"] if price.get("cache_write") is None else price["cache_write"]
    cost = (plain * price["input"] + (read or 0) * read_price + (write or 0) * write_price
            + (tokens_out or 0) * price["output"]) / 1e6
    return cost, "tokens" if read is not None or write is not None else "upper bound"


def run_cost(row, prices):
    model = row.get("leader_model", "")
    leader, leader_how = side_cost(row, "leader", prices["models"].get(model))
    others, others_how = side_cost(row, "others", prices.get("persuader"))
    if leader is None or others is None:
        return None, leader_how, others_how
    gross = leader * (1 + prices.get("vat", 0)) + others * (1 + prices.get("vat", 0)) * (1 + prices.get("openrouter_fee", 0))
    return {"leader": leader, "others": others, "net": leader + others, "gross": gross}, leader_how, others_how


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", nargs="+", help="CSV export(s) from the lab")
    parser.add_argument("--prices", default=str(DEFAULT_PRICES), help=f"price file (default {DEFAULT_PRICES})")
    parser.add_argument("--runs", action="store_true", help="also print one line per run")
    args = parser.parse_args(argv)
    prices_path = Path(args.prices)
    if not prices_path.exists():
        sys.exit(f"Price file {prices_path} not found. Copy scripts/prices.example.json there and fill in the prices.")
    prices = json.loads(prices_path.read_text(encoding="utf-8"))
    rows = read_rows(args.csv)

    groups = defaultdict(list)
    problems = []
    for row in rows:
        cost, leader_how, others_how = run_cost(row, prices)
        if cost is None:
            problems.append(f"{row['id']}: leader {leader_how}, others {others_how} (leader_model '{row.get('leader_model', '')}')")
            continue
        key = (row.get("leader_model", ""), row.get("design_id", ""))
        groups[key].append((row, cost, leader_how))
        if args.runs:
            print(f"{row['id']}  {key[0]:<20} {key[1]:<28} leader {cost['leader']:7.3f}  others {cost['others']:7.3f}"
                  f"  net {cost['net']:7.3f}  gross {cost['gross']:7.3f}  ({leader_how})")
    if args.runs:
        print()

    print(f"{'leader_model':<20} {'design':<28} {'runs':>4} {'net/run':>8} {'estimate':>8} {'diff':>6} {'net':>8} {'gross':>8}")
    total_net = total_gross = 0.0
    upper = False
    for (model, design), items in sorted(groups.items()):
        net = sum(c["net"] for _, c, _ in items)
        gross = sum(c["gross"] for _, c, _ in items)
        total_net, total_gross = total_net + net, total_gross + gross
        per_run = net / len(items)
        estimate = (prices["models"].get(model) or {}).get("estimate_per_run_usd")
        diff = f"{per_run / estimate - 1:+.0%}" if estimate else ""
        flag = "  over estimate" if estimate and per_run > estimate * (1 + OVER_ESTIMATE) else ""
        bound = any(how == "upper bound" for _, _, how in items)
        upper = upper or bound
        print(f"{model:<20} {design:<28} {len(items):>4} {per_run:>8.3f} {estimate or '':>8} {diff:>6} {net:>8.2f} {gross:>8.2f}"
              f"{flag}{'  (upper bound)' if bound else ''}")

    budget = prices.get("budget_usd")
    print(f"\nTotal: {total_net:.2f} USD net, {total_gross:.2f} USD with VAT and fees"
          + (f", {total_gross / budget:.0%} of the {budget} USD budget" if budget else ""))
    if upper:
        print("Upper bound: for these models the lab stores no cache tokens, so all input was priced as uncached input. "
              "Check the provider's billing dashboard for the real cost.")
    if problems:
        print(f"\nNot priced ({len(problems)} runs):")
        for line in problems:
            print("  " + line)


if __name__ == "__main__":
    main()
