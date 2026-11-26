# Oracle ERP Automation Suite (v6.3.1: Hardened OTP Generation, Direct Browser Printing, Remarks PDF Stamping, In-App Self-Updater & Enterprise SQLite State)

An automated RPA (Robotic Process Automation) application that extracts complaint job cards from an Oracle Forms ERP system, generates PDFs via Internet Explorer / Foxit Reader (or sends print jobs straight to a physical printer with **Direct Browser Print**), automatically sorts saved PDFs into technician folders (`Today_Complaints/<Technician_Name>/<Complaint_No>.pdf`), stamps technician remarks onto matching PDFs (`Stamped_Complaints/`), performs **Universal Silent Physical Printing**, automates single/batch **OTP Generation** with visual modal transition guards, and includes an **In-App Self-Updater** via GitHub Releases.

## Key Enterprise Features (v6.3.1)
- **Hardened Interactive OTP Generation Mode (`[4]`, `engine.py`, `main.py`, `config_manager.py`, `config.json`)**:
  - **6-Stage Visual Modal Transition State Machine**: Uses zero-calibration screen-bounds-clamped grayscale patch diffing (`Pillow` `ImageGrab` + `ImageChops`) to verify that the OTP `Add` sub-window opens, the Complaint Search LOV modal opens and closes cleanly after querying, the centered OTP Reason LOV opens (trapping any active Oracle Alert popup before `Enter x4` commits), and the sub-window closes back to the primary screen.
  - **Tunable `config.json` Query & Modal Timings**: Splits `otp_query_wait_seconds` (default `2.5` s) from `otp_enter_delay_seconds` (`0.4` s) and exposes all OTP delays (`otp_start_countdown_seconds`, `otp_input_focus_delay_seconds`, `otp_post_paste_delay_seconds`, `otp_modal_close_timeout_seconds`, `otp_post_select_delay_seconds`, `otp_between_complaints_seconds`) and screen-transition guards (`otp_verify_screen_transitions`, `otp_screen_diff_threshold`) in `config.json` with type-safe validation (`get_timings_settings()`, `get_otp_guard_settings()`).
  - **Edge IE Mode / Java Applet Foreground Guard & `[ESC]`-Chunked Sleeps**: Verifies active window focus across parent `msedge.exe`/`iexplore.exe` and child Java (`jp2launcher.exe`/`java.exe`/`javaw.exe`) LOV dialogs while immediately halting if focus shifts to the CLI console or unrelated apps, with `50ms` chunked `sleep_interruptible()` for instant `[ESC]` response.
  - **Input Sanitization & Copy-Pasteable Remainder Summary**: Strips `.0` float suffixes, filters pasted Excel headers (`COMPLAINT_NO`, `#REF!`), deduplicates IDs preserving order, and halts the batch immediately with an audible alert chime and a copy-pasteable list of remaining complaint IDs if any complaint stalls.
- **Direct Browser Print (No PDF Save) & OS Default Printer Guard (`engine.py`, `printer_manager.py`, `main.py`)**:
  - In **Bulk Mode (`[1]`)** and **Manual Mode (`[2]`)**, operators choose between **`[1] Save PDF to Disk`** (existing `Today_Complaints/` workflow) and **`[2] Direct Browser Print (No PDF Save - Straight to Printer)`**.
  - **Automatic Windows OS Default Printer Guard (`temporary_default_printer`)**: Temporarily sets the Windows OS default printer (`win32print.SetDefaultPrinter`) to the validated physical printer during Direct Browser Print (or `Microsoft Print to PDF` during Save PDF mode) and strictly restores the original OS default printer inside a `try/finally` block on completion, error, or abort (`[ESC]` / `Ctrl+C`).
  - **Sliding-Window `win32print` Spool Verification**: Captures prior spooler job IDs, submits browser `Ctrl+P`, verifies new spool entries and hardware status via `wait_for_direct_print_spool` (with configurable `direct_print_spool_seconds`), enforces the `max_spool_queue` window via `enforce_queue_gate`, reconciles in-flight jobs (`SPOOLED` -> `PRINTED` or `PRINT_FAILED`) in `complaints_state.db`, and drains remaining spooled jobs at batch completion.
  - **Direct-Print Tracker Reset (`[R]` in Mode `[5]`)**: Creates a timestamped `.db.bak` snapshot, deletes direct-print rows that have an empty `file_path` so they can be re-printed cleanly from the ERP, and downgrades saved-PDF rows (`file_path != ''`) back to `DOWNLOADED`.
