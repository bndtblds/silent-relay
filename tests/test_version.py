from pathlib import Path

import pytest

from app.entitlements import (
    ENTITLEMENT_PROVIDER_CONTRACT_VERSION,
    AllowAllEntitlementProvider,
)
from app.version import __version__
from scripts.check_version import (
    VersionCheckError,
    check_repository_version,
    is_newer,
    parse_version,
    validate_release_tags,
    validate_version_transition,
    version_from_tag,
)


ROOT = Path(__file__).resolve().parents[1]


def test_project_uses_canonical_version_source():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'dynamic = ["version"]' in pyproject
    assert 'path = "app/version.py"' in pyproject
    assert __version__ == "1.5.5"


def test_repository_version_transition_is_valid():
    current, previous = check_repository_version()

    assert current == __version__
    assert not is_newer(parse_version(previous), parse_version(current))


@pytest.mark.parametrize(
    "changed_paths",
    [
        ["README.md"],
        ["docs/VERSIONING.md"],
        ["SECURITY.md", "CONTRIBUTING.md"],
    ],
)
def test_non_product_changes_may_keep_the_current_version(changed_paths):
    validate_version_transition("1.5.3", "1.5.3", changed_paths)


@pytest.mark.parametrize(
    "changed_paths",
    [
        ["app/main.py"],
        ["README.md", "app/main.py"],
        [".github/workflows/tests.yml"],
    ],
)
def test_product_relevant_changes_require_a_newer_version(changed_paths):
    with pytest.raises(VersionCheckError, match="must be newer"):
        validate_version_transition("1.5.3", "1.5.3", changed_paths)


def test_product_relevant_change_accepts_a_newer_version():
    validate_version_transition("1.5.4", "1.5.3", ["app/main.py"])


def test_version_regression_is_rejected_even_for_documentation():
    with pytest.raises(VersionCheckError, match="must not be older"):
        validate_version_transition("1.5.2", "1.5.3", ["README.md"])


@pytest.mark.parametrize("value", ["1.0", "01.0.0", "1.0.0-01", "v1.0.0"])
def test_invalid_semantic_versions_are_rejected(value):
    with pytest.raises(VersionCheckError):
        parse_version(value)


def test_semantic_version_precedence():
    assert is_newer(parse_version("1.1.0"), parse_version("1.0.2"))
    assert is_newer(parse_version("2.0.0"), parse_version("2.0.0-rc.1"))
    assert not is_newer(parse_version("1.0.0+build.2"), parse_version("1.0.0+build.1"))


def test_release_tag_contains_the_exact_semantic_version():
    assert version_from_tag("v1.1.0") == "1.1.0"
    with pytest.raises(VersionCheckError):
        version_from_tag("release-1.1.0")


def test_release_tag_must_match_the_canonical_version():
    with pytest.raises(VersionCheckError, match="does not match"):
        validate_release_tags("1.3.8", ["v1.3.7"], ["v1.3.7"])


def test_release_version_must_advance_past_existing_release_tags():
    validate_release_tags("1.3.8", ["v1.3.8"], ["v1.3.7", "v1.3.8"])

    with pytest.raises(VersionCheckError, match="must be newer"):
        validate_release_tags("1.3.8", ["v1.3.8"], ["v1.3.8", "v1.4.0"])


def test_ordinary_commit_may_retain_the_current_release_version():
    validate_release_tags("1.3.8", [], ["v1.3.8"])


def test_entitlement_provider_contract_is_version_one():
    assert ENTITLEMENT_PROVIDER_CONTRACT_VERSION == 1
    assert (
        AllowAllEntitlementProvider.contract_version
        == ENTITLEMENT_PROVIDER_CONTRACT_VERSION
    )
