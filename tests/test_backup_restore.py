from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tarfile

import pytest

from app.backup_restore import create_archive, restore_archive


def create_test_archive(tmp_path: Path) -> bytes:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.db").write_bytes(b"database")
    nested = source / "nested"
    nested.mkdir()
    (nested / "payload").write_bytes(b"encrypted payload")
    env_file = tmp_path / "source.env"
    env_file.write_text("FIELD_ENCRYPTION_KEY=secret\n", encoding="utf-8")
    output = BytesIO()
    create_archive(
        output,
        data_dir=source,
        env_file=env_file,
        commit="a" * 40,
        installation_id="11111111-1111-1111-1111-111111111111",
        age_recipient="age1test",
        created_at="2026-08-04T12:00:00Z",
    )
    return output.getvalue()


def test_archive_contains_only_manifest_environment_and_data(tmp_path: Path) -> None:
    archive = create_test_archive(tmp_path)

    with tarfile.open(fileobj=BytesIO(archive), mode="r:gz") as payload:
        names = payload.getnames()
        manifest = json.load(payload.extractfile("manifest.json"))  # type: ignore[arg-type]

    assert names == [
        "manifest.json",
        "config/.env",
        "data/app.db",
        "data/nested/payload",
    ]
    assert manifest["format_version"] == 2
    assert manifest["git_commit"] == "a" * 40
    assert set(manifest["files"]) == set(names[1:])
    assert manifest["files"]["data/app.db"] == {
        "sha256": "3549b0028b75d981cdda2e573e9cb49dedc200185876df299f912b79f69dabd8",
        "size": 8,
    }


def test_restore_validates_and_restores_data_and_environment(tmp_path: Path) -> None:
    archive = create_test_archive(tmp_path)
    target_data = tmp_path / "target-data"
    target_env = tmp_path / "target" / ".env"

    manifest = restore_archive(BytesIO(archive), data_dir=target_data, env_file=target_env)

    assert manifest["installation_id"] == "11111111-1111-1111-1111-111111111111"
    assert (target_data / "app.db").read_bytes() == b"database"
    assert (target_data / "nested" / "payload").read_bytes() == b"encrypted payload"
    assert target_env.read_text(encoding="utf-8") == "FIELD_ENCRYPTION_KEY=secret\n"


def test_restore_refuses_existing_installation(tmp_path: Path) -> None:
    archive = create_test_archive(tmp_path)
    target_data = tmp_path / "target-data"
    target_data.mkdir()
    (target_data / "existing").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="not empty"):
        restore_archive(
            BytesIO(archive), data_dir=target_data, env_file=tmp_path / "target.env"
        )

    assert (target_data / "existing").read_text(encoding="utf-8") == "keep"


def test_explicit_restore_replaces_existing_installation(tmp_path: Path) -> None:
    archive = create_test_archive(tmp_path)
    target_data = tmp_path / "target-data"
    target_data.mkdir()
    (target_data / "existing").write_text("replace", encoding="utf-8")
    target_env = tmp_path / "target.env"
    target_env.write_text("OLD=value\n", encoding="utf-8")

    restore_archive(
        BytesIO(archive),
        data_dir=target_data,
        env_file=target_env,
        replace_existing=True,
    )

    assert not (target_data / "existing").exists()
    assert (target_data / "app.db").read_bytes() == b"database"
    assert target_env.read_text(encoding="utf-8") == "FIELD_ENCRYPTION_KEY=secret\n"


def test_restore_rejects_checksum_mismatch(tmp_path: Path) -> None:
    archive = create_test_archive(tmp_path)
    source = BytesIO(archive)
    rebuilt = BytesIO()
    with tarfile.open(fileobj=source, mode="r:gz") as original:
        with tarfile.open(fileobj=rebuilt, mode="w:gz") as changed:
            for member in original:
                content = original.extractfile(member).read()  # type: ignore[union-attr]
                if member.name == "data/app.db":
                    content = b"tampered"
                    member.size = len(content)
                changed.addfile(member, BytesIO(content))

    with pytest.raises(ValueError, match="checksum"):
        restore_archive(
            BytesIO(rebuilt.getvalue()),
            data_dir=tmp_path / "target-data",
            env_file=tmp_path / "target.env",
        )


def test_restore_rejects_unsafe_archive_member(tmp_path: Path) -> None:
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        manifest = {
            "format_version": 1,
            "files": {"../outside": "0" * 64},
        }
        raw = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        archive.addfile(info, BytesIO(raw))
        unsafe = tarfile.TarInfo("../outside")
        unsafe.size = 1
        archive.addfile(unsafe, BytesIO(b"x"))

    with pytest.raises(ValueError, match="unsafe"):
        restore_archive(
            BytesIO(output.getvalue()),
            data_dir=tmp_path / "target-data",
            env_file=tmp_path / "target.env",
        )


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"max_file_size": 7}, "file exceeds"),
        ({"max_total_size": 20}, "total restore size"),
        ({"max_file_count": 2}, "too many files"),
    ],
)
def test_restore_enforces_resource_limits(
    tmp_path: Path, limits: dict[str, int], message: str
) -> None:
    archive = create_test_archive(tmp_path)

    with pytest.raises(ValueError, match=message):
        restore_archive(
            BytesIO(archive),
            data_dir=tmp_path / "target-data",
            env_file=tmp_path / "target.env",
            **limits,
        )


def test_restore_rejects_size_that_differs_from_manifest(tmp_path: Path) -> None:
    archive = create_test_archive(tmp_path)
    rebuilt = BytesIO()
    with tarfile.open(fileobj=BytesIO(archive), mode="r:gz") as original:
        with tarfile.open(fileobj=rebuilt, mode="w:gz") as changed:
            for member in original:
                content = original.extractfile(member).read()  # type: ignore[union-attr]
                if member.name == "data/app.db":
                    content += b"x"
                    member.size = len(content)
                changed.addfile(member, BytesIO(content))

    with pytest.raises(ValueError, match="size does not match"):
        restore_archive(
            BytesIO(rebuilt.getvalue()),
            data_dir=tmp_path / "target-data",
            env_file=tmp_path / "target.env",
        )


def test_restore_supports_legacy_format_one_archive(tmp_path: Path) -> None:
    content = b"legacy"
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        manifest = {
            "format_version": 1,
            "files": {
                "config/.env": "91d6a3d55e9fea7911c537afae6607c77fa8bc0f3a76c375e104ac5a8cfa84db",
                "data/app.db": "c49fea7425fa7f8699897a97c159c6690267d9003bb78c53fafa8fc15c325d84",
            },
        }
        raw = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        archive.addfile(info, BytesIO(raw))
        for name, value in (("config/.env", b"A=1\n"), ("data/app.db", content)):
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, BytesIO(value))

    restore_archive(
        BytesIO(output.getvalue()),
        data_dir=tmp_path / "target-data",
        env_file=tmp_path / "target.env",
    )

    assert (tmp_path / "target-data" / "app.db").read_bytes() == content
