"""Read runtime observations without treating resident existence as online status."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

RESIDENTS = (("codex", "ChatGPT"), ("claude", "Claude"), ("gemini", "Gemini"))
MAX_AGE_SECONDS = 90


def _worktree_name(path):
    if not isinstance(path, str) or not path:
        return None
    try:
        root = Path(path).resolve(strict=True)
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        actual = Path(result.stdout.strip()).resolve(strict=True)
        return actual.name if actual == root else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def resident_status(observations_path, *, now=None):
    """The runtime owns this optional file; missing/stale telemetry means unknown.

    Each agents[id] record contains observed_at (UTC ISO timestamp), online and
    working booleans, and a local worktree path. Paths never enter the response.
    The caller must write observations atomically after observing the runtime.
    """
    now = now or datetime.now(timezone.utc)
    try:
        with Path(observations_path).open(encoding="utf-8") as stream:
            raw = stream.read(65537)
        data = json.loads(raw) if len(raw) <= 65536 else {}
        observations = data.get("agents", {}) if isinstance(data, dict) else {}
        if not isinstance(observations, dict):
            observations = {}
    except (OSError, ValueError, TypeError, RecursionError):
        observations = {}
    agents = []
    for agent_id, label in RESIDENTS:
        agent = {"id": agent_id, "label": label, "online": None,
                 "activity": "unknown", "worktree": None,
                 "observed_at": None, "reason": "observation_unavailable"}
        record = observations.get(agent_id)
        if isinstance(record, dict):
            try:
                observed = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
                if observed.utcoffset() is None:
                    raise ValueError("Timestamp needs timezone")
                age = (now - observed).total_seconds()
                if age < 0:
                    raise ValueError("Future observation")
                agent["observed_at"] = observed.astimezone(timezone.utc).isoformat()
                if age > MAX_AGE_SECONDS:
                    agent["reason"] = "observation_stale"
                elif type(record.get("online")) is bool and type(record.get("working")) is bool:
                    if not record["online"] and record["working"]:
                        raise ValueError("Offline observation cannot report working")
                    agent.update(online=record["online"],
                                 activity="working" if record["working"] else "idle",
                                 reason=None)
                    if record["working"]:
                        agent["worktree"] = _worktree_name(record.get("worktree"))
                        if agent["worktree"] is None:
                            agent["reason"] = "worktree_unavailable"
                else:
                    raise ValueError("Runtime flags must be booleans")
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                agent["reason"] = "observation_invalid"
        agents.append(agent)
    return {"schema_version": 1, "checked_at": now.isoformat(),
            "max_age_seconds": MAX_AGE_SECONDS, "agents": agents}
