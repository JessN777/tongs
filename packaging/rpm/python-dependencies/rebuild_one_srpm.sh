#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --distribution NAME --source-rpm FILE --output-dir DIR\n' "$0" >&2
}

distribution=""
source_rpm=""
output_dir=""
while (($#)); do
    case "$1" in
        --distribution) distribution=$2; shift 2 ;;
        --source-rpm) source_rpm=$2; shift 2 ;;
        --output-dir) output_dir=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -n "$distribution" && -f "$source_rpm" && -d "$output_dir" ]] || {
    usage
    exit 2
}

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

# A fixed build root, not a random mktemp name: compiled companions embed
# their build paths (Rust panic locations, the CycloneDX SBOM), so a random
# directory would make every rebuild differ.  Each rebuild runs in its own
# disposable container, and an existing directory is refused, never reused.
topdir="${TMPDIR:-/tmp}/tongs-srpm-rebuild"
mkdir -- "$topdir"
cleanup() {
    rm -rf -- "$topdir"
}
trap cleanup EXIT
mkdir -p -- "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS,cache,cargo-home}
export XDG_CACHE_HOME="$topdir/cache"
export CARGO_HOME="$topdir/cargo-home"

# Every companion RPM is published, so each takes the level 19 payload.
# Multithreaded zstd output does not depend on the worker count, so T0 (one
# worker per CPU) stays reproducible across runners with different CPU counts.
defines=(
    --define "_topdir $topdir"
    --define "_binary_payload w19T0.zstdio"
    --define "source_date_epoch_from_changelog 1"
    --define "clamp_mtime_to_source_date_epoch 1"
    --define "use_source_date_epoch_as_buildtime 1"
    --define "_buildhost tongs-rpm-builder"
)
rpmbuild --rebuild "${defines[@]}" "$source_rpm" \
    2>&1 | tee "$output_dir/rebuild-${distribution}.log"

mapfile -t built < <(find "$topdir/RPMS" -type f -name '*.rpm' -print | sort)
[[ ${#built[@]} -gt 0 ]] || { printf 'SRPM rebuild produced no RPMs\n' >&2; exit 1; }
for rpm_path in "${built[@]}"; do
    rpm_name=$(basename -- "$rpm_path")
    cp -- "$rpm_path" "$output_dir/$rpm_name"
    rpm -qp --queryformat '%{NEVRA}\nLicense: %{LICENSE}\n' "$rpm_path" \
        >"$output_dir/${rpm_name}.metadata.txt"
    rpm -qp --provides "$rpm_path" | sort \
        >"$output_dir/${rpm_name}.provides.txt"
    rpm -qp --requires "$rpm_path" | sort \
        >"$output_dir/${rpm_name}.requires.txt"
    rpm -qp --queryformat '[%{FILENAMES}|%{FILEFLAGS:fflags}\n]' "$rpm_path" \
        >"$output_dir/${rpm_name}.file-flags.txt"
done
