from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "offline_backup_cli.py"
FORBIDDEN_DATA_DIR = Path("/workspaces/open-accounting-data")


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def create_database(path: Path, *, foreign_keys: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON" if foreign_keys else "PRAGMA foreign_keys = OFF")
        connection.execute("CREATE TABLE companies (id TEXT PRIMARY KEY, generation_id TEXT NOT NULL)")
        connection.execute("CREATE TABLE attachments (id TEXT PRIMARY KEY, rel_path TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE outgoing_documents (id INTEGER PRIMARY KEY, pdf_rel_path TEXT)"
        )
        connection.execute(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY, child_id INTEGER REFERENCES children(id))"
        )
        connection.execute("CREATE TABLE children (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO companies VALUES (?, ?)", ("alpha", "gen-alpha"))
        connection.execute(
            "INSERT INTO attachments VALUES (?, ?)",
            ("attachment-1", "attachments/invoices/2026-01/attachment.pdf"),
        )
        connection.execute(
            "INSERT INTO outgoing_documents VALUES (?, ?)",
            (1, "outgoing/receipts/receipt.pdf"),
        )


def create_source_fixture(tmp_path: Path) -> Path:
    source = tmp_path / "source-data"
    source.mkdir()
    create_database(source / "master.db")
    with sqlite3.connect(source / "master.db") as connection:
        connection.execute(
            "INSERT INTO companies VALUES (?, ?)",
            ("beta", "gen-beta"),
        )
    for company_id in ("alpha", "beta"):
        database = source / "companies" / company_id / "books.db"
        create_database(database)
        company_dir = database.parent
        attachment = company_dir / "attachments" / "invoices" / "2026-01" / "attachment.pdf"
        attachment.parent.mkdir(parents=True, exist_ok=True)
        attachment.write_bytes(f"attachment-{company_id}".encode())
        document = company_dir / "outgoing" / "receipts" / "receipt.pdf"
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_bytes(f"document-{company_id}".encode())
    return source


