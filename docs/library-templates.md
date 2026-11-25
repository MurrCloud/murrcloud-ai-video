# Project library and reusable templates

The Phase 5 workspace adds bounded library queries, optimistic metadata updates,
archive controls and atomic project/template reuse. It creates no provider jobs,
speech calls, media downloads or renders. Apply the application migration before
using these routes. Production rollout remains a separately approved operation.

## Library and project controls

`GET /api/v1/library/projects` accepts `q`, `status`, `archived` (default false),
`limit` (default 24, maximum 100) and `offset`. Search matches literal text in
project names and prompts; SQL wildcard characters have no special meaning.
Editors/viewers see their projects and administrators can see all projects.
Responses include `items`, `total`, `limit` and `offset`. Each item includes a
500-character prompt preview (`prompt_truncated` indicates shortening), scene
count, budget/commitment/approved-cap fields and the latest available final video.
Preview/download links remain authenticated API routes; object keys and storage
credentials never enter this response. `latest_output.is_current` compares the
storyboard revision and target duration; the production detail view performs the
full immutable source-binding comparison before production decisions.

Project mutations require an editor role and project ownership, or an
administrator. `PATCH /projects/{id}/metadata` takes `expected_updated_at`,
`name` and `budget_usd`. A stale timestamp returns 409. The budget must be
nonnegative, at most USD 10,000, and at least the existing spending exposure and
approved cap. Exposure retains commitments and includes verified charge overruns
and standalone charges. Editing a budget grants no spending approval. Project names are limited to
160 visible characters.

`POST /projects/{id}/archive` takes `expected_updated_at` and a strict Boolean
`archived`. Archive hides projects from the normal library and blocks new
production writes. Unresolved paid/director work or active rendering must be
resolved first. Archive preserves existing files, scenes, versions, costs and
audit records; it does not cancel a charge, delete media or release commitments.
Unarchive uses the same optimistic timestamp check. An archived project may
still be duplicated or saved as a template.

## Safe duplication and application

`POST /projects/{id}/duplicate` takes `name` and `idempotency_key`. It creates a
new draft project with its own input, storyboard, scene and initial scene-version
rows. The original budget is copied within the USD 10,000 creation bound;
committed spend becomes zero and the approved spending cap becomes null.

Portable content includes the prompt/source script, active scene text, scene
model/settings/duration, language, narration configuration, transitions, titles
and bounded composition options. References, music and logo asset IDs are
removed and upscale acknowledgement is reset. Source document filenames, raw
DOCX files, asset URLs/keys, private media, prior versions/lineage, provider jobs,
generation attempts, approvals, estimates, charges and credentials are not
copied. Previously archived scenes are excluded. A fresh storyboard must be
validated, estimated and explicitly approved before paid work.

Every referenced model must exist in the catalog. Disabled models and incomplete
or unsupported draft combinations remain draft content; copying never enables a
model or silently changes a scene to another model. Normal validation/generation
gates still apply, including the need to attach new owned reference images.

Duplicate, template creation and template application use a durable mutation
record scoped to actor, operation and key. Repeating the same payload returns
the original result. Reusing its key with a different payload returns 409. A user
row lock precedes source project/template locks, serializing concurrent retries.
New project, script, settings, scenes, versions, audit and mutation rows commit
together; later failures roll back the entire copy. No background task is
dispatched by these endpoints.

## Template visibility and maintenance

`POST /projects/{id}/templates` takes `name` (200 characters), `description`
(2,000 characters), `shared` (default false), `acknowledge_shared_content`
(default false) and `idempotency_key`. Editors may create private templates.
Shared creation requires an administrator and explicit acknowledgement.

**Prompts, source scripts, visual descriptions and narration can contain sensitive
content.** Structural allowlists remove media references and provider metadata;
they do not redact arbitrary secrets a user has written into creative text.
Review the entire template before acknowledging shared content. Shared templates
are readable/applicable by authenticated users; private templates are visible to
their owner and administrators. Archived shared templates are hidden from other
users.

`GET /templates` accepts `q`, `archived`, `limit` and `offset` with the same bounds
as the library. It returns metadata and a small validated summary: ownership,
revision, archive status, scene count, target duration, language and model IDs.
The full creative plan is only returned by the authorized `GET /templates/{id}`.
Invalid legacy plans fail closed on detail/application and return `plan_invalid`
with an empty summary in lists, so owners/operators can still archive them.

`PATCH /templates/{id}` changes only name, description, sharing and archive
metadata, with a required `expected_revision`. A successful update increments
the revision; stale changes return 409. Only owners or administrators may edit.
Changes to a template that remains shared require administrator acknowledgement;
an owner may make it private without acknowledgement. Template content is
immutable: apply it to a project, edit the storyboard, and save a new template.

`POST /templates/{id}/apply` takes `name`, `budget_usd` (0–10,000) and
`idempotency_key`. It creates an independent draft using the same copy rules.
Archived templates cannot create new applications. An authorized idempotent
retry can retrieve its existing result even after sharing is withdrawn.

Portable plans forbid unknown fields and media IDs, limit scenes to 120, scene
text to 100,000 aggregate characters and source text to 100,000 characters, and
require unique scene keys and contiguous order. Template contents are validated
again at application; stored JSON is not trusted merely because it is in the DB.

## Automated verification

`tests/test_library.py` covers ownership, viewer restrictions, literal search,
pagination, preview privacy, optimistic updates, budget protection, archive
history, pending work, exact-payload idempotency, private media removal, disabled
models, incomplete drafts, invalid plans and rollback after a late copy failure.
`tests/test_library_postgres.py` checks concurrent duplicate/create/apply retries
against actual PostgreSQL row locks. Those integration cases require
`VIDEO_TEST_DATABASE_URL` pointing at a dedicated `video_studio_test` database;
they create/drop only a random disposable schema and skip without that setting.
SQLite unit results do not establish PostgreSQL concurrency acceptance. These
tests do not submit paid jobs, contact live speech/S3 or touch protected-workload infrastructure.
