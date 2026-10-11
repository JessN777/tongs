"""Pin the desktop formatter and linter, the CI steps that run them, and the
pre-commit hooks that mirror the lint job.

Prettier, ESLint and typescript-eslint live in their own package under
``desktop/tools/lint`` with TypeScript 6, because typescript-eslint does not
support the TypeScript 7 the app builds with.  The app's ``package.json``
only delegates to that package, and ``npm ci`` installs it from its committed
lockfile, so the versions the lint job runs are the ones this module reads.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.ci.ci_plan import classify_paths
from tests.ci.verify_desktop_production_gate import ROOT

CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
DESKTOP = ROOT / "desktop"
LINT_PACKAGE = DESKTOP / "tools/lint"
PACKAGE_JSON = LINT_PACKAGE / "package.json"
PACKAGE_LOCK = LINT_PACKAGE / "package-lock.json"
ESLINT_CONFIG = LINT_PACKAGE / "eslint.config.mjs"
PRETTIER_IGNORE = DESKTOP / ".prettierignore"
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"
REGISTRY = "https://registry.npmjs.org/"
TOOLS = (
    "@eslint/js",
    "eslint",
    "globals",
    "prettier",
    "typescript",
    "typescript-eslint",
)
#: The desktop scripts and the lint package scripts they delegate to.  Every
#: lint package script runs from desktop/, the root its configuration names.
SCRIPTS = {
    "format": "prettier --write .",
    "format:check": "prettier --check .",
    "lint": "eslint --config tools/lint/eslint.config.mjs --max-warnings 0",
}
INSTALL = "npm ci --prefix desktop/tools/lint --ignore-scripts --no-audit --no-fund"
FORMAT_CHECK = "npm run format:check --prefix desktop"
LINT = "npm run lint --prefix desktop"
CONTROL_CHECK = "scripts/check_control_chars.py"
_EXACT = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def ci_jobs() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]


def _runs(job: dict[str, Any]) -> list[str]:
    return [" ".join(step["run"].split()) for step in job["steps"] if "run" in step]


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


# The pinned lint package.


def test_the_lint_package_pins_every_tool_exactly_and_matches_its_lockfile() -> None:
    package = _json(PACKAGE_JSON)
    assert package["private"] is True
    assert "dependencies" not in package
    pins = package["devDependencies"]
    assert tuple(sorted(pins)) == TOOLS
    assert all(_EXACT.fullmatch(version) for version in pins.values()), pins
    lock = _json(PACKAGE_LOCK)
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["devDependencies"] == pins
    for name, version in pins.items():
        assert lock["packages"][f"node_modules/{name}"]["version"] == version, name


def test_every_locked_package_is_registry_resolved_with_integrity() -> None:
    packages = {
        name: entry
        for name, entry in _json(PACKAGE_LOCK)["packages"].items()
        if name != ""
    }
    assert packages
    for name, entry in packages.items():
        assert entry["resolved"].startswith(REGISTRY), name
        assert entry["integrity"].startswith("sha512-"), name


def test_the_lint_typescript_is_one_typescript_eslint_supports() -> None:
    """typescript-eslint's peer range is what keeps this TypeScript apart from
    the app's; a Dependabot bump outside it must fail here, not in the job."""

    lock = _json(PACKAGE_LOCK)["packages"]
    peer = lock["node_modules/typescript-eslint"]["peerDependencies"]["typescript"]
    match = re.fullmatch(r">=(\S+) <(\S+)", peer)
    assert match, f"unrecognised typescript-eslint peer range {peer!r}"
    typescript = _version(lock["node_modules/typescript"]["version"])
    assert _version(match.group(1)) <= typescript < _version(match.group(2))


def test_the_app_keeps_its_own_toolchain_and_only_delegates() -> None:
    package = _json(DESKTOP / "package.json")
    dependencies = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
    }
    assert not set(dependencies) & (set(TOOLS) - {"typescript"})
    assert dependencies["typescript"].startswith("7.")
    lint_scripts = _json(PACKAGE_JSON)["scripts"]
    assert set(lint_scripts) == set(SCRIPTS)
    for name, command in SCRIPTS.items():
        assert package["scripts"][name] == f"npm --prefix tools/lint run {name}"
        assert lint_scripts[name] == f"cd ../.. && {command}"
    # The tests stay bounded by default, one test file at a time.
    assert "node --test --test-concurrency=1 " in package["scripts"]["test"]


