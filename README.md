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
