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
    version_from_tag,
)


ROOT = Path(__file__).resolve().parents[1]


def test_project_uses_canonical_version_source():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'dynamic = ["version"]' in pyproject
    assert 'path = "app/version.py"' in pyproject
    assert __version__ == "1.4.7"


def test_pull_request_version_advances_from_main_once():
    current, previous = check_repository_version()

    assert current == __version__
    assert previous == "1.4.6"


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
