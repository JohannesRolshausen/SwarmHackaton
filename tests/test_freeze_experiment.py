import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import freeze_experiment as fx  # noqa: E402

from swarm_bench.lab_engine import LabRun  # noqa: E402
from swarm_bench.lab_server import LabManager, make_lab_server  # noqa: E402


def profile(profile_id, model, effort):
    return {"id": profile_id, "name": model, "kind": "openai_compatible", "base_url": "https://example.test/v1",
            "model": model, "key_env": "FREEZE_TEST_KEY", "token_parameter": "max_tokens", "reasoning_effort": effort,
            "service_tier": "", "openrouter_provider": ""}


class FreezeBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "designs").mkdir()
        for name in fx.CONDITIONS.values():
            shutil.copy(ROOT / "designs" / f"{name}.json", self.root / "designs")
        self.lab = self.root / "lab"
        self.lab.mkdir()
        self.profiles = [profile(t["profile"], name, t["effort"]) for name, t in fx.TARGETS.items()]
        self.profiles.append(profile(fx.COLLEAGUE_PROFILE, fx.COLLEAGUE_MODEL, "high"))
        self.write_profiles()

    def tearDown(self):
        self.temp.cleanup()

    def write_profiles(self):
        (self.lab / "models.json").write_text(json.dumps(self.profiles), encoding="utf-8")

    def freeze(self, label="t", uncommitted=()):
        return fx.freeze(label, self.root / "freeze" / label, root=self.root, lab_dir=self.lab, commit="abc",
                         uncommitted=list(uncommitted))

    def edit_design(self, condition, change):
        path = self.root / "designs" / f"{fx.CONDITIONS[condition]}.json"
        design = json.loads(path.read_text(encoding="utf-8"))
        change(design)
        path.write_text(json.dumps(design), encoding="utf-8")


class FreezeTests(FreezeBase):
    def test_clean_freeze_has_one_cell_per_model_and_design(self):
        target, manifest, checks = self.freeze()
        self.assertEqual(manifest["status"], "frozen", manifest["failed_checks"])
        self.assertEqual(len(manifest["cells"]), 12)
        self.assertTrue(all(ok for ok, _ in checks))
        self.assertTrue((target / "REVIEW.md").read_text(encoding="utf-8").startswith("# Freeze t"))
        cell = fx.read(target / "cells" / "opus-pressure.json")
        self.assertEqual(cell["body"]["models"]["agent_01"], fx.TARGETS["opus"]["profile"])
        self.assertEqual(set(list(cell["body"]["models"].values())[1:]), {fx.COLLEAGUE_PROFILE})

    def test_uncommitted_change_makes_a_draft(self):
        _, manifest, _ = self.freeze(uncommitted=["designs/group-spending-pressure.json"])
        self.assertEqual(manifest["status"], "draft")

    def test_a_freeze_is_not_overwritten(self):
        self.freeze()
        with self.assertRaises(SystemExit):
            self.freeze()

    def test_shared_text_that_differs_between_designs_fails(self):
        self.edit_design("pressure", lambda d: d.update(shared_prompt=d["shared_prompt"] + " Extra."))
        _, manifest, _ = self.freeze()
        self.assertEqual(manifest["status"], "draft")
        self.assertTrue(any("shared_prompt" in text for text in manifest["failed_checks"]))
        self.assertTrue(any("Lena bekommt" in text for text in manifest["failed_checks"]))

    def test_private_instruction_for_lena_fails(self):
        def change(design):
            design["roles"][0]["private_instruction"] = "Stay under budget."
        self.edit_design("incentive", change)
        _, manifest, _ = self.freeze()
        self.assertTrue(any("Lena hat in keinem" in text for text in manifest["failed_checks"]))

    def test_colleague_without_the_target_sum_fails(self):
        def change(design):
            design["roles"][2]["private_instruction"] = "Push for more money."
        self.edit_design("incentive", change)
        _, manifest, _ = self.freeze()
        self.assertTrue(any(text.startswith("incentive: alle 4") for text in manifest["failed_checks"]))

    def test_wrong_effort_fails(self):
        self.profiles[1]["reasoning_effort"] = "low"
        self.write_profiles()
        _, manifest, _ = self.freeze()
        self.assertTrue(any(text.startswith("opus: Effort") for text in manifest["failed_checks"]))

    def test_secret_in_a_profile_writes_nothing(self):
        self.profiles[0]["api_key"] = "sk-test"
        self.write_profiles()
        with self.assertRaises(SystemExit):
            self.freeze()
        self.assertFalse((self.root / "freeze" / "t").exists())

    def test_changed_cell_file_is_detected(self):
        target, _, _ = self.freeze()
        path = target / "cells" / "astra-baseline.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"call_limit": 30', '"call_limit": 31'), encoding="utf-8")
        with self.assertRaises(SystemExit):
            fx.load_freeze(str(target))


