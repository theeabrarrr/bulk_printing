# Changelog

All notable changes to **ERP_Automation_Suite** are documented in this file.

## [6.3.0] - 2026-11-23
### Added
- **Direct Browser Print (No PDF Save) Workflow (`main.py`, `engine.py`, `config.json`, `config_manager.py`)**:
  - Added interactive print workflow selector (`_prompt_print_workflow_mode`) in both **Bulk Mode `[1]`** and **Manual Mode `[2]`** to choose between `[1] Save PDF to Disk` (existing `Today_Complaints/` workflow) and `[2] Direct Browser Print (No PDF Save - Straight to Printer)` with validated physical printer confirmation.
  - Added `direct_print=True` execution path in `engine.process_complaint` and `engine.run_batch` that skips Save As dialogs/PDF disk output and sends `Ctrl+P` -> `Enter` straight to the target physical printer, using `direct_print_spool_seconds` (default `3.0` s) in `timings`.
  - If a matching PDF already exists on disk during Direct Browser Print in Bulk Mode, dispatches it directly to the physical printer via `_dispatch_live_print` without re-opening the browser print view.
- **Automatic Windows OS Default Printer Guard (`printer_manager.py`)**:
  - Added `get_default_printer()`, `set_default_printer()`, and the `temporary_default_printer()` context manager so Direct Browser Print temporarily sets the Windows OS default printer to the validated physical printer (and Save PDF mode sets `Microsoft Print to PDF` when installed) and guarantees restoration of the prior OS default printer inside a `try/finally` block on completion, error, or abort (`[ESC]` / `Ctrl+C`).
- **Sliding-Window `win32print` Spool Verification (`printer_manager.py`, `engine.py`)**:
  - Added `wait_for_direct_print_spool()` to capture prior job IDs, detect newly spooled jobs (`job_id not in prior_job_ids`), catch immediate hardware/spooler errors, and safely allow fast-drained jobs.
  - Integrated `enforce_queue_gate`, `reconcile_in_flight_jobs`, and `drain_in_flight_jobs` into Direct Browser Print so active jobs progress cleanly through `SPOOLED` -> `PRINTED` (or `PRINT_FAILED`) in `complaints_state.db`.
- **Automated Test Suite (`test/test_direct_print.py`)**:
  - Added 9 unit and integration tests covering `temporary_default_printer` restoration, `wait_for_direct_print_spool`, direct-print vs saved-PDF reset semantics, `engine.run_batch` / `engine.process_complaint`, and CLI prompts for Bulk `[1]` and Manual `[2]`.

### Changed
- **Updated `[R]` Print Tracker Reset Behavior (`state_manager.py`)**:
  - Updated `reset_printed_tracker_safe()` (triggered by `[R]` in Mode `[5]`) so that after creating a timestamped `.db.bak` snapshot, direct-print rows (`status IN ('SPOOLED', 'PRINTED', 'PRINT_FAILED')` with empty `file_path`) are deleted so they can be re-printed cleanly from ERP, while saved-PDF rows (`file_path != ''`) remain preserved and are downgraded back to `DOWNLOADED`.
  - Added `technician` propagation to `mark_complaint_spooled`, `mark_complaint_printed`, and `mark_complaint_print_failed`.

## [6.2.0] - 2026-11-22
### Added
- **Batch Technician Remarks PDF Stamping (`[7]`, `pdf_remarks.py`)**:
  - Pre-flight validation for `complaint_remarks.xlsx`, recursive PDF matching in `Today_Complaints/`, dynamic vertical border anchoring, auto-scaling Regular Helvetica text (`9.5 pt` down to `6.0 pt` with word-boundary `...` fallback), atomic saves into mirrored `Stamped_Complaints/` folders, and CSV issue reports (`logs/remarks_report_*.csv`).
- **Batch Print Source Switching (`[5]`)**:
  - Added `[S]` source switching between `Today_Complaints/` (Original) and `Stamped_Complaints/` (With Remarks).
- **Automated Test Suite (`test/test_pdf_remarks.py`)**:
  - Added 19 unit and integration tests covering config schema fallback, remarks Excel validation, dynamic border anchoring, font auto-scaling/truncation, and source switching.

## [6.1.2] - 2026-11-16
### Fixed
- Hardened spooler query timeout propagation across dispatch paths and `enforce_queue_gate`, thresholded monochrome fallback `ImageWin.Dib` output, deduplicated active `SpoolerStatusProbe` threads per printer, and kept unobserved in-flight jobs pending on polling query errors.

## [6.1.1] - 2026-11-16
### Fixed
- Fixed PCL XL buffer overflow via 1-bit monochrome GDI `StretchDIBits` streaming (<0.5 MB/page), programmatic `DMCOLOR_MONOCHROME` DEVMODE negotiation, and non-blocking spooler query timeout guard.

## [6.1.0] - 2026-11-14
### Added
- Added in-app self-updater (`updater.py`, `version.py`) with SHA-256 verification and atomic `.bat` swap/rollback, plus automated Disk-to-DB PDF sync (`state_manager.py`).
