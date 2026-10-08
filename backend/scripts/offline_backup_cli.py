#!/usr/bin/env python3
"""Offline backup, verification, and restore CLI for Open Accounting data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT = "open-accounting-offline-backup"
VERSION = 1
COMPANY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
GENERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,35}$")


class BackupError(Exception):
    """A fail-closed backup operation error."""


@dataclass(frozen=True)
class FileRecord:
    path: str
    kind: str
    size: int
    sha256: str


@dataclass(frozen=True)
class CompanyRecord:
    company_id: str
    generation_id: str
    database: FileRecord
    integrity_check: str
    foreign_key_check: str
    attachments: tuple[FileRecord, ...]
    generated_documents: tuple[FileRecord, ...]


def _safe_relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BackupError(f"{field} must be a non-empty relative path")
    if value.startswith("/") or "\\" in value:
        raise BackupError(f"{field} is not a normalized relative path: {value!r}")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise BackupError(f"{field} is not a normalized relative path: {value!r}")
    return path.as_posix()


def _safe_company_id(value: Any) -> str:
    if not isinstance(value, str) or not COMPANY_ID_RE.fullmatch(value):
        raise BackupError(f"invalid or unsafe company ID: {value!r}")
    return value


def _safe_generation_id(value: Any) -> str:
    if not isinstance(value, str) or not GENERATION_ID_RE.fullmatch(value):
        raise BackupError(f"invalid or unsafe generation ID: {value!r}")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path, root: Path, kind: str) -> FileRecord:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise BackupError(f"{kind} is outside its company root: {path}") from exc
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"{kind} is not a regular file: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BackupError(f"cannot inspect {kind} {path}: {exc}") from exc
    return FileRecord(relative, kind, size, _sha256_file(path))


def _open_read_only(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open SQLite database {path}: {exc}") from exc


def _database_checks(path: Path) -> tuple[str, str]:
    try:
        with _open_read_only(path) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite validation failed for {path}: {exc}") from exc
    integrity_ok = integrity == [("ok",)]
    foreign_key_ok = not foreign_keys
    if not integrity_ok:
        raise BackupError(f"SQLite integrity failure for {path}: {integrity}")
    if not foreign_key_ok:
        raise BackupError(f"SQLite foreign-key failure for {path}: {foreign_keys}")
    return "ok", "ok"


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _load_registry(master: Path) -> tuple[tuple[str, str], ...]:
    if not master.is_file() or master.is_symlink():
        raise BackupError(f"missing master database: {master}")
    try:
        with _open_read_only(master) as connection:
            columns = _table_columns(connection, "companies")
            if not {"id", "generation_id"}.issubset(columns):
                raise BackupError("master database companies table is missing required columns")
            rows = connection.execute(
                "SELECT id, generation_id FROM companies ORDER BY id"
            ).fetchall()
    except sqlite3.Error as exc:
        raise BackupError(f"cannot inspect master database {master}: {exc}") from exc
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for company_id, generation_id in rows:
        company_id = _safe_company_id(company_id)
        generation_id = _safe_generation_id(generation_id)
        if company_id in seen:
            raise BackupError(f"duplicate company ID in master database: {company_id}")
        seen.add(company_id)
        result.append((company_id, generation_id))
    return tuple(result)


def _snapshot_database(source: Path, destination: Path) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_connection = _open_read_only(source)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite online backup failed for {source}: {exc}") from exc


def _referenced_files(
    database: Path,
    company_id: str,
    company_root: Path,
    source: Path,
) -> tuple[tuple[FileRecord, ...], tuple[FileRecord, ...]]:
    try:
        with _open_read_only(database) as connection:
            attachment_rows: list[str] = []
            outgoing_rows: list[str] = []
            if "attachments" in {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }:
                attachment_columns = _table_columns(connection, "attachments")
                if "rel_path" in attachment_columns:
                    attachment_rows = [
                        row[0] for row in connection.execute(
                            "SELECT rel_path FROM attachments ORDER BY id"
                        )
                    ]
            if "outgoing_documents" in {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }:
                outgoing_columns = _table_columns(connection, "outgoing_documents")
                if "pdf_rel_path" in outgoing_columns:
                    outgoing_rows = [
                        row[0] for row in connection.execute(
                            "SELECT pdf_rel_path FROM outgoing_documents ORDER BY id"
                        )
                    ]
    except sqlite3.Error as exc:
        raise BackupError(f"cannot inspect SQLite database {database}: {exc}") from exc

    attachments: list[FileRecord] = []
    for raw_path in attachment_rows:
        if raw_path is None:
            raise BackupError(f"missing referenced attachment for company {company_id}")
        try:
            relative = _safe_relative_path(raw_path, field="attachment path")
        except BackupError as exc:
            raise BackupError(
                f"unsafe attachment path for company {company_id}: {raw_path!r}"
            ) from exc
        if not relative.startswith("attachments/"):
            raise BackupError(f"unsafe attachment path for company {company_id}: {raw_path!r}")
        absolute = _resolve_company_file(company_root, relative, company_id, "attachment")
        attachments.append(
            _file_record(absolute, source, "attachment")
        )

    documents: list[FileRecord] = []
    for raw_path in outgoing_rows:
        if raw_path is None:
            continue
        try:
            relative = _safe_relative_path(raw_path, field="generated document path")
        except BackupError as exc:
            raise BackupError(
                f"unsafe generated document path for company {company_id}: {raw_path!r}"
            ) from exc
        if not relative.startswith("outgoing/"):
            raise BackupError(
                f"unsafe generated document path for company {company_id}: {raw_path!r}"
            )
        absolute = _resolve_company_file(
            company_root, relative, company_id, "generated document"
        )
        documents.append(
            _file_record(absolute, source, "generated-document")
        )
    return tuple(attachments), tuple(documents)


def _resolve_company_file(
    company_root: Path, relative: str, company_id: str, kind: str
) -> Path:
    root = company_root.resolve()
    absolute = (root / relative).resolve()
    try:
        absolute.relative_to(root)
    except ValueError as exc:
        raise BackupError(
            f"unsafe {kind} path for company {company_id}: {relative!r}"
        ) from exc
    if absolute.is_symlink() or not absolute.is_file():
        raise BackupError(f"missing referenced {kind} for company {company_id}: {relative}")
    return absolute


def _discover_source_files(source: Path, registry: tuple[tuple[str, str], ...]) -> tuple[list[CompanyRecord], list[FileRecord]]:
    companies_dir = source / "companies"
    if companies_dir.exists() and not companies_dir.is_dir():
        raise BackupError(f"unsafe companies path: {companies_dir}")
    registered = {company_id for company_id, _ in registry}
    if companies_dir.is_dir():
        for child in companies_dir.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            database = child / "books.db"
            if database.is_file() and not database.is_symlink() and child.name not in registered:
                raise BackupError(
                    f"unregistered/orphan company database: {database}"
                )

    company_records: list[CompanyRecord] = []
    all_files: list[FileRecord] = []
    for company_id, generation_id in registry:
        company_root = companies_dir / company_id
        database = company_root / "books.db"
        if not database.is_file() or database.is_symlink():
            raise BackupError(f"missing registered company database: {database}")
        integrity, foreign_keys = _database_checks(database)
        attachments, documents = _referenced_files(
            database, company_id, company_root, source
        )
        database_record = _file_record(database, source, "database")
        database_record = FileRecord(
            f"companies/{company_id}/books.db",
            "database",
            database_record.size,
            database_record.sha256,
        )
        company_records.append(
            CompanyRecord(
                company_id,
                generation_id,
                database_record,
                integrity,
                foreign_keys,
                attachments,
                documents,
            )
        )
        all_files.extend((database_record, *attachments, *documents))
    return company_records, all_files


def _manifest_record(record: FileRecord) -> dict[str, Any]:
    return {
        "path": record.path,
        "kind": record.kind,
        "size": record.size,
        "sha256": record.sha256,
    }


def _build_manifest(
    source: Path,
    master: FileRecord,
    companies: list[CompanyRecord],
    files: list[FileRecord],
    manifest_bytes: bytes,
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "version": VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "source": {"path": str(source), "resolved_path": str(source.resolve())},
        "master_database": {
            "relative_path": master.path,
            "size": master.size,
            "sha256": master.sha256,
            "integrity_check": "ok",
            "foreign_key_check": "ok",
        },
        "companies": [
            {
                "company_id": company.company_id,
                "generation_id": company.generation_id,
                "database": {
                    "relative_path": company.database.path,
                    "size": company.database.size,
                    "sha256": company.database.sha256,
                    "integrity_check": company.integrity_check,
                    "foreign_key_check": company.foreign_key_check,
                },
                "attachments": [
                    _manifest_record(record) for record in company.attachments
                ],
                "generated_documents": [
                    _manifest_record(record) for record in company.generated_documents
                ],
            }
            for company in companies
        ],
        "summary": {
            "company_count": len(companies),
            "database_count": len(companies),
            "attachment_count": sum(len(company.attachments) for company in companies),
            "generated_document_count": sum(
                len(company.generated_documents) for company in companies
            ),
            "file_count": len(files) + 2,
            "total_bytes": len(manifest_bytes) + master.size + sum(record.size for record in files),
        },
    }


def _copy_staged_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        while chunk := input_file.read(1024 * 1024):
            output_file.write(chunk)


def _validate_manifest_file(record: FileRecord, payload: bytes) -> None:
    if len(payload) != record.size:
        raise BackupError(f"manifest file size mismatch: {record.path}")
    if _sha256(payload) != record.sha256:
        raise BackupError(f"manifest file hash mismatch: {record.path}")


def _require_manifest_record(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BackupError(f"manifest field {field!r} must be an object")
    return value


def _validated_file_records(value: Any, field: str, prefix: str) -> tuple[FileRecord, ...]:
    if not isinstance(value, list):
        raise BackupError(f"manifest field {field!r} must be a list")
    records: list[FileRecord] = []
    for index, raw in enumerate(value):
        record = _require_manifest_record(raw, f"{field}[{index}]")
        path = _safe_relative_path(record.get("path"), f"{field}[{index}].path")
        if not path.startswith(prefix):
            raise BackupError(f"unsafe path in manifest: {path}")
        size = record.get("size")
        digest = record.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackupError(f"invalid file size in manifest: {path}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise BackupError(f"invalid file hash in manifest: {path}")
        records.append(FileRecord(path, record.get("kind", "file"), size, digest))
    return tuple(records)


def _validate_manifest(manifest: Any) -> tuple[dict[str, Any], tuple[CompanyRecord, ...], tuple[FileRecord, ...]]:
    manifest = _require_manifest_record(manifest, "manifest")
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise BackupError("unsupported backup manifest format or version")
    _safe_relative_path(manifest.get("created_at"), "created_at")
    source = _require_manifest_record(manifest.get("source"), "source")
    if not isinstance(source.get("path"), str) or not source["path"]:
        raise BackupError("manifest source.path must be a non-empty string")
    if not isinstance(source.get("resolved_path"), str) or not source["resolved_path"]:
        raise BackupError("manifest source.resolved_path must be a non-empty string")
    master = _require_manifest_record(manifest.get("master_database"), "master_database")
    master_path = _safe_relative_path(
        master.get("relative_path"), "master_database.relative_path"
    )
    if master_path != "master.db" or master.get("integrity_check") != "ok" or master.get("foreign_key_check") != "ok":
        raise BackupError("invalid master database metadata")
    if not isinstance(master.get("size"), int) or isinstance(master.get("size"), bool) or master.get("size") < 0:
        raise BackupError("invalid master database size")
    if not isinstance(master.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", master.get("sha256")):
        raise BackupError("invalid master database hash")
    raw_companies = manifest.get("companies")
    if not isinstance(raw_companies, list):
        raise BackupError("manifest companies must be a list")

    companies: list[CompanyRecord] = []
    all_files: list[FileRecord] = []
    company_ids: set[str] = set()
    for index, raw_company in enumerate(raw_companies):
        company = _require_manifest_record(raw_company, f"companies[{index}]")
        company_id = _safe_company_id(company.get("company_id"))
        generation_id = _safe_generation_id(company.get("generation_id"))
        if company_id in company_ids:
            raise BackupError(f"duplicate company ID in manifest: {company_id}")
        company_ids.add(company_id)
        database = _require_manifest_record(
            company.get("database"), f"companies[{index}].database"
        )
        database_path = _safe_relative_path(
            database.get("relative_path"), f"companies[{index}].database.relative_path"
        )
        expected_database_path = f"companies/{company_id}/books.db"
        if database_path != expected_database_path:
            raise BackupError(f"unsafe database path for company {company_id}")
        integrity = database.get("integrity_check")
        foreign_keys = database.get("foreign_key_check")
        if integrity != "ok" or foreign_keys != "ok":
            raise BackupError(f"invalid database check result for company {company_id}")
        company_prefix = f"companies/{company_id}/"
        attachments = _validated_file_records(
            company.get("attachments"),
            f"companies[{index}].attachments",
            f"{company_prefix}attachments/",
        )
        documents = _validated_file_records(
            company.get("generated_documents"),
            f"companies[{index}].generated_documents",
            f"{company_prefix}outgoing/",
        )
        attachment_paths = [record.path for record in attachments]
        document_paths = [record.path for record in documents]
        if len(attachment_paths) != len(set(attachment_paths)):
            raise BackupError(f"duplicate attachment path for company {company_id}")
        if len(document_paths) != len(set(document_paths)):
            raise BackupError(f"duplicate generated document path for company {company_id}")
        database_size = database.get("size")
        database_digest = database.get("sha256")
        if not isinstance(database_size, int) or isinstance(database_size, bool) or database_size < 0:
            raise BackupError(f"invalid database size for company {company_id}")
        if not isinstance(database_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", database_digest):
            raise BackupError(f"invalid database hash for company {company_id}")
        database_record = FileRecord(
            database_path, "database", database_size, database_digest
        )
        companies.append(
            CompanyRecord(
                company_id,
                generation_id,
                database_record,
                integrity,
                foreign_keys,
                attachments,
                documents,
            )
        )
        all_files.extend((database_record, *attachments, *documents))

    paths = [record.path for record in all_files]
    if len(paths) != len(set(paths)):
        raise BackupError("duplicate file member in manifest")
    summary = _require_manifest_record(manifest.get("summary"), "summary")
    expected = {
        "company_count": len(companies),
        "database_count": len(companies),
        "attachment_count": sum(len(company.attachments) for company in companies),
        "generated_document_count": sum(
            len(company.generated_documents) for company in companies
        ),
        "file_count": len(all_files) + 2,
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            raise BackupError(f"manifest summary field {key!r} is invalid")
    total_bytes = summary.get("total_bytes")
    if not isinstance(total_bytes, int) or isinstance(total_bytes, bool) or total_bytes < 0:
        raise BackupError("manifest summary total_bytes is invalid")
    return manifest, tuple(companies), tuple(all_files)


def _read_archive(archive: Path) -> tuple[dict[str, Any], tuple[CompanyRecord, ...], tuple[FileRecord, ...]]:
    if not archive.is_file() or archive.is_symlink():
        raise BackupError(f"archive is missing or unsafe: {archive}")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            member_data: dict[str, bytes] = {}
            seen: set[str] = set()
            for member in members:
                name = _safe_relative_path(member.name, "archive member path")
                if name in seen:
                    raise BackupError(f"duplicate archive member: {name}")
                seen.add(name)
                if not member.isfile():
                    raise BackupError(f"unsupported archive member: {name}")
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise BackupError(f"cannot read archive member: {name}")
                member_data[name] = extracted.read()
            if "manifest.json" not in member_data:
                raise BackupError("archive is missing manifest.json")
            try:
                manifest = json.loads(member_data["manifest.json"])
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupError("archive manifest is invalid JSON") from exc
            manifest, companies, files = _validate_manifest(manifest)
            expected = {"manifest.json", "master.db"}
            expected.update(record.path for record in files)
            if set(member_data) != expected:
                raise BackupError("archive contains unexpected or missing members")
            master_metadata = _require_manifest_record(
                manifest.get("master_database"), "master_database"
            )
            master_record = FileRecord(
                "master.db",
                "database",
                master_metadata["size"],
                master_metadata["sha256"],
            )
            _validate_manifest_file(master_record, member_data["master.db"])
            for record in files:
                _validate_manifest_file(record, member_data[record.path])
            if not _database_is_valid(member_data["master.db"]):
                raise BackupError("archive master database integrity or foreign-key check failed")
            for company in companies:
                if not _database_is_valid(member_data[company.database.path]):
                    raise BackupError(
                        f"archive database validation failed: {company.database.path}"
                    )
            return manifest, companies, files
    except (OSError, tarfile.TarError) as exc:
        raise BackupError(f"cannot read archive {archive}: {exc}") from exc


def _database_is_valid(payload: bytes) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".db") as temporary:
        temporary.write(payload)
        temporary.flush()
        try:
            integrity, foreign_keys = _database_checks(Path(temporary.name))
            return integrity == "ok" and foreign_keys == "ok"
        except BackupError:
            return False


def _build_backup(source: Path, archive: Path) -> None:
    source = source.resolve()
    archive = archive.resolve()
    if not source.is_dir() or source.is_symlink():
        raise BackupError(f"source data directory is missing or unsafe: {source}")
    master = source / "master.db"
    if not master.is_file() or master.is_symlink():
        raise BackupError(f"missing master database: {master}")
    if os.path.lexists(archive):
        raise BackupError(f"archive already exists: {archive}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    _database_checks(master)
    registry = _load_registry(master)
    companies, _ = _discover_source_files(source, registry)

    with tempfile.TemporaryDirectory(prefix="offline-backup-", dir=archive.parent) as temp:
        staging = Path(temp)
        staged_master = staging / "master.db"
        _snapshot_database(master, staged_master)
        _database_checks(staged_master)
        master_record = _file_record(staged_master, staging, "database")
        for index, company in enumerate(companies):
            staged_database = staging / company.database.path
            _snapshot_database(source / company.database.path, staged_database)
            _database_checks(staged_database)
            staged_database_record = _file_record(
                staged_database, staging, "database"
            )
            companies[index] = replace(
                company,
                database=FileRecord(
                    staged_database_record.path,
                    staged_database_record.kind,
                    staged_database_record.size,
                    staged_database_record.sha256,
                ),
            )
            for record in company.attachments + company.generated_documents:
                source_file = source / record.path
                staged_file = staging / record.path
                _copy_staged_file(source_file, staged_file)
                if _sha256_file(staged_file) != record.sha256:
                    raise BackupError(f"file changed during backup: {record.path}")

        master_record = _file_record(staged_master, staging, "database")
        files = [
            record
            for company in companies
            for record in (company.database, *company.attachments, *company.generated_documents)
        ]
        manifest = _build_manifest(
            source, master_record, companies, files, b""
        )
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        manifest = _build_manifest(
            source, master_record, companies, files, manifest_bytes
        )
        (staging / "manifest.json").write_bytes(manifest_bytes)
        _validate_staged_tree(staging, manifest)
        temporary_archive = Path(
            tempfile.mkstemp(prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent)[1]
        )
        try:
            with tarfile.open(temporary_archive, "w:gz") as tar:
                for path in sorted(staging.rglob("*")):
                    if path.is_symlink() or not path.is_file():
                        continue
                    relative = path.relative_to(staging).as_posix()
                    info = tarfile.TarInfo(relative)
                    info.size = path.stat().st_size
                    info.mode = 0o600
                    info.mtime = 0
                    with path.open("rb") as source_file:
                        tar.addfile(info, source_file)
            os.link(temporary_archive, archive)
        except Exception:
            temporary_archive.unlink(missing_ok=True)
            raise
        else:
            temporary_archive.unlink(missing_ok=True)


def _validate_staged_tree(root: Path, manifest: dict[str, Any]) -> None:
    expected = {"manifest.json", "master.db"}
    for company in manifest["companies"]:
        expected.add(company["database"]["relative_path"])
        expected.update(record["path"] for record in company["attachments"])
        expected.update(record["path"] for record in company["generated_documents"])
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise BackupError(f"staged tree contains an unsafe member: {path}")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != expected:
        raise BackupError("staged backup tree does not match the manifest")


def _verify_archive(archive: Path) -> None:
    _read_archive(archive)
    print(f"Verification passed: {archive}")


def _restore_archive(archive: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise BackupError(f"restore destination is unsafe: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise BackupError(f"restore destination is not a directory: {destination}")
        if any(destination.iterdir()):
            raise BackupError(f"restore destination is not empty: {destination}")
    manifest, companies, _ = _read_archive(archive)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="offline-restore-", dir=destination.parent) as temp:
        staging = Path(temp)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(staging, filter="data")
        except (OSError, tarfile.TarError) as exc:
            raise BackupError(f"archive extraction failed: {exc}") from exc
        _validate_staged_tree(staging, manifest)
        for company in companies:
            if not _database_is_valid((staging / company.database.path).read_bytes()):
                raise BackupError(
                    f"staged database validation failed: {company.database.path}"
                )
        if destination.exists():
            try:
                destination.rmdir()
            except OSError as exc:
                raise BackupError(f"cannot prepare empty restore destination: {exc}") from exc
        try:
            os.rename(staging, destination)
        except OSError as exc:
            raise BackupError(f"cannot move restored data into destination: {exc}") from exc
    print(f"Restore complete: {destination}")
    print("Start the application afterward to use the restored data.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="create an offline backup")
    backup_parser.add_argument("--source", required=True, type=Path)
    backup_parser.add_argument("--archive", required=True, type=Path)
    backup_parser.add_argument("--operator-confirm-stopped", action="store_true")
    verify_parser = subparsers.add_parser("verify", help="verify an offline backup")
    verify_parser.add_argument("--archive", required=True, type=Path)
    restore_parser = subparsers.add_parser("restore", help="restore an offline backup")
    restore_parser.add_argument("--archive", required=True, type=Path)
    restore_parser.add_argument("--destination", required=True, type=Path)
    restore_parser.add_argument("--operator-confirm-stopped", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            if not args.operator_confirm_stopped:
                raise BackupError("backup requires --operator-confirm-stopped")
            _build_backup(args.source, args.archive)
        elif args.command == "verify":
            _verify_archive(args.archive)
        elif args.command == "restore":
            if not args.operator_confirm_stopped:
                raise BackupError("restore requires --operator-confirm-stopped")
            _restore_archive(args.archive, args.destination.resolve())
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, tarfile.TarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