def test_prettier_runs_with_its_defaults_and_skips_generated_files() -> None:
    for directory in (ROOT, DESKTOP, LINT_PACKAGE):
        assert not list(directory.glob(".prettierrc*")), directory
        assert not list(directory.glob("prettier.config.*")), directory
        assert not (directory / ".editorconfig").exists(), directory
    for package in (ROOT / "package.json", DESKTOP / "package.json", PACKAGE_JSON):
        if package.exists():
            assert "prettier" not in _json(package), package
    entries = [
        line
        for line in PRETTIER_IGNORE.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert entries == ["dist/", "node_modules/", "package-lock.json"]


def test_eslint_uses_the_recommended_configs_without_type_information() -> None:
    text = ESLINT_CONFIG.read_text()
    assert "js.configs.recommended" in text
    assert "tseslint.configs.recommended" in text
    for type_aware in ("TypeChecked", "projectService", "project:", "strict"):
        assert type_aware not in text, type_aware
    assert "rules:" not in text, "disable a rule inline, with a one-line reason"


# The CI steps.


def test_the_lint_job_installs_the_lint_package_then_checks_the_desktop(
    ci_jobs: dict[str, Any],
) -> None:
    job = ci_jobs["lint-and-format"]
    runs = _runs(job)
    assert runs.count(INSTALL) == 1
    install = runs.index(INSTALL)
    assert install < runs.index(FORMAT_CHECK) < runs.index(LINT)
    # Node is set up the way every other Node job in ci.yml does it.
    (node,) = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    ]
    assert node["with"] == {"node-version": "22", "package-manager-cache": False}
    assert job["steps"].index(node) < [step.get("run") for step in job["steps"]].index(
        INSTALL
    )
    # The desktop app's own dependencies are not needed to lint it.
    assert not any("npm ci --prefix desktop " in f"{run} " for run in runs)


@pytest.mark.parametrize(
    ("job", "python"), [("lint-and-format", "python"), ("docs", "python3")]
)
def test_both_lint_and_docs_jobs_run_the_control_character_check(
    ci_jobs: dict[str, Any], job: str, python: str
) -> None:
    assert f"{python} {CONTROL_CHECK}" in _runs(ci_jobs[job])


def _tracked() -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return [name.decode() for name in listing.split(b"\0") if name]


def test_every_tracked_path_runs_the_control_character_check_before_merge() -> None:
    """The check reads the whole tree in the lint and docs jobs, so every
    path's plan must select one of them, or a change could merge with a NUL."""

    tracked = _tracked()
    assert CONTROL_CHECK in tracked
    unchecked = sorted(
        path for path in tracked if not {"docs", "lint"} & classify_paths([path])[0]
    )
    assert unchecked == []


def test_every_desktop_path_selects_the_lint_lane() -> None:
    """Prettier and ESLint read the whole desktop tree in the lint job."""

    desktop = [path for path in _tracked() if path.startswith("desktop/")]
    assert "desktop/tools/lint/eslint.config.mjs" in desktop
    assert "desktop/.prettierignore" in desktop
    assert [path for path in desktop if "lint" not in classify_paths([path])[0]] == []


# The pre-commit hooks.


@pytest.fixture(scope="module")
def hooks() -> dict[str, dict[str, Any]]:
    config = yaml.safe_load(PRE_COMMIT.read_text())
    (repository,) = config["repos"]
    assert repository["repo"] == "local"
    found = {hook["id"]: hook for hook in repository["hooks"]}
    assert len(found) == len(repository["hooks"])
    return found


def test_the_hooks_mirror_the_lint_job(
    hooks: dict[str, dict[str, Any]], ci_jobs: dict[str, Any]
) -> None:
    assert set(hooks) == {
        "ruff-check",
        "ruff-format",
        "control-characters",
        "desktop-format",
        "desktop-lint",
    }
    runs = _runs(ci_jobs["lint-and-format"])
    (ruff_check,) = [run for run in runs if run.startswith("ruff check ")]
    (ruff_format,) = [run for run in runs if run.startswith("ruff format --check ")]
    # Ruff comes from the active environment, whose lint extra pins it, so
    # the version is declared once.
    for hook_id, command in (("ruff-check", ruff_check), ("ruff-format", ruff_format)):
        hook = hooks[hook_id]
        assert hook["language"] == "system"
        assert "additional_dependencies" not in hook
        words = shlex.split(hook["entry"])
        assert words[:-1] == shlex.split(command)[: len(words) - 1]
        assert words[-1] == "--force-exclude"
        roots = [word.rstrip("/") for word in shlex.split(command) if "/" in word]
        assert hook["files"] == "^(" + "|".join(map(re.escape, roots)) + ")/"
    for hook_id, command in (
        ("desktop-format", FORMAT_CHECK),
        ("desktop-lint", LINT),
    ):
        hook = hooks[hook_id]
        assert hook["entry"] == command
        assert hook["pass_filenames"] is False
        assert hook["files"] == "^desktop/"


def test_the_control_character_hook_reads_every_staged_file(
    hooks: dict[str, dict[str, Any]],
) -> None:
    hook = hooks["control-characters"]
    assert hook["entry"] == f"python {CONTROL_CHECK}"
    assert hook["language"] == "python"
    # pre-commit's type detection calls a file holding NUL binary.
    for key in ("types", "types_or", "files", "exclude", "pass_filenames"):
        assert key not in hook, key
