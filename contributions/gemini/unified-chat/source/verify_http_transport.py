"""Local HTTP smoke check using temporary state; never submits to a provider."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
from urllib.request import Request, urlopen

from server import LocalServer
from workroom import Workroom


def main():
    with tempfile.TemporaryDirectory(prefix="glance-http-") as folder:
        room = Workroom(folder)
        server = LocalServer(("127.0.0.1", 0), room)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def state():
            with urlopen(base + "/api/state", timeout=5) as response:
                return json.load(response)

        def send(command, payload):
            current = state()
            data = json.dumps({"command": command, "expected_version": current["version"], "payload": payload}).encode()
            request = Request(base + "/api/commands", data=data, headers={
                "Content-Type": "application/json", "Origin": base,
                "X-Workroom-Token": current["session_token"],
            })
            with urlopen(request, timeout=5) as response:
                return json.load(response)

        def wait_run(run_id):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                run = next(row for row in state()["runs"] if row["id"] == run_id)
                if run["status"] not in {"queued", "running", "cancel_requested"}:
                    return run
                time.sleep(.03)
            raise AssertionError("Local fixture did not terminate")

        try:
            before = state()
            outcomes = {}
            for scenario in ("success", "nonzero_exit", "wait_for_cancel"):
                run_id = send("start_fixture_run", {"scenario": scenario, "phase_id": None})["run_id"]
                if scenario == "wait_for_cancel":
                    send("cancel_run", {"run_id": run_id})
                run = wait_run(run_id)
                outcomes[scenario] = run["status"]
            assert outcomes == {"success": "succeeded", "nonzero_exit": "failed", "wait_for_cancel": "cancelled"}, outcomes
            imported = send("import_history", {"source_id": "argon-coupon-past"})
            history = imported["state"]["history_imports"][-1]["receipt"]
            assert history["provider_calls_added"] == 0
            assert history["eligible_current_phase"] is False
            assert history["usage"]["total_tokens"] == 1233
            plan = send("prepare_run", {"provider": "antigravity", "prompt": "Fictional local verification draft only.", "phase_id": None})
            assert plan["state"]["runs"][-1]["status"] == "awaiting_approval"
            after = state()
            assert after["project"]["status"] == before["project"]["status"]
            assert not any(event["type"] == "verified_completion" for event in after["events"])
            report = {"result": "passed", "isolated_temporary_state": True, "http_fixture_outcomes": outcomes,
                      "historical_source": "argon-coupon-past", "historical_reported_tokens": 1233,
                      "provider_calls_added": 0, "construction_events_added": 0,
                      "live_plan_status": "awaiting_approval", "project_status": after["project"]["status"]}
            Path("TRANSPORT-HTTP-QA.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report))
        finally:
            server.shutdown()
            server.server_close()
            room.close()
            worker.join(timeout=3)


if __name__ == "__main__":
    main()
