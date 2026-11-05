# T3 launcher backend (partial; integration blocked)

This isolated Worker provides `GET /login` and `POST /api/waitlist`. It does not alter the baseline app, agent worktrees, Access policies, DNS or production routes.

The site repository is `DLAte-1201/dlate-1201.github.io`, checked out at `/workspace/dlate-1201.github.io`. Its README explicitly identifies it as static public output with development source/build tools in a separate working copy. The local checkout and fetched `origin/main` contain portfolio/MOA pages, but no Glance launcher page, alpha form, Worker configuration or development source. No public output files were modified. The missing prerequisite is the separate launcher development working copy, not access to the public site repository. The source `specs/current.md`, `specs/RULES.md`, `specs/FACTS.md` and `.agents/skills` are also absent. The two user-provided Markdown attachments were read; their T3 scope is the basis of this implementation. ChatGPT, Claude and Gemini are residents with independent Git worktrees as stated by the supplied specification.

## Local commands

Requires Node 24+ and npm. From `launcher-t3/`:

```sh
npm ci
npm test
npm run db:local
npm run check
npm run dev
```

`db:local` applies the migration to local D1 only. `check` bundles without deploying. The all-zero D1 ID is a local placeholder, not an existing production resource.

## Site integration contract

The existing hero login CTA should link to `/login` on `glance.reseorch-ai.com`. This Worker redirects to the fixed `https://glanceworld.app/` URL, where the pre-existing Access application must enforce authentication. No user-provided redirect destination is accepted. Access login/session/callback behavior remains owned by Cloudflare. See [Cloudflare self-hosted applications](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/).

The existing alpha form can POST `email` and optional `note` to `/api/waitlist` using JSON, URL-encoded or multipart form data. A successful response is `200 {"ok":true}`; the site should display its existing success state only after checking `response.ok`. Invalid input returns 400, unsupported content type 415 and unavailable D1 503. An AJAX form must preserve user input on failure. The server creates `created_at` as UTC ISO text. Repeated normalized emails succeed without overwriting the original entry or timestamp. There is no public list/read endpoint. No new UI or copy is supplied because those belong to the site owner.

## Remaining prerequisites and unverified behavior

- Provide the separate launcher development working copy (its location is not given in the public site README) so the existing CTA and form can be wired and browser tested. This directory alone does not change the live hero or render an alpha form.
- Supply the existing Cloudflare token, account/D1 identifiers and Access application details through the task environment. No Cloudflare-related environment variables were present. Bind the existing D1 database as `DB` when integrating. See [Cloudflare D1 bindings and local development](https://developers.cloudflare.com/d1/get-started/).
- The owner must confirm that the existing Access application protects `glanceworld.app`; no successful Access round trip, remote database migration or live submission is claimed here.
- Deployment/route configuration and remote migrations remain with the owner under the supplied scope. No new Cloudflare resources or paid services were requested or created.

The tests execute the SQL migration and prepared insert in an in-memory SQLite database. Wrangler local migration and bundling exercise the local Cloudflare toolchain; they do not establish production access.
