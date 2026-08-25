from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "app" / "version.py"
SEMVER_PATTERN = (
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
VERSION_PATTERN = re.compile(rf'(?m)^__version__\s*=\s*"({SEMVER_PATTERN})"\s*$')


class VersionCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()


def parse_version(value: str) -> SemVer:
    match = re.fullmatch(SEMVER_PATTERN, value)
    if match is None:
        raise VersionCheckError(f"Invalid Semantic Version: {value!r}")
    prerelease = tuple((match.group(4) or "").split(".")) if match.group(4) else ()
    for identifier in prerelease:
        if (
            identifier.isdigit()
            and len(identifier) > 1
            and identifier.startswith("0")
        ):
            raise VersionCheckError(
                f"Numeric prerelease identifiers must not have leading zeroes: {value!r}"
            )
    return SemVer(*(int(match.group(index)) for index in range(1, 4)), prerelease)


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left or not right:
        return (not left) - (not right)
    for left_part, right_part in zip(left, right):
        if left_part == right_part:
            continue
        if left_part.isdigit() and right_part.isdigit():
            left_number = int(left_part)
            right_number = int(right_part)
            return (left_number > right_number) - (left_number < right_number)
        if left_part.isdigit() != right_part.isdigit():
            return -1 if left_part.isdigit() else 1
        return (left_part > right_part) - (left_part < right_part)
    return (len(left) > len(right)) - (len(left) < len(right))


def is_newer(current: SemVer, previous: SemVer) -> bool:
    current_core = (current.major, current.minor, current.patch)
    previous_core = (previous.major, previous.minor, previous.patch)
    if current_core != previous_core:
        return current_core > previous_core
    return _compare_prerelease(current.prerelease, previous.prerelease) > 0


def version_from_tag(tag: str) -> str:
    if not tag.startswith("v"):
        raise VersionCheckError(f"Release tag must start with 'v': {tag!r}")
    value = tag[1:]
    parse_version(value)
    return value


def version_from_source(source: str) -> str:
    match = VERSION_PATTERN.search(source)
    if match is None:
        raise VersionCheckError("Version source does not contain a valid version")
    value = match.group(1)
    parse_version(value)
    return value


def _git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def validate_release_tags(
    current_value: str,
    tags_at_head: list[str],
    repository_tags: list[str],
) -> None:
    release_tags_at_head = [tag for tag in tags_at_head if tag.startswith("v")]
    for tag in release_tags_at_head:
        if version_from_tag(tag) != current_value:
            raise VersionCheckError(
                f"Release tag {tag!r} does not match version {current_value}"
            )
    if not release_tags_at_head:
        return

    current = parse_version(current_value)
    for tag in (tag for tag in repository_tags if tag.startswith("v")):
        tagged_value = version_from_tag(tag)
        if tag not in release_tags_at_head and not is_newer(
            current, parse_version(tagged_value)
        ):
            raise VersionCheckError(
                f"Release version {current_value} must be newer than {tagged_value}"
            )


def check_repository_version() -> str:
    current_value = version_from_source(VERSION_FILE.read_text(encoding="utf-8"))
    tags_at_head = _git("tag", "--points-at", "HEAD").stdout.splitlines()
    repository_tags = _git("tag", "--list", "v*").stdout.splitlines()
    validate_release_tags(current_value, tags_at_head, repository_tags)
    return current_value


def main() -> int:
    try:
        current = check_repository_version()
    except (OSError, subprocess.SubprocessError, VersionCheckError) as exc:
        print(f"Version check failed: {exc}", file=sys.stderr)
        return 1
    print(f"Version check passed: {current}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
