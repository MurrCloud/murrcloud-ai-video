# Contributing

Application source is licensed under [Apache-2.0](LICENSE). Intentional
contributions are submitted under that license, subject to its contribution
terms. Submit only work you have the right to contribute, and preserve existing
third-party license and attribution notices. Dependency licenses are documented
separately in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The repository is in private pre-release review. Public visibility and any
package/image release require a separate maintainer decision. Do not attach
production records or external customer material to a contribution.

## Development and review

Read [AGENTS.md](AGENTS.md) and the architecture before changing code. Use a
focused branch and pull request; describe the problem, resulting behavior,
validation and any remaining acceptance limits. Discuss large behavior or
dependency changes before implementation.

Use the local setup in [README.md](README.md). Run:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

In `apps/frontend`, run `npm ci`, `npm test`, `npm run typecheck` and
`npm run build`. The isolated acceptance workflow additionally exercises real
PostgreSQL/Redis, migrations, encrypted recovery, synthetic media, frontend proxy,
container isolation and dependency failure behavior. Report failures and skipped
checks accurately. Mocked requests do not prove provider quality or billing.

Keep `uv.lock`, exported `requirements.lock`/`requirements-dev.lock`, and the
frontend lockfile consistent with any dependency changes. Review dependency
licenses and the installed binary inventory before adding or redistributing
components. Do not replace pinned action commits with mutable tags.

## Data, infrastructure and paid work

Use synthetic fixtures. Exclude secrets, customer media, runtime files, local
deployment directories and completed host assessments. Review filenames,
metadata and logs as well as file contents. Use the private process in
[SECURITY.md](SECURITY.md) for vulnerability reports.

Tests run locally or in disposable CI. Do not contact a live VPS, change shared
infrastructure, enable workers, or submit paid/provider requests as part of a PR.
Preserve disabled defaults, fail-closed checks and explicit migration boundaries.
Each live integration or deployment needs its own scoped authorization.
