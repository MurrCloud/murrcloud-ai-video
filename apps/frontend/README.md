# MurrCloud Video Studio frontend

Next.js / TypeScript / Tailwind internal studio. Brand primary: `#FF7D00`.
The workspace includes prompt/DOCX planning, a versioned storyboard editor,
selectable verified models, explicit budget approvals, clip generations and
retained scene versions, production settings, final previews and downloads.
Provider credentials remain backend-only.

## Local use

Requires Node 22+ (tested with Node 24). From this directory:

1. `npm ci`
2. Copy `.env.example` to `.env.local` if the backend origin differs.
3. Start the isolated backend and seed its verified catalog using repository docs.
4. `npm run dev` serves only `http://127.0.0.1:3100`.

The backend must explicitly allow that browser Origin. Development sessions need
the backend's `VIDEO_COOKIE_SECURE=false`; production requires HTTPS secure
cookies. The server-only `API_BASE_URL` rewrite proxies `/api/v1/*` to FastAPI;
provider credentials never enter browser code or `NEXT_PUBLIC_*` variables.
When building a container, set the intended API origin at build time because
Next.js compiles rewrite destinations into the build.

## Verify

- `npm test`: Vitest / React Testing Library tests; all provider work is mocked.
- `npm run typecheck` generates Next.js route types before TypeScript validation.
- `NEXT_TELEMETRY_DISABLED=1 npm run build`
- `npm start` serves the production build on loopback port 3100.

Paid generation remains disabled when the server gate is false, missing or
unreadable. Users must explicitly approve a total project cap and acknowledge
unverified/unavailable pricing. Unknown prices display as unavailable, never zero.
Public/unknown pricing reserves the full remaining approved cap; actual charges
remain pending until reconciled. Cancellation does not promise avoided charges.
Model-specific settings and available generation modes follow the backend catalog.
Reference uploads are private planning attachments; enabling image delivery to a
provider requires its separate security and acceptance gates.

## Phase 5 workspace

- `/` shows server-aggregated project counts and recent active projects. Cumulative
  spending includes archived work and separates reservations from charge totals.
- `/library` offers bounded search, status/archive filters, MP4 previews/downloads,
  metadata/budget updates, reversible archive, draft duplication and template saves.
- `/templates` lists private/shared plans and creates independent draft projects.
  Editors can unshare their templates; shared changes require administrator review
  and explicit content acknowledgement. No approvals, jobs or media are copied.
- `/usage` groups cumulative costs by project, scene, model or provider. A confirmed
  subtotal is distinct from total actual charges, which stay unavailable while any
  charge is unknown or pending. Exposure retains reservations and adds verified
  charge overruns/standalone charges. Budget remaining and approved authorization
  remaining are separate; negative deficits are preserved. Archived costs remain
  included. Project details read the same scoped report and display unavailable
  balances on report failure rather than subtracting reservations locally.
- `/settings` shows safe feature/configuration presence and supports a password
  change. A successful change revokes all sessions and returns to sign-in.
- `/operations` is administrator-only. Admission, bounded storage, persisted queue
  states and sanitized audit metadata are snapshots requiring manual refresh;
  queue counts do not prove worker reachability or approved shared-host capacity. Explicit
  provider reconciliation attaches an existing verified UUID for polling or records
  provider-confirmed failure. Billing confirmation requires an explicitly entered
  final amount, provider evidence and review acknowledgement; it never releases
  reservations or submits paid work. All panels refresh after a successful action.

Mutation retries retain the same idempotency key for the same duplicate/template
payload after an ambiguous network failure. Metadata updates bind to a timestamp
or revision; conflicts preserve typed edits and require reload. Opening an action
focuses its first field; synchronous action guards prevent repeated submissions
while a response is pending. Archived projects
stay readable with downloads and block new production writes. Viewers have no
mutation controls. Backend authorization and gates remain authoritative.

Nothing is deployed by frontend scripts. Mocked UI tests do not establish paid
provider, live speech, S3, output quality, shared-host isolation or production acceptance.

Official framework references reviewed:

- https://nextjs.org/docs/app/getting-started/installation
- https://nextjs.org/docs/app/api-reference/config/next-config-js/rewrites
- https://nextjs.org/docs/app/api-reference/cli/next