def make_archive(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o600
            archive.addfile(info, __import__("io").BytesIO(payload))


def test_successful_backup_verify_restore(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    destination = tmp_path / "restored-data"

    result = run_cli(
        "backup",
        "--source",
        str(source),
        "--archive",
        str(archive),
        "--operator-confirm-stopped",
    )
    assert result.returncode == 0, result.stderr
    assert archive.is_file()

    result = run_cli("verify", "--archive", str(archive))
    assert result.returncode == 0, result.stderr

    result = run_cli(
        "restore",
        "--archive",
        str(archive),
        "--destination",
        str(destination),
        "--operator-confirm-stopped",
    )
    assert result.returncode == 0, result.stderr
    assert "Start the application afterward" in result.stdout
    assert (destination / "companies" / "alpha" / "attachments" / "invoices" / "2026-01" / "attachment.pdf").read_bytes() == b"attachment-alpha"
    assert (destination / "companies" / "beta" / "outgoing" / "receipts" / "receipt.pdf").read_bytes() == b"document-beta"

    with sqlite3.connect(destination / "companies" / "alpha" / "books.db") as connection:
        assert connection.execute("SELECT rel_path FROM attachments").fetchone() == (
            "attachments/invoices/2026-01/attachment.pdf",
        )


def test_archive_manifest_and_repeated_verification(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    assert run_cli(
        "backup", "--source", str(source), "--archive", str(archive),
        "--operator-confirm-stopped",
    ).returncode == 0

    with tarfile.open(archive, "r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["format"] == "open-accounting-offline-backup"
    assert manifest["version"] == 1
    assert manifest["summary"]["company_count"] == 2
    assert manifest["summary"]["attachment_count"] == 2
    assert manifest["summary"]["generated_document_count"] == 2
    assert manifest["master_database"]["relative_path"] == "master.db"
    assert {company["company_id"] for company in manifest["companies"]} == {"alpha", "beta"}
    assert all(
        company["database"]["integrity_check"] == "ok"
        and company["database"]["foreign_key_check"] == "ok"
        for company in manifest["companies"]
    )

    first = run_cli("verify", "--archive", str(archive))
    second = run_cli("verify", "--archive", str(archive))
    assert first.returncode == 0 and second.returncode == 0
    assert not archive.read_bytes().startswith(b"") or archive.stat().st_size > 0


def test_backup_is_a_snapshot_and_online_backup_uses_sqlite(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    result = run_cli(
        "backup", "--source", str(source), "--archive", str(archive),
        "--operator-confirm-stopped",
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(source / "companies" / "alpha" / "books.db") as connection:
        connection.execute("UPDATE companies SET generation_id = ?", ("changed",))
    result = run_cli("verify", "--archive", str(archive))
    assert result.returncode == 0, result.stderr
    with tarfile.open(archive, "r:gz") as tar:
        archived = json.loads(tar.extractfile("manifest.json").read())
    assert archived["companies"][0]["generation_id"] == "gen-alpha"


def test_failure_modes(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    result = run_cli("backup", "--source", str(source), "--archive", str(archive))
    assert result.returncode != 0 and "--operator-confirm-stopped" in result.stderr

    missing_master = tmp_path / "missing-master"
    missing_master.mkdir()
    result = run_cli(
        "backup", "--source", str(missing_master), "--archive", str(tmp_path / "missing.tar.gz"),
        "--operator-confirm-stopped",
    )
    assert result.returncode != 0 and "missing master database" in result.stderr
    assert not (tmp_path / "missing.tar.gz").exists()

    missing_company = tmp_path / "missing-company"
    missing_company.mkdir()
    create_database(missing_company / "master.db")
    with sqlite3.connect(missing_company / "master.db") as connection:
        connection.execute("INSERT INTO companies VALUES (?, ?)", ("missing", "gen"))
    result = run_cli(
        "backup", "--source", str(missing_company), "--archive", str(tmp_path / "missing-company.tar.gz"),
        "--operator-confirm-stopped",
    )
    assert result.returncode != 0 and "missing registered company database" in result.stderr

    orphan = tmp_path / "orphan"
    orphan.mkdir()
    create_database(orphan / "master.db")
    create_database(orphan / "companies" / "orphan" / "books.db")
    result = run_cli(
        "backup", "--source", str(orphan), "--archive", str(tmp_path / "orphan.tar.gz"),
        "--operator-confirm-stopped",
    )
    assert result.returncode != 0 and "unregistered/orphan" in result.stderr

    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    create_database(corrupt / "master.db")
    with sqlite3.connect(corrupt / "master.db") as connection:
        connection.execute("DELETE FROM companies")
        connection.execute("INSERT INTO companies VALUES (?, ?)", ("bad", "gen"))
    database = corrupt / "companies" / "bad" / "books.db"
    create_database(database)
    database.write_bytes(b"not a sqlite database")
    result = run_cli(
        "backup", "--source", str(corrupt), "--archive", str(tmp_path / "corrupt.tar.gz"),
        "--operator-confirm-stopped",
    )
    assert result.returncode != 0 and "SQLite" in result.stderr

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    create_database(foreign / "master.db")
    with sqlite3.connect(foreign / "master.db") as connection:
        connection.execute("DELETE FROM companies")
        connection.execute("INSERT INTO companies VALUES (?, ?)", ("bad", "gen"))
    database = foreign / "companies" / "bad" / "books.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, child_id INTEGER REFERENCES children(id))")
        connection.execute("CREATE TABLE children (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO parent VALUES (1, 99)")
    result = run_cli(
        "backup", "--source", str(foreign), "--archive", str(tmp_path / "foreign.tar.gz"),
        "--operator-confirm-stopped",
    )
    assert result.returncode != 0 and "foreign-key" in result.stderr.lower()

    unsafe = tmp_path / "unsafe-attachment"
    unsafe.mkdir()
    create_database(unsafe / "master.db")
    with sqlite3.connect(unsafe / "master.db") as connection:
        connection.execute("DELETE FROM companies")
        connection.execute("INSERT INTO companies VALUES (?, ?)", ("bad", "gen"))
    database = unsafe / "companies" / "bad" / "books.db"
    create_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO attachments VALUES (?, ?)", ("1", "../../escape.pdf"))
    result = run_cli(
        "backup", "--source", str(unsafe), "--archive", str(tmp_path / "unsafe.tar.gz"),
        "--operator-confirm-stopped",
    )
    assert result.returncode != 0 and "unsafe attachment" in result.stderr


def test_existing_archive_and_restore_safety(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    assert run_cli(
        "backup", "--source", str(source), "--archive", str(archive),
        "--operator-confirm-stopped",
    ).returncode == 0
    assert run_cli(
        "backup", "--source", str(source), "--archive", str(archive),
        "--operator-confirm-stopped",
    ).returncode != 0

    destination = tmp_path / "non-empty"
    destination.mkdir()
    sentinel = destination / "sentinel"
    sentinel.write_text("keep")
    result = run_cli(
        "restore", "--archive", str(archive), "--destination", str(destination),
        "--operator-confirm-stopped",
    )
    assert result.returncode != 0 and "not empty" in result.stderr
    assert sentinel.read_text() == "keep"


def test_archive_member_and_manifest_failures(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path)
    archive = tmp_path / "archive.tar.gz"
    assert run_cli(
        "backup", "--source", str(source), "--archive", str(archive),
        "--operator-confirm-stopped",
    ).returncode == 0

    with tarfile.open(archive, "r:gz") as tar:
        members = {member.name: tar.extractfile(member).read() for member in tar.getmembers()}
    manifest = json.loads(members["manifest.json"])
    manifest["companies"][0]["database"]["sha256"] = "0" * 64
    members["manifest.json"] = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    make_archive(tmp_path / "bad-hash.tar.gz", members)
    assert "hash mismatch" in run_cli(
        "verify", "--archive", str(tmp_path / "bad-hash.tar.gz")
    ).stderr

    unsafe = tmp_path / "unsafe-member.tar.gz"
    make_archive(unsafe, {"../escape": b"bad"})
    assert "relative path" in run_cli("verify", "--archive", str(unsafe)).stderr

    symlink = tmp_path / "symlink.tar.gz"
    with tarfile.open(symlink, "w:gz") as tar:
        info = tarfile.TarInfo("master.db")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    assert "unsupported archive member" in run_cli(
        "verify", "--archive", str(symlink)
    ).stderr

    duplicate = tmp_path / "duplicate.tar.gz"
    with tarfile.open(duplicate, "w:gz") as tar:
        for name in ("manifest.json", "manifest.json"):
            payload = b"{}"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, __import__("io").BytesIO(payload))
    assert "duplicate archive member" in run_cli(
        "verify", "--archive", str(duplicate)
    ).stderr


def test_failed_restore_leaves_destination_unchanged(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    assert run_cli(
        "backup", "--source", str(source), "--archive", str(archive),
        "--operator-confirm-stopped",
    ).returncode == 0
    bad = tmp_path / "bad.tar.gz"
    make_archive(bad, {"manifest.json": b"not-json"})
    destination = tmp_path / "restore-target"
    destination.mkdir()
    sentinel = destination / "sentinel"
    sentinel.write_text("unchanged")
    result = run_cli(
        "restore", "--archive", str(bad), "--destination", str(destination),
        "--operator-confirm-stopped",
    )
    assert result.returncode != 0
    assert sentinel.read_text() == "unchanged"
    assert list(destination.iterdir()) == [sentinel]


def test_forbidden_data_directory_is_never_referenced(tmp_path: Path) -> None:
    source = create_source_fixture(tmp_path)
    archive = tmp_path / "backup.tar.gz"
    result = run_cli(
        "backup", "--source", str(source), "--archive", str(archive),
        "--operator-confirm-stopped",
    )
    assert result.returncode == 0, result.stderr
