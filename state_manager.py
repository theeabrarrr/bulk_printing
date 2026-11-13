"""
state_manager.py
SQLite-backed State Management & Legacy Flat-File Migrator for the Oracle ERP Automation Suite.
Replaces completed_complaints.txt and printed_complaints.txt with a crash-safe SQLite database
('complaints_state.db') operating in Write-Ahead Logging (WAL) mode.
"""

from contextlib import contextmanager
from datetime import datetime
import os
import re
import shutil
import sqlite3
from typing import Dict, Iterator, Optional, Set, Tuple, Union

import config_manager
import logger_manager

VALID_STATUSES = ("DOWNLOADED", "SPOOLED", "PRINTED", "PRINT_FAILED")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS complaints (
    complaint_id   TEXT PRIMARY KEY,
    status         TEXT NOT NULL CHECK(status IN ('DOWNLOADED', 'SPOOLED', 'PRINTED', 'PRINT_FAILED')),
    technician     TEXT NOT NULL DEFAULT 'Unknown',
    file_path      TEXT DEFAULT '',
    printer_name   TEXT DEFAULT '',
    job_id         INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_complaints_status ON complaints(status);
CREATE INDEX IF NOT EXISTS idx_complaints_technician ON complaints(technician);
"""


def _logger():
    return logger_manager.get_logger("state_manager")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _infer_technician_from_path(file_path: str) -> str:
    """Infers technician folder name from a PDF file path when not explicitly provided."""
    if not file_path:
        return "Unknown"
    try:
        parent_name = os.path.basename(os.path.dirname(os.path.abspath(file_path)))
        if parent_name and parent_name not in ("Today_Complaints", "All_PDFs", "Manual_Batch", ".", ""):
            return parent_name
    except Exception:
        pass
    return "Unknown"


def resolve_db_path(target: Optional[Union[str, Dict]] = None) -> Tuple[str, Optional[str]]:
    """
    Resolves a database path or legacy tracker path into:
        (sqlite_db_path, legacy_txt_path_if_passed)
    Supports:
      - None: resolves default 'complaints_state.db' from config_manager.
      - dict (config): resolves settings['database_file'] from config.
      - str ending with .db/.sqlite/.sqlite3: resolves directly.
      - str ending with .txt (legacy caller compatibility): maps to 'complaints_state.db'
        in the same directory as the legacy .txt path and returns the .txt path for
        automatic on-the-fly migration if present.
    """
    if target is None:
        return config_manager.get_database_path(), None

    if isinstance(target, dict):
        return config_manager.get_database_path(target), None

    raw = str(target).strip()
    if not raw:
        return config_manager.get_database_path(), None

    resolved = config_manager.resolve_path(raw)
    low = resolved.lower()
    if low.endswith((".db", ".sqlite", ".sqlite3")):
        return resolved, None

    # Legacy .txt path passed by caller: map to complaints_state.db in the same directory
    parent_dir = os.path.dirname(os.path.abspath(resolved))
    db_path = os.path.join(parent_dir, "complaints_state.db")
    return db_path, resolved


@contextmanager
def _connect_db(db_path: str) -> Iterator[sqlite3.Connection]:
    """
    Context manager that opens a SQLite connection with WAL mode, busy timeout,
    and automatic schema initialization, then cleanly closes the connection on exit.
    """
    parent_dir = os.path.dirname(os.path.abspath(db_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(_SCHEMA_SQL)
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def init_db(
    target: Optional[Union[str, Dict]] = None,
    output_dir: Optional[str] = None,
    sync_disk: bool = True,
    announce_sync: bool = True,
) -> str:
    """
    Initializes the SQLite database file, WAL journal mode, table schema, and indexes.
    When sync_disk is True, also runs an idempotent Startup Disk-to-DB Sync against Today_Complaints/.
    Returns the absolute path to the initialized SQLite database.
    """
    db_path, legacy_txt = resolve_db_path(target)
    with _connect_db(db_path) as conn:
        conn.commit()
    if legacy_txt and os.path.exists(legacy_txt):
        _migrate_single_legacy_txt(db_path, legacy_txt, output_dir=output_dir)
    if sync_disk:
        sync_disk_to_db(target=target, output_dir=output_dir, announce=announce_sync)
    _logger().debug("Initialized SQLite state database at '%s'.", db_path)
    return db_path


def _resolve_output_dir(target: Optional[Union[str, Dict]], db_path: str, output_dir: Optional[str] = None) -> str:
    """Resolves the Today_Complaints directory path corresponding to target or db_path."""
    if output_dir and str(output_dir).strip():
        return os.path.abspath(str(output_dir).strip())
    if isinstance(target, dict):
        settings = target.get("settings", config_manager.DEFAULT_CONFIG["settings"])
        return config_manager.resolve_path(settings.get("output_dir", "Today_Complaints"))
    db_parent = os.path.dirname(os.path.abspath(db_path))
    if db_parent:
        return os.path.join(db_parent, "Today_Complaints")
    return config_manager.resolve_path("Today_Complaints")


def _discover_existing_pdfs(output_dir: Optional[str]) -> Dict[str, Tuple[str, str]]:
    """
    Scans Today_Complaints/ (excluding 'Manual_Batch') for valid numeric PDFs (^\\d+\\.pdf$)
    with size > 0 bytes, mapping complaint_id -> (technician, abs_pdf_path).
    If a complaint PDF exists in both 'All_PDFs'/root ('Unknown') and a named technician
    folder, the named technician folder takes precedence.
    """
    mapping: Dict[str, Tuple[str, str]] = {}
    if not output_dir or not os.path.exists(output_dir):
        return mapping

    try:
        for root, dirs, files in os.walk(output_dir):
            # Exclude Manual_Batch directory so manual one-off runs do not contaminate bulk state
            dirs[:] = sorted(d for d in dirs if d.lower() != "manual_batch" and not d.startswith("."))
            for fname in sorted(files):
                if fname.startswith("._") or not re.fullmatch(r"\d+\.pdf", fname, flags=re.IGNORECASE):
                    continue
                full_path = os.path.abspath(os.path.join(root, fname))
                try:
                    if not os.path.isfile(full_path) or os.path.getsize(full_path) <= 0:
                        continue
                except OSError:
                    continue

                cid = os.path.splitext(fname)[0].strip()
                tech = _infer_technician_from_path(full_path)
                prev = mapping.get(cid)
                if prev is None or (prev[0] == "Unknown" and tech != "Unknown"):
                    mapping[cid] = (tech, full_path)
    except Exception as e:
        _logger().debug("Could not scan output_dir '%s' during PDF discovery: %s", output_dir, e)
    return mapping


def sync_disk_to_db(
    target: Optional[Union[str, Dict]] = None,
    output_dir: Optional[str] = None,
    announce: bool = True,
) -> int:
    """
    Idempotent Startup Disk-to-DB Sync:
    Scans Today_Complaints/ (all technician subdirectories except 'Manual_Batch', plus root/All_PDFs)
    for valid numeric PDFs (^\\d+\\.pdf$, size > 0 bytes) and registers any missing records in SQLite
    with status='DOWNLOADED', technician=<folder>, and file_path=<resolved_pdf_path>.
    Returns the number of newly synced PDF records inserted into the database.
    """
    db_path, legacy_txt = resolve_db_path(target)
    resolved_output_dir = _resolve_output_dir(target, db_path, output_dir)

    if legacy_txt and os.path.exists(legacy_txt):
        _migrate_single_legacy_txt(db_path, legacy_txt, output_dir=resolved_output_dir)

    pdf_map = _discover_existing_pdfs(resolved_output_dir)
    if not pdf_map:
        return 0

    now = _now_str()
    newly_synced = 0

    try:
        with _connect_db(db_path) as conn:
            existing_rows = {
                str(row["complaint_id"]).strip(): (
                    str(row["technician"] or "Unknown"),
                    str(row["file_path"] or ""),
                )
                for row in conn.execute("SELECT complaint_id, technician, file_path FROM complaints").fetchall()
                if row["complaint_id"]
            }

            to_insert = []
            to_update_meta = []
            for cid, (tech, fpath) in pdf_map.items():
                if cid not in existing_rows:
                    to_insert.append((cid, tech, fpath, now, now))
                else:
                    curr_tech, curr_path = existing_rows[cid]
                    new_tech = tech if (curr_tech == "Unknown" and tech != "Unknown") else curr_tech
                    new_path = fpath if (not curr_path or not os.path.exists(curr_path)) else curr_path
                    if new_tech != curr_tech or new_path != curr_path:
                        to_update_meta.append((new_tech, new_path, cid))

            if to_insert or to_update_meta:
                with conn:
                    if to_insert:
                        conn.executemany(
                            """
                            INSERT OR IGNORE INTO complaints
                                (complaint_id, status, technician, file_path, printer_name, job_id, created_at, updated_at)
                            VALUES (?, 'DOWNLOADED', ?, ?, '', NULL, ?, ?)
                            """,
                            to_insert,
                        )
                        newly_synced = len(to_insert)
                    if to_update_meta:
                        conn.executemany(
                            """
                            UPDATE complaints
                            SET technician = ?, file_path = ?
                            WHERE complaint_id = ?
                            """,
                            to_update_meta,
                        )
    except Exception as e:
        _logger().error("[!] Error during Startup Disk-to-DB Sync on '%s': %s", db_path, e, exc_info=True)
        return 0

    if newly_synced > 0 and announce:
        folder_label = os.path.basename(os.path.normpath(resolved_output_dir)) or "Today_Complaints"
        _logger().info("[+] Synced %d existing PDFs from %s/ into database.", newly_synced, folder_label)

    return newly_synced


def _archive_migrated_txt(txt_path: str) -> Optional[str]:
    """Renames a migrated legacy .txt file to .migrated.bak (or leaves no stale .txt if empty)."""
    if not txt_path or not os.path.exists(txt_path):
        return None

    bak_path = f"{txt_path}.migrated.bak"
    if os.path.exists(bak_path):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_path = f"{txt_path}.{ts}.migrated.bak"

    try:
        os.replace(txt_path, bak_path)
        _logger().info("[*] Migrated legacy tracking file archived: %s -> %s", os.path.basename(txt_path), os.path.basename(bak_path))
        return bak_path
    except Exception as e:
        _logger().warning("[!] Could not rename migrated file '%s' to '%s': %s", txt_path, bak_path, e)
        return None


def _parse_printed_txt_line(line: str) -> Optional[Tuple[str, str, str, str, str, Optional[int]]]:
    """
    Parses a line from printed_complaints.txt.
    Returns (complaint_id, status, timestamp, printer_name, file_name, job_id) or None.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    parts = [p.strip() for p in stripped.split("|")]
    cid = parts[0].split(",")[0].strip()
    if not cid:
        return None

    now = _now_str()
    ts = parts[1] if len(parts) >= 2 and parts[1] else now
    printer_name = parts[2] if len(parts) >= 3 else ""
    file_name = parts[3] if len(parts) >= 4 and parts[3] != "N/A" else ""

    status = "PRINTED"
    if len(parts) >= 5:
        raw_status = parts[4].upper()
        if raw_status == "PRINTED":
            status = "PRINTED"
        elif raw_status == "SPOOLED":
            status = "SPOOLED"
        else:
            status = "PRINT_FAILED"

    job_id: Optional[int] = None
    if len(parts) >= 6 and parts[5].lower().startswith("job_id="):
        try:
            job_id = int(parts[5].split("=", 1)[1].strip())
        except (ValueError, TypeError):
            job_id = None

    return cid, status, ts, printer_name, file_name, job_id


def _migrate_single_legacy_txt(db_path: str, txt_path: str, output_dir: Optional[str] = None) -> int:
    """
    Migrates a single legacy .txt file (completed_complaints.txt or printed_complaints.txt)
    into db_path if it exists and has not been migrated yet.
    """
    if not txt_path or not os.path.exists(txt_path):
        return 0

    bname = os.path.basename(txt_path).lower()
    if "printed" in bname:
        res = migrate_legacy_txt_files(
            db_path=db_path, completed_txt_path=None, printed_txt_path=txt_path, output_dir=output_dir
        )
        return res.get("printed_migrated", 0)
    else:
        res = migrate_legacy_txt_files(
            db_path=db_path, completed_txt_path=txt_path, printed_txt_path=None, output_dir=output_dir
        )
        return res.get("completed_migrated", 0)


def migrate_legacy_txt_files(
    db_path: Optional[str] = None,
    completed_txt_path: Optional[str] = None,
    printed_txt_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    archive_after_migrate: bool = True,
) -> Dict[str, int]:
    """
    Automatically migrates legacy completed_complaints.txt and printed_complaints.txt
    records into the SQLite 'complaints' table in an atomic transaction, then renames
    non-empty migrated .txt files to .migrated.bak.
    """
    resolved_db, _ = resolve_db_path(db_path)
    pdf_map = _discover_existing_pdfs(output_dir)
    now = _now_str()

    completed_ids = []
    if completed_txt_path and os.path.exists(completed_txt_path):
        try:
            with open(completed_txt_path, "r", encoding="utf-8") as f:
                for line in f:
                    cid = line.strip()
                    if cid and not cid.startswith("#"):
                        completed_ids.append(cid)
        except Exception as e:
            _logger().error("[!] Failed reading legacy file '%s' during migration: %s", completed_txt_path, e)
            raise

    printed_entries = []
    if printed_txt_path and os.path.exists(printed_txt_path):
        try:
            with open(printed_txt_path, "r", encoding="utf-8") as f:
                for line in f:
                    parsed = _parse_printed_txt_line(line)
                    if parsed:
                        printed_entries.append(parsed)
        except Exception as e:
            _logger().error("[!] Failed reading legacy print file '%s' during migration: %s", printed_txt_path, e)
            raise

    if not completed_ids and not printed_entries:
        # Remove 0-byte placeholder .txt files if archive_after_migrate is requested
        for empty_txt in (completed_txt_path, printed_txt_path):
            if archive_after_migrate and empty_txt and os.path.exists(empty_txt):
                try:
                    if os.path.getsize(empty_txt) == 0:
                        os.remove(empty_txt)
                        _logger().debug("Removed empty legacy placeholder file: %s", empty_txt)
                    else:
                        _archive_migrated_txt(empty_txt)
                except Exception:
                    pass
        return {"completed_migrated": 0, "printed_migrated": 0}

    with _connect_db(resolved_db) as conn:
        with conn:
            # 1. Insert DOWNLOADED records from completed_complaints.txt
            for cid in completed_ids:
                tech, fpath = pdf_map.get(cid, ("Unknown", ""))
                conn.execute(
                    """
                    INSERT INTO complaints (complaint_id, status, technician, file_path, printer_name, job_id, created_at, updated_at)
                    VALUES (?, 'DOWNLOADED', ?, ?, '', NULL, ?, ?)
                    ON CONFLICT(complaint_id) DO UPDATE SET
                        technician = CASE
                            WHEN complaints.technician = 'Unknown' AND excluded.technician != 'Unknown'
                            THEN excluded.technician
                            ELSE complaints.technician
                        END,
                        file_path = CASE
                            WHEN (complaints.file_path IS NULL OR complaints.file_path = '') AND excluded.file_path != ''
                            THEN excluded.file_path
                            ELSE complaints.file_path
                        END
                    """,
                    (cid, tech, fpath, now, now),
                )

            # 2. Apply chronological state transitions from printed_complaints.txt
            for cid, status, ts, printer_name, file_name, job_id in printed_entries:
                tech, discovered_path = pdf_map.get(cid, ("Unknown", ""))
                fpath = discovered_path or file_name
                if tech == "Unknown" and fpath:
                    tech = _infer_technician_from_path(fpath)
                conn.execute(
                    """
                    INSERT INTO complaints (complaint_id, status, technician, file_path, printer_name, job_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(complaint_id) DO UPDATE SET
                        status = excluded.status,
                        technician = CASE
                            WHEN complaints.technician = 'Unknown' AND excluded.technician != 'Unknown'
                            THEN excluded.technician
                            ELSE complaints.technician
                        END,
                        file_path = CASE
                            WHEN excluded.file_path != '' THEN excluded.file_path
                            ELSE complaints.file_path
                        END,
                        printer_name = CASE
                            WHEN excluded.printer_name != '' THEN excluded.printer_name
                            ELSE complaints.printer_name
                        END,
                        job_id = excluded.job_id,
                        updated_at = excluded.updated_at
                    """,
                    (cid, status, tech, fpath, printer_name, job_id, ts, ts),
                )

    _logger().info(
        "[+] SQLite migration complete: %d downloaded record(s), %d print log entry(s) imported into '%s'.",
        len(completed_ids),
        len(printed_entries),
        os.path.basename(resolved_db),
    )

    if archive_after_migrate:
        for p in (completed_txt_path, printed_txt_path):
            if p and os.path.exists(p):
                if os.path.getsize(p) > 0:
                    _archive_migrated_txt(p)
                else:
                    try:
                        os.remove(p)
                    except Exception:
                        pass

    return {
        "completed_migrated": len(completed_ids),
        "printed_migrated": len(printed_entries),
    }


def get_completed_complaints(target: Optional[Union[str, Dict]] = None) -> Set[str]:
    """
    Returns the set of all complaint IDs that have been downloaded/recorded in SQLite
    (status in DOWNLOADED, SPOOLED, PRINTED, PRINT_FAILED).
    """
    db_path, legacy_txt = resolve_db_path(target)
    if legacy_txt and os.path.exists(legacy_txt):
        _migrate_single_legacy_txt(db_path, legacy_txt)

    if not os.path.exists(db_path):
        return set()

    try:
        with _connect_db(db_path) as conn:
            cursor = conn.execute(
                "SELECT complaint_id FROM complaints WHERE status IN ('DOWNLOADED', 'SPOOLED', 'PRINTED', 'PRINT_FAILED')"
            )
            return {str(row["complaint_id"]).strip() for row in cursor.fetchall() if row["complaint_id"]}
    except Exception as e:
        _logger().error("[!] Failed reading completed complaints from '%s': %s", db_path, e, exc_info=True)
        return set()


def load_printed_complaints(target: Optional[Union[str, Dict]] = None) -> Set[str]:
    """
    Loads all confirmed PRINTED complaint IDs from the SQLite database.
    Complaints whose status is DOWNLOADED, SPOOLED, or PRINT_FAILED are excluded
    so they remain eligible for printing or retry.
    Propagates database errors to the caller so batch print never silently double-prints.
    """
    db_path, legacy_txt = resolve_db_path(target)
    if legacy_txt and os.path.exists(legacy_txt):
        _migrate_single_legacy_txt(db_path, legacy_txt)

    if not os.path.exists(db_path):
        return set()

    try:
        with _connect_db(db_path) as conn:
            cursor = conn.execute("SELECT complaint_id FROM complaints WHERE status = 'PRINTED'")
            return {str(row["complaint_id"]).strip() for row in cursor.fetchall() if row["complaint_id"]}
    except Exception as e:
        _logger().error("[!] Error reading printed complaints from '%s': %s", db_path, e, exc_info=True)
        raise


def mark_complaint_downloaded(
    target: Optional[Union[str, Dict]],
    complaint_no: str,
    technician: str = "Unknown",
    file_path: str = "",
) -> bool:
    """
    Records a complaint PDF as DOWNLOADED in SQLite.
    If the complaint was already marked PRINTED, preserves PRINTED while updating metadata.
    """
    cid = str(complaint_no).strip()
    if not cid:
        raise ValueError("complaint_no cannot be empty.")

    db_path, legacy_txt = resolve_db_path(target)
    if legacy_txt and os.path.exists(legacy_txt):
        _migrate_single_legacy_txt(db_path, legacy_txt)

    tech = str(technician).strip() if technician else _infer_technician_from_path(file_path)
    if not tech or tech.lower() == "nan":
        tech = "Unknown"
    fpath = str(file_path or "").strip()
    now = _now_str()

    try:
        with _connect_db(db_path) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO complaints (complaint_id, status, technician, file_path, printer_name, job_id, created_at, updated_at)
                    VALUES (?, 'DOWNLOADED', ?, ?, '', NULL, ?, ?)
                    ON CONFLICT(complaint_id) DO UPDATE SET
                        status = CASE
                            WHEN complaints.status = 'PRINTED' THEN 'PRINTED'
                            ELSE 'DOWNLOADED'
                        END,
                        technician = CASE
                            WHEN excluded.technician != 'Unknown' THEN excluded.technician
                            ELSE complaints.technician
                        END,
                        file_path = CASE
                            WHEN excluded.file_path != '' THEN excluded.file_path
                            ELSE complaints.file_path
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (cid, tech, fpath, now, now),
                )
        _logger().debug("Recorded complaint #%s as DOWNLOADED (tech='%s', file='%s').", cid, tech, fpath)
        return True
    except Exception as e:
        _logger().error("[!] Failed to record complaint #%s as DOWNLOADED in '%s': %s", cid, db_path, e, exc_info=True)
        raise


