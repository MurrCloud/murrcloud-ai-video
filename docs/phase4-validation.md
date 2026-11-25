# Phase 4 verification checklist

This is a reusable checklist for the sanitized source. It contains no historical
private CI, deployment or live-provider evidence. Current staging results must be
recorded against the staging commit, with failures and skipped cases explained.

## Coverage to verify

Replaceable speech contract and boundaries, missing timing/overlong narration,
SRT offsets, synthetic FFmpeg exports in each aspect ratio, titles/CJK/subtitles,
transitions/music/watermark, FFprobe/full decode, immutable snapshots, render leases,
parent-death/process cleanup, cancellation, quota limits, private ranges/downloads
and archive retry without repeating paid work.

## Reproduction

Run `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .`.
In `apps/frontend`, run `npm ci`, `npm test`, `npm run typecheck` and `npm run build`.
Use the sequence in `.github/workflows/phase1.yml` on a disposable Docker host for
full integration. The `infrastructure/docker/compose.test.yml` overlay removes
public ports and restricts live API tests to the isolated Compose service.

Generate ephemeral dedicated database/cache/session/master credentials; never copy
production secrets or `.env`. Verify explicit migration upgrade/drift/downgrade/
upgrade, PostgreSQL races, Redis/Celery health transport, actual container identity,
capabilities/read-only storage/network/ports/resource limits, frontend proxy,
API restart and Redis-outage liveness/readiness. Clean only disposable volumes.
PostgreSQL cases use temporary schemas inside `video_studio_test`; Redis cases
use DB1 and random keys without flushing a cache. Synthetic media and health
signals must stay isolated. Unmocked provider transports are blocked by tests.

## Result record template

| Check | Result for staging commit |
| --- | --- |
| Backend tests and monitoring tests | Pending current verification |
| Backend lint and format | Pending current verification |
| Frontend tests | Pending current verification |
| Typecheck and production build | Pending current verification |
| Real PostgreSQL/Redis/Compose acceptance | Pending isolated verification |
| Failure/skip details and environment limits | Record explicitly |

A local SQLite or mock pass is not real database, Docker, paid-provider or voice
acceptance. Live model/director quality, speech rights/reliability, optional S3
compatibility/restore, shared-host workload capacity, retained-data migration and
production recovery/deployment remain independent reviewed gates. Passing tests
does not enable features or authorize infrastructure changes or paid calls.
