# Versioning and releases

SilentRelay uses [Semantic Versioning](https://semver.org/) in the form
`MAJOR.MINOR.PATCH`, with optional prerelease and build metadata.

## Canonical version

`app/version.py` is the single canonical source of the SilentRelay version.
The Python package metadata is generated from it. Generated metadata such as
`uv.lock` must be refreshed after changing the canonical version; it is not an
independent version source.

The version on `main` identifies the current SilentRelay release. Every pull
request merged into `main` advances it exactly once, using:

- `PATCH` for backward-compatible fixes and documentation-only changes;
- `MINOR` for backward-compatible features; and
- `MAJOR` for incompatible public behavior or contracts.

Commits within one pull request may share its target version. Fixing or
refining that pull request therefore does not consume more versions. The
squash merge creates one `main` commit with one new version. Do not add a
fourth numeric component; Git commit identifiers, CI run IDs, and container
image digests remain separate build identities.

Run the repository check before committing:

```sh
uv run python scripts/check_version.py
```

The check validates Semantic Versioning and compares the pull request version
with its target branch. It must be newer once, regardless of how many commits
the pull request contains. On a push to `main`, CI compares the squash commit
with its first parent. A `v<version>` tag must additionally match the canonical
version exactly and be newer than every other release tag.

## Releases

A release tag may be used as an immutable marker for a `main` version. It is
annotated and named `v<version>`, for example
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
