import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdtemp, readFile, access, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { capture, probe, stamp } from './capture.mjs';

// This fixture verifies the media pipeline. It does not simulate real provider integration.
const fixture = `<!doctype html><meta charset="utf-8"><style>body{background:#18282a;color:white;font:40px sans-serif}#scene{height:600px}#building{background:#dda840;width:300px;height:0;transition:height 1s}button,textarea{font:24px sans-serif}</style><h1>SYNTHETIC PIPELINE TEST</h1><textarea id="message-input"></textarea><button id="send-button">Send</button><div id="messages"><article class="message">Historical resident</article><article class="message">Historical resident</article></div><button id="world-toggle">World</button><span id="verified-count">4</span><div id="scene"><div id="building"></div></div><button data-camera="left">Left</button><button data-camera="center">Center</button><button data-camera="right">Right</button><script>
document.querySelector('#send-button').onclick=()=>{setTimeout(()=>{document.querySelector('#messages').insertAdjacentHTML('beforeend','<article class="message">Resident A</article><article class="message">Resident B</article>')},700)};
document.querySelector('#world-toggle').onclick=()=>setTimeout(()=>{document.querySelector('#building').style.height='350px';document.querySelector('#verified-count').textContent='5'},500);
for(const button of document.querySelectorAll('[data-camera]'))button.onclick=()=>document.querySelector('#scene').style.transform='perspective(1500px) rotateY('+({left:-15,center:0,right:15}[button.dataset.camera])+'deg)';
</script>`;

test('timestamps retain millisecond carry', () => {
  assert.equal(stamp(59.9996), '00:01:00.000');
});

test('records all scenes and exports bounded 1080p formats, poster and timed VTT', { timeout: 180000 }, async () => {
  const server = createServer((req, res) => { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(fixture); });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const root = await mkdtemp(join(tmpdir(), 'glance-trailer-test-'));
  try {
    const config = JSON.parse(await readFile(new URL('./glance.json', import.meta.url)));
    config.url = `http://127.0.0.1:${server.address().port}`;
    const out = join(root, 'capture');
    const report = await capture(config, out, { synthetic: true });
    assert.equal(report.status, 'DONE', JSON.stringify(report));
    assert.equal(report.mode, 'synthetic-test');
    assert.deepEqual(report.scenes.map(s => s.name), ['instruction', 'resident-dialogue', 'building', 'camera']);
    assert.ok(report.scenes[1].at - report.scenes[0].at >= 0.6, 'must wait for new residents, not historical messages');
    for (const media of Object.values(report.media)) { assert.equal(media.width, 1920); assert.equal(media.height, 1080); assert.ok(media.seconds <= 30); }
    const poster = probe(join(out, 'poster.jpg')).streams[0];
    assert.equal(poster.width, 1920); assert.equal(poster.height, 1080);
    const captions = await readFile(join(out, 'trailer.vtt'), 'utf8');
    assert.ok(captions.startsWith('WEBVTT\n'));
    assert.equal((captions.match(/-->/g) || []).length, 4);
    assert.ok(captions.includes('건물 생성'));
    await assert.rejects(capture(config, out), /EEXIST/);
    console.log(JSON.stringify({ evidence: out, report }));
  } finally { server.close(); if (!process.env.KEEP_TRAILER_TEST) await rm(root, { recursive: true, force: true }); }
});

test('unreachable local app returns BLOCKED without trailer assets', { timeout: 30000 }, async () => {
  const server = createServer(); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const port = server.address().port; await new Promise(resolve => server.close(resolve));
  const root = await mkdtemp(join(tmpdir(), 'glance-trailer-blocked-'));
  try {
    const config = JSON.parse(await readFile(new URL('./glance.json', import.meta.url)));
    config.url = `http://127.0.0.1:${port}`;
    const report = await capture(config, join(root, 'capture'));
    assert.equal(report.status, 'BLOCKED'); assert.equal(report.blockedAt, 'navigation');
    for (const asset of ['trailer.mp4', 'trailer.webm', 'poster.jpg', 'trailer.vtt', '.raw']) await assert.rejects(access(join(root, 'capture', asset)));
  } finally { await rm(root, { recursive: true, force: true }); }
});
