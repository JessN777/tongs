from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Self

import pytest

ROOT = Path(__file__).parents[4]
PACKAGING = ROOT / "packaging" / "rpm" / "python-dependencies"


def _load_script(name: str) -> ModuleType:
    path = PACKAGING / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tongs_rpm_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_providers = _load_script("audit_providers")
prepare_sources = _load_script("prepare_sources")
resolve_srpms = _load_script("resolve_srpms")
verify_install = _load_script("verify_install")

SOURCE_URL = "https://files.pythonhosted.org/source.tar.gz"


class _Response(io.BytesIO):
    """A served download whose final URL stays on the expected HTTPS host."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def geturl(self) -> str:
        return SOURCE_URL


def _source(contents: bytes) -> dict[str, object]:
    return {
        "url": SOURCE_URL,
        "filename": "source.tar.gz",
        "bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


def test_repoquery_requests_one_record_per_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(audit_providers.subprocess, "run", fake_run)

    assert audit_providers._query("python(abi) >= 3.12") == []
    query_format = captured[captured.index("--queryformat") + 1]
    assert query_format.endswith("\n")


def test_audit_rejects_missing_system_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_providers, "_query", lambda _requirement: [])
    manifest = {
        "system_requirements": [{"requirement": "python(abi) >= 3.12"}],
        "companions": [],
    }

    with pytest.raises(RuntimeError, match="missing Fedora system providers"):
        audit_providers.audit(manifest)


def test_audit_rejects_gratuitous_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = {
        "name": "python3-sigstore",
        "epoch": "0",
        "version": "4.5.0",
        "release": "1.fc44",
        "arch": "noarch",
        "repo": "updates",
    }
    monkeypatch.setattr(audit_providers, "_query", lambda _requirement: [provider])
    manifest = {
        "system_requirements": [],
        "companions": [{"distribution": "sigstore", "version": "4.5.0"}],
    }

    with pytest.raises(RuntimeError, match="reuse them"):
        audit_providers.audit(manifest)


def test_safe_extract_rejects_parent_path(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("../escape")
        contents = b"bad"
        member.size = len(contents)
        archive.addfile(member, io.BytesIO(contents))

    with pytest.raises(RuntimeError, match="unsafe source member"):
        prepare_sources._safe_extract(archive_path, tmp_path / "output")


def test_prepare_refuses_nonempty_evidence_directory(tmp_path: Path) -> None:
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "old.json").write_text("{}")

    with pytest.raises(RuntimeError, match="must be empty"):
        prepare_sources.prepare({"companions": []}, output)


def test_cargo_vendor_config_is_relocatable() -> None:
    vendor = Path("/tmp/preparation/source/vendor")
    config = f'[source.vendored-sources]\ndirectory = "{vendor}"\n'

    result = prepare_sources._relativize_vendor_config(config, vendor)

    assert result == '[source.vendored-sources]\ndirectory = "vendor"\n'
    assert str(vendor) not in result


@pytest.mark.parametrize(
    "final_url",
    [
        "http://files.pythonhosted.org/source.tar.gz",
        "https://example.com/source.tar.gz",
    ],
)
def test_download_redirect_requires_same_https_host(final_url: str) -> None:
    with pytest.raises(RuntimeError, match="outside HTTPS host"):
        prepare_sources._validate_download_url(
            "https://files.pythonhosted.org/source.tar.gz", final_url
        )


def test_download_accepts_same_https_host() -> None:
    prepare_sources._validate_download_url(
        "https://files.pythonhosted.org/source.tar.gz",
        "https://files.pythonhosted.org/redirected/source.tar.gz",
    )


def test_download_retries_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = b"source"
    attempts = 0

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def geturl(self) -> str:
            return "https://files.pythonhosted.org/source.tar.gz"

    def urlopen(*_args: object, **_kwargs: object) -> Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionResetError("reset")
        return Response(contents)

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(prepare_sources.time, "sleep", lambda _seconds: None)
    output = tmp_path / "source.tar.gz"
    prepare_sources._download(
        {
            "url": "https://files.pythonhosted.org/source.tar.gz",
            "filename": output.name,
            "bytes": len(contents),
            "sha256": prepare_sources.hashlib.sha256(contents).hexdigest(),
        },
        output,
    )

    assert attempts == 2
    assert output.read_bytes() == contents


def test_download_rejects_bytes_that_differ_from_the_pinned_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = b"forged"

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def geturl(self) -> str:
            return "https://files.pythonhosted.org/source.tar.gz"

    monkeypatch.setattr(
        prepare_sources.urllib.request, "urlopen", lambda *_a, **_k: Response(served)
    )
    output = tmp_path / "source.tar.gz"
    # The declared size matches, so only the pinned hash can reject the bytes.
    with pytest.raises(RuntimeError, match="source hash mismatch"):
        prepare_sources._download(
            {
                "url": "https://files.pythonhosted.org/source.tar.gz",
                "filename": output.name,
                "bytes": len(served),
                "sha256": prepare_sources.hashlib.sha256(b"source").hexdigest(),
            },
            output,
        )


def test_download_retries_server_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = b"source"
    attempts = 0
    sleeps: list[int] = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def geturl(self) -> str:
            return "https://files.pythonhosted.org/source.tar.gz"

    def urlopen(*_args: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(
                "https://files.pythonhosted.org/source.tar.gz",
                503,
                "Service Unavailable",
                {},
                None,
            )
        return Response(contents)

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(prepare_sources.time, "sleep", sleeps.append)
    output = tmp_path / "source.tar.gz"
    prepare_sources._download(
        {
            "url": "https://files.pythonhosted.org/source.tar.gz",
            "filename": output.name,
            "bytes": len(contents),
            "sha256": prepare_sources.hashlib.sha256(contents).hexdigest(),
        },
        output,
    )

    assert attempts == 3
    assert sleeps == [1, 2]
    assert output.read_bytes() == contents


def test_download_does_not_retry_client_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def urlopen(*_args: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            "https://files.pythonhosted.org/missing.tar.gz", 404, "Not Found", {}, None
        )

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        prepare_sources._download(
            {
                "url": "https://files.pythonhosted.org/missing.tar.gz",
                "filename": "missing.tar.gz",
                "bytes": 1,
                "sha256": "unused",
            },
            tmp_path / "missing.tar.gz",
        )

    assert exc_info.value.code == 404
    assert attempts == 1


def test_download_raises_last_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors = [urllib.error.URLError(f"failure-{attempt}") for attempt in range(3)]

    def urlopen(*_args: object, **_kwargs: object) -> object:
        raise errors.pop(0)

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(prepare_sources.time, "sleep", lambda _seconds: None)

    with pytest.raises(urllib.error.URLError, match="failure-2"):
        prepare_sources._download(
            {
                "url": "https://files.pythonhosted.org/source.tar.gz",
                "filename": "source.tar.gz",
                "bytes": 1,
                "sha256": "unused",
            },
            tmp_path / "source.tar.gz",
        )

    assert errors == []


def test_cargo_package_identity_excludes_temporary_path_id() -> None:
    package = {
        "id": "path+file:///tmp/random/rfc3161#1.0.8",
        "name": "rfc3161-client",
        "version": "1.0.8",
        "source": None,
    }

    assert prepare_sources._cargo_package_identity(package) == (
        "rfc3161-client",
        "1.0.8",
        None,
    )


def test_resolve_srpms_uses_exact_names_with_prefix_overlap(tmp_path: Path) -> None:
    filenames = [
        "python-sigstore-4.5.0-1.fc44.src.rpm",
        "python-sigstore-models-0.0.6-1.fc44.src.rpm",
        "python-sigstore-rekor-types-0.0.18-1.fc44.src.rpm",
    ]
    for filename in filenames:
        (tmp_path / filename).touch()
    metadata = (
        "python-sigstore|4.5.0|1.fc44|noarch|"
        "python-sigstore-4.5.0-1.fc44.src.rpm\n"
        "python-sigstore-models|0.0.6|1.fc44|noarch|"
        "python-sigstore-models-0.0.6-1.fc44.src.rpm\n"
        "python-sigstore-rekor-types|0.0.18|1.fc44|noarch|"
        "python-sigstore-rekor-types-0.0.18-1.fc44.src.rpm"
    )

    resolved = resolve_srpms.resolve_srpms(
        ["sigstore", "sigstore-models", "sigstore-rekor-types"],
        metadata,
        tmp_path,
    )

    assert [path.name for _, path in resolved] == filenames


def test_resolve_srpm_uses_queried_filename_with_binary_header_arch(
    tmp_path: Path,
) -> None:
    filename = "python-rfc3161-client-1.0.8-1.fc44.src.rpm"
    (tmp_path / filename).touch()

    resolved = resolve_srpms.resolve_srpms(
        ["rfc3161-client"],
        "python-rfc3161-client|1.0.8|1.fc44|x86_64|"
        "python-rfc3161-client-1.0.8-1.fc44.src.rpm",
        tmp_path,
    )

    assert resolved == [("rfc3161-client", tmp_path / filename)]


def test_resolve_srpm_rejects_filename_traversal(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="unsafe SRPM filename"):
        resolve_srpms.resolve_srpms(
            ["sigstore"],
            "python-sigstore|4.5.0|1.fc44|noarch|../source.src.rpm",
            tmp_path,
        )


@pytest.mark.parametrize(
    "config",
    [
        # A second source still names a path under the absolute vendor tree.
        (
            '[source.vendored-sources]\ndirectory = "{vendor}"\n'
            '[source.nested]\ndirectory = "{vendor}/nested"\n'
        ),
        # The vendor directory is spelled differently, so nothing is rewritten.
        "[source.vendored-sources]\ndirectory = '{vendor}'\n",
    ],
    ids=["leftover-absolute-path", "unrecognized-directory"],
)
def test_cargo_vendor_config_rejects_a_leftover_absolute_path(config: str) -> None:
    vendor = Path("/tmp/preparation/source/vendor")

    with pytest.raises(RuntimeError, match="expected directory"):
        prepare_sources._relativize_vendor_config(config.format(vendor=vendor), vendor)


def test_download_raises_a_server_error_on_the_final_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    sleeps: list[int] = []

    def urlopen(*_args: object, **_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(
            SOURCE_URL, 500 + attempts, "Server Error", {}, None
        )

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(prepare_sources.time, "sleep", sleeps.append)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        prepare_sources._download(_source(b"source"), tmp_path / "source.tar.gz")

    # The third server error is the one raised; it is never swallowed.
    assert exc_info.value.code == 503
    assert attempts == 3
    assert sleeps == [1, 2]


def test_download_stops_when_the_body_exceeds_the_declared_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declared = b"source"
    attempts = 0

    def urlopen(*_args: object, **_kwargs: object) -> _Response:
        nonlocal attempts
        attempts += 1
        return _Response(declared + b"!")

    monkeypatch.setattr(prepare_sources.urllib.request, "urlopen", urlopen)
    output = tmp_path / "source.tar.gz"

    with pytest.raises(RuntimeError, match="exceeds declared size"):
        prepare_sources._download(_source(declared), output)

    # An oversized body is a hard failure, not a transient one to retry.
    assert attempts == 1
    assert output.read_bytes() == b""


def _verify_cargo_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packages: list[tuple[str, bool]],
) -> tuple[dict[str, object], Path]:
    """Stage an installed Cargo inventory for ``verify_install.verify``.

    Each package is ``(name, resolved_for_fedora_x86_64)``. Resolved packages
    get a bundled license file whose hash matches the inventory, so only the
    vendored OpenSSL gate decides the outcome before the Sigstore fixture
    check, whose retained fixtures are deliberately absent.
    """

    manifest = json.loads((PACKAGING / "manifest.json").read_text())
    versions = {
        item["distribution"]: item["version"] for item in manifest["companions"]
    }
    monkeypatch.setattr(
        verify_install,
        "importlib",
        SimpleNamespace(
            import_module=lambda name: SimpleNamespace(__file__=f"/{name}.so"),
            metadata=SimpleNamespace(version=versions.__getitem__),
        ),
    )
    license_root = tmp_path / "licenses"
    monkeypatch.setattr(verify_install, "CARGO_LICENSE_ROOT", license_root)
    records = []
    for name, resolved in packages:
        bundled = []
        if resolved:
            license_path = license_root / "cargo" / f"{name}-0.1.0" / "LICENSE"
            license_path.parent.mkdir(parents=True)
            license_path.write_bytes(f"license for {name}\n".encode())
            bundled.append(
                {
                    "path": f"{name}-0.1.0/LICENSE",
                    "sha256": hashlib.sha256(license_path.read_bytes()).hexdigest(),
                }
            )
        records.append(
            {
                "name": name,
                "version": "0.1.0",
                "resolved_for_fedora_x86_64": resolved,
                "bundled_license_files": bundled,
            }
        )
    rust = next(
        item
        for item in manifest["companions"]
        if item["distribution"] == "rfc3161-client"
    )
    license_root.mkdir(exist_ok=True)
    (license_root / "cargo-inventory.json").write_text(
        json.dumps(
            {
                "upstream_cargo_lock_sha256": rust["cargo"]["lock_sha256"],
                "packages": records,
            }
        )
    )
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    for name in (
        "sigstore-python-4.5.0.intoto.sigstore.json",
        "sigstore-production-client-trust-config.json",
    ):
        (fixture_dir / name).write_text("not the retained fixture\n")
    return manifest, fixture_dir


def test_verify_install_rejects_vendored_openssl_in_the_resolved_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, fixture_dir = _verify_cargo_inventory(
        tmp_path, monkeypatch, [("openssl", True), ("openssl-src", True)]
    )

    with pytest.raises(RuntimeError, match="vendored OpenSSL"):
        verify_install.verify(manifest, fixture_dir)


def test_verify_install_accepts_system_openssl_and_retained_vendor_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The system ``openssl`` binding is resolved and ``openssl-src`` is only
    # retained vendor source, so the OpenSSL gate passes and verification
    # reaches the next stage: the retained Sigstore fixture hashes.
    manifest, fixture_dir = _verify_cargo_inventory(
        tmp_path, monkeypatch, [("openssl", True), ("openssl-src", False)]
    )

    with pytest.raises(RuntimeError, match="retained Sigstore fixture hash mismatch"):
        verify_install.verify(manifest, fixture_dir)


_FAKE_PODMAN = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$PODMAN_LOG"
option() {
    local name=$1
    shift
    while (($#)); do
        if [[ $1 == "$name" ]]; then
            printf '%s\n' "$2"
            return
        fi
        shift
    done
    return 1
}
case $1 in
    run)
        if [[ " $* " == *" --entrypoint rpm "* ]]; then
            srpm=$(basename -- "${@: -1}")
            # The header name, not the file name, identifies each SRPM.
            case $srpm in
                python-foo-bar-*) printf 'python-foo-bar|2.0|1.fc44|noarch' ;;
                python-foo-*) printf 'python-foo|1.0|1.fc44|noarch' ;;
                *) exit 3 ;;
            esac
        elif [[ " $* " == *"/rebuild_one_srpm.sh "* ]]; then
            output=""
            for argument in "$@"; do
                if [[ $argument == *:/output:* ]]; then
                    output=${argument%%:/output:*}
                fi
            done
            distribution=$(option --distribution "$@")
            : >"$output/python3-$distribution.rpm"
        fi
        ;;
    build)
        tag=$(option --tag "$@")
        context=${@: -1}
        printf '%s=%s\n' "$tag" "$(cat -- "$context/source.src.rpm")" >>"$BUILD_LOG"
        ;;
    image)
        printf '{}\n'
        ;;
    *)
        exit 4
        ;;
esac
"""


