T4 | BLOCKED (pipeline implemented and synthetic media verified; actual resident dialogue/build capture requires local integrated Glance) | [draft PR for codex/t4-capture-trailer](https://github.com/DLAte-1201/glance-workspace/pulls?q=is%3Apr+head%3Acodex%2Ft4-capture-trailer) | `cd scripts/capture-trailer && npm ci && npx playwright install chromium && npm test && node capture.mjs glance.json output/take-001`

Implementation: DONE. Synthetic verification: 3 tests passed; 1080p H.264/VP9 media,
poster and four VTT cues generated. Real baseline: BLOCKED at resident-dialogue;
external execution is disabled. Run capture against the user's local build-capable
Glance with the existing resident integrations to finish the actual trailer.
No merge/deploy/DNS/Access changes. PR link uses the task branch filter so this
single-commit status resolves the draft PR after creation without a second commit.
