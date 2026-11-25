# Third-party dependencies and release notices

The application source is licensed under [Apache-2.0](LICENSE). Dependencies,
container base images, fonts and external services retain their own licenses and
terms. Apache-2.0 does not relicense those components or grant service access,
voice-output rights, trademarks or third-party patent rights.

This document is a dependency inventory and upstream notice index. It is not a
complete set of license texts, an artifact-specific SBOM, a source offer or a
certification that a binary release meets every distribution obligation.

## Inventory scope

Use `uv.lock`, `requirements.lock`, `requirements-dev.lock` and
`apps/frontend/package-lock.json` for exact package versions and hashes. Docker
and Compose files define additional operating-system packages and services.
Optional packages vary by platform, and build/test dependencies do not all appear
in runtime images. Recheck this inventory whenever dependencies or packaging change.

The source repository does not vendor installed Python/npm dependency trees or
container images. Installing dependencies, distributing browser bundles, and
distributing complete images each require reviewing the components actually used
or shipped. Preserve upstream copyright, license and `NOTICE` files as applicable.

## Python packages

| Component or group | Declared license / notice concern |
| --- | --- |
| FastAPI, SQLAlchemy, redis-py and other MIT packages | Preserve each package's copyright and license notices. The redis-py client license is distinct from the Redis server license below. |
| Uvicorn, Celery and other BSD packages | Preserve the relevant BSD notices and conditions. |
| boto3/botocore and other Apache-2.0 packages | Preserve required license, attribution and upstream `NOTICE` material; some wheels contain separate notice files. |
| Cryptography | Apache-2.0 OR BSD-3-Clause as declared by the locked package; bundled native components need their own inventory. |
| Pillow | MIT-CMU as declared by the locked package; review bundled image libraries in the actual wheel/image. |
| psycopg and psycopg-binary | LGPL-3.0-only in the locked package metadata. Binary wheels also package native client libraries. Preserve applicable library licenses and satisfy corresponding-source/replacement obligations for the distribution. |
| edge-tts | The locked 7.3.1 release principally uses LGPLv3; its `srt_composer.py` has an MIT exception. Package licensing does not settle Microsoft service or audio-output rights. |
| certifi | MPL-2.0 applies to its certificate-bundle source form; preserve its notice and review source-availability requirements when distributing it. |

Primary references: [Psycopg license](https://github.com/psycopg/psycopg/blob/3.3.8/LICENSE.txt),
[binary-wheel contents](https://www.psycopg.org/psycopg3/docs/basic/install.html#binary-installation),
[edge-tts license](https://github.com/rany2/edge-tts/blob/7.3.1/LICENSE),
and [certifi license](https://github.com/certifi/python-certifi/blob/master/LICENSE).
The table summarizes material groups, not every transitive package.

## Frontend and build packages

Next.js and React are MIT-licensed in the lockfile. The dependency graph also
includes Apache-2.0, BSD, ISC, MIT-0, 0BSD, BlueOak-1.0.0 and CC0 components.
Preserve the applicable notices in the content actually distributed.

Pay particular attention to these additional groups:

| Component | Scope and declared license |
| --- | --- |
| sharp and platform-specific libvips packages | Optional runtime image-processing packages. sharp declares Apache-2.0; libvips binaries and their bundled libraries have separate licenses, including LGPL-3.0-or-later in the lockfile. |
| lightningcss and platform packages | Build dependencies declaring MPL-2.0; inspect any distributed build tooling or runtime traces before deciding they are excluded. |
| caniuse-lite | Declares CC-BY-4.0; retain applicable attribution when distributing its data or material derived from it. |

Next standalone tracing must not be assumed to copy license files. Collect and
verify required notices explicitly for the final browser bundle and runtime image.
The upstream [sharp license](https://github.com/lovell/sharp/blob/main/LICENSE)
and [libvips packaging project](https://github.com/lovell/sharp-libvips/blob/main/README.md)
describe separate licensing for the wrapper, packaging scripts and native libraries.

## Redis server and container images

The Compose templates and deployment-image bundle reference `redis:7.4-alpine`.
Redis server 7.4 is offered under RSALv2 or SSPLv1, not Apache-2.0 or the MIT
license used by redis-py. Review the chosen terms against the actual service model
and redistribution. Do not infer either blanket permission for every hosted use
or a prohibition of ordinary private application operation from this summary.
See the [Redis version/license matrix](https://redis.io/legal/licenses/),
[RSALv2 terms](https://redis.io/legal/rsalv2-agreement/), and
[SSPLv1 terms](https://redis.io/legal/server-side-public-license-sspl/).

The `postgres`, Python, Node, Alpine and Debian images and their installed
packages add further components. Their licenses must be collected from each
resolved image. Floating image tags and unversioned apt packages are insufficient
to identify the contents of a particular binary release.

## FFmpeg and fonts

The backend `base` target does not install FFmpeg or fonts. The `media`, `render`,
`narration` and test targets install Debian FFmpeg, DejaVu and Noto CJK packages.
The compositor uses the `libx264` encoder. Debian's standard FFmpeg build enables
GPL components, so these media images must not be described as LGPL-only.
The initial deployment-image workflow exports `base`, not the media target;
that bundle still includes Python dependencies and the separate Redis image.

Before distributing media images, record the exact FFmpeg/package versions,
configuration and included libraries. Preserve license texts and notices and
provide the applicable corresponding source and build material. Review codec
patent requirements for the intended markets/use. A subprocess invocation alone
does not establish the license of the entire application.
[FFmpeg's licensing guidance](https://www.ffmpeg.org/legal.html) and
[Debian's build rules](https://sources.debian.org/src/ffmpeg/7:5.1.9-0%2Bdeb12u1/debian/rules)
describe the upstream conditions and GPL-enabled packaging.

Noto CJK uses [SIL OFL 1.1](https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE).
DejaVu has [Bitstream Vera/Arev notices and conditions](https://dejavu-fonts.github.io/License.html).
Retain the font licenses and copyright notices in images containing the fonts.
Font-file distribution and the rights in rendered media are separate questions;
do not label ordinary rendered output with the font's license by default.

## External services and media

The Edge adapter is disabled by default and intended for separately approved
evaluation. The [Edge Read Aloud feature documentation](https://www.microsoft.com/en-us/edge/features/read-aloud)
describes a browser feature; it does not establish permission for this project's
unofficial client or commercial redistribution of generated audio. Keep the gate
off until the applicable permission is established or a provider with suitable
contracted terms is integrated and accepted.

OpenAI, fal, S3-compatible services and other external providers have independent
service, pricing, data-use and output terms. An open-source license for this
application grants no provider quota, account, paid-call permission or rights in
uploaded scripts, images, music or other third-party media.

Follow the separate [source, binary and live release gates](docs/open-source-release.md).
