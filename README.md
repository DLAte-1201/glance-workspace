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

## T1 shared content build (partial)

Node.js 22 or newer:

```sh
node --test scripts/build-patches.test.mjs
node scripts/build-patches.mjs
# When integrated into the site repository, write its single canonical file:
node scripts/build-patches.mjs /path/to/site/content/patches.json
```

`GLANCE_REPOSITORY` optionally selects another owner/repository; the default is
`DLAte-1201/glance-workspace`. `GITHUB_TOKEN` is optional for authenticated API
reads and must remain in the build environment. API errors fail the command
without overwriting the previous JSON. The script follows all release/tag pages,
excludes draft releases, preserves prereleases and release Markdown verbatim,
and uses commit time plus an empty body for a tag without a release. Output is
sorted newest first with stable IDs and no build timestamp.

`content/notices.json`, `content/patches.json`, and `content/roadmap.json` are
arrays. Notices and roadmap are empty because no corresponding source content
is present in this repository. The checked-in empty patches array is an initial
placeholder, not evidence of a successful live build. Patch entries have `id`,
`tag`, `title`, `body` (Markdown), `date` (ISO timestamp), `url`, `prerelease`, and
`source` (`release` or `tag`); tag entries also have `commit`. Consumers must
render text/Markdown safely using their existing rendering conventions.

The main site and launcher must both consume the site's **same**
`/content/*.json` files rather than embedding separate arrays. The site repository
`DLAte-1201/dlate-1201.github.io` is available in the task environment, but its
README identifies it as generated public static output with development source
in a separate working copy. The current tree contains portfolio pages and MOA,
without the main/Glance launcher's content source. No generated output, UI, copy,
site CSP, or content-loading code has been changed. Once the separate development
working copy is available, move these build and content files there, migrate the
existing notices/roadmap verbatim, and wire both existing consumers to that
canonical path. Do not deploy this draft.
