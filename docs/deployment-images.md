# Initial Studio deployment images

The `Studio deployment images` workflow builds runtime images on an isolated
GitHub runner. It never contacts the VPS, starts production services, changes protected workloads,
publishes a container registry package or submits a provider request. It receives
no deployment credentials, provider keys or generated application secrets.

This packaging workflow does not replace the isolated acceptance workflow. Verify
the applicable source commit's acceptance evidence before deploying its images.
The frontend Dockerfile itself runs its full tests, typecheck and production build
before producing the runtime image. No build or automated suite runs on the protected workloads
host as part of image transfer/import.

## Trigger and artifact contract

Dispatch the workflow manually for a reviewed ref when it is available on the
default branch. Any configured push trigger only builds and packages images;
no push or dispatch performs deployment. Review workflow triggers before enabling
additional automated packaging.

One artifact is uploaded with seven-day retention:

`studio-deployment-images-<full-source-commit-sha>`

Its root contains exactly these packaging outputs:

- `studio-images.tar.gz`: a Docker-save stream of four `linux/amd64` runtime images.
- `manifest.json`: source/run identity, resolved image IDs/digests, platform and
  image sizes, plus the bundle's SHA-256, compressed bytes and uncompressed bytes.
- `SHA256SUMS`: SHA-256 for the bundle and manifest; the same hashes are recorded
  in the authenticated workflow log independently of the artifact.

The API image is `murrcloud-video-backend:deploy-<sha>` using the `base` target.
The frontend is `murrcloud-video-frontend:deploy-<sha>` using the `runtime` target.
Both carry `org.opencontainers.image.revision=<sha>` and require image user
`10001:10001`. The data images are `postgres:17-bookworm` and `redis:7.4-alpine`;
their resolved registry digests and actual image content IDs are recorded. The
workflow does not build the narration/rendering media-image targets or start any
worker. The shared backend base still contains queue code; its presence is not
permission to enable a worker. The bundle contains image files, not test artifacts,
container volumes, running-container state, `.env` or credentials.

Images must total no more than 4 GiB of reported image bytes. Export also enforces
a 4 GiB raw-stream bound, a 2 GiB gzip bound and a five-minute Docker-export
deadline. A failed export removes only its operation-owned partial gzip. The
runner must retain at least 1 GiB beyond the worst-case compressed bundle for
staging. Upload uses compression level zero because the bundle is already gzip.
GitHub's artifact ZIP wrapper still exists; it does not add a second compression
pass. The action reports its artifact ID and outer archive digest separately.

## Operator verification and import

Identify the successful run in the requested private repository, its workflow,
full source commit, run/attempt, artifact ID, creation time and expiry. Confirm
that these match the reviewed deployment source. Do not trust an artifact merely
because its name or sidecar checksum looks correct. A hash obtained alongside an
untrusted download does not establish origin.

Retrieve only the artifact associated with that authenticated run, using GitHub
CLI/API or a short-lived download URL obtained from that API. Keep any temporary
bearer URL or GitHub token out of shell history, logs, repo files and server
configuration. The VPS does not need a persistent GitHub/registry credential.

After safely unpacking the expected artifact members into private bounded staging,
compare the outer download digest with trusted GitHub metadata when available,
and the inner manifest/bundle hashes against the authenticated run evidence.
Then `sha256sum -c SHA256SUMS` confirms local content has not changed. Check the
manifest's source SHA, exact four image references, platform, byte bounds and
image IDs before any import. Stage with an explicit disk reserve and import only
after the approved shared host preflight and Studio-only rollback plan are ready.

The host-side artifact tool requires explicit `--expected-machine-id` and
`--expected-repository owner/name`. Use a privately reviewed machine identity and
repository, retain fail-closed checks, and never derive either expectation from
the untrusted artifact payload. There are no production identity defaults.