def _upsert_print_state(
    target: Optional[Union[str, Dict]],
    complaint_no: str,
    status: str,
    file_path: str = "",
    printer_name: str = "",
    job_id: Optional[int] = None,
    technician: Optional[str] = None,
) -> bool:
    """Internal helper to atomically upsert SPOOLED, PRINTED, or PRINT_FAILED state."""
    cid = str(complaint_no).strip()
    if not cid:
        raise ValueError("complaint_no cannot be empty.")

    norm_status = str(status or "PRINTED").strip().upper()
    if norm_status not in VALID_STATUSES:
        norm_status = "PRINT_FAILED"

    db_path, legacy_txt = resolve_db_path(target)
    if legacy_txt and os.path.exists(legacy_txt):
        _migrate_single_legacy_txt(db_path, legacy_txt)

    fpath = str(file_path or "").strip()
    tech = str(technician).strip() if technician else _infer_technician_from_path(fpath)
    if not tech or tech.lower() == "nan":
        tech = "Unknown"
    printer_label = str(printer_name or "Default").strip()
    valid_job_id = job_id if (isinstance(job_id, int) and not isinstance(job_id, bool) and job_id > 0) else None
    now = _now_str()

    try:
        with _connect_db(db_path) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO complaints (complaint_id, status, technician, file_path, printer_name, job_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(complaint_id) DO UPDATE SET
                        status = excluded.status,
                        technician = CASE
                            WHEN complaints.technician = 'Unknown' AND excluded.technician != 'Unknown'
                            THEN excluded.technician
                            ELSE complaints.technician
                        END,
                        file_path = CASE
                            WHEN excluded.file_path != '' THEN excluded.file_path
                            ELSE complaints.file_path
                        END,
                        printer_name = CASE
                            WHEN excluded.printer_name != '' THEN excluded.printer_name
                            ELSE complaints.printer_name
                        END,
                        job_id = excluded.job_id,
                        updated_at = excluded.updated_at
                    """,
                    (cid, norm_status, tech, fpath, printer_label, valid_job_id, now, now),
                )
        _logger().debug(
            "Updated complaint #%s status -> %s (printer='%s', job_id=%s).",
            cid,
            norm_status,
            printer_label,
            valid_job_id,
        )
        return True
    except Exception as e:
        _logger().error(
            "[!] Error updating complaint #%s to %s in '%s': %s", cid, norm_status, db_path, e, exc_info=True
        )
        raise


def mark_complaint_spooled(
    target: Optional[Union[str, Dict]],
    complaint_no: str,
    file_path: str = "",
    printer_name: str = "",
    job_id: Optional[int] = None,
    technician: Optional[str] = None,
) -> bool:
    """Records a complaint print job as SPOOLED in SQLite."""
    return _upsert_print_state(
        target,
        complaint_no,
        status="SPOOLED",
        file_path=file_path,
        printer_name=printer_name,
        job_id=job_id,
        technician=technician,
    )


def mark_complaint_printed(
    target: Optional[Union[str, Dict]],
    complaint_no: str,
    file_path: str = "",
    printer_name: str = "",
    status: str = "PRINTED",
    job_id: Optional[int] = None,
    technician: Optional[str] = None,
) -> bool:
    """Atomically records a complaint as confirmed PRINTED (or fallback status) in SQLite."""
    return _upsert_print_state(
        target,
        complaint_no,
        status=status,
        file_path=file_path,
        printer_name=printer_name,
        job_id=job_id,
        technician=technician,
    )


def mark_complaint_print_failed(
    target: Optional[Union[str, Dict]],
    complaint_no: str,
    file_path: str = "",
    printer_name: str = "",
    job_id: Optional[int] = None,
    reason: str = "",
) -> bool:
    """
    Records a complaint print job as PRINT_FAILED in SQLite so its PDF download state is
    retained while keeping it eligible for print retry.
    """
    if reason:
        _logger().warning("Print job failed for complaint #%s: %s", complaint_no, reason)
    return _upsert_print_state(
        target,
        complaint_no,
        status="PRINT_FAILED",
        file_path=file_path,
        printer_name=printer_name,
        job_id=job_id,
    )


def reset_printed_tracker_safe(target: Optional[Union[str, Dict]] = None) -> Optional[str]:
    """
    Safely archives the SQLite database to a timestamped backup ('complaints_state_<ts>.db.bak')
    and downgrades all SPOOLED, PRINTED, and PRINT_FAILED records back to 'DOWNLOADED'.
    Preserves ERP download state so Mode [1] never unnecessarily re-downloads existing PDFs.
    If creating the backup fails, aborts before modifying any records.
    """
    db_path, legacy_txt = resolve_db_path(target)
    if legacy_txt and os.path.exists(legacy_txt):
        _migrate_single_legacy_txt(db_path, legacy_txt)

    backup_path = None
    with _connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM complaints WHERE status IN ('SPOOLED', 'PRINTED', 'PRINT_FAILED')"
        ).fetchone()
        has_print_records = bool(row and row["cnt"] > 0)

        if has_print_records:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_dir = os.path.dirname(os.path.abspath(db_path))
            backup_name = f"complaints_state_{ts}.db.bak"
            backup_path = os.path.join(base_dir, backup_name)
            try:
                # Use SQLite online backup API for WAL-safe consistent snapshot
                bck_conn = sqlite3.connect(backup_path)
                try:
                    conn.backup(bck_conn)
                finally:
                    bck_conn.close()
                if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
                    raise OSError(f"Backup file '{backup_path}' was not created or is empty.")
                _logger().info(" [✓] Previous print tracker database archived to: %s", backup_name)
            except Exception as e:
                _logger().error(
                    " [!] Error: Failed to create archive backup '%s': %s", backup_path, e, exc_info=True
                )
                _logger().error(" [!] Aborting tracker reset to preserve existing print history.")
                raise

        now = _now_str()
        with conn:
            conn.execute(
                """
                UPDATE complaints
                SET status = 'DOWNLOADED',
                    job_id = NULL,
                    updated_at = ?
                WHERE status IN ('SPOOLED', 'PRINTED', 'PRINT_FAILED')
                """,
                (now,),
            )

    _logger().info(" [✓] Print tracker states in '%s' reset to clean state (DOWNLOADED preserved).", os.path.basename(db_path))
    return backup_path


def get_complaint_record(complaint_no: str, target: Optional[Union[str, Dict]] = None) -> Optional[Dict]:
    """Fetches the full SQLite row dictionary for a single complaint_id, or None if not found."""
    cid = str(complaint_no).strip()
    if not cid:
        return None
    db_path, _ = resolve_db_path(target)
    if not os.path.exists(db_path):
        return None
    with _connect_db(db_path) as conn:
        row = conn.execute("SELECT * FROM complaints WHERE complaint_id = ?", (cid,)).fetchone()
        return dict(row) if row else None


def get_status_counts(target: Optional[Union[str, Dict]] = None) -> Dict[str, int]:
    """Returns counts of complaints grouped by status, plus TOTAL."""
    counts = {"TOTAL": 0, "DOWNLOADED": 0, "SPOOLED": 0, "PRINTED": 0, "PRINT_FAILED": 0}
    db_path, _ = resolve_db_path(target)
    if not os.path.exists(db_path):
        return counts
    with _connect_db(db_path) as conn:
        for row in conn.execute("SELECT status, COUNT(*) AS cnt FROM complaints GROUP BY status"):
            st = row["status"]
            cnt = int(row["cnt"])
            if st in counts:
                counts[st] = cnt
            counts["TOTAL"] += cnt
    return counts