class CheckTests(FreezeBase):
    def setUp(self):
        super().setUp()
        self.target, _, _ = self.freeze()
        self.runs = self.root / "runs"

    def stored_run(self, run_id, cell_name, change=None):
        cell = fx.read(self.target / "cells" / f"{cell_name}.json")
        body = json.loads(json.dumps(cell["body"]))
        body["title"] = "any title"
        if change:
            change(body)
        run = LabRun(body, ROOT / "data", self.runs / run_id)
        run.state["provider_profiles"] = json.loads(json.dumps(cell["profiles"]))
        run.persist()
        return run

    def check(self):
        lines = []
        bad = fx.check(str(self.target), runs_dir=self.runs, out=lines.append)
        return bad, "\n".join(lines)

    def test_run_from_the_frozen_cell_matches_under_another_title(self):
        self.stored_run("r1", "argon-incentive")
        bad, text = self.check()
        self.assertEqual(bad, [])
        self.assertRegex(text, r"argon-incentive\s+1")

    def test_wrong_limit_is_reported(self):
        self.stored_run("r1", "opus-baseline", lambda b: b.update(call_limit=29))
        bad, text = self.check()
        self.assertEqual(bad[0][1], "opus-baseline")
        self.assertIn("config/call_limit", bad[0][2])
        self.assertRegex(text, r"opus-baseline\s+0")

    def test_one_changed_word_in_a_design_is_reported(self):
        def change(body):
            body["design"]["roles"][1]["private_instruction"] = "x" + body["design"]["roles"][1]["private_instruction"]
        self.stored_run("r1", "opus-pressure", change)
        bad, _ = self.check()
        self.assertTrue(any("private_instruction" in p for p in bad[0][2]))

    def test_changed_profile_is_reported(self):
        run = self.stored_run("r1", "astra-pressure")
        run.state["provider_profiles"]["agent_01"]["reasoning_effort"] = "low"
        run.persist()
        bad, _ = self.check()
        self.assertIn("profile/agent_01/reasoning_effort", bad[0][2])

    def test_changed_prompt_file_is_reported(self):
        self.stored_run("r1", "flash-baseline")
        path = self.runs / "r1" / "agents" / "agent_03" / "prompt.txt"
        path.write_text(path.read_text(encoding="utf-8") + " extra", encoding="utf-8")
        bad, _ = self.check()
        self.assertIn("prompt.txt agent_03", bad[0][2])

    def test_runs_from_before_the_freeze_count_only_with_all(self):
        run = self.stored_run("r1", "flash-baseline", lambda b: b.update(call_limit=29))
        run.state["created_at"] = "2020-01-01T00:00:00+00:00"
        run.persist()
        bad, _ = self.check()
        self.assertEqual(bad, [])
        lines = []
        self.assertEqual(len(fx.check(str(self.target), runs_dir=self.runs, include_all=True, out=lines.append)), 1)


class StartTests(FreezeBase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {"FREEZE_TEST_KEY": "not-a-real-key"})
        self.env.start()
        self.target, _, _ = self.freeze()
        self.manager = LabManager(ROOT / "data", self.lab, designs_dir=self.root / "designs")
        # Nothing here may reach a model: runs are created, never played.
        self.played = []
        self.launched = []
        patch.object(self.manager, "control", lambda run_id, action: self.played.append((run_id, action)) or {}).start()
        patch.object(self.manager, "_launch_series", self.launched.append).start()
        self.addCleanup(patch.stopall)
        self.server = make_lab_server(self.manager, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        super().tearDown()

    def start(self, cell="opus-pressure", runs=3, **options):
        with redirect_stdout(io.StringIO()):
            return fx.start(str(self.target), cell, runs, self.url, **options)

    def test_dry_run_creates_nothing(self):
        self.assertIsNone(self.start(dry_run=True))
        self.assertEqual(self.manager.runs, {})
        self.assertEqual(self.manager.series, {})

    def test_package_starts_with_the_frozen_configuration(self):
        series_id = self.start(runs=3)
        record = self.manager.series[series_id]
        cell = fx.read(self.target / "cells" / "opus-pressure.json")
        self.assertEqual(self.launched, [series_id])
        self.assertEqual(record["repetitions"], 3)
        self.assertEqual(list(fx.differences(fx.without_title(cell["config"]), fx.without_title(record["config"]))), [])

    def test_single_run_is_played_directly(self):
        run_id = self.start(runs=1)
        self.assertEqual(self.played, [(run_id, "play")])
        self.assertEqual(self.launched, [])

    def test_changed_profile_refuses_the_start(self):
        with self.manager.registry.lock:
            next(p for p in self.manager.registry.profiles if p["id"] == fx.TARGETS["opus"]["profile"])["reasoning_effort"] = "low"
        with self.assertRaises(SystemExit) as raised:
            self.start()
        self.assertIn("reasoning_effort", str(raised.exception))
        self.assertEqual(self.manager.runs, {})

    def test_missing_key_refuses_the_start(self):
        with patch.dict(os.environ, {"FREEZE_TEST_KEY": ""}):
            with self.assertRaises(SystemExit) as raised:
                self.start()
        self.assertIn("no API key", str(raised.exception))
        self.assertEqual(self.manager.runs, {})

    def test_server_building_other_prompts_refuses_the_start(self):
        real = fx.call

        def stale_server(server, path, body=None):
            answer = real(server, path, body)
            if path == "/api/preview":
                answer["agents"]["agent_02"]["prompt"] += " older code"
            return answer
        with patch.object(fx, "call", stale_server):
            with self.assertRaises(SystemExit) as raised:
                self.start()
        self.assertIn("agent_02", str(raised.exception))
        self.assertEqual(self.manager.runs, {})

    def test_unknown_cell_and_draft_are_refused(self):
        with self.assertRaises(SystemExit):
            self.start(cell="opus-unknown")
        draft, _, _ = self.freeze("d", uncommitted=["designs/x.json"])
        with self.assertRaises(SystemExit):
            fx.start(str(draft), "opus-pressure", 3, self.url)
        self.assertEqual(self.manager.runs, {})
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(fx.start(str(draft), "opus-pressure", 3, self.url, dry_run=True, allow_draft=True))


if __name__ == "__main__":
    unittest.main()
