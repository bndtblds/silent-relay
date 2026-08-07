# Versioning and releases

SilentRelay uses [Semantic Versioning](https://semver.org/) in the form
`MAJOR.MINOR.PATCH`, with optional prerelease and build metadata.

## Canonical version

`app/version.py` is the single canonical source of the SilentRelay version.
The Python package metadata is generated from it. Generated metadata such as
`uv.lock` must be refreshed after changing the canonical version; it is not an
independent version source.

Every commit must advance the version from its first parent. Use:

- `PATCH` for backward-compatible fixes and documentation-only changes;
- `MINOR` for backward-compatible features; and
- `MAJOR` for incompatible public behavior or contracts.

Several commits must never share a version, even when they belong to one
planned release. A push only transports existing commits and does not change
their versions.

Run the repository check before committing:

```sh
uv run python scripts/check_version.py
```

The check validates Semantic Versioning and requires the working version to be
newer than `HEAD` when changes are present. In a clean checkout it compares
`HEAD` with its first parent. This makes the same check suitable before a
commit and in automated verification after a commit.

## Releases

A release tag is immutable, annotated, and named `v<version>`, for example
`v1.1.0`. It must point to a commit whose canonical version is exactly the tag
version. Moving or reusing a published tag is not allowed. Release notes state
the tag and full commit identifier. Container images use the same version and
must also be recorded by immutable digest for reproducible deployment.

A deployment manifest records the immutable SilentRelay release tag, full
commit identifier, and exact container image digest. A floating branch,
mutable image tag, or version range is not a reproducible build input.

## Public contract versions

Public integration contracts have their own small integer versions. They are
independent of the SilentRelay Semantic Version. An incompatible contract
change increments the contract version and requires an appropriate SilentRelay
version change. Backward-compatible additions may retain the contract version
when existing integrations remain safe and functional.
