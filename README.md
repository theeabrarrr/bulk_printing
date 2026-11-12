# Oracle ERP Automation Suite (v5.0: Universal Silent Printing & Spooler Integration)

An automated RPA (Robotic Process Automation) application that extracts complaint job cards from an Oracle Forms ERP system, generates PDFs via Internet Explorer / Foxit Reader, automatically sorts them into technician folders (`Today_Complaints/<Technician_Name>/<Complaint_No>.pdf`), and automates single-complaint **OTP Generation**.

Version 5.0 introduces **Universal Silent Physical Printing**:
- **100% Invisible Headless GDI Rendering**: Uses `pypdfium2` and Windows GDI Printer Device Contexts (`win32ui`/`win32print`) to stream pages directly into the Windows Print Spooler. Zero reliance on external PDF viewers (Foxit, Acrobat, Edge), eliminating window pop-ups or focus theft that could disrupt Oracle Forms.
- **Hardware Failsafe & Queue Monitoring**: Monitors the Windows Print Spooler. If pending jobs reach the threshold (default: 3) or a hardware/spooler alert (paper jam, out-of-paper, offline, paused printer/job, or spooler query error) is detected, the bot sounds an audio alarm and pauses execution until the operator clears the printer.
- **Job-Status Reconciliation**: Records jobs as `SPOOLED` when `EndDoc` completes and only marks them as `PRINTED` in `printed_complaints.txt` after reconciling with the Windows Spooler that the job completed without error or deletion.
- **Dual Printing Modes**:
  - **Live Print**: Option to print each PDF immediately after download in Bulk and Manual modes, with automatic resume retry for any completed PDFs missing from `printed_complaints.txt`.
  - **Offline Batch Print**: Main Menu option `[5]` to scan `Today_Complaints/` and print unprinted documents with automatic resume capability via `printed_complaints.txt`.
- **Printer Calibration & Setup**: Setup Wizard sub-menu and Main Menu option `[6]` to detect, select, and configure connected physical printers.

---

## Quick-Start Guide for Office Users (No Python Required)

### 1. Folder Contents
Ensure the following files are in the `ERP_Automation_Suite` folder:
- `ERP_Automation_Suite.exe` — The main application (double-click to run).
- `config.json` — Stores calibrated screen coordinates (5 Print + 4 OTP targets), printer preferences, and timing thresholds.
- `complaints.xlsx` — Your daily Excel export containing `COMPLAINT_NO` and `TECHNICIAN_NAME` columns.
- `completed_complaints.txt` — Automatically tracks complaints whose PDFs have been extracted and saved from the ERP so interrupted runs resume seamlessly.
- `printed_complaints.txt` — Automatically tracks complaints whose physical print jobs have been spooled and confirmed printed to prevent duplicate paper prints.
- `Today_Complaints/` — Output folder where sorted technician PDFs are saved.

### 2. Running the Bot
1. Open your Oracle Forms ERP application and maximize the window.
2. Make sure `complaints.xlsx` is **closed** in Microsoft Excel.
3. Double-click **`ERP_Automation_Suite.exe`**.
4. Choose an option from the Main Menu:
   - **`[1] Bulk Mode (Process complaints.xlsx)`**: Automatically extracts complaints from `complaints.xlsx`. Prompts whether to sort PDFs into per-technician folders or a unified folder (`Today_Complaints/All_PDFs/`), and prompts whether to enable Live Physical Printing.
   - **`[2] Manual Mode (Process single/batch complaint #s)`**: Prompts for one or multiple comma/space-separated complaint numbers (e.g. `2826001 2826002, 2826003`), deduplicates them, and processes them sequentially with optional live physical printing.
   - **`[3] Recalibrate Coordinates (Setup Wizard)`**: Guided wizard to calibrate screen coordinates or configure physical printer and queue limits.
   - **`[4] Generate OTP (Interactive Mode)`**: Prompts for complaint number and automates OTP generation in Oracle Forms in a continuous loop.
   - **`[5] Batch Print Offline PDFs (Today_Complaints)`**: Scans existing PDFs in `Today_Complaints/`, cross-references against `printed_complaints.txt`, and silent-prints unprinted documents.
   - **`[6] Select / Change Target Printer`**: Lists installed Windows printers and updates the configured target printer in `config.json`.
   - **`[7] Exit`**: Closes the program.

---

## Physical Printer Configuration & Hardware Failsafe

### Setting Target Printer (Option 6 or Option 3 -> 4)
1. In the Main Menu, choose **`[6] Select / Change Target Printer`** (or choose **`[3] Recalibrate Coordinates (Setup Wizard)`** and select **`[4] Configure Physical Printer & Spooler`**).
2. The selector enumerates all installed local and network printers (`1` through the number of installed printers) along with `PHYSICAL` / `VIRTUAL` classification badges.
3. Type a valid printer index from `1` through the number of installed printers (or press `Enter` to keep the currently configured printer).
4. In the Calibration Wizard printer menu (`[3]` -> `[4]`), you can also set the spooler threshold limit (default: 3 pending jobs) and check whether the Windows Spooler reports any error flags for the selected printer.

