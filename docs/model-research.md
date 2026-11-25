# Verified fal model profiles

Verified against official fal model pages, API pages, and their linked OpenAPI
documents on **2026-11-06**. This was read-only documentation research. No
authenticated pricing lookup, paid generation, or actual provider cancellation
was performed. Catalog `verified_at` records the documentation verification date,
not proof of successful inference or account-specific billing.

## Initial catalog

| Exact endpoint ID | Input mode | Duration representation | Resolution | Aspect selection | Public USD rate |
| --- | --- | --- | --- | --- | --- |
| `fal-ai/wan/v2.7/text-to-video` | Text | Integer 2–15 | 720p or 1080p | 16:9, 9:16, 1:1, 4:3, 3:4 | 0.10/s at 720p; 0.15/s at 1080p |
| `fal-ai/wan/v2.7/image-to-video` | First frame | Integer 2–15 | 720p or 1080p | Inherits reference geometry; no ratio parameter | Same |
| `fal-ai/kling-video/v3/pro/text-to-video` | Text | String `"3"`–`"15"` | Pro tier up to 1080p; no resolution parameter | 16:9, 9:16, 1:1 | 0.112/s without audio; 0.168/s with audio |
| `fal-ai/kling-video/v3/pro/image-to-video` | First frame | String `"3"`–`"15"` | Same | Inherits start-image geometry; no ratio parameter | Same |

The two text models are enabled in the catalog. This does not enable paid
generation: the application feature gate, owned project, spending approval and
provider credentials are separate requirements. Both image models are cataloged
but disabled with `Reference uploads arrive Phase3`. Their selectable aspect list
is empty because the provider does not accept an aspect parameter. Before image
models become reachable, owned uploads must pass type/signature/size checks and
reference geometry must match the production plan. There is no public arbitrary
reference-URL submission endpoint in Phase 2.

Official request schemas:

