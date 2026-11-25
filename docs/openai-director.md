# OpenAI director adapter

Implementation verified against official OpenAI documentation on **2026-11-06**.
The OpenAI Docs skill was read; no direct OpenAI documentation/key helper was
available in this session, so official-domain search and fetched documentation
were used. All implementation tests use HTTPX mock transports. No OpenAI or fal
generation request was submitted, and no production service was changed.

## Official API contract

The adapter uses `POST https://api.openai.com/v1/responses` with a configured
model, a developer instruction and serialized user data. Structured output is
configured under `text.format` with `type=json_schema` and `strict=true`.
Every object property is required and object extras are forbidden. Definitions
and references keep the scene shape consistent with the runtime parser.
Refusals and incomplete responses must be handled before accepting a plan.
These choices follow the [official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).

Responses can contain reasoning or other output items before the assistant
message. The parser traverses output types instead of assuming text is at the
first array position; it accepts exactly one assistant `output_text` containing
the plan. No SDK convenience `output_text` echo or arbitrary tool arguments are
parsed. See [official text generation guidance](https://developers.openai.com/api/docs/guides/text).

## Application contract

`app.director` exports `DirectorRequest`, `DirectorScene`, `DirectorPlan`,
`DirectorResult`, `DirectorError` and `OpenAIDirector`.

`DirectorRequest` carries script, target duration, language, style, audience,
default video model, project budget, model capability metadata and owned
reference metadata. `DirectorPlan` contains a title and scenes. Every scene
includes its stable key, order, duration, mode/model/settings, audio preference,
visual/negative prompts, narration, reference IDs, transition and draft status.
The JSON schema comes from the same Pydantic types that parse the result; no
hand-maintained parallel field list can silently diverge.

The wire parser enforces types, required fields, known enums and forbidden
extras. It does **not** authorize a plan. The storyboard service must additionally
validate scalar limits, unique/sequential scene keys, duration totals/tolerance,
enabled model capabilities, reference ownership, project access and budget
approval before saving or generating. A structurally valid scene with duration
zero is still rejected by that service.

## Trust and privacy boundaries

Script, extracted document text, style/audience and reference captions are
untrusted creative data in the user message. They cannot alter the developer
instruction, add a role, enable tools or change the endpoint. No tools,
conversation history, previous response ID or external resource fetch is used.
`store=false` and `stream=false` are requested; this is not a claim about every
provider retention policy or account setting.

Only enabled models with a documentation verification timestamp reach the
director prompt. Model/provider secrets and arbitrary configuration fields are
excluded. References contain canonical owned asset IDs, captions, MIME type and
bounded dimensions. URL/storage-path/header/base64 fields are dropped. **Image
pixels are not sent in this increment.** Captions can guide a draft, but the
director must not claim to have inspected an image. Vision support remains a
separate implementation and acceptance task.

## Approval, billing and bounded execution

There is no default live model alias. Root configuration must explicitly provide
a model and key, enable the director, and record operator approval. Independently,
`generate(..., approved=True)` is required before the adapter can issue a request.
The orchestration layer persists the paid intent before calling it.

The HTTP client rejects redirects, ignores environment proxies for its owned
transport, sends credentials only to the fixed OpenAI endpoint, and requests
uncompressed JSON. The entire request has a configurable absolute deadline
(default 45 seconds), independent socket timeouts, a 1 MiB UTF-8 input-data limit and
a 1 MiB response limit. Output tokens are explicitly bounded. A peer sending
small chunks cannot keep the request alive indefinitely.

No POST retries or schema-repair calls occur. Network failures, deadlines, 5xx,
redirects and unreadable/oversized outer responses can have ambiguous paid
outcomes. They require reconciliation or a newly authorized attempt. Refusal,
incomplete output and invalid plans fail closed and never authorize video work.
Exceptions contain safe codes, never scripts, provider bodies or raw refusals.

Returned usage includes only actual bounded integer `input_tokens`,
`output_tokens` and `total_tokens` fields supplied by the API. Missing usage
stays missing; USD costs are not invented or assumed to be zero. Safe response
IDs and available usage also accompany known failed-output errors. Token limits
bound processing; they do not independently establish a currency-denominated
project budget or prove a provider charge. The application must retain unknown
costs and require the appropriate explicit approval.

## Verification and remaining acceptance

Run `uv run pytest tests/test_director_provider.py`. Mock tests cover request
shape, schema/runtime agreement, paid guards, disabled/unverified model filtering,
reference privacy, embedded role injection, strict parsing, refusals/incomplete
responses, multiple outputs, duplicate JSON keys, usage sanitization, oversized
input/output, redirects, transport errors and absolute streaming deadlines.

These tests establish application behavior against fixtures. They do not prove
account/model availability, live script quality, real token charges or full video
production. A live OpenAI call still needs explicit authorization and configured
credentials; downstream paid fal acceptance remains a separate gate.
