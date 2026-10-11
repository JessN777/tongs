from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "desktop"


def test_desktop_retains_runtime_and_has_no_scriptlets() -> None:
    spec = (PACKAGING / "templates" / "tongs-desktop.spec.in").read_text()

    assert "cp -a payload/runtime/." in spec
    assert "BuildRequires:  diffutils" in spec
    assert "%{_libexecdir}/tongs-desktop" in spec
    assert "chrome-sandbox" in spec and "test ! -u" in spec
    assert "cmp %{SOURCE11} %{buildroot}%{_bindir}/tongs-desktop" in spec
    for scriptlet in ("%pre\n", "%post\n", "%preun\n", "%postun\n"):
        assert scriptlet not in spec


def test_system_launcher_uses_fixed_system_contract() -> None:
    launcher = (PACKAGING / "templates" / "tongs-desktop.in").read_text()

    assert launcher.startswith("#!/usr/bin/sh\n")
    assert 'if [ "$#" -ne 0 ]' in launcher
    assert "--ozone-platform=x11" in launcher
    assert "--tongs-python-executable /usr/bin/python3" in launcher
    assert "--tongs-core-version @CORE_PEP440_VERSION@" in launcher
    assert "--tongs-safe-cwd /usr/libexec/tongs-desktop" in launcher


#: The only containers that may reach the network: the Fedora provider audit,
#: the companion source download and the clean install from Fedora.
NETWORKED_PROGRAMS = {
    "packaging/rpm/desktop/audit_providers.py",
    "packaging/rpm/python-dependencies/prepare_sources.py",
    "packaging/rpm/desktop/install_and_verify.sh",
}


def _podman_runs(script: str) -> list[list[str]]:
    """Return each ``podman run`` command, continuation lines joined, as words."""

    joined = script.replace("\\\n", " ")
    return [line.split() for line in joined.splitlines() if "podman run" in line]


def _program(run: list[str]) -> str:
    return next(
        (
            word.removeprefix("/checkout/")
            for word in run
            if word.startswith("/checkout/") and word.endswith((".py", ".sh"))
        ),
        "<inline>",
    )


def test_hosted_harness_is_disposable_and_rebuilds_offline() -> None:
    harness = (PACKAGING / "run_hosted.sh").read_text()
    rebuilder = (PACKAGING / "rebuild_srpms.sh").read_text()

    assert "RUNNER_ENVIRONMENT:-} == github-hosted" in harness
    runs = _podman_runs(harness)
    networked = {_program(run) for run in runs if "--network=none" not in run}
    assert networked == NETWORKED_PROGRAMS
    rebuilds = _podman_runs(rebuilder)
    assert rebuilds
    for run in rebuilds:
        assert "--network=none" in run, " ".join(run)


def test_the_harness_names_no_moving_fedora_tag() -> None:
    """Every image comes from the builder pin by digest, never from fedora:44."""

    for name in ("run_hosted.sh", "rebuild_srpms.sh", "Containerfile"):
        assert "fedora:44" not in (PACKAGING / name).read_text(), name
    harness = (PACKAGING / "run_hosted.sh").read_text()
    assert 'pin_file="$repo_root/packaging/rpm/builder/image.json"' in harness
    assert "--check --require-published" in harness
    # The builder and the clean install base are digest references only.
    assignments = re.findall(
        r"^\s*(builder_image|base_image)=(.*)$", harness, re.MULTILINE
    )
    assert sorted(assignments) == [
        ("base_image", '"$fedora_repository@$base_digest"'),
        ("builder_image", '"$builder_repository@$builder_digest"'),
        ("builder_image", "$local_builder"),
    ]
    assert "fedora_repository=registry.fedoraproject.org/fedora\n" in harness
    assert "builder_repository=ghcr.io/andre-motta/tongs-rpm-builder\n" in harness


