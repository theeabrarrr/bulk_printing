# Current Issues & Resolution Log

## Date: 2026-11-13
**Feature:** Phase 5 - Universal Silent Physical Printing & Spooler Monitoring  
**Target File / Subsystem:** `printer_manager.py`, `main.py`, `config.json`, `config_manager.py`

### 1. Reported Issue
- When attempting Offline Batch Printing (Option `[5]`), the application displayed:
  ```text
  Target Printer   : Microsoft Print to PDF (Dynamic OS Default)
  Directory Scanned: C:\Users\PC\Desktop\bulk_printing\dist\ERP_Automation_Suite\Today_Complaints
  Total PDFs Found : 12
  Already Printed  : 0
  Pending to Print : 12
  Spooler Threshold: 3 active jobs
  ```
- **Symptom:** The application defaulted to `Microsoft Print to PDF` (a virtual software printer) instead of detecting or offering connected physical/network printers (such as `HP LaserJet 400 M401 PCL 6`).
- **Root Cause:**
  1. `config.json` had `"printer_name": null`.
  2. `config_manager.get_configured_printer()` fell back to Windows OS default printer (`Microsoft Print to PDF`).
  3. Batch printing had no inline prompt to select or change the printer, and printer setup was tucked away under Setup Wizard Option `[4]`.

### 2. Agreed Architectural Resolutions (from `/grill-me` alignment)
1. **Unified Interactive Printer Resolver (`printer_manager.py`)**:
   - Classifies printers into `[PHYSICAL]` vs `[VIRTUAL]`.
   - Displays a simplified numbered list with status badges.
   - Saves selection permanently to `config.json`.
2. **Main Menu Direct Access (`main.py`)**:
   - Displays `Active Target Printer: ...` directly in the Main Menu header.
   - Adds dedicated Main Menu Option `[6] Select / Change Target Printer` (with `[7] Exit`).
3. **Smart Batch Print Flow (`main.py`)**:
   - If printer is unconfigured or virtual, automatically prompts for printer selection before starting.
   - Banner includes `[C]` to change printer.
   - If files were previously printed, prompts: `[Enter] Print pending only`, `[A] Force re-print all`, `[R] Reset/clear print tracker` (with auto-archive to `.bak`).
4. **Smart Auto-Drain & Hardware Alert (`printer_manager.py`)**:
   - Quietly polls for up to 15s to allow queue to drain during rapid printing without audio beeps.
   - Only alarms if queue remains stuck or an explicit hardware error occurs.
   - Adds `[F] Force Print (Ignore warning & send job)`.
