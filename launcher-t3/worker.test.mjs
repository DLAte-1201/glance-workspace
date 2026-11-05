import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import worker from './worker.mjs';

function setup() {
  const sqlite = new DatabaseSync(':memory:');
  sqlite.exec(readFileSync(new URL('./migrations/0001_waitlist.sql', import.meta.url), 'utf8'));
  const env = { DB: { prepare(sql) { return { bind(...args) { return {
    async run() { sqlite.prepare(sql).run(...args); return { success: true }; }
  }; } }; } } };
  return { sqlite, env };
}
const post = (body) => new Request('https://glance.reseorch-ai.com/api/waitlist', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
});

test('login uses a fixed destination; untrusted query parameters cannot redirect it', async () => {
  const response = await worker.fetch(new Request('https://glance.reseorch-ai.com/login?next=https://example.com'), {});
  assert.equal(response.status, 302);
  assert.equal(response.headers.get('Location'), 'https://glanceworld.app/');
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
});

test('JSON and native forms insert into the actual SQLite schema; duplicate requests preserve the original', async () => {
  const { sqlite, env } = setup();
  try {
    const response = await worker.fetch(post({ email: ' ALPHA@Example.com ', note: "a ' note" }), env);
    assert.deepEqual(await response.json(), { ok: true });
    const original = sqlite.prepare('SELECT * FROM waitlist').get();
    assert.equal(original.email, 'alpha@example.com');
    assert.equal(original.note, "a ' note");
    assert.ok(Number.isFinite(Date.parse(original.created_at)));
    assert.equal((await worker.fetch(post({ email: original.email, note: 'changed' }), env)).status, 200);
    assert.deepEqual(sqlite.prepare('SELECT * FROM waitlist').get(), original);
    for (const body of [new URLSearchParams({ email: 'form@example.com', note: 'native' }), (() => {
      const form = new FormData(); form.set('email', 'multipart@example.com'); return form;
    })()]) {
      assert.equal((await worker.fetch(new Request('https://glance.reseorch-ai.com/api/waitlist', { method: 'POST', body }), env)).status, 200);
    }
    assert.equal(sqlite.prepare('SELECT count(*) AS n FROM waitlist').get().n, 3);
  } finally { sqlite.close(); }
});

test('bad fields and malformed bodies are rejected without writes', async () => {
  const { sqlite, env } = setup();
  try {
    for (const body of [null, {}, { email: 'bad' }, { email: 'a@example.com', note: {} }, { email: 'a@example.com', note: 'x'.repeat(2001) }]) {
      assert.equal((await worker.fetch(post(body), env)).status, 400);
    }
    const malformed = new Request('https://glance.reseorch-ai.com/api/waitlist', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{'
    });
    assert.equal((await worker.fetch(malformed, env)).status, 400);
    assert.equal(sqlite.prepare('SELECT count(*) AS n FROM waitlist').get().n, 0);
  } finally { sqlite.close(); }
});

test('methods, unknown routes, missing binding and database failures return explicit errors', async () => {
  assert.equal((await worker.fetch(new Request('https://launcher/api/waitlist'), {})).status, 405);
  assert.equal((await worker.fetch(new Request('https://launcher/login', { method: 'POST' }), {})).status, 405);
  assert.equal((await worker.fetch(new Request('https://launcher/missing'), {})).status, 404);
  assert.equal((await worker.fetch(post({ email: 'a@example.com' }), {})).status, 503);
  const failure = { DB: { prepare() { throw new Error('private database diagnostic'); } } };
  const response = await worker.fetch(post({ email: 'a@example.com' }), failure);
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: 'unavailable' });
  assert.equal((await worker.fetch(new Request('https://launcher/api/waitlist', { method: 'POST', body: 'text' }), {})).status, 415);
});
