# Versioning and releases

SilentRelay uses [Semantic Versioning](https://semver.org/) in the form
`MAJOR.MINOR.PATCH`, with optional prerelease and build metadata.

## Canonical version

`app/version.py` is the single canonical source of the SilentRelay version.
The Python package metadata is generated from it. Generated metadata such as
`uv.lock` must be refreshed after changing the canonical version; it is not an
independent version source.

The canonical version describes a release, not an individual development
commit. Several commits and pull requests may therefore retain the same
version. Change it deliberately when preparing a release, using:

- `PATCH` for backward-compatible fixes and documentation-only changes;
- `MINOR` for backward-compatible features; and
- `MAJOR` for incompatible public behavior or contracts.

Do not add a fourth numeric component. Git commit identifiers, CI run IDs, and
container image digests are build identities and remain separate from the
product version. Where a combined display value is useful, SemVer build
metadata such as `1.3.8+git.19f0f54` may be derived without changing the
canonical source or version precedence.

Run the repository check before committing:

```sh
uv run python scripts/check_version.py
```

The check always validates Semantic Versioning. Ordinary commits do not need
to advance it. On a `v<version>` release tag, the check additionally requires
the tag to match the canonical version exactly and the release version to be
newer than every other release tag in the repository.

## Releases

A release tag is immutable, annotated, and named `v<version>`, for example
`v1.1.0`. It must point to a commit whose canonical version is exactly the tag
version. Moving or reusing a published tag is not allowed. Release notes state
the tag and full commit identifier. Container images use the same version and
must also be recorded by immutable digest for reproducible deployment.

A deployment manifest records the immutable SilentRelay release tag, full
commit identifier, and exact container image digest. A floating branch,
mutable image tag, or version range is not a reproducible build input.

CI artifacts record the full Git commit and GitHub Actions run identifiers.
These identify a concrete build without consuming or extending the product's
three-part Semantic Version.

## Public contract versions

Public integration contracts have their own small integer versions. They are
independent of the SilentRelay Semantic Version. An incompatible contract
change increments the contract version and requires an appropriate SilentRelay
version change. Backward-compatible additions may retain the contract version
when existing integrations remain safe and functional.
