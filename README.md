<p align="center">
  <a href="https://murrcloud.com">
    <img src="docs/assets/murrcloud-logo-tm.png" alt="MurrCloud logo with TM" width="260">
  </a>
</p>

# MurrCloud AI Video Studio

Plan a video, build a storyboard, and manage production in one workspace.
MurrCloud AI Video Studio brings together script/DOCX planning, model selection,
scene versions, budgets, video production, and reusable project templates.

**Pre-release software.** You can run the local planning workspace without
provider API keys. Paid video generation, the AI Director, narration, rendering,
and S3 are disabled by default and need separate configuration and acceptance.
The interface is English; projects and narration support English and 简体中文.

[Watch the demo](#watch-the-demo) · [Quick start](#quick-start) · [First project](#create-your-first-project) ·
[Development](#development-and-checks) · [Documentation](#documentation) ·
[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

## Watch the demo

A **1 minute 26 second** walkthrough of the local planning workspace: start from
a script, edit storyboard scenes, validate the saved plan, review estimates, and
reuse a project as a template.

[Watch the demo on the MurrCloud website](https://murrcloud.com/projects/ai-video-studio#demo).

Recorded locally with a fictional project. Paid generation is disabled, and
billing prices shown are unverified. The voiceover was created separately from
the Studio narration pipeline.

## What you can do

| Area | Capabilities |
| --- | --- |
| Planning | Start from a prompt, script, or DOCX; edit scenes, timing, model settings, narration text, and private planning references. |
| Workspace | Search the project library, duplicate drafts, archive projects, and reuse private or administrator-reviewed shared templates. |
| Budgets | Validate storyboards, prepare estimates, and track reservations, confirmed charges, and remaining spending authorization. |
| AI and production | Adapters for fal video jobs, an OpenAI Director, narration/SRT, FFmpeg composition, and private previews/downloads. These require their own enablement. |
| Administration | Account settings, encrypted inactive provider-key staging, operations/reconciliation, and encrypted isolated backup/recovery. |

The stack is Next.js, TypeScript, and Tailwind on the frontend; FastAPI,
PostgreSQL, Redis, and Celery on the backend. See the
[architecture](docs/architecture.md) for the service and security boundaries.

## Quick start

Use a dedicated local development environment. The commands below start the
planning workspace and keep provider calls and media workers disabled.

### 1. Install prerequisites and clone

You need **Git**, **Docker Engine with Compose v2** (or Docker Desktop on macOS),
and **Python 3** for the one-time configuration snippet below. The snippet uses
only Python's standard library. Python/Node application runtimes are supplied by
the containers; host `uv` and Node are only needed for contributor checks.
Start Docker before continuing.

```sh
git clone https://github.com/MurrCloud/murrcloud-ai-video.git
cd murrcloud-ai-video
docker compose version
```

### 2. Create your private local configuration

Run this from the repository root. It copies `.env.example`, generates independent
database, Redis, session, and provider-staging encryption secrets, and writes a
private `.env`. It refuses to overwrite an existing file and prints no secrets.

```sh
python3 - <<'PY'
from pathlib import Path
import base64
import os
import secrets

postgres_password = secrets.token_hex(32)
redis_password = secrets.token_hex(32)
values = {
    "POSTGRES_PASSWORD": postgres_password,
    "REDIS_PASSWORD": redis_password,
    "VIDEO_SESSION_SECRET": secrets.token_hex(32),
    "VIDEO_DATABASE_URL": f"postgresql+psycopg://video:{postgres_password}@postgres:5432/video_studio",
    "VIDEO_REDIS_URL": f"redis://:{redis_password}@redis:6379/0",
    "VIDEO_PROVIDER_KEY_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
}
lines = []
for line in Path(".env.example").read_text().splitlines():
    name = line.partition("=")[0]
    lines.append(f"{name}={values[name]}" if name in values else line)
os.umask(0o077)
with open(".env", "x") as config:
    config.write("\n".join(lines) + "\n")
print("Created .env. Keep it private and preserve it for this local installation.")
PY
```

Keep `.env` out of Git. The `postgres` and `redis` hostnames are correct inside
Compose. Preserve these secrets when restarting an existing installation;
regenerating them can break database access, sessions, or staged-key decryption.
Provider API keys and archive credentials remain blank. The generated encryption
key supports inactive key staging; it does not activate a provider.

### 3. Build and initialize

Validate the configuration and build both application images. The first build
requires internet access to download base images and dependencies.

```sh
docker compose --profile ui config --quiet
docker compose --profile ui build api frontend
docker compose up -d --wait postgres redis
docker compose run --rm --no-deps api alembic -c apps/backend/alembic.ini upgrade head
docker compose run --rm --no-deps api python -m app.cli seed-models
```

Migrations create the schema explicitly. `seed-models` loads local model metadata
without contacting a provider or submitting generation; it is safe to repeat.

Create your administrator account, replacing the example email with your own:

```sh
docker compose run --rm --no-deps api python -m app.cli create-user --email you@example.com --role admin
```

Enter and confirm a password of **12–256 characters** when prompted. There is no
default password or public signup. Do not put passwords in command arguments.

### 4. Start and open the Studio

```sh
docker compose --profile ui up -d --wait api frontend
curl --fail http://127.0.0.1:8100/health/ready
```

| Service | Local address |
| --- | --- |
| Studio | <http://127.0.0.1:3100> |
| API readiness | <http://127.0.0.1:8100/health/ready> |
| Development API documentation | <http://127.0.0.1:8100/docs> |

Sign in to the Studio with the account you just created. The API prefix is
`/api/v1`. Both published ports bind to loopback; PostgreSQL and Redis have no
published host ports. This local HTTP setup uses `VIDEO_COOKIE_SECURE=false`;
production requires HTTPS and its own reviewed configuration.

## Create your first project

1. Choose **Plan a video** in the sidebar.
2. Enter a project name and a prompt/full script, or import a DOCX file up to
   **5 MiB**. Set the target duration, language, style, audience, model, and budget.
3. Select **Save & open storyboard**.
4. Use **Add scene** to build the plan. Edit prompts, durations, model settings,
   narration text, transitions, and optional private planning references.
5. Select **Save draft**, then **Validate saved storyboard**. Resolve the reported
   issues before selecting **Estimate all scenes**. Estimates may contain
   unavailable prices; an unknown price is never treated as zero.
6. Find the project in **Project library**. You can duplicate it as a fresh draft,
   archive it, or save it as a reusable template.

Saving, importing, editing, and validating a plan do not call a provider. A project
budget is not spending approval. Paid generation requires its separate runtime
configuration and explicit approval against a saved estimate.

Planning reference images stay private; their pixels are not sent to the Director
or text-to-video provider. Image-to-video remains disabled pending secure provider
delivery. Draft/template copies carry no jobs, media, approvals, or committed spending.

### Find your way around

| Workspace | Use it for |
| --- | --- |
| Plan a video | Create a project and edit its storyboard. |
| Project library | Search, preview available outputs, duplicate, archive, and manage projects. |
| Templates | Save plans and create independent drafts from private/shared templates. |
| Usage and costs | Inspect reservations, confirmed subtotals, unknown charges, and remaining authorization. |
| Model catalog | Review seeded models and their supported settings. |
| Settings | Change your password and view configuration status; administrators can stage provider keys. |
| Operations | Administrator-only status checks and evidence-based reconciliation. |

## Optional AI and media configuration

The default Compose file keeps paid generation, the Director, narration, rendering,
and S3 disabled. Changing a feature flag in `.env` alone does not override these
Compose settings. Workers are opt-in. Use the relevant guide to review credentials,
models/pricing, spending authorization, storage, and runtime acceptance before
changing that configuration.

| Capability | Guide |
| --- | --- |
| fal cloud clips and spending controls | [Cloud generation](docs/phase2.md), [model research](docs/model-research.md) |
| OpenAI Director and storyboards | [Planning](docs/phase3.md), [Director adapter](docs/openai-director.md) |
| Narration, subtitles, and FFmpeg output | [Production](docs/phase4.md), [narration](docs/narration.md), [compositor](docs/ffmpeg-composer.md) |
| Provider credentials in Settings | [Encrypted inactive key staging](docs/provider-key-settings.md) |
| Deployment and operations | [Deployment checklist](docs/deployment-checklist.md), [operator runbook](docs/phase5-operations.md) |

Saving a key in **Settings → Provider API keys** requires administrator password
reauthentication. It stores encrypted, inactive credentials, makes no provider test
call, and does not enable spending or workers. Keep the independent encryption
master API-only and preserve it privately.

Synthetic tests verify application behavior, not live provider quality, commercial
voice rights, S3 compatibility, or host capacity. Production deployment and binary
releases have separate [release gates](docs/open-source-release.md).

## Stop, restart, and troubleshoot

Stop the local workspace while preserving its named data volumes:

```sh
docker compose --profile ui down
```

Start it again with `docker compose --profile ui up -d --wait api frontend`.
Do not add `--volumes` unless you deliberately intend to delete local data.

| Symptom | What to check |
| --- | --- |
| Docker is unavailable | Start Docker Desktop/Engine and confirm `docker compose version` works. |
| Configuration reports an unset variable | Create `.env` in the repository root and verify the required credentials and URLs match. |
| API is not ready or login fails | Run `docker compose ps` and `docker compose logs --tail=100 api postgres redis`; confirm migrations completed and both dependencies are healthy. Redis failure closes access. |
| Port 3100 or 8100 is busy | Stop the conflicting local development instance or use a reviewed Compose override; update allowed browser origins if the UI address changes. |
| Browser writes fail | Use the documented Studio URL and an exactly allowed Origin. Keep local cookie/origin settings consistent. |
| Generate/render controls are unavailable | This is expected in the default planning setup. Follow the optional configuration guides for those features. |

Review logs before sharing them. Use [private vulnerability reporting](SECURITY.md)
for sensitive security reports.

## Development and checks

For contributors, install **Python 3.12+**, **uv**, and **Node.js 22+** (CI uses
Node 24). From the repository root:

```sh
uv sync --frozen
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Frontend checks:

```sh
cd apps/frontend
npm ci
npm test
npm run typecheck
NEXT_TELEMETRY_DISABLED=1 npm run build
```

For frontend hot reload after the quick start, keep the backend running and stop
the container frontend so port 3100 is available. From the repository root:

```sh
docker compose --profile ui stop frontend
cd apps/frontend
npm run dev
```

The default proxy targets `http://127.0.0.1:8100`; see the
[frontend guide](apps/frontend/README.md) for `.env.local` and proxy configuration.
Use the same provisioned account and allowed browser Origin.

Some backend checks require disposable PostgreSQL/Redis, workers, and Linux/media
tooling; a host-only test run may skip them. The **Studio isolated acceptance**
workflow covers real container/service checks, migration lifecycle, frontend
checks, and failure behavior. **Source security checks** runs secret and dependency
scans. See [isolated validation](docs/phase1-validation.md) and
[CONTRIBUTING.md](CONTRIBUTING.md) before submitting a PR. Do not run these checks
against a production or shared host.

## Documentation

- [Architecture](docs/architecture.md) and [workspace features](docs/phase5.md).
- [Libraries and templates](docs/library-templates.md).
- [Provider-key Settings](docs/provider-key-settings.md).
- [Encrypted backup and isolated recovery](docs/backup-recovery.md).
- [Deployment checklist](docs/deployment-checklist.md) and
  [private host-assessment template](docs/server-assessment.md).
- [Source and binary release gates](docs/open-source-release.md).

## License and contributing

Application source is licensed under [Apache-2.0](LICENSE). Third-party dependencies
retain their own terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The MurrCloud logo and TM identify the project; the source license does not grant
trademark rights.

Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).
Report vulnerabilities privately using [SECURITY.md](SECURITY.md).
