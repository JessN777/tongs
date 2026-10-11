"""Packaged JSON schema contract tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy

import jsonschema
import pytest

from tongs.desktop.artifact_contract import (
    ArtifactContractError,
    install_schema_bytes,
    parse_install_manifest,
    parse_release_manifest,
    release_schema_bytes,
)

from .reference_builder import FIXTURE_ROOT, canonical_json


def _references(value: object) -> list[str]:
    if isinstance(value, dict):
        return [
            reference
            for key, child in value.items()
            for reference in ([child] if key == "$ref" else _references(child))
        ]
    if isinstance(value, list):
        return [reference for child in value for reference in _references(child)]
    return []


def test_packaged_schemas_are_strict_versioned_json_documents() -> None:
    release_schema = json.loads(release_schema_bytes())
    install_schema = json.loads(install_schema_bytes())

    assert release_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert install_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert release_schema["additionalProperties"] is False
    assert install_schema["additionalProperties"] is False
    assert release_schema["properties"]["schema_version"]["const"] == 1
    assert install_schema["properties"]["schema_version"]["const"] == 1
    assert all(reference.startswith("#/") for reference in _references(release_schema))
    assert all(reference.startswith("#/") for reference in _references(install_schema))


def test_schema_examples_match_the_parser_fixture_shapes() -> None:
    release = json.loads(
        (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes()
    )
    install = json.loads((FIXTURE_ROOT / "desktop-install.synthetic.json").read_bytes())
    release_schema = json.loads(release_schema_bytes())
    install_schema = json.loads(install_schema_bytes())

    assert set(release) == set(release_schema["required"])
    assert set(install) == set(install_schema["required"])


def test_each_packaged_schema_validates_its_fixture_standalone() -> None:
    fixtures_and_schemas = (
        (
            json.loads(
                (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes()
            ),
            json.loads(release_schema_bytes()),
        ),
        (
            json.loads((FIXTURE_ROOT / "desktop-install.synthetic.json").read_bytes()),
            json.loads(install_schema_bytes()),
        ),
    )
    for fixture, schema in fixtures_and_schemas:
        validator_type = jsonschema.validators.validator_for(schema)
        validator_type.check_schema(schema)
        validator_type(schema).validate(fixture)


@pytest.mark.parametrize("invalid_version", ["1.2.3-01", "1٢.2.3", "1.2٢.3", "1.2.3٣"])
@pytest.mark.parametrize(
    ("document_name", "schema_bytes", "parser"),
    [
        (
            "desktop-manifest-v1.synthetic.json",
            release_schema_bytes,
            parse_release_manifest,
        ),
        (
            "desktop-install.synthetic.json",
            install_schema_bytes,
            parse_install_manifest,
        ),
    ],
)
def test_semver_schema_and_parser_both_reject_invalid_numeric_components(
    invalid_version: str,
    document_name: str,
    schema_bytes: Callable[[], bytes],
    parser: Callable[[bytes], object],
) -> None:
    invalid = json.loads((FIXTURE_ROOT / document_name).read_bytes())
    invalid["release_version"] = invalid_version
    schema = json.loads(schema_bytes())
    validator_type = jsonschema.validators.validator_for(schema)
    with pytest.raises(jsonschema.ValidationError):
        validator_type(schema).validate(invalid)
    with pytest.raises(ArtifactContractError):
        parser(canonical_json(invalid))


def test_release_schema_matches_package_kind_ownership_and_name_rules() -> None:
    schema = json.loads(release_schema_bytes())
    validator_type = jsonschema.validators.validator_for(schema)
    release = json.loads(
        (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes()
    )
    for fields in (
        {"ownership": "system"},
        {"name": "desktop.zip"},
        {"package_kind": "rpm", "ownership": "system", "name": "desktop.tar.gz"},
    ):
        invalid = deepcopy(release)
        invalid["artifacts"][0].update(fields)
        with pytest.raises(jsonschema.ValidationError):
            validator_type(schema).validate(invalid)


def _rpm_release(ownership: str) -> dict[str, object]:
    release = json.loads(
        (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes()
    )
    release["artifacts"][0].update(
        {
            "artifact_id": "fedora-44-x86_64-rpm",
            "name": "tongs-desktop-1.2.3-1.fc44.x86_64.rpm",
            "package_kind": "rpm",
            "ownership": ownership,
        }
    )
    return release


def test_release_schema_and_parser_accept_a_system_owned_rpm() -> None:
    schema = json.loads(release_schema_bytes())
    validator_type = jsonschema.validators.validator_for(schema)
    release = _rpm_release("system")

    validator_type(schema).validate(release)
    parsed = parse_release_manifest(canonical_json(release))

    assert parsed.artifacts[0].ownership.value == "system"


def test_release_schema_and_parser_reject_a_per_user_rpm() -> None:
    schema = json.loads(release_schema_bytes())
    validator_type = jsonschema.validators.validator_for(schema)
    release = _rpm_release("per-user")

    with pytest.raises(jsonschema.ValidationError) as raised:
        validator_type(schema).validate(release)
    assert list(raised.value.absolute_path) == ["artifacts", 0, "ownership"]
    assert raised.value.validator == "const"
    with pytest.raises(ArtifactContractError):
        parse_release_manifest(canonical_json(release))
