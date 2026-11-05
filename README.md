# Glance workspace — public source selection

This repository contains the current Glance app baseline, separate Claude/Gemini candidate implementations, synthetic test code and supplemental demo/contracts. It is **not** a completed three-provider integration. No actual provider requests or new product tests were run while preparing this package.

- `app/`: baseline application; implementation code unchanged. Only private source-path metadata in `static/characters.json` was removed.
- `contributions/claude/chat-integration/`: candidate changed files and patch, not applied to `app/`.
- `contributions/gemini/unified-chat/`: alternative candidate, not sequentially applied over Claude.
- `evidence/gemini/independent-regression/`: test source only, not a claim of a passing rerun.
- `supplemental/`: separate voxel demo source and synthetic meeting contracts; not actual model orchestration.

Original private preservation packages remain separate. Private Library identifiers, machine paths, process observations, personal conversations, approval discussions, raw execution logs, screenshots and auth/login/live-smoke helpers are excluded here. Historical raw evidence is not represented by redacted substitutes. Three character reference images were visually inspected and show illustrated characters, not account/desktop/private conversation screenshots; image bytes are preserved. Redistribution rights have not been independently established; no blanket third-party asset license is asserted.

Two `characters.json` files have three `source_path` values each replaced by a public placeholder. All other selected existing files are byte-preserved. API field names such as `session_id` and synthetic approval scenarios are application/test definitions, not actual account sessions or user approval dialogue.

`PUBLIC-MANIFEST.json` gives hashes of selected content and notes metadata changes. `PUBLIC-EXCLUSIONS.json` lists omitted paths without their private contents. SHA256SUMS covers all files except itself. Original package manifests are intentionally not reused as public evidence.

Latest Claude submission-ID/fingerprint correction and Gemini revised regression suite remain unconfirmed. No candidate patch has been integrated by this packaging step. Three-provider repository access and real execution are unverified. Public source visibility alone does not establish integration. Do not auto-run login, provider requests or deployment from this package.

## T2 resident status source

The three AI residents (ChatGPT, Claude, Gemini) each have an independent Git
worktree. The local server now exposes read-only `GET /api/status` for their
runtime observations. Residence alone does not imply that a resident is online
or currently working. This endpoint does not launch providers or expose session
tokens, conversation data, absolute paths, or connector discovery metadata.

Run from the repository root:

```sh
python3 app/server.py --resident-status app/.state/resident-status.json
curl http://127.0.0.1:8874/api/status
(cd app && python3 -m unittest test_resident_status test_workroom)
```

The optional file is local runtime input, not committed source. Its producer
must atomically replace it with observations of actual resident processes:

```json
{"agents":{"codex":{"observed_at":"<UTC ISO timestamp>","online":true,"working":true,"worktree":"<absolute local Git worktree root>"}}}
```

Keys are `codex`, `claude`, and `gemini`; observations are independent. The runtime
must refresh them within 90 seconds. Missing, malformed, future, and stale
observations return `online: null`, `activity: "unknown"`; an explicit fresh
offline observation returns `online: false`. `activity` is `working` or `idle`
only with a fresh pair of boolean flags. A working resident's worktree basename
is returned only after resolving its configured root with Git; Git dirtiness,
CLI installation, and saved connector state are never treated as activity.
An unavailable worktree yields a null name without discarding the runtime's
observed activity. Every response is uncached and includes observation times.

The observation producer in the live runtime is not connected in this task.
The Cloudflare Worker health check for `glanceworld.app` and launcher badges/chips
remain blocked: the designated site repository `DLAte-1201/dlate-1201.github.io`
is accessible and checked out, but contains static publication artifacts and
neither the launcher nor Worker source. Its README places development sources
and build/validation tools in a separate working copy, which is unavailable here.
The published artifacts are not assumed to be the development source of truth.
No site UI, Cloudflare settings, DNS, Access policy, or deployment
was changed. Runtime ingestion by a future Worker must use the existing authorized
runtime channel; this loopback endpoint adds no external exposure.

The original public manifests describe the packaged baseline. New T2 source
files and modified README/server/tests are outside those baseline hashes.
