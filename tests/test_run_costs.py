import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_costs  # noqa: E402

FIELDS = ["id", "design_id", "leader_model", "leader_input_tokens", "leader_output_tokens", "leader_cache_read_tokens",
          "leader_cache_write_tokens", "leader_cost_usd", "others_input_tokens", "others_output_tokens",
          "others_cache_read_tokens", "others_cache_write_tokens", "others_cost_usd"]
PRICES = {"vat": 0.19, "openrouter_fee": 0.05, "budget_usd": 100,
          "models": {"Opus": {"input": 5, "cache_read": 0.5, "cache_write": 6, "output": 25, "estimate_per_run_usd": 0.5},
                     "Astra": {"input": 2, "output": 10}},
          "persuader": {"input": 1, "output": 4}}


class RunCostTests(unittest.TestCase):
    def test_cache_tokens_get_their_own_price_and_reported_cost_wins(self):
        row = {"leader_input_tokens": "100000", "leader_output_tokens": "10000", "leader_cache_read_tokens": "80000",
               "leader_cache_write_tokens": "10000", "leader_cost_usd": "", "leader_model": "Opus",
               "others_input_tokens": "400000", "others_output_tokens": "20000", "others_cost_usd": "0.08"}
        cost, leader_how, others_how = run_costs.run_cost(row, PRICES)
        # 10k plain input x 5 + 80k reads x 0.5 + 10k writes x 6 + 10k output x 25, per million tokens.
        self.assertAlmostEqual(cost["leader"], (10_000 * 5 + 80_000 * 0.5 + 10_000 * 6 + 10_000 * 25) / 1e6)
        self.assertEqual((cost["others"], leader_how, others_how), (0.08, "tokens", "reported"))
        self.assertAlmostEqual(cost["gross"], cost["leader"] * 1.19 + 0.08 * 1.19 * 1.05)

    def test_without_cache_columns_all_input_is_plain_and_marked_as_upper_bound(self):
        row = {"leader_input_tokens": "100000", "leader_output_tokens": "5000", "leader_model": "Astra",
               "others_input_tokens": "1000", "others_output_tokens": "0"}
        cost, leader_how, _ = run_costs.run_cost(row, PRICES)
        self.assertAlmostEqual(cost["leader"], (100_000 * 2 + 5000 * 10) / 1e6)
        self.assertEqual(leader_how, "upper bound")

    def test_report_groups_by_model_and_design_and_names_runs_it_cannot_price(self):
        with tempfile.TemporaryDirectory() as folder:
            export = Path(folder) / "package.csv"
            with open(export, "w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                for i, design in enumerate(["baseline", "pressure", "pressure"]):
                    writer.writerow({"id": f"R{i}", "design_id": design, "leader_model": "Opus",
                                     "leader_input_tokens": 100000, "leader_output_tokens": 30000,
                                     "leader_cache_read_tokens": 0, "leader_cache_write_tokens": 0,
                                     "others_input_tokens": 0, "others_output_tokens": 0, "others_cost_usd": 0.1})
                writer.writerow({"id": "R9", "design_id": "pressure", "leader_model": "Unknown",
                                 "leader_input_tokens": 1, "leader_output_tokens": 1, "others_cost_usd": 0.1})
            prices = Path(folder) / "prices.json"
            prices.write_text(json.dumps(PRICES), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                run_costs.main([str(export), str(export), "--prices", str(prices)])
            text = out.getvalue()
        lines = {line.split()[1]: line for line in text.splitlines() if line.startswith("Opus")}
        # 100k x 5 + 30k x 25 = 1.25 leader, plus 0.1 others: 1.35 per run, 170 % over the 0.5 estimate.
        self.assertIn("1.350", lines["pressure"])
        self.assertIn("over estimate", lines["pressure"])
        self.assertEqual(lines["pressure"].split()[2], "2")  # Duplicate rows from two exports count once.
        self.assertIn("R9: leader no price", text)


if __name__ == "__main__":
    unittest.main()