- **In-App Self-Updater & Atomic Binary Swap (`updater.py`, `version.py`)**:
  - Performs a fast (3-second timeout) non-blocking update check against GitHub Releases on startup and offers an on-demand **`[U] Check for Updates`** menu option.
  - Downloads release archives (`ERP_Automation_Suite_vX.Y.Z.zip`), verifies SHA-256 checksum integrity, and spawns a detached Windows helper (`_run_update.bat`) that waits for the active process to exit (`tasklist`), atomically replaces `ERP_Automation_Suite.exe` and `_internal/` with rollback protection, and restarts automatically while strictly preserving `config.json`, `complaints_state.db`, `complaints.xlsx`, `complaint_remarks.xlsx`, `Today_Complaints/`, `Stamped_Complaints/`, and `logs/`.
- **ACID-Compliant SQLite State Management (`complaints_state.db`)**:
  - Replaces legacy flat-file trackers with a crash-safe SQLite database operating in Write-Ahead Logging (`WAL`) mode.
  - Tracks each complaint across progressive states (`DOWNLOADED`, `SPOOLED`, `PRINTED`, `PRINT_FAILED`) alongside technician name, PDF path, printer name, spooler job ID, and timestamps.
  - Automatically migrates legacy `completed_complaints.txt` and `printed_complaints.txt` on startup and archives them as `.migrated.bak`.
  - Non-destructive print tracker reset (`[R]` in Mode `[5]`) creates a timestamped `.db.bak` snapshot and downgrades print statuses back to `DOWNLOADED` so ERP downloads are never lost.
- **Centralized Rotating Logging (`logs/app.log`)**:
  - Outputs clean, operator-friendly status messages (`INFO`+) to the console while writing detailed technical logs, RPA coordinate steps, spooler diagnostics (`DEBUG`+), and full stack traces (`ERROR`/`CRITICAL`) to `logs/app.log` (5 MB max size, 5 rotating backup files).
- **Strict Excel Pre-Flight Validation**:
  - Normalizes column headers, strips float `.0` suffixes, rejects blank rows, formula errors (`#REF!`, `#N/A`), and non-numeric `COMPLAINT_NO` entries (logging exact Excel row numbers), deduplicates repeated IDs, sanitizes `TECHNICIAN_NAME` (defaulting blanks to `'Unknown'`), and displays a **Pre-Flight Validation Summary** before execution.
- **100% Invisible Headless GDI Rendering & Spooler Queue Monitoring**:
  - Uses `pypdfium2` and Windows GDI Printer Device Contexts (`win32ui`/`win32print`) to stream pages directly into the Windows Print Spooler without external PDF viewer pop-ups.
  - Pauses with audible alarms if the spooler queue stalls or reports hardware errors (paper jam, out-of-paper, offline, paused).
- **Batch Technician Remarks PDF Stamping (`pdf_remarks.py`)**:
  - Reads `COMPLAINT_NO` and `REMARKS` from `complaint_remarks.xlsx` (normalizing headers, stripping `.0` float suffixes, skipping blank remarks, and keeping the last non-empty remark for duplicate IDs with row-level warnings).
  - Recursively scans `Today_Complaints/` (or a custom input folder), dynamically anchors vertically to the Technician Remarks border (compensating for layout-shifted forms), and transparently overlays wrapped Regular Helvetica text (`9.5 pt` auto-scaling down to `6.0 pt` in `0.5 pt` steps, with word-boundary `...` truncation fallback) without altering existing PDF lines or layout.
  - Writes matched stamped PDFs atomically into mirrored technician subfolders under `Stamped_Complaints/` and exports a timestamped CSV report (`logs/remarks_report_YYYYMMDD_HHMMSS.csv`) whenever missing PDFs, truncated remarks, or errors occur.

---

## Quick-Start Guide for Office Users (No Python Required)

