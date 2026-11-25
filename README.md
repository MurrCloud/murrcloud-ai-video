# MurrCloud AI Video Studio

Private source staging copy for review. This repository contains the Phase 5
application and reusable infrastructure examples. It contains no production
inventory, deployment evidence, authorization or original repository history.
Keep this repository private during review. No license has been selected.

The platform includes a model catalog and fal jobs, OpenAI director/storyboards,
DOCX and private planning references, narration adapters, speech-boundary SRT,
FFmpeg composition, private production previews/downloads, project libraries,
templates, usage reports, administrator reconciliation and encrypted isolated
backup/recovery. All paid calls, narration, rendering, S3 and workers are disabled
by default. Live acceptance requires separate credentials, configuration and
scoped authorization; synthetic tests do not establish provider quality or host capacity.

Architecture and acceptance boundaries: [docs/architecture.md](docs/architecture.md).
Host assessment template: [docs/server-assessment.md](docs/server-assessment.md).
Deployment checklist: [docs/deployment-checklist.md](docs/deployment-checklist.md).

## Local development

Requires Python 3.12+, uv, and Docker Engine with Compose v2 on an isolated
development machine. Preserve unrelated workloads and use dedicated credentials.

1. Copy `.env.example` to `.env` and fill unique Studio-only secrets. Generate each
   random value with `python -c 'import secrets; print(secrets.token_hex(32))'`.
   URL-encode credentials in connection URLs. Keep `.env` private (`chmod 600`).
2. `uv sync --frozen`
3. `docker compose config --quiet` then `docker compose up -d postgres redis`
4. `docker compose run --rm api alembic -c apps/backend/alembic.ini upgrade head`
5. `docker compose run --rm api python -m app.cli create-user --email admin@example.test --role admin`
   The CLI prompts for the password; do not put it in shell arguments.
6. `docker compose up -d api` then check `http://127.0.0.1:8100/health/ready`.
7. `docker compose run --rm api python -m app.cli seed-models`
8. `docker compose --profile ui up -d --build frontend` opens the Studio at
   `http://127.0.0.1:3100`. Global paid/render gates remain false.

Choose **Plan a video** to save a prompt/script, import DOCX, attach planning
references and edit/validate a storyboard without API keys. Whole-plan quotes
require separate spending approval before generation. The director additionally
requires a backend-only key, explicit model and operator approval reference.
Reference images guide planning through captions and metadata; image-to-video
stays disabled pending secure provider delivery.

The API is `/api/v1`; development OpenAPI UI is `/docs`. Login returns a revocable
HttpOnly session cookie. Browser mutations require an exact allowed Origin.
Use HTTPS in production; local HTTP requires `VIDEO_COOKIE_SECURE=false`.
There is no public signup. Password changes revoke all account sessions.

## Verification

Run `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .`.
Frontend commands in `apps/frontend`: `npm ci`, `npm test`, `npm run typecheck`
and `npm run build`. Current staging results must be recorded for its own commit;
prior private audit and deployment records are not included as acceptance evidence.

[Phase 1 verification](docs/phase1-validation.md) explains isolated service tests.
The acceptance workflow exercises disposable PostgreSQL/Redis, explicit migration
upgrade/drift/downgrade/upgrade, service/proxy/restart/outage and container isolation.
SQLite and mocked providers do not establish those real-runtime gates.

## Features and operations

- [Cloud clips](docs/phase2.md) and [model research](docs/model-research.md).
- [Director/storyboards](docs/phase3.md) and [OpenAI adapter](docs/openai-director.md).
- [Production](docs/phase4.md), [compositor](docs/ffmpeg-composer.md) and [narration](docs/narration.md).
- [Library/templates](docs/library-templates.md), [Phase 5](docs/phase5.md),
  [operator runbook](docs/phase5-operations.md) and [encrypted recovery](docs/backup-recovery.md).
- [Provider-key Settings](docs/provider-key-settings.md) and [verification checklist](docs/provider-key-settings-validation.md).

Libraries and templates create fresh drafts without approvals, jobs, costs or
private media. Shared templates require administrator review. Archives preserve
history and media. Unknown charges remain unknown; reservations are retained until
reviewed reconciliation, and confirmed overruns reduce remaining authorization.
Recovery attaches existing provider requests without repeating a paid submission.

Administrators may save, replace or remove staged OpenAI/fal keys in Settings after
password reauthentication. Keys remain encrypted and inactive; no save/test request
is sent to a provider and no spending gate or worker is enabled. The encryption
master remains API-only and independent of database/media backups.

## Layout

- `apps/backend`: FastAPI, authentication, schema and explicit Alembic migrations.
- `apps/frontend`: Next.js / TypeScript / Tailwind workspace and production UI.
- `workers`: generation, narration and rendering queue launchers.
- `packages/shared-schemas`: future generated contracts.
- `infrastructure`: containers, generic proxy/systemd examples and admission monitor.
- `scripts`, `tests`, `docs`: operations tools, synthetic verification and design.

Follow [AGENTS.md](AGENTS.md). Public release, reuse licensing, security policy,
binary dependency notices and production acceptance remain review decisions.
