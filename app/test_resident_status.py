"""Status observations and actual, independent Git worktree resolution."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from resident_status import resident_status


class ResidentStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "observations.json"
        self.now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, agents):
        self.path.write_text(json.dumps({"agents": agents}), encoding="utf-8")
        return resident_status(self.path, now=self.now)["agents"]

    def record(self, **overrides):
        return {"observed_at": self.now.isoformat(), "online": True,
                "working": False, **overrides}

    def test_missing_file_preserves_three_residents_without_inventing_presence(self):
        for path in (self.path, None):
            agents = resident_status(path, now=self.now)["agents"]
            self.assertEqual(["codex", "claude", "gemini"], [a["id"] for a in agents])
            self.assertTrue(all(a["online"] is None and a["activity"] == "unknown" for a in agents))

    def test_actual_independent_worktrees_are_resolved_and_paths_not_exposed(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "fixture", "--allow-empty"], check=True)
        observations = {}
        for agent_id in ("codex", "claude", "gemini"):
            worktree = self.root / (agent_id + "-task")
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(worktree)], check=True)
            observations[agent_id] = self.record(working=True, worktree=str(worktree))
        agents = self.read(observations)
        self.assertEqual(["codex-task", "claude-task", "gemini-task"], [a["worktree"] for a in agents])
        self.assertTrue(all(a["online"] is True and a["activity"] == "working" for a in agents))
        self.assertNotIn(str(self.root), json.dumps(agents))

    def test_stale_online_and_offline_observations_become_unknown(self):
        stamp = (self.now - timedelta(seconds=91)).isoformat()
        agents = self.read({"codex": self.record(observed_at=stamp),
                            "claude": self.record(observed_at=stamp, online=False)})
        for agent in agents[:2]:
            self.assertIsNone(agent["online"])
            self.assertEqual("observation_stale", agent["reason"])

    def test_idle_and_explicit_offline_do_not_infer_activity_from_worktree(self):
        agents = self.read({"codex": self.record(worktree=str(self.root)),
                            "claude": self.record(online=False)})
        self.assertIs(agents[0]["online"], True)
        self.assertIs(agents[1]["online"], False)
        self.assertEqual(["idle", "idle"], [a["activity"] for a in agents[:2]])
        self.assertTrue(all(a["worktree"] is None for a in agents))

    def test_invalid_and_future_observations_are_unknown(self):
        for overrides in ({"online": "true"}, {"online": False, "working": True},
                          {"observed_at": "bad"}, {"observed_at": "2026-11-05T12:00:00"},
                          {"observed_at": (self.now + timedelta(seconds=1)).isoformat()}):
            agent = self.read({"codex": self.record(**overrides)})[0]
            self.assertIsNone(agent["online"])
            self.assertEqual("unknown", agent["activity"])
            self.assertEqual("observation_invalid", agent["reason"])

    def test_unavailable_worktree_keeps_observed_activity_and_null_name(self):
        agent = self.read({"codex": self.record(working=True, worktree=str(self.root))})[0]
        self.assertEqual("working", agent["activity"])
        self.assertIsNone(agent["worktree"])
        self.assertEqual("worktree_unavailable", agent["reason"])

    def test_malformed_or_oversized_file_returns_unknown(self):
        for raw in ("not json", "[]", '{"agents": []}', "x" * 65537):
            self.path.write_text(raw, encoding="utf-8")
            self.assertTrue(all(a["online"] is None for a in resident_status(self.path)["agents"]))


if __name__ == "__main__":
    unittest.main()