### 1. Folder Contents
Ensure the following items are in the `ERP_Automation_Suite` folder:
- `ERP_Automation_Suite.exe` — The main application (double-click to run).
- `config.json` — Stores calibrated screen coordinates (5 Print + 4 OTP targets), printer preferences, logging rules, and timing thresholds.
- `complaints.xlsx` — Your daily Excel export containing `COMPLAINT_NO` and `TECHNICIAN_NAME` columns.
- `complaint_remarks.xlsx` — Optional remarks Excel workbook containing `COMPLAINT_NO` and `REMARKS` columns (used by Menu `[7]`).
- `complaints_state.db` — SQLite database automatically tracking downloaded, spooled, and printed complaints so interrupted runs resume seamlessly.
- `logs/app.log` — Rotating technical log file for troubleshooting and audit history.
- `Today_Complaints/` — Output folder where sorted technician PDFs are saved.
- `Stamped_Complaints/` — Output folder where remarks-stamped PDFs are written in mirrored technician subfolders.

### 2. Running the Bot
1. Open your Oracle Forms ERP application and maximize the window.
2. Make sure `complaints.xlsx` is **closed** in Microsoft Excel.
3. Double-click **`ERP_Automation_Suite.exe`**.
4. Choose an option from the Main Menu:
   - **`[1] Bulk Mode (Process complaints.xlsx)`**: Validates and sanitizes `complaints.xlsx`, displays the Pre-Flight Validation Summary, lets you select **`[1] Save PDF to Disk`** (with technician/unified folder sorting and optional Live Physical Printing) or **`[2] Direct Browser Print (No PDF Save)`** straight to a validated physical printer with `win32print` spool verification.
   - **`[2] Manual Mode (Process single/batch complaint #s)`**: Prompts for one or multiple comma/space-separated complaint numbers (e.g. `2826001 2826002, 2826003`), deduplicates them, lets you choose **`[1] Save PDF to Disk`** or **`[2] Direct Browser Print (No PDF Save)`**, and processes them sequentially with temporary OS default printer switching and automatic `try/finally` restoration.
   - **`[3] Recalibrate Coordinates (Setup Wizard)`**: Guided wizard to calibrate screen coordinates or configure physical printer and queue limits.
   - **`[4] Generate OTP (Interactive Mode)`**: Prompts for complaint numbers (stripping `.0` float suffixes, filtering Excel headers, and deduplicating IDs) and automates OTP generation in Oracle Forms with configurable post-query delays (`otp_query_wait_seconds`), Edge IE Mode / Java foreground focus verification, and zero-calibration visual modal transition guards that halt safely with an audible chime and a copy-pasteable list of remaining IDs if an ERP screen lags or shows an alert popup.
   - **`[5] Batch Print Offline PDFs (Today_Complaints / Stamped)`**: Lists Technician folders with `[Total | Printed | Pending]` counts backed by `complaints_state.db`, lets you select or switch (`[S]`) between `Today_Complaints/` (original) and `Stamped_Complaints/` (with technician remarks), and silent-prints unprinted documents using a progress-aware sliding-window spooler pipeline.
   - **`[6] Select / Change Target Printer`**: Lists installed Windows printers and updates the configured target printer in `config.json`.
   - **`[7] Stamp Technician Remarks onto PDFs`**: Prompts (press `[Enter]` for defaults) for `complaint_remarks.xlsx`, input PDF folder (`Today_Complaints/`), and output folder (`Stamped_Complaints/`), displays the Remarks Excel Pre-Flight Validation Summary, and batch-stamps matched PDFs into mirrored technician subfolders.
   - **`[U] Check for Updates`**: Checks GitHub Releases for a newer version and performs a verified, state-preserving in-place update and restart.
   - **`[8] Exit`**: Closes the program.

---

## Emergency Stop (Kill-Switch) & Resume Behavior

You can safely stop the bot at any time during execution using any of these methods:
- **Keyboard Stop (`[ESC]`)**: Press the **`[ESC]`** key at any point during countdowns or execution to abort the active run and return to the Main Menu.
- **Console Interrupt (`Ctrl+C`)**: Press **`Ctrl+C`** in the console window to abort the active operation and return to the Main Menu (or press **`Ctrl+C`** at the Main Menu to exit the application).
- **Mouse Corner Fail-Safe**: Slam your mouse cursor into **any of the 4 corners** of your monitor.

