# Oracle ERP Automation Suite (v6.0: Enterprise SQLite State, Logging & Silent Printing)

An automated RPA (Robotic Process Automation) application that extracts complaint job cards from an Oracle Forms ERP system, generates PDFs via Internet Explorer / Foxit Reader, automatically sorts them into technician folders (`Today_Complaints/<Technician_Name>/<Complaint_No>.pdf`), performs **Universal Silent Physical Printing**, and automates single-complaint **OTP Generation**.

## Key Enterprise Features (v6.0)
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

---

## Quick-Start Guide for Office Users (No Python Required)

### 1. Folder Contents
Ensure the following items are in the `ERP_Automation_Suite` folder:
- `ERP_Automation_Suite.exe` — The main application (double-click to run).
- `config.json` — Stores calibrated screen coordinates (5 Print + 4 OTP targets), printer preferences, logging rules, and timing thresholds.
- `complaints.xlsx` — Your daily Excel export containing `COMPLAINT_NO` and `TECHNICIAN_NAME` columns.
- `complaints_state.db` — SQLite database automatically tracking downloaded, spooled, and printed complaints so interrupted runs resume seamlessly.
- `logs/app.log` — Rotating technical log file for troubleshooting and audit history.
- `Today_Complaints/` — Output folder where sorted technician PDFs are saved.

### 2. Running the Bot
1. Open your Oracle Forms ERP application and maximize the window.
2. Make sure `complaints.xlsx` is **closed** in Microsoft Excel.
3. Double-click **`ERP_Automation_Suite.exe`**.
4. Choose an option from the Main Menu:
   - **`[1] Bulk Mode (Process complaints.xlsx)`**: Validates and sanitizes `complaints.xlsx`, displays the Pre-Flight Validation Summary, prompts whether to sort PDFs into per-technician folders or a unified folder (`Today_Complaints/All_PDFs/`), and prompts whether to enable Live Physical Printing.
   - **`[2] Manual Mode (Process single/batch complaint #s)`**: Prompts for one or multiple comma/space-separated complaint numbers (e.g. `2826001 2826002, 2826003`), deduplicates them, and processes them sequentially with optional live physical printing.
   - **`[3] Recalibrate Coordinates (Setup Wizard)`**: Guided wizard to calibrate screen coordinates or configure physical printer and queue limits.
   - **`[4] Generate OTP (Interactive Mode)`**: Prompts for complaint numbers and automates OTP generation in Oracle Forms in a continuous loop.
   - **`[5] Batch Print Offline PDFs (Today_Complaints)`**: Lists Technician folders inside `Today_Complaints/` with `[Total | Printed | Pending]` counts backed by `complaints_state.db`, lets you pick one or more folders (or all), and silent-prints unprinted documents using a progress-aware sliding-window spooler pipeline.
   - **`[6] Select / Change Target Printer`**: Lists installed Windows printers and updates the configured target printer in `config.json`.
   - **`[7] Exit`**: Closes the program.

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

### Building the Standalone Executable (`ERP_Automation_Suite.exe`)
```bash
python build.py
```
This compiles `main.py` via PyInstaller with `--onedir` into `dist/ERP_Automation_Suite/` while preserving existing `config.json`, `complaints.xlsx`, `complaints_state.db`, `logs/`, and `Today_Complaints/`.

### Architecture Overview
| File | Responsibility |
| :--- | :--- |
| `main.py` | Entry point, CLI menu router (`[1]`–`[7]`), strict Excel validation & Pre-Flight Summary, and Batch Print mode `[5]`. |
| `state_manager.py` | SQLite WAL database engine (`complaints_state.db`), progressive job state transitions, safe backups, and legacy `.txt` auto-migrator. |
| `logger_manager.py` | Centralized logging manager with `RotatingFileHandler` (`logs/app.log`, 5 MB x 5 backups), console stream formatting, and crash hook. |
| `printer_manager.py` | Modular printing engine: printer enumeration, spooler queue monitoring, queue gate failsafe, headless GDI printing, and job reconciliation. |
| `config_manager.py` | Portable path anchoring (`sys.executable`), Per-Monitor DPI awareness, and unified schema validation. |
| `calibration_wizard.py` | Guided wizard for coordinate capture (Space/F8) and physical printer configuration. |
| `engine.py` | Unified RPA loop for PDF generation, Live Print dispatch & focus restoration, and isolated `generate_otp()` sequence. |
| `build.py` | PyInstaller build script compiling standalone `--onedir` distribution to `dist/ERP_Automation_Suite/`. |
| `legacy/masterbot.py` | Original Phase 2 baseline script (archived & preserved untouched). |