def test_the_source_builder_takes_a_required_base_image() -> None:
    containerfile = (PACKAGING / "Containerfile").read_text()
    assert re.findall(r"^FROM\s+(\S+)", containerfile, re.MULTILINE) == [
        "${BASE_IMAGE}"
    ]
    # No default: a build without the pinned reference fails at FROM.
    assert re.findall(r"^ARG BASE_IMAGE.*$", containerfile, re.MULTILINE) == [
        "ARG BASE_IMAGE"
    ]
    assert containerfile.index("ARG BASE_IMAGE") < containerfile.index("FROM ")


def test_builds_layer_on_the_builder_and_the_clean_install_stays_plain() -> None:
    harness = (PACKAGING / "run_hosted.sh").read_text()
    rebuilder = (PACKAGING / "rebuild_srpms.sh").read_text()
    joined = harness.replace("\\\n", " ")

    builds = [line for line in joined.splitlines() if "podman build" in line]
    assert len(builds) == 2
    for build in builds:
        assert '--pull=never --build-arg "BASE_IMAGE=$builder_image"' in build
    rebuilds = [line for line in joined.splitlines() if 'rebuild_srpms.sh"' in line]
    assert len(rebuilds) == 2
    for rebuild in rebuilds:
        assert '--base-image "$builder_image"' in rebuild
    # The clean install, upgrade and removal cycles run in plain Fedora at
    # the pinned base digest, never in the builder image.
    (install,) = [
        line for line in joined.splitlines() if "install_and_verify.sh" in line
    ]
    assert '"$base_image" /checkout/packaging/rpm/desktop/install_and_verify.sh' in (
        install
    )
    assert "builder_image" not in install
    (probe,) = [
        line for line in joined.splitlines() if "--entrypoint /bin/bash" in line
    ]
    assert '"$base_image"' in probe
    # Each rebuild layer starts from the builder and still resolves its own
    # source RPM's BuildRequires.
    assert "FROM $base_image\n" in rebuilder
    assert "dnf builddep --assumeyes" in rebuilder
    assert "podman build --pull=never" in rebuilder


def test_the_attestation_check_precedes_every_pull_and_build() -> None:
    harness = (PACKAGING / "run_hosted.sh").read_text()
    verify = harness.index("gh attestation verify")
    for command in ("podman pull", "podman build", "podman run"):
        assert verify < harness.index(command), command
    assert harness.index("--require-published") < verify
    for option in (
        '"oci://$builder_image"',
        "--repo andre-motta/tongs",
        '--signer-workflow "$builder_workflow"',
        "--source-ref refs/heads/main",
        '--source-digest "$pinned_commit"',
        "--deny-self-hosted-runners",
    ):
        assert option in harness[verify : harness.index("--format json", verify)]
    assert (
        "builder_workflow=andre-motta/tongs/.github/workflows/rpm-builder-image.yml\n"
    ) in harness


