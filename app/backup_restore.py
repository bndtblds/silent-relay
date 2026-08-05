"""Create and restore the data-only SilentRelay backup payload."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from typing import BinaryIO


FORMAT_VERSION = 2
SUPPORTED_FORMAT_VERSIONS = {1, FORMAT_VERSION}
MANIFEST_NAME = "manifest.json"
ENV_ARCHIVE_NAME = "config/.env"
DATA_PREFIX = PurePosixPath("data")
DEFAULT_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_TOTAL_SIZE = 10 * 1024 * 1024 * 1024
DEFAULT_MAX_FILE_COUNT = 10_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _data_files(data_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(data_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Backup data must not contain symbolic links: {path}")
        if path.is_file():
            files.append(path)
    return files


def _database_revision(data_dir: Path) -> str | None:
    database = data_dir / "app.db"
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def create_archive(
    output: BinaryIO,
    *,
    data_dir: Path,
    env_file: Path,
    commit: str,
    installation_id: str,
    age_recipient: str,
    created_at: str,
) -> None:
    if not env_file.is_file():
        raise ValueError("The matching .env file does not exist.")
    files = _data_files(data_dir)
    files_manifest = {
        ENV_ARCHIVE_NAME: {
            "sha256": _sha256(env_file),
            "size": env_file.stat().st_size,
        }
    }
    for path in files:
        name = (DATA_PREFIX / path.relative_to(data_dir).as_posix()).as_posix()
        files_manifest[name] = {"sha256": _sha256(path), "size": path.stat().st_size}

    manifest = {
        "format_version": FORMAT_VERSION,
        "created_at": created_at,
        "git_commit": commit,
        "database_revision": _database_revision(data_dir),
        "installation_id": installation_id,
        "age_recipient": age_recipient,
        "files": files_manifest,
    }
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")

    with tarfile.open(fileobj=output, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(manifest_bytes))
        archive.add(env_file, ENV_ARCHIVE_NAME, recursive=False, filter=_safe_info)
        for path in files:
            name = (DATA_PREFIX / path.relative_to(data_dir).as_posix()).as_posix()
            archive.add(path, name, recursive=False, filter=_safe_info)


def _safe_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = 0o600
    return info


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts


def _manifest_files(
    manifest: dict[str, object],
) -> tuple[dict[str, str], dict[str, int] | None]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("The backup manifest is invalid.")
    if manifest.get("format_version") == 1:
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in files.items()
        ):
            raise ValueError("The backup manifest is invalid.")
        return files, None
    checksums: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for name, details in files.items():
        if (
            not isinstance(name, str)
            or not isinstance(details, dict)
            or not isinstance(details.get("sha256"), str)
            or not isinstance(details.get("size"), int)
            or details["size"] < 0
        ):
            raise ValueError("The backup manifest is invalid.")
        checksums[name] = details["sha256"]
        sizes[name] = details["size"]
    return checksums, sizes


def _read_and_validate_archive(
    source: BinaryIO,
    staging: Path,
    *,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
) -> dict[str, object]:
    if min(max_file_size, max_total_size, max_file_count) < 1:
        raise ValueError("Restore resource limits must be positive.")
    expected: dict[str, str] | None = None
    expected_sizes: dict[str, int] | None = None
    seen: set[str] = set()
    manifest: dict[str, object] | None = None
    total_size = 0

    with tarfile.open(fileobj=source, mode="r|gz") as archive:
        for member in archive:
            if not _safe_archive_name(member.name) or not member.isfile():
                raise ValueError("The backup contains an unsafe archive entry.")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError("The backup contains an unreadable archive entry.")
            if member.name == MANIFEST_NAME:
                if manifest is not None or seen:
                    raise ValueError("The backup manifest is missing or misplaced.")
                raw = extracted.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("The backup manifest is too large.")
                manifest = json.loads(raw)
                if not isinstance(manifest, dict):
                    raise ValueError("The backup manifest is invalid.")
                if manifest.get("format_version") not in SUPPORTED_FORMAT_VERSIONS:
                    raise ValueError("The backup format is not supported.")
                expected, expected_sizes = _manifest_files(manifest)
                if len(expected) > max_file_count:
                    raise ValueError("The backup contains too many files.")
                if expected_sizes is not None:
                    if any(size > max_file_size for size in expected_sizes.values()):
                        raise ValueError("A backup file exceeds the restore size limit.")
                    declared_total = sum(expected_sizes.values())
                    if declared_total > max_total_size:
                        raise ValueError("The backup exceeds the total restore size limit.")
                    if declared_total > shutil.disk_usage(staging).free:
                        raise ValueError("There is not enough free space to stage the backup.")
                continue
            if expected is None or member.name not in expected or member.name in seen:
                raise ValueError("The backup contains an unexpected archive entry.")
            if member.name != ENV_ARCHIVE_NAME and not member.name.startswith("data/"):
                raise ValueError("The backup contains an unexpected path.")
            if member.size > max_file_size:
                raise ValueError("A backup file exceeds the restore size limit.")
            if expected_sizes is not None and member.size != expected_sizes[member.name]:
                raise ValueError("A backup file size does not match its manifest entry.")
            total_size += member.size
            if total_size > max_total_size:
                raise ValueError("The backup exceeds the total restore size limit.")
            if len(seen) + 1 > max_file_count:
                raise ValueError("The backup contains too many files.")
            target = staging.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with target.open("xb") as destination:
                while chunk := extracted.read(1024 * 1024):
                    digest.update(chunk)
                    destination.write(chunk)
            os.chmod(target, 0o600)
            if digest.hexdigest() != expected[member.name]:
                raise ValueError("A backup file failed checksum verification.")
            seen.add(member.name)

    if manifest is None or expected is None or seen != set(expected):
        raise ValueError("The backup is incomplete.")
    if ENV_ARCHIVE_NAME not in seen:
        raise ValueError("The backup does not contain its matching .env file.")
    return manifest


def restore_archive(
    source: BinaryIO,
    *,
    data_dir: Path,
    env_file: Path,
    replace_existing: bool = False,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    max_file_count: int = DEFAULT_MAX_FILE_COUNT,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="silentrelay-restore-") as temporary:
        staging = Path(temporary)
        manifest = _read_and_validate_archive(
            source,
            staging,
            max_file_size=max_file_size,
            max_total_size=max_total_size,
            max_file_count=max_file_count,
        )
        if data_dir.exists() and any(data_dir.iterdir()) and not replace_existing:
            raise ValueError("The target data volume is not empty.")
        if env_file.exists() and not replace_existing:
            raise ValueError("The target .env already exists.")
        staged_size = sum(
            path.stat().st_size for path in (staging / "data").rglob("*") if path.is_file()
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        if staged_size > shutil.disk_usage(data_dir).free:
            raise ValueError("There is not enough free space in the target data volume.")
        if replace_existing:
            if data_dir.exists():
                for path in data_dir.iterdir():
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            env_file.unlink(missing_ok=True)
        for source_path in (staging / "data").rglob("*"):
            if source_path.is_file():
                target = data_dir / source_path.relative_to(staging / "data")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, target)
                os.chmod(target, 0o600)
        try:
            import pwd

            account = pwd.getpwnam("silentrelay")
            for path in [data_dir, *data_dir.rglob("*")]:
                os.chown(path, account.pw_uid, account.pw_gid)
        except (ImportError, KeyError, PermissionError):
            pass
        env_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staging / ENV_ARCHIVE_NAME, env_file)
        os.chmod(env_file, 0o600)
        return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--data-dir", type=Path, required=True)
    create.add_argument("--env-file", type=Path, required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--installation-id", required=True)
    create.add_argument("--age-recipient", required=True)
    create.add_argument("--created-at", required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--data-dir", type=Path, required=True)
    restore.add_argument("--env-file", type=Path, required=True)
    restore.add_argument("--replace-existing", action="store_true")
    restore.add_argument("--max-file-size", type=int, default=DEFAULT_MAX_FILE_SIZE)
    restore.add_argument("--max-total-size", type=int, default=DEFAULT_MAX_TOTAL_SIZE)
    restore.add_argument("--max-file-count", type=int, default=DEFAULT_MAX_FILE_COUNT)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--field", choices=("git_commit",), required=False)
    inspect.add_argument("--max-file-size", type=int, default=DEFAULT_MAX_FILE_SIZE)
    inspect.add_argument("--max-total-size", type=int, default=DEFAULT_MAX_TOTAL_SIZE)
    inspect.add_argument("--max-file-count", type=int, default=DEFAULT_MAX_FILE_COUNT)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    try:
        if arguments.command == "create":
            create_archive(
                sys.stdout.buffer,
                data_dir=arguments.data_dir,
                env_file=arguments.env_file,
                commit=arguments.commit,
                installation_id=arguments.installation_id,
                age_recipient=arguments.age_recipient,
                created_at=arguments.created_at,
            )
        elif arguments.command == "restore":
            manifest = restore_archive(
                sys.stdin.buffer,
                data_dir=arguments.data_dir,
                env_file=arguments.env_file,
                replace_existing=arguments.replace_existing,
                max_file_size=arguments.max_file_size,
                max_total_size=arguments.max_total_size,
                max_file_count=arguments.max_file_count,
            )
            summary = {key: value for key, value in manifest.items() if key != "files"}
            print(json.dumps(summary, sort_keys=True))
        else:
            with tempfile.TemporaryDirectory(prefix="silentrelay-inspect-") as temporary:
                manifest = _read_and_validate_archive(
                    sys.stdin.buffer,
                    Path(temporary),
                    max_file_size=arguments.max_file_size,
                    max_total_size=arguments.max_total_size,
                    max_file_count=arguments.max_file_count,
                )
            if arguments.field:
                value = manifest.get(arguments.field)
                if not isinstance(value, str) or not value:
                    raise ValueError(f"The manifest has no valid {arguments.field}.")
                print(value)
            else:
                print(json.dumps(manifest, sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        print(f"Backup operation failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