### How the Hardware Queue Failsafe Works
When printing either in Live Mode or Batch Print Mode:
1. Before spooling a new document, the bot queries `win32print` for active jobs (`cJobs`) and status flags (`PRINTER_STATUS_PAUSED`, `PRINTER_STATUS_PAPER_JAM`, `PRINTER_STATUS_PAPER_OUT`, `JOB_STATUS_PAUSED`, `JOB_STATUS_ERROR`, etc.).
2. If `cJobs >= max_spool_queue` or an error/paused state is detected:
   - The bot sounds audible alert beeps (`winsound.Beep`).
   - A warning banner displays in the console detailing the issue.
   - Execution pauses, allowing the operator to clear jammed paper, refill the tray, unpause, or turn on the printer.
   - Once cleared, press **`[ENTER]`** to re-check and resume, **`[F]`** to force print, **`[S]`** to skip printing the current document, or **`[Q]`** to abort.
   - In Live Mode, after any interactive queue gate choice, the bot automatically restores focus to the ERP window before proceeding to the next complaint.

---

## Calibrating Coordinates on a New PC or Monitor (Option 3)

If you move the bot to a computer with a different screen resolution, or if you see the **`[WARNING] SCREEN RESOLUTION DRIFT DETECTED`** banner:

1. Select **`[3] Recalibrate Coordinates (Setup Wizard)`** from the main menu.
2. Select target set:
   - `[1] Calibrate Bulk Print Targets (5 targets)`
   - `[2] Calibrate OTP Generation Targets (4 targets)`
   - `[3] Calibrate All Targets (9 targets)`
   - `[4] Configure Physical Printer & Spooler`
   - `[5] Return to Main Menu`
3. For each target listed in the terminal:
   - Hover your mouse cursor directly over the target button/field in the ERP window.
   - Press **`[SPACE]`** or **`[F8]`** on your keyboard.
   - You will hear a confirmation **beep** and see the captured `(X, Y)` coordinates.
   - Type **`Y`** (or press **`Enter`**) to accept, or **`R`** to retry.
4. **Visual Cursor Replay**: The bot will smoothly glide your mouse across the calibrated points on screen so you can visually verify accuracy.

---

## Emergency Stop (Kill-Switch) & Resume Behavior

You can safely stop the bot at any time during execution using any of these methods:
- **Keyboard Stop (`[ESC]`)**: Press the **`[ESC]`** key at any point during countdowns or execution to abort the active run and return to the Main Menu.
- **Console Interrupt (`Ctrl+C`)**: Press **`Ctrl+C`** in the console window to abort the active operation and return to the Main Menu (or press **`Ctrl+C`** at the Main Menu to exit the application).
- **Mouse Corner Fail-Safe**: Slam your mouse cursor into **any of the 4 corners** of your monitor.

**Resume & Print Tracking**:
- Complaints whose PDFs were extracted and saved prior to stopping are recorded in `completed_complaints.txt`, while only complaints whose physical print jobs were confirmed printed appear as `PRINTED` in `printed_complaints.txt`.
- Completing a PDF download does not automatically mean the complaint was physically printed (for example, if Live Print was disabled, skipped, or interrupted before print confirmation).
- For saved PDFs in `Today_Complaints/` that still need paper copies, either run **`[5] Batch Print Offline PDFs (Today_Complaints)`** or resume **`[1] Bulk Mode`** with Live Print enabled (which automatically retries printing saved PDFs that are missing from `printed_complaints.txt`).

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
This compiles `main.py` via PyInstaller with `--onedir` into `dist/ERP_Automation_Suite/` containing `ERP_Automation_Suite.exe`, `config.json`, `complaints.xlsx`, `completed_complaints.txt`, and `printed_complaints.txt`.

### Architecture Overview
| File | Responsibility |
| :--- | :--- |
| `main.py` | Entry point, CLI menu router (`[1]`–`[7]`), Excel lock checking, routing toggles, and Batch Print mode `[5]`. |
| `printer_manager.py` | Modular printing engine: printer enumeration, spooler queue monitoring, queue gate failsafe, headless GDI printing, and job-status reconciliation. |
| `config_manager.py` | Portable path anchoring (`sys.executable`), Per-Monitor DPI awareness, and unified schema validation. |
| `calibration_wizard.py` | Guided wizard for coordinate capture (Space/F8) and physical printer configuration. |
| `engine.py` | Unified RPA loop for PDF generation, Live Print dispatch & focus restoration, and isolated `generate_otp()` sequence. |
| `build.py` | PyInstaller build script compiling standalone `--onedir` distribution to `dist/ERP_Automation_Suite/`. |
| `masterbot.py` | Original Phase 2 baseline script (preserved untouched). |
