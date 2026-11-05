import { mkdir, rename, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const DEFAULT_REPOSITORY = 'DLAte-1201/glance-workspace';

// No runtime GitHub dependency: the site and launcher read the generated file.
export async function buildPatches({ repository = DEFAULT_REPOSITORY, fetchImpl = fetch, token = process.env.GITHUB_TOKEN } = {}) {
  if (!/^[\w.-]+\/[\w.-]+$/.test(repository)) throw new Error('Expected repository in owner/name form');
  const base = `https://api.github.com/repos/${repository}`;
  const headers = { Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'glance-content-build' };
  if (token) headers.Authorization = `Bearer ${token}`;
  async function get(path) {
    const response = await fetchImpl(`${base}${path}`, { headers, signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error(`GitHub ${path.split('?')[0]} returned HTTP ${response.status}`);
    return response.json();
  }
  async function list(path) {
    const result = [];
    for (let page = 1; ; page++) {
      const batch = await get(`${path}?per_page=100&page=${page}`);
      if (!Array.isArray(batch)) throw new Error(`Expected array from ${path}`);
      result.push(...batch);
      if (batch.length < 100) return result;
    }
  }
  const [releases, tags] = await Promise.all([list('/releases'), list('/tags')]);
  const releasedTags = new Set();
  const patches = releases.filter(release => !release.draft).map(release => {
    if (!release.tag_name || !release.published_at || !release.html_url) throw new Error('Incomplete release metadata');
    releasedTags.add(release.tag_name);
    return { id: `release:${release.id}`, tag: release.tag_name, title: release.name || release.tag_name,
      body: release.body || '', date: release.published_at, url: release.html_url,
      prerelease: Boolean(release.prerelease), source: 'release' };
  });
  for (const tag of tags) {
    if (!tag.name || !tag.commit?.sha) throw new Error('Incomplete tag metadata');
    if (releasedTags.has(tag.name)) continue;
    // Unreleased tags use commit time; do not invent a release date or notes.
    const commit = await get(`/commits/${encodeURIComponent(tag.commit.sha)}`);
    const date = commit.commit?.committer?.date;
    if (!date) throw new Error('Missing commit date for tag');
    patches.push({ id: `tag:${tag.name}`, tag: tag.name, title: tag.name, body: '', date,
      url: `https://github.com/${repository}/tree/${encodeURIComponent(tag.name)}`,
      prerelease: false, source: 'tag', commit: tag.commit.sha });
  }
  for (const patch of patches) {
    if (!Number.isFinite(Date.parse(patch.date))) throw new Error('Invalid patch date');
  }
  return patches.sort((a, b) => Date.parse(b.date) - Date.parse(a.date) || a.id.localeCompare(b.id, 'en'));
}

export async function writePatches(output, patches) {
  await mkdir(dirname(output), { recursive: true });
  const temporary = `${output}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(patches, null, 2)}\n`);
  await rename(temporary, output);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const output = resolve(process.argv[2] || new URL('../content/patches.json', import.meta.url).pathname);
    const patches = await buildPatches({ repository: process.env.GLANCE_REPOSITORY || DEFAULT_REPOSITORY });
    await writePatches(output, patches);
    console.log(`Wrote ${patches.length} patches to ${output}`);
  } catch (error) {
    console.error(`Patch build failed: ${error.message}`);
    process.exitCode = 1;
  }
}
