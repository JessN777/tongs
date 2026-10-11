#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --source-rpm FILE --output-dir DIR [--dist DIST] [--desktop-version VERSION] [--binary-payload MODE]\n' "$0" >&2
}
source_rpm=""
output_dir=""
dist=.fc44
desktop_version=""
# Multithreaded zstd at the Fedora default level 19.  Once zstd runs with
# workers its output no longer depends on how many there are, so T0 (one
# worker per CPU) stays reproducible across runners with different CPU counts.
binary_payload=w19T0.zstdio
while (($#)); do
    case "$1" in
        --source-rpm) source_rpm=$2; shift 2 ;;
        --output-dir) output_dir=$2; shift 2 ;;
        --dist) dist=$2; shift 2 ;;
        --desktop-version) desktop_version=$2; shift 2 ;;
        --binary-payload) binary_payload=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -f "$source_rpm" && -d "$output_dir" ]] || { usage; exit 2; }
# Only the two reviewed payload modes: level 19 for every published RPM and
# level 3 for the unpublished previous builds that seed the upgrade cycle.
case "$binary_payload" in
    w19T0.zstdio | w3T0.zstdio) ;;
    *)
        printf 'unsupported binary payload mode: %s\n' "$binary_payload" >&2
        usage
        exit 2
        ;;
esac
# Whole-file reproducibility.  rpm dates the build from the newest %changelog
# entry of the source RPM (SOURCE_DATE_EPOCH), clamps file times to it and
# records it as the build time, and the build host is a fixed name instead of
# the container's random hostname.  An inherited SOURCE_DATE_EPOCH would win
# over the changelog, so it is cleared, and a source RPM without a changelog
# entry is refused instead of falling back to the clock.
unset SOURCE_DATE_EPOCH
changelog_time=$(rpm -qp --queryformat '%{CHANGELOGTIME}' "$source_rpm")
[[ "$changelog_time" =~ ^[1-9][0-9]*$ ]] || {
    printf 'source RPM has no %%changelog entry to date the build: %s\n' "$source_rpm" >&2
    exit 1
}
# A fixed build root, not a random mktemp name, so no build path differs
# between two rebuilds.  Each rebuild runs in its own disposable container,
# and an existing directory is refused, never reused.
topdir="${TMPDIR:-/tmp}/tongs-desktop-rebuild"
mkdir -- "$topdir"
trap 'rm -rf -- "$topdir"' EXIT
mkdir -p -- "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS,cache}
export XDG_CACHE_HOME="$topdir/cache"
defines=(
    --define "_topdir $topdir"
    --define "dist $dist"
    --define "_binary_payload $binary_payload"
    --define "source_date_epoch_from_changelog 1"
    --define "clamp_mtime_to_source_date_epoch 1"
    --define "use_source_date_epoch_as_buildtime 1"
    --define "_buildhost tongs-rpm-builder"
)
if [[ -n "$desktop_version" ]]; then
    defines+=(--define "tongs_desktop_version $desktop_version")
fi
rpmbuild --rebuild "${defines[@]}" "$source_rpm"
find "$topdir/RPMS" -type f -name '*.rpm' -exec cp -- {} "$output_dir/" \;