def test_verifier_python_reason_stage_replays_retained_dnf5_output(
    tmp_path: Path,
) -> None:
    installer = (PACKAGING / "install_and_verify.sh").read_text()
    function_start = installer.index("record_verifier_python_reason() {")
    function_end = installer.index("\nassert_tongs_import_absent() {", function_start)
    function_source = installer[function_start:function_end]
    retained_before = (
        Path(__file__).with_name("fixtures")
        / "verifier-python-reason-before-6e109a8.txt"
    ).read_bytes()
    assert hashlib.sha256(retained_before).hexdigest() == (
        "902790265bb6626c5aa77f04db75cf8a8a491da92fa48e7362f4b157b4865f5a"
    )
    assert retained_before == b"Dependency\n"

    expected_nevra = "python3|0|3.14.7|1.fc44|x86_64"

    def run_case(
        name: str,
        after: bytes,
        *,
        after_nevra: str = expected_nevra,
    ) -> subprocess.CompletedProcess[str]:
        case_dir = tmp_path / name
        case_dir.mkdir()
        before_path = case_dir / "reason-before.txt"
        after_path = case_dir / "reason-after.txt"
        mark_state = case_dir / "marked"
        before_path.write_bytes(retained_before)
        after_path.write_bytes(after)
        script = f"""
set -e
evidence_dir=$EVIDENCE_DIR
rpm() {{
    [[ $1 == -q && $2 == python3 ]]
    if [[ -e $MARK_STATE ]]; then
        printf '%s\n' "$RPM_AFTER"
    else
        printf '%s\n' "$RPM_BEFORE"
    fi
}}
cmp() {{
    [[ $# -eq 2 ]]
    "$TEST_PYTHON" -E -P -c \
        'from pathlib import Path; import sys; raise SystemExit(Path(sys.argv[1]).read_bytes() != Path(sys.argv[2]).read_bytes())' \
        "$1" "$2"
}}
dnf() {{
    if [[ $1 == repoquery ]]; then
        if [[ -e $MARK_STATE ]]; then
            cat -- "$REASON_AFTER"
        else
            cat -- "$REASON_BEFORE"
        fi
    elif [[ $# -eq 4 && $1 == --assumeyes && $2 == mark && $3 == user && $4 == python3 ]]; then
        : >"$MARK_STATE"
        printf 'Package python3 marked as user installed.\n'
    else
        return 2
    fi
}}
assert_sentinels() {{
    [[ $1 == verifier-python-mark ]]
}}
{function_source}
retain_verifier_python
"""
        return subprocess.run(
            ["bash", "-c", script],
            env={
                **os.environ,
                "EVIDENCE_DIR": str(case_dir),
                "MARK_STATE": str(mark_state),
                "REASON_AFTER": str(after_path),
                "REASON_BEFORE": str(before_path),
                "RPM_AFTER": after_nevra,
                "RPM_BEFORE": expected_nevra,
                "TEST_PYTHON": sys.executable,
            },
            text=True,
            check=False,
            capture_output=True,
        )

    for name, after in (("title-user", b"User\n"), ("lower-user", b"user\n")):
        result = run_case(name, after)
        assert result.returncode == 0, result.stderr
        case_dir = tmp_path / name
        assert (case_dir / "verifier-python-reason-before.txt").read_bytes() == (
            retained_before
        )
        assert (case_dir / "after-mark-verifier-python-reason.txt").read_bytes() == (
            after
        )
        assert (
            case_dir / "after-mark-verifier-python-reason-normalized.txt"
        ).read_text() == "user\n"
        assert (case_dir / "verifier-python-nevra.txt").read_bytes() == (
            case_dir / "after-mark-verifier-python-nevra.txt"
        ).read_bytes()

    for name, after in (
        ("dependency", b"Dependency\n"),
        ("substring", b"Superuser\n"),
        ("nonword", b"User account\n"),
        ("empty", b"\n"),
        ("hidden-trailing", b"User\ntrailing"),
    ):
        result = run_case(name, after)
        assert result.returncode == 1
        assert "verifier Python reason" in result.stderr

    changed_nevra = run_case(
        "changed-nevra",
        b"User\n",
        after_nevra="python3|0|3.14.8|1.fc44|x86_64",
    )
    assert changed_nevra.returncode == 1


