# Fedora 44 desktop RPMs

This directory defines two source RPMs. `python-tongs` builds the independently
installable `python3-tongs` core and the optional `python3-tongs+mcp` command
subpackage. `tongs-desktop` installs the accepted issue 51 Electron payload and
requires the exact core EVR used by its launcher.

The source preparation step derives the version from the exact checked-out
commit: the tag version at a `vX.Y.Z` tag (as for the unsigned beta RPMs attached
to the 1.0.0 release), otherwise a development version after the latest tag. By default it requires the desktop producer contract
to name that same commit, and embeds both identities into the desktop SRPM. It
copies the accepted issue 51 archive only after
checking its size, SHA-256, source identity, platform, compatibility interval,
and every declared payload file. The desktop SRPM repeats that validation before
extracting the payload. No wheel, virtual environment, npm build, or network
operation is part of either SRPM rebuild.

`python3-tongs` owns `/usr/bin/tongs`, the Python modules, and their metadata.
`python3-tongs+mcp` owns only `/usr/bin/tongs-mcp`; its explicit dependency on
`python3dist(mcp[cli])` keeps MCP out of the base closure. `tongs-desktop` owns
the complete runtime under `/usr/libexec/tongs-desktop`, its system launcher,
desktop entry, icon, AppStream metadata, man page, and conventional license and
inventory copies. The RPMs contain no scriptlets and do not inspect or modify
home directories.

The repository manifest names the historical `825a421` archive only as the
reviewed initial issue 52 fixture. The hosted issue 52 proof opts into that
fixture explicitly. A combined or release candidate supplies its own immutable
producer manifest to `prepare_sources.py` and uses the default exact-source
pairing. A differing payload source requires separate reviewed byte-equivalence
evidence and the explicit fixture switch; semver compatibility alone is not an
acceptable production pairing.

The desktop license field is an honest aggregate expression for a GitHub Release
artifact containing the upstream Electron distribution. It does not claim that
this package is eligible for Fedora or COPR publication review.

The end-to-end harness is deliberately restricted to a disposable GitHub-hosted
runner, apart from the local builder mode below, because it performs normal
Fedora DNF and Podman operations. It first
probes every command required before the clean container's bootstrap
transaction, without installing anything into that image. It rebuilds the exact
source wheel once with Fedora's packaging macros and rejects any version mismatch
before rebuilding the seven accepted issue 85 companion SRPMs.
It matches the rebuilt companion RPM metadata against the manifest's exact seven
binary identities and copies only that allowlist into consumer repositories;
debuginfo and debugsource outputs remain in the raw build evidence. It then creates
and independently rebuilds both issue 52 SRPMs and source-builds a test-only
desktop plugin RPM. It creates
local install repositories and proves a minimal clean install, installed sidecar
plugin discovery and invocation, Sigstore and system OpenSSL behavior, Electron
SONAME providers, exact ownership and metadata, a bounded X11 launch, explicit
MCP command integration and removal, same-NEVRA reinstall, a 0.4.9 to accepted-payload-version
desktop upgrade (0.5.0 for a candidate, the tag version on a release), lower-to-current core upgrade, classified corrupted-package and
dependency-negative failures, and uninstall preservation of user and unrelated
sentinels. Every successful transaction is bound to the intended RPM NEVRA and
an empty `rpm -V` result. The clean install also binds all seven companion NEVRAs
and their `tongs-final` repository origin. The X11 smoke is a lifecycle check and
makes no hardware GPU claim.

## Payload compression and reproducible files

Every rebuild sets its binary payload explicitly instead of taking rpm's
default, single-threaded zstd level 19 (`w19.zstdio`), which spent about 200
seconds of the hosted lifecycle step writing the two `tongs-desktop` payloads.
The final RPMs, which are the published ones, and every companion RPM use
multithreaded zstd at the same level, `w19T0.zstdio`. Once zstd runs with
workers its output no longer depends on how many there are, so `T0` (one
worker per CPU) gives the same payload on a 4-vCPU runner and on a 32-core
workstation. The previous RPMs only seed the upgrade cycle and are never
published, so they use the much faster `w3T0.zstdio`. `rebuild_one_srpm.sh`
accepts only those two modes, and `rebuild_srpms.sh` passes one of them to
each rebuild.

Every rebuild, desktop and companion alike, also passes
`--define "_buildhost tongs-rpm-builder"` and
`--define "use_source_date_epoch_as_buildtime 1"`, so two rebuilds of the same
SRPM give byte-identical RPM files rather than only identical payloads.
`SOURCE_DATE_EPOCH` comes from the newest `%changelog` entry of the source RPM
(`source_date_epoch_from_changelog`, which Fedora enables and the scripts
define explicitly, together with `clamp_mtime_to_source_date_epoch`); every
Tongs spec template and companion spec carries a changelog entry. The Tongs
templates date theirs with `@CHANGELOG_DATE@`, which `prepare_sources.py` sets
to the source commit's UTC committer date, so each release builds with its own
date and a rebuild of one commit stays identical; the companion specs keep the
fixed date of their packaged upstream version. The scripts
clear any inherited `SOURCE_DATE_EPOCH`, so the changelog is its only source,
and refuse a source RPM without a changelog entry instead of letting rpm fall
back to the clock. The build time is that epoch, the build host is a fixed
name instead of the container's random hostname, and file times are clamped
to the epoch. Each rebuild also uses a fixed `_topdir` instead of a random
`mktemp` directory, because the compiled `rfc3161-client` companion embeds its
build paths in its Rust panic locations and its CycloneDX SBOM; each rebuild
runs in its own disposable container, and the scripts refuse a build root that
already exists.
`tests/packaging/rpm/test_rebuild_reproducibility.py` pins the modes and the
defines.