def test_rebuild_srpms_pairs_each_distribution_with_its_exact_srpm(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    podman = fake_bin / "podman"
    podman.write_text(_FAKE_PODMAN)
    python3 = fake_bin / "python3"
    python3.write_text('#!/bin/sh\nexec "$TEST_PYTHON" "$@"\n')
    for executable in (podman, python3):
        executable.chmod(0o755)
    srpm_dir = tmp_path / "srpms"
    srpm_dir.mkdir()
    # Both file names start with ``python-foo-``, so a name-prefix glob for
    # ``foo`` would match the ``foo-bar`` SRPM as well.
    for filename, contents in (
        ("python-foo-1.0-1.fc44.src.rpm", "python-foo"),
        ("python-foo-bar-2.0-1.fc44.src.rpm", "python-foo-bar"),
    ):
        (srpm_dir / filename).write_text(contents)
    (srpm_dir / "build-order.txt").write_text("foo-bar\nfoo\n")
    output_dir = tmp_path / "rpms"
    output_dir.mkdir()
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    build_log = tmp_path / "build.log"
    source_sha = "a" * 40

    result = subprocess.run(
        [
            "bash",
            str(PACKAGING / "rebuild_srpms.sh"),
            "--base-image",
            "registry.example/fedora@sha256:" + "0" * 64,
            "--checkout",
            str(ROOT),
            "--source-sha",
            source_sha,
            "--srpm-dir",
            str(srpm_dir),
            "--output-dir",
            str(output_dir),
        ],
        env={
            **os.environ,
            "BUILD_LOG": str(build_log),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "PODMAN_LOG": str(tmp_path / "podman.log"),
            "RUNNER_TEMP": str(runner_temp),
            "TEST_PYTHON": sys.executable,
        },
        text=True,
        check=False,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert (output_dir / "resolved-srpms.txt").read_text().splitlines() == [
        f"foo-bar|{srpm_dir / 'python-foo-bar-2.0-1.fc44.src.rpm'}",
        f"foo|{srpm_dir / 'python-foo-1.0-1.fc44.src.rpm'}",
    ]
    assert build_log.read_text().splitlines() == [
        f"localhost/tongs-python-rpm-foo-bar:{source_sha}=python-foo-bar",
        f"localhost/tongs-python-rpm-foo:{source_sha}=python-foo",
    ]
    assert sorted(path.name for path in output_dir.glob("*.rpm")) == [
        "python3-foo-bar.rpm",
        "python3-foo.rpm",
    ]
    assert list(runner_temp.iterdir()) == []
