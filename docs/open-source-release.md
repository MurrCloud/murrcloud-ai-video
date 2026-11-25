# Source publication and release gates

The application source uses [Apache-2.0](../LICENSE). Third-party dependencies
retain their own terms; see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
Licensing the source does not authorize a visibility change, binary distribution,
production deployment or paid provider calls. Keep the review repository private
until the maintainer explicitly authorizes publication.

## Source-only publication

Use this publication sequence:

1. Record the full source commit SHA proposed for publication and the source-only
   scope for the maintainer's decision. Binary/container distribution, deployment
   and live feature enablement remain separate decisions.
2. Complete the pre-publication checks below for that exact revision. Review
   Actions artifacts as well as source. Before changing visibility, confirm that
   no packaging workflow runs are queued or running and no binary artifacts are
   retained; private runtime-image artifacts must not become public through a
   source-only visibility change.
3. Record the maintainer's authorization for the reviewed revision and scope.
   Recheck the final commit, intended disclosure and packaging run/artifact state
   immediately before changing visibility. The workflow's private-only guard and
   final privacy check do not atomically lock repository visibility. If the source
   revision changes, update the review and authorization record before proceeding.
4. After the repository becomes public, enable and verify private vulnerability
   reporting, required status checks and branch/PR rules, dependency alerts, and
   available secret-scanning/push-protection controls. Review Actions permissions
   and repository write access in the public configuration. Record unavailable
   controls and the maintainer's chosen handling; do not claim they are enabled.
   Require the source validation contexts `foundation`, `Secret scan`,
   `Python dependency audit` and `Frontend dependency audit` on the protected
   branch, using the exact names emitted by successful workflow runs.
5. Record the exact published SHA, verification results and remaining limitations
   before inviting contributions or vulnerability reports. Verify the reporting
   route in [SECURITY.md](../SECURITY.md), including the email fallback.

Complete these checks for the exact source revision proposed for publication:

- [ ] Confirm authority to license the first-party source and assets under
  Apache-2.0. Preserve any third-party copyright/attribution already present.
- [ ] Include the application license, contribution terms, security reporting
  policy and dependency notice index. Check package metadata for consistent
  first-party licensing without relabeling third-party dependencies.
- [ ] Review the complete reachable history, tracked filenames, file modes,
  content and commit metadata. Include only sanitized source and synthetic
  fixtures; exclude deployment settings, credentials, runtime media, customer
  data, completed host assessments and private operational evidence.
- [ ] Run Gitleaks and a separate operational-identifier scan. Review results,
  including false positives, and repeat on a fresh remote clone.
- [ ] Pass backend/frontend tests, types, production build and applicable isolated
  acceptance. Record exact revision, failures, warnings and skips. Explain live
  gates honestly; synthetic tests do not establish provider quality or capacity.
- [ ] Verify disabled paid/director/narration/render/S3 defaults, blank runtime
  provider secrets, opt-in workers and fail-closed admission/host checks.
- [ ] Review repository write access, branch/PR policy, required checks, Actions
  permissions, dependency alerts and secret-protection availability. Prepare the
  public-settings verification above and verify the private email fallback in
  [SECURITY.md](../SECURITY.md). Enable and verify GitHub private reporting after
  the authorized visibility change, before inviting contributions or reports.
- [ ] Review public README, examples, issue/release templates, CI logs, artifacts,
  links and repository metadata for intended disclosure. Do not publish binary
  bundles under the label of a source-only release.
- [ ] Obtain explicit maintainer authorization for the visibility change and
  release scope. Recheck the final commit and repository settings immediately
  before the authorized action.

Passing automated checks does not complete the ownership, disclosure or
authorization decisions. Source publication may describe unfinished live features
provided their limits and disabled defaults remain clear.

## Binary or container distribution

This is a separate release gate, even when the source is already public. Before
distributing browser bundles, wheels, images or downloadable runtime bundles:

1. Identify the exact final artifacts, target platforms and immutable digests.
   Generate an artifact-specific SBOM covering language packages, native libraries,
   operating-system packages, fonts, data and base-image components. Lockfiles
   alone do not describe all shipped contents.
2. Collect required copyright, license and upstream `NOTICE` files into readable
   release materials and preserve them in the artifacts as applicable. Inspect
   the final image and Next standalone trace; do not assume the build copies them.
3. Resolve LGPL/MPL/GPL and other applicable source, replacement/relinking and
   notice requirements for the particular distribution. Retain exact matching
   source, patches and build/configuration material where required. Links in the
   dependency index alone do not constitute a complete source offer.
4. Review Redis 7.4 server's RSALv2/SSPLv1 terms for the intended distribution and
   service model. Its license is independent of redis-py and the application.
   No dependency license or service is changed by this checklist.
5. For media images, review GPL-enabled Debian FFmpeg, libx264 and bundled codec
   libraries, plus font notices and applicable codec patent considerations.
   The backend `base` target excludes FFmpeg/fonts; media targets include them.
6. Verify final artifact security, source provenance, signatures/checksums,
   reproducible contents where applicable, and the documented release scope.
   Resolve remaining distribution questions before offering the artifacts.

The repository's packaging workflow is private-only and prepares bounded image
artifacts for private review. It does not distribute binaries from the public
repository. Producing an artifact does not establish that its license obligations
have been completed or authorize public/customer distribution. This document is
a release procedure, not a certification of complete third-party compliance.

## Live feature and deployment acceptance

These gates are separate from source publication and remain disabled until the
applicable configuration, evidence and scoped authorization exist:

| Gate | Evidence required before enablement |
| --- | --- |
| Paid video/director | Reviewed endpoint/model/pricing and provider terms; explicit budget and call authorization; interruption/reconciliation acceptance without duplicate paid submissions. |
| Narration | Established service/access and commercial output rights for the selected provider/voice, real timing/quality tests, reliability review and bounded failure behavior. The edge-tts package license alone does not establish Microsoft service/output permission. |
| Rendering | Trusted fresh host admission, capacity testing, one render/thread defaults, bounded scratch/output and accepted real media behavior on the intended runtime. |
| Private S3 archive | Approved endpoint/account/bucket and data terms; private conditional writes, deadlines and compatibility checks. An upload adapter does not imply archive restore or eviction support. |
| Production deployment/recovery | Private completed host/storage assessment, independent secrets, verified images, protected-workload checks, approved rollback, backup custody/retention and isolated restore. Never use a live shared host as a substitute for an isolated test runner. |

Keep completed operational assessments, production identifiers, credentials and
customer evidence outside Git. Record environment acceptance privately without
turning synthetic CI signals into production health claims. A repository release
does not grant permission to change infrastructure or enable a paid feature.
