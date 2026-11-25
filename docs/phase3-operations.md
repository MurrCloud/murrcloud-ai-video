# Phase 3 local planning and controlled live prerequisites

This increment changes application code and prepares migration `0003`. It does
not install anything on the VPS. No production services, routes, DNS, firewall,
networks, persistent volumes or resource ceilings have changed.

## Local planning without keys

Follow the README on a development machine with Docker. Explicitly upgrade the
isolated database, seed the model catalog, create a user and start API/frontend.
Open **Plan a video**, create a project with a duration and budget, and save a text
prompt/script or import a DOCX. Upload optional PNG/JPEG/WebP references with
captions. Add scenes, choose each scene's model/settings, edit prompts/narration,
reorder, remove and save. Validate duration and capability constraints, then quote
the saved plan. Invalid drafts may be saved for later editing but cannot be
approved for generation.

A quote does not submit a paid job. The supplied Compose files force the director,
video generation and render gates to false. Approval endpoints reject paid work
before reserving costs while their gate is disabled. Manual planning and private
reference previews continue to work.

Scripts are limited to 100,000 characters. DOCX sources are discarded after
extraction. History responses include the active input text and bounded historical
metadata. Images are static, 5 MiB maximum, at least 300 pixels on each side and
at most 8,388,608 pixels; canonical output is at most 20 MiB. Image decode runs in
a subprocess with time/process/address-space limits, with one decoder at a time.
The API admits two concurrent large bodies (1 MiB planning JSON or 5 MiB multipart
plus framing), rejecting additional large requests with 429. Reference storage
shares the existing studio quota/free-space checks and never unrelated directories.

If an upload transaction fails, cleanup checks a fresh database session under the
project lock before removing only that upload's unclaimed canonical file. A lost
commit acknowledgement can represent a committed asset, so uncertain cleanup
retains the file and logs asset/project UUIDs. After a `Reference cleanup deferred`
warning, restore database availability and verify absence under the same project
lock before using `remove_uncommitted_reference`; never delete while the commit
outcome is unknown. No recursive or neighboring-file cleanup is performed.

References are planning attachments: owned IDs, captions and dimensions reach the
director. Pixels, storage paths and external URLs do not. fal image-to-video stays
disabled. No cloud reference delivery or vision capability is claimed.

## Live OpenAI director prerequisites

Live testing still needs explicit approval for a defined request and spending
allowance. Providing a key alone does not grant that approval or enable calls.
Store a dedicated key privately as `VIDEO_OPENAI_API_KEY`, never `NEXT_PUBLIC_*`
or a committed file. Configure `VIDEO_DIRECTOR_MODEL` to an account-accessible
model supporting strict Responses structured outputs, with official model/pricing
verification at the time of the live test. There is no assumed model alias or
invented OpenAI price. Record `VIDEO_DIRECTOR_APPROVAL_REFERENCE` and use a
reviewed local-only Compose override to set `VIDEO_DIRECTOR_ENABLED=true` on API
and generation worker, after approval. The tracked defaults remain false.

The generation worker also needs a fresh, approved host-admission signal and the
existing independent studio database/broker/storage. A project owner/editor must
approve the unknown-price acknowledgement and per-call reservation, within the
project's total approved cap. Available token usage and safe response ID are
recorded; USD stays unavailable. Reservations are conservative application
accounting, not a provider-enforced billing cap. Provider/account spending controls
and live price verification must be reviewed before authorizing a live request.

A durable director intent precedes the one HTTP POST. Refusal/invalid output fails
closed. Timeout, interruption or uncertain provider outcome becomes
`reconciliation_required`; it blocks further paid work and editing for that
project. Recovery never reissues the paid POST. An operator must compare the job,
response ID/token usage if available and provider usage records before resolving
an uncertain outcome or authorizing a new attempt. There is no automated billing
settlement or director reconciliation UI in this increment. Unknown charges stay
reserved rather than being silently released. Use a new test project for a later
separately approved attempt until an operator settlement workflow is added.

The director produces a draft. Independently validate/review it, obtain a new
whole-plan quote and approve video spending before any fal jobs may run. Paid fal
acceptance has its own key/gate/approval requirements from Phase 2.

## Rollout and outstanding gates

Production rollout remains approval-gated. Before rollout, back up and rehearse
migration `0003` on an isolated copy with actual retained data, verify host
capacity/admission signals and storage quotas, and review the existing proxy/TLS
candidate without changing unrelated routes. Docker limits are ceilings and do not
reserve protected-workload capacity. Current CI validates isolated services, not protected-workload capacity.

Before calling the full product production-ready, complete live OpenAI/fal
acceptance and the independent production/recovery/capacity gates. Secure
image-to-video delivery is a separate missing capability. Later phases implement
operator reconciliation, narration, FFmpeg and verified outputs; their presence
does not establish live acceptance. A storyboard or clip is not a completed
assembled video. No live acceptance is inferred from mock fixtures.
