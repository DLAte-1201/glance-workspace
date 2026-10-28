"""Fixed local stdio fixture: no networking, credentials, tools or descendants."""
import json
import os
import sys
import time


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    provider, scenario, run_id = sys.argv[1:4]
    conversation = "fixture-" + run_id
    model = "synthetic-" + provider + "-fixture"
    common = {"synthetic": True, "cwd": os.getcwd(), "model": model, "tools": []}
    if provider == "antigravity":
        emit({"event": "init", "conversation_id": conversation, "init": common})
    elif provider == "claude":
        emit({"type": "system", "subtype": "init", "session_id": conversation, **common})
    else:
        emit({"type": "thread.started", "thread_id": conversation, **common})
    raw = sys.stdin.buffer.read()
    if provider == "antigravity":
        message = json.loads(raw)
        if message.get("event") != "user" or not isinstance(message.get("message", {}).get("content"), str):
            raise ValueError("Invalid fixture Antigravity input")
    elif not raw.strip():
        raise ValueError("Missing fixture prompt")
    if scenario in ("timeout", "wait_for_cancel"):
        time.sleep(60)
    if scenario == "output_overflow":
        for _ in range(400):
            sys.stderr.write("synthetic stderr " * 512 + "\n")
            sys.stderr.flush()
            sys.stdout.write("synthetic stdout " * 512 + "\n")
            sys.stdout.flush()
        return
    if scenario == "malformed_output":
        print('{"synthetic":', flush=True)
        return
    if scenario == "nonzero_exit":
        print("Synthetic fixture nonzero exit", file=sys.stderr, flush=True)
        return 7
    if scenario == "conversation_mismatch":
        conversation += "-mismatch"
    usage = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    report = "Synthetic local fixture report; no external provider was called."
    # Concurrent pipe activity exercises independent stdout/stderr drain.
    print("Synthetic diagnostic; excluded from report.", file=sys.stderr, flush=True)
    if provider == "antigravity":
        emit({"event": "step_update", "step_update": {"conversation_id": conversation, "usage": {"total_tokens": 9}}})
        emit({"event": "result", "result": {"conversation_id": conversation, "status": "SUCCESS", "response": report, "usage": usage}})
    elif provider == "claude":
        emit({"type": "result", "subtype": "success", "session_id": conversation, "result": report, "is_error": False, "usage": usage})
    else:
        emit({"type": "item.completed", "item": {"id": "fixture-item", "type": "agent_message", "text": report}})
        emit({"type": "turn.completed", "usage": usage})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