**Resume & Print Tracking (`complaints_state.db`)**:
- Complaints whose PDFs were extracted and saved prior to stopping are recorded as `DOWNLOADED` in `complaints_state.db`, while only complaints whose physical print jobs were confirmed by the Windows Spooler are marked `PRINTED`.
- For saved PDFs in `Today_Complaints/` that still need paper copies, either run **`[5] Batch Print Offline PDFs (Today_Complaints)`** or resume **`[1] Bulk Mode`** with Live Print enabled (which automatically retries printing saved PDFs that are not yet marked `PRINTED`).

---

## Developer Setup & Building from Source

### Prerequisites
- Windows 10 / 11
- Python 3.10+
- Connected Windows Printer

### Installation
```bash
pip install -r requirements.txt
```

### Running from Source
```bash
python main.py
```

### Building the Standalone Executable (`ERP_Automation_Suite.exe`) & Release Asset
```bash
python -B build.py
# Or build and automatically publish the GitHub Release via GitHub CLI:
python -B build.py --publish
```
This compiles `main.py` via PyInstaller with `--onedir` into `dist/ERP_Automation_Suite/` (bundling `pypdfium2` and `pymupdf`) while preserving existing `config.json`, `complaints.xlsx`, `complaint_remarks.xlsx`, `complaints_state.db`, `logs/`, `Today_Complaints/`, and `Stamped_Complaints/`, and packages the clean binary release archive `dist/ERP_Automation_Suite_v6.3.1.zip` + `.sha256` checksum.

### Architecture Overview
| File | Responsibility |
| :--- | :--- |
| `main.py` | Entry point, CLI menu router (`[1]`–`[8]`, `[U]`), print workflow mode selector (`Save PDF` vs `Direct Browser Print`), strict Excel validation & Pre-Flight Summary, Interactive OTP Mode `[4]` with input sanitization & remainder halt summary, Batch Remarks Stamping `[7]`, and Batch Print mode `[5]` (`Today_Complaints` / `Stamped_Complaints`). |
| `pdf_remarks.py` | PyMuPDF batch remarks stamping engine: Excel validation, dynamic border anchoring, font auto-scaling (`9.5`–`6.0 pt`) with word-boundary truncation, atomic saves, and CSV issue reporting. |
| `version.py` | Single source of truth for semantic versioning (`__version__ = "6.3.1"`), SemVer comparisons, and release asset naming. |
| `updater.py` | In-App Self-Updater: GitHub Releases API polling, SHA-256 verification, staging, and detached `.bat` swap & rollback helper (preserving `Stamped_Complaints/` and `complaint_remarks.xlsx`). |
| `state_manager.py` | SQLite WAL database engine (`complaints_state.db`), progressive job state transitions, safe backups, direct-print vs saved-PDF reset logic, and legacy `.txt` auto-migrator. |
| `logger_manager.py` | Centralized logging manager with `RotatingFileHandler` (`logs/app.log`, 5 MB x 5 backups), console stream formatting, and crash hook. |
| `printer_manager.py` | Modular printing engine: printer enumeration, `temporary_default_printer` OS default switching/restoration, `wait_for_direct_print_spool` verification, spooler queue monitoring, queue gate failsafe, headless GDI printing, and job reconciliation. |
| `config_manager.py` | Portable path anchoring (`sys.executable`), Per-Monitor DPI awareness, and unified schema validation (`coordinates`, `timings`, `settings`, `printing`, `logging`, `updater`, `remarks_stamping`). |
| `calibration_wizard.py` | Guided wizard for coordinate capture (Space/F8) and physical printer configuration. |
| `engine.py` | Unified RPA loop for Save-PDF extraction, Direct Browser Print (`direct_print=True`) with sliding-window spool tracking, Live Print dispatch & focus restoration, and isolated `generate_otp()` sequence. |
| `build.py` | PyInstaller build script compiling standalone `--onedir` distribution, release `.zip` + `.sha256`, and optional `--publish` release automation. |
| `legacy/masterbot.py` | Original Phase 2 baseline script (archived & preserved untouched). |