Local source-level checks do not invoke Podman or RPM tooling:

```bash
.venv/bin/pytest tests/packaging/rpm/desktop tests/test_plugins/test_plugin_system.py -q
.venv/bin/ruff check packaging/rpm/desktop tests/packaging/rpm/desktop src/tongs/mcp/plugin.py
```

## Builder image

The harness builds on the RPM builder image that
`packaging/rpm/builder/image.json` pins (see the
[CI guide](../../../.agents/ci/README.md#rpm-builder-image)). Before it pulls
anything it refuses a pin without a published digest
(`scripts/pin_rpm_builder.py --check --require-published`). It then runs
`gh attestation verify` on `ghcr.io/andre-motta/tongs-rpm-builder@<digest>`,
requiring this repository, `rpm-builder-image.yml` as the signer workflow,
`refs/heads/main` as the source ref, the pin's `source_commit` as the source
digest and a GitHub-hosted runner. Only then does it pull the image, by digest,
and it refuses a builder whose revision or Fedora base label differs from the
pin. The pin, the verification output and the builder's `podman image inspect`
land in the output directory as `builder-pin.json`, `builder-attestation.json`
and `builder-image.inspect.json`, so `ALL-SHA256SUMS` and the stage receipt
bind them to the run. The `rpm-lifecycle` and `release-rpm` jobs hold
`attestations: read` for that check.

The source builder, the companion builder and every per-SRPM rebuild layer
start from the builder image. Each rebuild layer still runs `dnf builddep`
against its own source RPM, so a new `BuildRequires` works before the next
image build, only slower, and every rebuild still runs with networking
disabled. The clean install container stays plain Fedora with nothing
preinstalled, pinned to the builder's base digest
(`registry.fedoraproject.org/fedora@<base_digest>`), so the clean install,
minimal transaction and MCP closure checks still transact the full closure.
No image comes from the moving `fedora:44` tag.

## Local runs

`--local-builder IMAGE` runs the full lifecycle in a local Podman loop with a
builder image you built yourself. It refuses to start when `GITHUB_ACTIONS` or
`RUNNER_ENVIRONMENT` is set, takes only a `localhost/` image, needs
`--accepted-dir` and `--payload-manifest` because it never downloads anything
from GitHub, skips the pull and the attestation check, and takes the Fedora
base digest from the image's `org.opencontainers.image.base.digest` label.
`tests/ci/test_workflow_hygiene.py` fails if a workflow passes the option or
sets or clears either variable.

Run it from a standalone clone of the commit under test, not a git worktree:
the containers mount the checkout, and a worktree's `.git` points outside it.
The accepted archive must come from that same commit. Keep the output outside
the checkout and run one lifecycle at a time.

```bash
export TONGS_HEAD_SHA=$(git rev-parse HEAD) RUNNER_TEMP=$HOME/.cache/tongs-rpm/tmp
mkdir -p "$RUNNER_TEMP"
base=$(skopeo inspect --format '{{.Digest}}' docker://registry.fedoraproject.org/fedora:44)
podman build --build-arg "FEDORA_DIGEST=$base" \
    --build-arg "SOURCE_COMMIT=$TONGS_HEAD_SHA" \
    --tag localhost/tongs-rpm-builder:local \
    --file packaging/rpm/builder/Containerfile packaging/rpm/builder
packaging/desktop/archive/run_hosted.sh --source-root . \
    --output-dir "$HOME/.cache/tongs-rpm/archive"
.venv/bin/python tests/integration/desktop/rpm_payload_contract.py \
    --archive-dir "$HOME/.cache/tongs-rpm/archive/archive" \
    --output "$HOME/.cache/tongs-rpm/payload-input-contract.json" \
    --source-commit "$TONGS_HEAD_SHA" \
    --artifact-id 1 --artifact-name local-archive --run-id 1
packaging/rpm/desktop/run_hosted.sh \
    --output-dir "$HOME/.cache/tongs-rpm/lifecycle" \
    --accepted-dir "$HOME/.cache/tongs-rpm/archive/archive" \
    --payload-manifest "$HOME/.cache/tongs-rpm/payload-input-contract.json" \
    --local-builder localhost/tongs-rpm-builder:local
```

A local run proves the harness and the timings, not the hosted gate: it
neither pulls the published image nor verifies its attestation.
