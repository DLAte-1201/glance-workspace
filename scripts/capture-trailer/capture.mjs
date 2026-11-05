import { createRequire } from 'node:module';
import { readFile, writeFile, mkdir, rm } from 'node:fs/promises';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { performance } from 'node:perf_hooks';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
export const MAX_SECONDS = 28;
export function stamp(seconds) {
  const n = Math.max(0, Math.round(seconds * 1000));
  return `${String(Math.floor(n / 3600000)).padStart(2, '0')}:${String(Math.floor(n / 60000) % 60).padStart(2, '0')}:${String(Math.floor(n / 1000) % 60).padStart(2, '0')}.${String(n % 1000).padStart(3, '0')}`;
}
export function vtt(cues, end) {
  return 'WEBVTT\n\n' + cues.map((cue, i) => `${i + 1}\n${stamp(cue.at)} --> ${stamp(cues[i + 1]?.at ?? end)}\n${cue.text}\n`).join('\n');
}
function run(program, args) {
  const r = spawnSync(program, args, { encoding: 'utf8', timeout: 180000, maxBuffer: 1024 * 1024 });
  if (r.error || r.status !== 0) throw Error(`${program} failed (exit ${r.status ?? 'unavailable'})`);
  return r.stdout;
}
export function probe(file) {
  return JSON.parse(run('ffprobe', ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', file]));
}
export async function encode(raw, out, duration, posterAt) {
  const filter = 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30';
  const common = ['-hide_banner', '-loglevel', 'error', '-n', '-i', raw, '-t', duration.toFixed(3), '-vf', filter, '-an'];
  run('ffmpeg', [...common, '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', join(out, 'trailer.mp4')]);
  run('ffmpeg', [...common, '-c:v', 'libvpx-vp9', '-crf', '32', '-b:v', '0', '-deadline', 'realtime', '-cpu-used', '6', '-pix_fmt', 'yuv420p', join(out, 'trailer.webm')]);
  run('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-n', '-ss', posterAt.toFixed(3), '-i', join(out, 'trailer.mp4'), '-frames:v', '1', '-update', '1', join(out, 'poster.jpg')]);
  const media = {};
  for (const name of ['trailer.mp4', 'trailer.webm']) {
    const p = probe(join(out, name)), s = p.streams.find(x => x.codec_type === 'video');
    const seconds = Number(p.format.duration);
    if (!s || s.width !== 1920 || s.height !== 1080 || !(seconds > 0 && seconds <= 30) || seconds < duration - 0.2) throw Error(`Invalid encoded media: ${name}`);
    media[name] = { width: s.width, height: s.height, seconds, codec: s.codec_name };
  }
  return media;
}
export async function capture(config, output, { synthetic = false } = {}) {
  const url = new URL(config.url);
  if (!['http:', 'https:'].includes(url.protocol) || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) throw Error('Configure a local Glance URL');
  for (const key of ['input', 'submit', 'residents', 'world', 'buildCounter', 'prompt']) if (!config[key] || typeof config[key] !== 'string') throw Error(`Missing config: ${key}`);
  if (!Array.isArray(config.camera) || !config.camera.length || config.camera.some(x => typeof x !== 'string')) throw Error('Missing camera selectors');
  await mkdir(output); // Exclusive run directory: preserve earlier captures.
  const rawDir = join(output, '.raw');
  await mkdir(rawDir);
  let browser, context, page, started, phase = 'preflight';
  const cues = [], report = { task: 'T4', mode: synthetic ? 'synthetic-test' : 'local-glance', status: 'BLOCKED', scenes: [] };
  const elapsed = () => (performance.now() - started) / 1000;
  const remaining = (reserve = 0) => Math.max(1, (MAX_SECONDS - elapsed() - reserve) * 1000);
  const mark = (name, text) => { phase = name; const at = elapsed(); report.scenes.push({ name, at }); cues.push({ at, text }); };
  try {
    run('ffmpeg', ['-version']); run('ffprobe', ['-version']);
    const { chromium } = require('playwright');
    browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
    context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1, recordVideo: { dir: rawDir, size: { width: 1920, height: 1080 } }, reducedMotion: 'no-preference' });
    started = performance.now();
    page = await context.newPage();
    phase = 'navigation';
    await page.goto(config.url, { waitUntil: 'domcontentloaded', timeout: remaining(10) });
    await page.locator(config.submit).waitFor({ state: 'visible', timeout: remaining(10) });
    await page.waitForFunction(selector => { const el = document.querySelector(selector); return el && !el.disabled; }, config.submit, { timeout: remaining(10) });
    const initialResidents = await page.locator(config.residents).count();
    const initialBuild = Number(await page.locator(config.buildCounter).textContent({ timeout: remaining(10) }));
    if (!Number.isFinite(initialBuild)) throw Error('Build counter must be numeric');
    mark('instruction', '채팅으로 건축 지시');
    await page.locator(config.input).fill(config.prompt, { timeout: remaining(10) });
    await page.locator(config.submit).click({ timeout: remaining(10) });
    phase = 'resident-dialogue';
    await page.waitForFunction(({ selector, count }) => document.querySelectorAll(selector).length >= count + 2, { selector: config.residents, count: initialResidents }, { timeout: remaining(9) });
    mark('resident-dialogue', '주민 대화');
    await page.waitForTimeout(1500);
    await page.locator(config.world).click({ timeout: remaining(7) });
    phase = 'building';
    await page.waitForFunction(({ selector, before }) => Number(document.querySelector(selector)?.textContent) > before, { selector: config.buildCounter, before: initialBuild }, { timeout: remaining(6) });
    mark('building', '건물 생성');
    await page.waitForTimeout(1500);
    mark('camera', '카메라 회전');
    for (const selector of config.camera) {
      await page.locator(selector).click({ timeout: remaining(1) });
      await page.waitForTimeout(Math.min(1000, remaining(0.5)));
    }
    if (elapsed() > MAX_SECONDS) throw Error('Scene sequence exceeded recording budget');
    const duration = elapsed();
    const rawVideo = page.video();
    await context.close(); context = null;
    const raw = await rawVideo.path();
    report.media = await encode(raw, output, duration, report.scenes.find(x => x.name === 'building').at + 0.5);
    await writeFile(join(output, 'trailer.vtt'), vtt(cues, duration));
    report.status = 'DONE';
  } catch (error) {
    report.blockedAt = phase;
    report.reason = error.name === 'TimeoutError' ? 'Required scene signal did not arrive within the 28-second recording budget' : error.message;
    for (const name of ['trailer.mp4', 'trailer.webm', 'poster.jpg', 'trailer.vtt']) await rm(join(output, name), { force: true });
  } finally {
    await context?.close();
    await browser?.close();
    await rm(rawDir, { recursive: true, force: true });
    await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  }
  return report;
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes('--help')) {
    console.log('node capture.mjs [config.json] [new-output-directory]\nRequires local Glance, Playwright Chromium, ffmpeg and ffprobe. Exit 0 DONE / 2 BLOCKED.');
  } else {
    try {
      const config = JSON.parse(await readFile(resolve(process.argv[2] || join(here, 'glance.json')), 'utf8'));
      const output = resolve(process.argv[3] || join(here, 'output', new Date().toISOString().replaceAll(':', '-')));
      await mkdir(dirname(output), { recursive: true });
      const report = await capture(config, output);
      console.log(JSON.stringify({ status: report.status, blockedAt: report.blockedAt, output }));
      process.exitCode = report.status === 'DONE' ? 0 : 2;
    } catch (error) { console.error(error.message); process.exitCode = 2; }
  }
}