def _run_dependency_negative_stage(
    evidence_dir: Path, dnf_log: Path
) -> subprocess.CompletedProcess[str]:
    """Replay the installer's dependency-negative stage with ``dnf_log`` as DNF output."""

    installer = (PACKAGING / "install_and_verify.sh").read_text()
    marker = '"$evidence_dir/dnf-failed-upgrade.log"'
    marker_index = installer.index(marker)
    stage_start = installer.rindex("set +e\n", 0, marker_index)
    stage_end = installer.index("\nassert_sentinels dependency-failure", marker_index)
    stage = installer[stage_start:stage_end]
    script = (
        """
set -e
evidence_dir=$EVIDENCE_DIR
final_desktop=/final-tongs-desktop.rpm
dnf_transaction_options=(
    --assumeyes
    --setopt=install_weak_deps=False
    --setopt=tsflags=
)
dnf() {
    if [[ $# -eq 7 && $1 == upgrade && $2 == --best \
        && $3 == --assumeyes && $4 == --setopt=install_weak_deps=False \
        && $5 == --setopt=tsflags= && $6 == '--disablerepo=*' \
        && $7 == "$final_desktop" ]]; then
        cat -- "$RETAINED_LOG"
        return 1
    fi
    printf 'unexpected dependency-negative DNF argv\n' >&2
    return 2
}
rpm() {
    [[ $# -eq 2 && $1 == -K && $2 == "$final_desktop" ]]
    printf 'digests signatures OK\n'
}
"""
        + stage
    )
    return subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "EVIDENCE_DIR": str(evidence_dir),
            "RETAINED_LOG": str(dnf_log),
        },
        text=True,
        check=False,
        capture_output=True,
    )


def test_dependency_negative_forces_dnf5_to_reject_retained_broken_update(
    tmp_path: Path,
) -> None:
    retained_log = (
        Path(__file__).with_name("fixtures") / "dnf-failed-upgrade-807d29e.log"
    )
    retained = retained_log.read_bytes()
    assert hashlib.sha256(retained).hexdigest() == (
        "f64d7a16871df211625950a3e3fe8a6767455cb9151396af284c2d6fe7d4223f"
    )
    assert b"nothing provides python3-tongs = " in retained
    assert b"Skipping packages with broken dependencies:" in retained
    assert retained.endswith(b"Nothing to do.\n")

    result = _run_dependency_negative_stage(tmp_path, retained_log)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "dnf-failed-upgrade.log").read_bytes() == retained
    assert (tmp_path / "exact-dependency-negative-classification.txt").read_text() == (
        "rpm -K: candidate integrity accepted\ndnf: exact core dependency rejection\n"
    )


