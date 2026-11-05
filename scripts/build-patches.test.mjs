import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { buildPatches, writePatches } from './build-patches.mjs';

function mock(routes) {
  return async url => {
    const path = new URL(url).pathname.replace('/repos/DLAte-1201/glance-workspace', '') + new URL(url).search;
    assert.ok(path in routes, `Unexpected request ${path}`);
    return { ok: true, json: async () => routes[path] };
  };
}
const releasesPath = '/releases?per_page=100&page=1';
const tagsPath = '/tags?per_page=100&page=1';
test('empty repository produces no invented content', async () => {
  assert.deepEqual(await buildPatches({ fetchImpl: mock({ [releasesPath]: [], [tagsPath]: [] }) }), []);
});
test('release notes, prereleases, tag fallback, deduplication and date order', async () => {
  const patches = await buildPatches({ fetchImpl: mock({
    [releasesPath]: [
      { id: 1, tag_name: 'v1', name: 'Release one', body: '# Notes\n- Original copy', published_at: '2026-01-01T00:00:00Z', html_url: 'https://github.com/DLAte-1201/glance-workspace/releases/tag/v1', prerelease: true },
      { id: 2, draft: true },
    ],
    [tagsPath]: [{ name: 'v1', commit: { sha: 'a' } }, { name: 'v2/test', commit: { sha: 'b' } }],
    '/commits/b': { commit: { committer: { date: '2026-02-01T00:00:00Z' } } },
  }) });
  assert.equal(patches.length, 2);
  assert.equal(patches[0].source, 'tag');
  assert.equal(patches[0].body, '');
  assert.ok(patches[0].url.endsWith('v2%2Ftest'));
  assert.equal(patches[1].body, '# Notes\n- Original copy');
  assert.equal(patches[1].prerelease, true);
});
test('paginates beyond first 100 releases', async () => {
  const page = Array.from({ length: 100 }, () => ({ draft: true }));
  assert.deepEqual(await buildPatches({ fetchImpl: mock({ [releasesPath]: page, '/releases?per_page=100&page=2': [], [tagsPath]: [] }) }), []);
});
test('API failure fails the build without exposing credentials', async () => {
  await assert.rejects(buildPatches({ token: 'private-token', fetchImpl: async () => ({ ok: false, status: 403 }) }), /HTTP 403$/);
});
test('writes canonical JSON for consumers', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'glance-patches-'));
  try {
    const output = join(directory, 'content', 'patches.json');
    await writePatches(output, []);
    assert.equal(await readFile(output, 'utf8'), '[]\n');
  } finally { await rm(directory, { recursive: true }); }
});