- [Wan text API](https://fal.ai/models/fal-ai/wan/v2.7/text-to-video/api),
  [Wan text OpenAPI](https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=fal-ai/wan/v2.7/text-to-video).
- [Wan image API](https://fal.ai/models/fal-ai/wan/v2.7/image-to-video/api),
  [Wan image OpenAPI](https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=fal-ai/wan/v2.7/image-to-video).
- [Kling text API](https://fal.ai/models/fal-ai/kling-video/v3/pro/text-to-video/api),
  [Kling text OpenAPI](https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=fal-ai/kling-video/v3/pro/text-to-video).
- [Kling image API](https://fal.ai/models/fal-ai/kling-video/v3/pro/image-to-video/api),
  [Kling image OpenAPI](https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=fal-ai/kling-video/v3/pro/image-to-video).

The profile adapter validates the selected model, mode, duration, resolution,
ratio, audio behavior and prompt lengths before submitting. It never sends a
Kling resolution field or an image-model aspect field. Model-specific payload
keys and duration types stay inside the adapter.

## Audio and reference behavior

Wan 2.7 describes generated audio and has no audio-disable field. Its initial
adapter therefore requires `audio_enabled=true`; the UI labels native audio as
always on. It does not silently pretend to generate a silent Wan clip. Driving
audio and start/end-frame/video-continuation features are documented by the
provider but are not exposed by this Phase 2 application.

Kling 3 supports `generate_audio`, including Chinese and English speech, and its
adapter sends the requested Boolean. A single prompt is used. Multi-prompt,
custom element, and voice-binding controls remain outside the initial request
contract. Voice control has a separate published 0.196 USD/s rate and must not be
estimated with the ordinary audio rate if added later. The I2V adapter translates
the internal reference to `start_image_url`; Wan uses `image_url`.

The provider response requires `video.url`. Wan also requires `seed` and may
return `actual_prompt`. Only allowlisted numeric video metadata and a known
video MIME type are retained; echoed prompts, filenames and arbitrary provider
objects are not copied into job metadata.

## Prices and provenance

Rates above come from the detailed [Wan text](https://fal.ai/models/fal-ai/wan/v2.7/text-to-video),
[Wan image](https://fal.ai/models/fal-ai/wan/v2.7/image-to-video),
[Kling text](https://fal.ai/models/fal-ai/kling-video/v3/pro/text-to-video), and
[Kling image](https://fal.ai/models/fal-ai/kling-video/v3/pro/image-to-video)
pages. The general pricing table has displayed a different Kling base rate;
therefore these public estimates are explicitly **unverified for the account**.
For example, 5 seconds of Wan at 720p estimates 0.50 USD and Kling with audio
estimates 0.84 USD, before the application's approved retry allowance.

`FalProvider.refresh_pricing` performs only an authenticated, read-only
`GET https://api.fal.ai/v1/models/pricing?endpoint_id=...`. It returns a sanitized
base-unit price, currency, retrieval timestamp and provenance. Without a key it
returns unavailable without a network request. The [official pricing API](https://fal.ai/docs/platform-apis/v1/models/pricing)
returns base billing units, which do not independently prove every resolution or
audio multiplier. A base-price refresh must not mark a full request quote
verified. All current adapter quotes remain unverified and require the relevant
spending acknowledgement. The [estimate API](https://fal.ai/docs/platform-apis/v1/models/pricing/estimate)
likewise works from supplied unit quantities or historical calls, rather than
accepting this exact model request's settings.

Money uses Decimal. A missing/invalid/zero baseline, missing multiplier, or unknown
billing unit yields `amount_usd=null`, never a free-job estimate. Unknown costs
need explicit approval and conservative reservation in the orchestration layer.
Catalog seeding inserts missing entries and never overwrites operator prices,
provider enablement, model enablement, settings or default choices.

## Queue, retries and cancellation

The [canonical queue submission reference](https://fal.ai/docs/api-reference/inference-api/queue/submit-path)
uses the full endpoint on POST, then returns authoritative status/result/cancel
URLs. These URLs use only the owner/app root followed by `requests/{UUID}`. The
adapter validates HTTPS, exact `queue.fal.run` hostname, canonical UUID, exact
path, and absence of credentials/query/fragment/port before reusing a URL. It
refuses redirects, including when an injected HTTP client defaults to following
them, and bounds JSON responses to 1 MiB.

The [status reference](https://fal.ai/docs/api-reference/inference-api/queue/status)
has states `IN_QUEUE`, `IN_PROGRESS`, and `COMPLETED`. Failed and cancelled jobs
also become `COMPLETED`; the adapter checks safe error categories and retrieves
the result separately. Generated model OpenAPI describes logs and cancellation
responses less precisely than the canonical queue reference. It must not be the
sole source for production queue handling.

Paid POSTs require `paid_authorized=true` and a configured secret, and send
`X-Fal-No-Retry: 1` to disable provider automatic retries. The HTTP client has no
automatic submission retry. A timeout, malformed success response, redirect or
5xx response after POST is ambiguous: the database orchestration must retain the
reservation and reconcile it, never replay it automatically. No provider
idempotency header is assumed.

The [cancellation reference](https://fal.ai/docs/api-reference/inference-api/queue/cancel)
documents `PUT cancel_url`: HTTP 200 with `status=OK` confirms removal from the
queue; HTTP 202 with `CANCELLATION_REQUESTED` only signals running work. A model
may not stop immediately. `ALREADY_COMPLETED` and `NOT_FOUND` do not prove zero
charges. Legacy `success=true` alone is not treated as confirmed cancellation.
The application also retains locally unwanted status and reconciles charges.

## Media trust boundary and remaining acceptance

Result and internal reference URLs are restricted to HTTPS `fal.media` and its
subdomains, with no user information, explicit port, fragment or control
characters. Legacy Google Storage examples are not part of this allowlist. A new
host needs a reviewed policy change. The downloader must independently resolve
and pin public addresses, enforce byte/time quotas, reject redirects and validate
the media signature; approving a host suffix alone does not implement safe
download. Provider credentials are sent only to validated queue routes or the
fixed account pricing endpoint, never to media URLs.

The official [Kling comparison published November 3](https://fal.ai/learn/devs/kling-4-0-vs-kling-3-0)
states that Kling 4 API access is coming soon. It is deliberately absent from
the initial enabled catalog. Wan 3 exists, so the verified Wan 2.7 option is not
presented as the latest family release.

Mock transports verify request translation, cost arithmetic, status failures,
cancellation semantics, ambiguous submissions, no retries, redirects, response
limits, URL policy, pricing refresh and secret-safe exceptions. They do not prove
provider access, generation quality, real charges or cancellation behavior.
Acceptance tests A/B still require explicit paid-generation approval and a
configured fal account; no such jobs have been run by this implementation.