def test_dependency_negative_classifies_a_bare_nothing_provides_failure(
    tmp_path: Path,
) -> None:
    # Only the "nothing provides" line names the dependency failure here: the
    # retained log also says "cannot install", so it alone cannot pin the
    # pattern.
    dnf_log = tmp_path / "nothing-provides.log"
    dnf_log.write_text(
        "Failed to resolve the transaction:\n"
        " - nothing provides python3-tongs = 1.1.0-1.fc44 needed by "
        "tongs-desktop-1.1.0-1.fc44.x86_64 from @commandline\n"
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    result = _run_dependency_negative_stage(evidence_dir, dnf_log)

    assert result.returncode == 0, result.stderr
    assert (
        evidence_dir / "exact-dependency-negative-classification.txt"
    ).read_text() == (
        "rpm -K: candidate integrity accepted\ndnf: exact core dependency rejection\n"
    )


def test_dependency_negative_refuses_an_unrelated_dnf_failure(tmp_path: Path) -> None:
    dnf_log = tmp_path / "download-failure.log"
    dnf_log.write_text(
        "Failed to download packages:\n"
        " - Curl error (6): Could not resolve host: mirrors.fedoraproject.org\n"
    )
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    result = _run_dependency_negative_stage(evidence_dir, dnf_log)

    assert result.returncode != 0
    assert not (evidence_dir / "exact-dependency-negative-classification.txt").exists()


def _run_verifier_reason_check(
    tmp_path: Path, reason: bytes
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run ``assert_verifier_python_user_reason`` on one recorded DNF reason."""

    installer = (PACKAGING / "install_and_verify.sh").read_text()
    function_start = installer.index("assert_verifier_python_user_reason() {")
    function_end = installer.index("\nassert_verifier_python() {", function_start)
    reason_path = tmp_path / "reason.txt"
    reason_path.write_bytes(reason)
    normalized = tmp_path / "reason-normalized.txt"
    script = (
        installer[function_start:function_end]
        + '\nassert_verifier_python_user_reason "$REASON" "$NORMALIZED"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "REASON": str(reason_path), "NORMALIZED": str(normalized)},
        text=True,
        check=False,
        capture_output=True,
    )
    return result, normalized


@pytest.mark.parametrize(
    "reason", [b"abc;def\n", b"User;id\n", b"User$(id)\n", b"user1\n", b"-user\n"]
)
def test_verifier_reason_must_be_one_ascii_word(tmp_path: Path, reason: bytes) -> None:
    result, normalized = _run_verifier_reason_check(tmp_path, reason)

    assert result.returncode == 1
    assert result.stderr == "lifecycle verifier Python reason is not an ASCII word\n"
    assert not normalized.exists()


def test_verifier_reason_word_is_normalized_before_the_user_check(
    tmp_path: Path,
) -> None:
    (tmp_path / "accepted").mkdir()
    accepted, accepted_normalized = _run_verifier_reason_check(
        tmp_path / "accepted", b"USER\n"
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted_normalized.read_text() == "user\n"

    (tmp_path / "rejected").mkdir()
    rejected, rejected_normalized = _run_verifier_reason_check(
        tmp_path / "rejected", b"Dependency\n"
    )
    assert rejected.returncode == 1
    assert rejected.stderr == "lifecycle verifier Python reason is not user\n"
    assert rejected_normalized.read_text() == "dependency\n"


def test_hosted_smoke_executes_complete_retained_validation_path(
    tmp_path: Path,
) -> None:
    installer = (PACKAGING / "install_and_verify.sh").read_text()
    function_start = installer.index("run_desktop_smoke() {")
    function_end = installer.index("\nrun_desktop_smoke hosted-launch", function_start)
    function_source = installer[function_start:function_end]
    assert '"$evidence_dir/$name.stdout" "$evidence_dir/$name.stderr"' in (
        function_source
    )

    encoded_retained = (
        Path(__file__).with_name("fixtures") / "hosted-launch-079059e.stdout.b64"
    ).read_bytes()
    assert encoded_retained.endswith(b"\n")
    retained = base64.b64decode(encoded_retained[:-1], validate=True)
    assert hashlib.sha256(retained).hexdigest() == (
        "4c8ac97b098c0d80ada5632acb062b7d5d8dafb5482ea64d6516f11019d955ea"
    )

    def run_case(
        name: str, stdout: bytes, stderr: bytes, status: int
    ) -> subprocess.CompletedProcess[str]:
        case_dir = tmp_path / name
        case_dir.mkdir()
        source_stdout = case_dir / "source.stdout"
        source_stderr = case_dir / "source.stderr"
        source_stdout.write_bytes(stdout)
        source_stderr.write_bytes(stderr)
        script = f"""
set -e
evidence_dir=$EVIDENCE_DIR
core_version=$CORE_VERSION
runuser() {{
    cat -- "$SMOKE_STDOUT"
    cat -- "$SMOKE_STDERR" >&2
    return "$SMOKE_STATUS"
}}
{function_source}
run_desktop_smoke candidate
"""
        result = subprocess.run(
            ["bash", "-c", script],
            env={
                **os.environ,
                "CORE_VERSION": "0.4.2.dev267+g079059eb96",
                "EVIDENCE_DIR": str(case_dir),
                "SMOKE_STDOUT": str(source_stdout),
                "SMOKE_STDERR": str(source_stderr),
                "SMOKE_STATUS": str(status),
            },
            text=True,
            check=False,
            capture_output=True,
        )
        assert (case_dir / "candidate.exit-status").read_text() == f"{status}\n"
        return result

    retained_result = run_case("retained", retained, b"", 124)
    assert retained_result.returncode == 0, retained_result.stderr
    assert "desktop-smoke-candidate: passed" in retained_result.stdout

    reverse_result = run_case("reverse", b"", retained, 124)
    assert reverse_result.returncode == 0, reverse_result.stderr

    exec_line = next(
        line
        for line in retained.splitlines(keepends=True)
        if line.startswith(b"+ exec ")
    )
    missing_result = run_case("missing", retained.replace(exec_line, b""), b"", 124)
    assert missing_result.returncode == 1
    assert "launcher exec vector missing" in missing_result.stderr

    wrong_argv_result = run_case(
        "wrong-argv",
        retained.replace(
            b"--tongs-python-executable /usr/bin/python3",
            b"--tongs-python-executable /usr/bin/python",
        ),
        b"",
        124,
    )
    assert wrong_argv_result.returncode == 1
    assert "launcher exec vector missing" in wrong_argv_result.stderr

    early_exit_result = run_case("early-exit", retained, b"", 1)
    assert early_exit_result.returncode == 1
    assert "launch did not remain live: 1" in early_exit_result.stderr

    fatal_result = run_case(
        "fatal",
        retained + b"[598:0908/183151.000000:FATAL:zygote.cc(201)] crashed\n",
        b"",
        124,
    )
    assert fatal_result.returncode == 1
    assert "fatal output classified" in fatal_result.stderr

    module_not_found = (
        b"Uncaught Exception:\n"
        b"Error [ERR_MODULE_NOT_FOUND]: Cannot find module "
        b"'.../runtime/resources/app.asar/dist/src/shared/utilities.js' imported "
        b"from .../runtime/resources/app.asar/dist/src/main/ipc.js\n"
    )
    module_not_found_result = run_case(
        "module-not-found", retained, module_not_found, 124
    )
    assert module_not_found_result.returncode == 1
    assert "fatal output classified" in module_not_found_result.stderr


def test_textual_floor_is_the_oldest_release_the_terminal_app_passes_on() -> None:
    """Every declared Textual floor is 4.0, which Fedora 44 ships.

    The terminal views use ``Content.from_markup`` with variables (Textual
    2.0), ``notify(markup=False)`` (3.1) and ``OptionList(compact=True)``
    (3.2); python3-textual on Fedora 44 is 4.0.0, so the RPM floor stays
    installable there. The Fedora 44 probe installs exactly 4.0.0, so the core
    suite runs on the floor itself and not only on the newest release. The
    PyPI range stops below the next major, which must cover the locked release
    build; raise it together with the diff render smoke tests.
    """
    pyproject = (ROOT / "pyproject.toml").read_text()
    release_inputs = (ROOT / "requirements" / "release.in").read_text()
    core_spec = (PACKAGING / "templates" / "python-tongs.spec.in").read_text()
    manifest = json.loads((PACKAGING / "manifest.json").read_text())
    dependencies = json.loads(
        (
            ROOT / "packaging" / "rpm" / "python-dependencies" / "manifest.json"
        ).read_text()
    )
    probe = (ROOT / "tests" / "containers" / "Containerfile").read_text()

    assert '"textual>=4.0,<9",' in pyproject
    release_lock = (ROOT / "requirements" / "release.lock").read_text()
    locked = re.search(r"^textual==(\d+)\.", release_lock, re.MULTILINE)
    assert locked is not None and 4 <= int(locked.group(1)) < 9
    assert re.search(r"^textual>=4\.0,<9$", release_inputs, re.MULTILINE)
    assert "BuildRequires:  python3dist(textual) >= 4\n" in core_spec
    assert "Requires:       python3dist(textual) >= 4\n" in core_spec
    assert "python3dist(textual) >= 4" in manifest["core_runtime_requirements"]
    assert {
        "requirement": "python3dist(textual) >= 4",
        "import": "textual",
    } in dependencies["system_requirements"]
    assert "        textual==4.0.0 \\\n" in probe
    assert probe.count("textual") == 1
