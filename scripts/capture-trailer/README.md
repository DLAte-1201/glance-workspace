# T4 — local trailer capture

Playwright records the local Glance UI at 1920×1080, sends a building instruction,
waits for two **new** resident messages, opens the world, waits for the confirmed
output count to increase, and clicks left / center / right camera controls.
ffmpeg exports H.264 MP4 (faststart), VP9 WebM, a JPEG poster, and Korean WebVTT
scene captions. The recording budget is 28 seconds; encoded media must be ≤30
seconds. Encoding itself can take longer than 30 seconds.

## Run

Requirements: Node.js ≥20, Python 3 for the bundled local app, ffmpeg + ffprobe
on PATH, and Playwright Chromium with its video recorder dependency.

```sh
cd scripts/capture-trailer
npm ci
npx playwright install chromium
npm test
```

Start a local Glance instance in a separate terminal. For the public baseline:

```sh
python3 app/server.py --port 8874 --data-dir /tmp/glance-trailer-state
```

Then run from `scripts/capture-trailer/`:

```sh
node capture.mjs glance.json output/take-001
```

Use a new output directory for every take. Existing directories are preserved.
Choose a local demonstration workspace without private conversations. The script
sends `prompt` through the actual chat UI; use a configured local instance if
building instructions can call providers. The script does not start providers,
change permissions, inject messages, seed completed buildings, or modify the app.

`glance.json` contains the URL, prompt, and CSS selectors. Adapt these to the
local integrated app when its DOM differs. `residents` must select resident
message elements, and `buildCounter` must expose a numeric confirmed-output
count. The public baseline uses `#verified-count`; its outputs are evidence
blocks. A build-capable local app must map this selector to its real building
completion count. The existence of the three residents and their independent
Git worktrees is an input fact; this task does not change or re-audit it.

Success exits 0 and creates:

- `trailer.mp4`, `trailer.webm`: 1080p, 30 fps, silent video.
- `poster.jpg`: 1080p frame from the building scene.
- `trailer.vtt`: four scene cues timed from the recording start.
- `report.json`: DONE, scene timestamps, codecs, dimensions, and measured durations.

A missing app, dependency, resident reply, build signal, or camera control exits
2 and creates `report.json` with BLOCKED and the failed phase. Partial video and
raw recordings are removed. VTT describes the captured scenes; it is not a
transcription of resident dialogue. Playwright initializes the recorder on page
creation; caption timing is approximate within browser initialization/frame
latency, not frame-accurate synchronization.

## Existing browser installation

Set `CHROMIUM_PATH` to an installed Chromium executable if the standard
Playwright browser cannot be downloaded. Playwright still needs its ffmpeg video
recorder dependency. In this cloud environment the download returned HTTP 403
`Domain forbidden`, so verification used installed `/usr/bin/chromium` and
`/usr/bin/ffmpeg` with a temporary recorder cache:

```sh
mkdir -p /tmp/t4-playwright/ffmpeg-1011
ln -s /usr/bin/ffmpeg /tmp/t4-playwright/ffmpeg-1011/ffmpeg-linux
PLAYWRIGHT_BROWSERS_PATH=/tmp/t4-playwright CHROMIUM_PATH=/usr/bin/chromium npm test
PLAYWRIGHT_BROWSERS_PATH=/tmp/t4-playwright CHROMIUM_PATH=/usr/bin/chromium node capture.mjs glance.json output/take-002
```

Revision 1011 is specific to locked Playwright 1.62.1. A normal Playwright install
supplies this file without the temporary cache setup.

## Verification and remaining local work

The end-to-end test serves an explicitly marked synthetic HTML fixture. It checks
new-message waiting despite historical messages, four scenes, both video formats,
1080p dimensions, ≤30-second durations, the poster, VTT, existing-output
preservation, and unreachable-app cleanup. It does not establish live provider
integration or actual building generation.

See `validation.json` for this task's measured results. The public baseline was
launched with a fresh temporary data directory and the actual UI capture was
attempted. It reached the instruction and blocked waiting for resident dialogue.
The server reports external execution disabled (`app/server.py`).

To finish the real trailer, run this command against the local build-capable
Glance instance with its existing resident integrations, then review the footage
and captions. If replies/building take longer than the recording budget, prepare
a faster demonstration task in that instance. No merge, deployment, DNS, Access,
Cloudflare resources, or provider charges were performed by this task.
