from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "python-dependencies"

#: The only containers that may reach the network: the provider audit and the
#: source download before the build, and the clean install from Fedora.
NETWORKED_PROGRAMS = {
    "audit_providers.py",
    "prepare_sources.py",
    "install_and_verify.sh",
}


def _podman_runs(script: str) -> list[list[str]]:
    """Return each ``podman run`` command, continuation lines joined, as words."""

    joined = script.replace("\\\n", " ")
    return [line.split() for line in joined.splitlines() if "podman run" in line]


def _program(run: list[str]) -> str:
    return next(
        Path(word).name
        for word in run
        if word.startswith("/checkout/") and word.endswith((".py", ".sh"))
    )


def test_every_companion_has_one_reviewable_spec() -> None:
    manifest = json.loads((PACKAGING / "manifest.json").read_text())
    expected = {f"python-{name}.spec" for name in manifest["build_order"]}
    actual = {path.name for path in (PACKAGING / "specs").glob("*.spec")}

    assert actual == expected


def test_hosted_containers_build_offline_without_new_privileges() -> None:
    harness = (PACKAGING / "run_hosted.sh").read_text()
    rebuilder = (PACKAGING / "rebuild_srpms.sh").read_text()
    installer = (PACKAGING / "install_and_verify.sh").read_text()

    assert "RUNNER_ENVIRONMENT:-} == github-hosted" in harness
    runs = _podman_runs(harness)
    assert runs
    for run in runs:
        assert "--security-opt=no-new-privileges" in run, _program(run)
    networked = {_program(run) for run in runs if "--network=none" not in run}
    assert networked == NETWORKED_PROGRAMS
    # Every source RPM rebuild runs without a network.
    rebuilds = _podman_runs(rebuilder)
    assert rebuilds
    for run in rebuilds:
        assert "--network=none" in run, " ".join(run)
    # The clean install takes packages from RPMs only, never from PyPI.
    assert "pip install" not in installer


def test_only_the_clean_install_keeps_the_capabilities_dnf_needs() -> None:
    """``dnf`` installs packages as root and needs the default capabilities.

    Every other container drops all of them, so a ``--cap-drop=all`` copied
    onto the clean install would break it, and one removed elsewhere would
    widen what a build step may do.
    """

    harness = (PACKAGING / "run_hosted.sh").read_text()
    rebuilder = (PACKAGING / "rebuild_srpms.sh").read_text()
    runs = _podman_runs(harness)

    keeps_capabilities = {_program(run) for run in runs if "--cap-drop=all" not in run}
    assert keeps_capabilities == {"install_and_verify.sh"}
    install = next(run for run in runs if _program(run) == "install_and_verify.sh")
    assert not any(word.startswith("--cap-drop") for word in install)
    for run in _podman_runs(rebuilder):
        assert "--cap-drop=all" in run, " ".join(run)


def test_the_manual_harness_builds_on_the_pinned_plain_fedora_base() -> None:
    """The manual workflow never uses the builder image.

    It builds on plain Fedora, so a companion spec that forgets a
    ``BuildRequires`` still fails here, at the base digest the builder pin
    records rather than the moving ``fedora:44`` tag.
    """

    harness = (PACKAGING / "run_hosted.sh").read_text()
    containerfile = (PACKAGING / "Containerfile").read_text()
    rebuilder = (PACKAGING / "rebuild_srpms.sh").read_text()
    for name, text in (
        ("run_hosted.sh", harness),
        ("Containerfile", containerfile),
        ("rebuild_srpms.sh", rebuilder),
    ):
        assert "fedora:44" not in text, name
    assert "--check --require-published" in harness
    assert 'base_image="registry.fedoraproject.org/fedora@$base_digest"' in harness
    assert "tongs-rpm-builder" not in harness
    assert "gh attestation" not in harness
    assert '--build-arg "BASE_IMAGE=$base_image"' in harness
    assert re.findall(r"^FROM\s+(\S+)", containerfile, re.MULTILINE) == [
        "${BASE_IMAGE}"
    ]
    assert re.findall(r"^ARG BASE_IMAGE.*$", containerfile, re.MULTILINE) == [
        "ARG BASE_IMAGE"
    ]
    assert "FROM $base_image\n" in rebuilder
