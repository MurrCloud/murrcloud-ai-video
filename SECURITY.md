# Security policy

## Supported versions

The current `main` branch is a pre-release development version. Security fixes
target that branch; no production support period, backport series or response
service-level agreement is promised. Released versions and their support windows
must be documented here before a supported release is announced.

## Reporting a vulnerability

Do not post exploit details, credentials, customer data or private host evidence
in issues, pull requests, discussions or workflow logs.

When GitHub private vulnerability reporting is enabled, use **Report a
vulnerability** on the repository's
[Security advisories page](https://github.com/MurrCloud/murrcloud-ai-video/security/advisories).
If that control is unavailable, including during private review, send a report
to [contact@murrcloud.com](mailto:contact@murrcloud.com) with the subject
`Security report: murrcloud-ai-video`. This mailbox is the private reporting
fallback; ordinary issues are not a confidential channel. Exclude credentials,
customer data and private host evidence from email, and arrange a suitable
private transfer with a maintainer if additional sensitive material is needed.

For an authorized source publication, maintainers must enable and verify GitHub
private vulnerability reporting immediately after the repository becomes public,
before inviting contributions or public vulnerability reports. Record that check
with the exact published source revision. This policy does not claim that the
GitHub reporting control is already enabled.

Include the affected commit/version, impact, a minimal synthetic reproduction,
and any proposed fix. Never test against a deployment you do not own or have
explicit authorization to assess. Use disposable services and synthetic media.

## Maintainer response

The repository owner assigns a responder, reproduces reports in isolation,
coordinates a fix and disclosure with the reporter, and decides whether supported
users need an advisory. Publish only sanitized evidence. Coordinate credential
revocation privately when required; deleting a value from source is insufficient
for an exposed live credential.

## Security boundaries

- Paid generation, director, narration, rendering and S3 stay disabled by default.
  A saved provider key does not activate work or authorize spending.
- Missing or unhealthy host admission, unavailable rate limiting, and ambiguous
  paid intent must fail closed. Preserve owner scope, session/revision locks and
  resource limits.
- Runtime secrets, private media, completed host assessments and deployment
  evidence do not belong in source, fixtures, PRs or logs.
- CI secret and vulnerability checks support review; passing scans do not prove
  absence of vulnerabilities or live-provider acceptance.

See [AGENTS.md](AGENTS.md) for implementation constraints and
[release preparation](docs/open-source-release.md) for publication gates.