`docker image load --input studio-images.tar.gz` imports these image contents;
it does not start application containers. Import can consume host I/O and Docker
daemon resources, so monitor protected workloads and host capacity during this approved step.
Do not rebuild on the VPS, prune unrelated images, restart Docker or alter protected workloads to
make an import succeed. Stop the Studio deployment on unsafe host conditions.

After import, verify each reference's content and `linux/amd64` platform against
the trusted bundle. A direct `.Id` comparison is valid only when both Docker
stores use the same identity representation. Classic Docker reports a config
digest; the containerd image store reports its target manifest or index digest.
A different `.Id` must be reconciled through the content chain, never accepted
on matching tags, labels or platform alone. Registry `RepoDigests` may also change
or disappear during Docker-save/load.

For a classic-CI to containerd-host transfer, verify this chain before
starting containers:

1. Retain the authenticated artifact/run evidence and verified outer, manifest
   and gzip SHA-256 values. Read OCI metadata only from that trusted archive;
   bound metadata sizes and verify each blob's raw bytes against its digest.
2. Require the imported reference's `.Id` to equal the SHA-256 of its OCI target
   manifest in the trusted archive. If the target is an index, verify every
   descriptor followed and select exactly one `linux/amd64` image manifest.
3. Require that manifest's config digest to equal the CI `image_id` recorded in
   the authenticated packaging manifest. Verify the config blob's raw hash and
   descriptor size. Require its ordered `rootfs.diff_ids` to equal the imported
   image's `RootFS.Layers` exactly.
4. Check config and runtime platform, and application user `10001:10001` and
   revision label, against the reviewed source. Retain both CI config digests and
   imported runtime target digests in the root-owned deployment inventory
   `/opt/video-studio/shared/verified-runtime-images.json`.

Pin each imported runtime using `repository@sha256:<verified-runtime-target>`
with `pull_policy: never` and `--pull never`; do not let floating data tags refresh
during startup. After startup, require each container's image identity to match
its verified runtime target. If any content-chain check fails or the archive
format cannot establish the chain, stop before starting services and investigate.

Start only the dedicated `postgres`, `redis`, `api` and `frontend` services. Use
new Studio-only secrets and storage, preserve protected services/database/routes, and
explicitly keep paid generation, director, narration, rendering and S3 disabled.
No worker profile is part of this initial deployment. Migrate only the new Studio
database, seed the catalog offline and provision the initial admin privately.
The image manifest is packaging evidence, not proof of production environment
settings, protected-workload functional acceptance or a completed deployment.

## Verified upstream references

- [Official upload-artifact v4.6.2 release](https://github.com/actions/upload-artifact/releases/tag/v4.6.2)
  resolves to [commit ea165f8d65b6e75b540449e92b4886f43607fa02](https://github.com/actions/upload-artifact/commit/ea165f8d65b6e75b540449e92b4886f43607fa02).
- [Pinned action inputs and outputs](https://github.com/actions/upload-artifact/blob/ea165f8d65b6e75b540449e92b4886f43607fa02/action.yml)
  define retention, no extra compression, artifact IDs and digests.
- [Downloading workflow artifacts](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)
  requires access to the repository and explains scoped CLI downloads.
- [Workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
  document push and manual-dispatch behavior.
- [Docker's containerd image store](https://docs.docker.com/engine/storage/containerd/)
  describes the default for new Docker Engine 29 installations and its differences
  from the classic store.
- [Moby's containerd image inspection implementation](https://github.com/moby/moby/blob/master/daemon/containerd/image_inspect.go)
  sets the reported image ID from the target descriptor's digest.
- [OCI image configuration](https://github.com/opencontainers/image-spec/blob/main/config.md#imageid)
  explains divergent ImageID representations and ordered filesystem DiffIDs.
- [OCI image manifest](https://github.com/opencontainers/image-spec/blob/main/manifest.md)
  defines configuration and layer descriptors and their digest relationships.

This workflow changes no production infrastructure.
