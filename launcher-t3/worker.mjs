const APP_URL = 'https://glanceworld.app/';

function json(body, status = 200, headers = {}) {
  return Response.json(body, { status, headers: { 'Cache-Control': 'no-store', ...headers } });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === '/login') {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405, { Allow: 'GET' });
      // Access on the destination host handles authentication using its existing policy.
      return new Response(null, { status: 302, headers: { Location: APP_URL, 'Cache-Control': 'no-store' } });
    }
    if (pathname !== '/api/waitlist') return json({ error: 'not_found' }, 404);
    if (request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405, { Allow: 'POST' });

    let fields;
    const type = request.headers.get('Content-Type')?.split(';')[0].trim();
    try {
      if (type === 'application/json') fields = await request.json();
      else if (type === 'application/x-www-form-urlencoded' || type === 'multipart/form-data') {
        const form = await request.formData();
        fields = { email: form.get('email'), note: form.get('note') ?? '' };
      } else return json({ error: 'unsupported_media_type' }, 415);
    } catch {
      return json({ error: 'invalid_body' }, 400);
    }
    if (!fields || typeof fields.email !== 'string' ||
        (fields.note != null && typeof fields.note !== 'string')) {
      return json({ error: 'invalid_fields' }, 400);
    }
    const email = fields.email.trim().toLowerCase();
    const note = (fields.note ?? '').trim();
    if (email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || note.length > 2000) {
      return json({ error: 'invalid_fields' }, 400);
    }
    if (!env.DB) return json({ error: 'unavailable' }, 503);
    try {
      const result = await env.DB.prepare(
        'INSERT INTO waitlist (email, created_at, note) VALUES (?, ?, ?) ON CONFLICT(email) DO NOTHING'
      ).bind(email, new Date().toISOString(), note).run();
      if (!result.success) return json({ error: 'unavailable' }, 503);
      return json({ ok: true });
    } catch {
      return json({ error: 'unavailable' }, 503);
    }
  }
};
