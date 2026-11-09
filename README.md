# Oracle ERP Automation Suite (v4.0: Bulk Printing & OTP Generation)

An automated RPA (Robotic Process Automation) application that extracts complaint job cards from an Oracle Forms ERP system, prints them to PDF via Internet Explorer / Foxit Reader, automatically sorts each PDF into technician-specific folders (`Today_Complaints/<Technician_Name>/<Complaint_No>.pdf`), and automates single-complaint **OTP Generation**.

Version 4.0 introduces **OTP Generation Automation**, a **Modular Calibration Sub-Menu**, an expanded 9-target **`config.json` Schema**, and a **One-Dir (`dist/ERP_Automation_Suite/`) Deployment** that permanently resolves Windows `%TEMP%` permission restrictions.

---

## Quick-Start Guide for Office Users (No Python Required)

### 1. Folder Contents
Ensure the following files are in the `ERP_Automation_Suite` folder:
- `ERP_Automation_Suite.exe` — The main application (double-click to run).
- `config.json` — Stores calibrated screen coordinates (5 Print + 4 OTP targets) and timing thresholds.
- `complaints.xlsx` — Your daily Excel export containing `COMPLAINT_NO` and `TECHNICIAN_NAME` columns.
- `completed_complaints.txt` — Automatically tracks printed complaints so interrupted runs resume seamlessly.
- `Today_Complaints/` — Output folder where sorted technician PDFs are saved.

### 2. Running the Bot
1. Open your Oracle Forms ERP application and maximize the window.
2. Make sure `complaints.xlsx` is **closed** in Microsoft Excel.
3. Double-click **`ERP_Automation_Suite.exe`**.
4. Choose an option from the Main Menu:
   - **`[1] Bulk Mode (Process complaints.xlsx)`**: Automatically processes all unprinted complaints from `complaints.xlsx` and sorts them into technician folders.
   - **`[2] Manual Mode (Process single complaint #)`**: Prompts for a single complaint number and technician name to print one specific job card on the spot.
   - **`[3] Recalibrate Coordinates (Setup Wizard)`**: Guided wizard with sub-menu options to calibrate Bulk Print targets (5), OTP targets (4), or All targets (9).
   - **`[4] Generate OTP (Interactive Mode)`**: Prompts for complaint number and automates OTP generation in Oracle Forms in a continuous loop.
   - **`[5] Exit`**: Closes the program.

---

## Calibrating Coordinates on a New PC or Monitor (Option 3)

If you move the bot to a computer with a different screen resolution, or if you see the **`[WARNING] SCREEN RESOLUTION DRIFT DETECTED`** banner:

1. Select **`[3] Recalibrate Coordinates (Setup Wizard)`** from the main menu.
2. Select target set:
   - `[1] Calibrate Bulk Print Targets (5)`
   - `[2] Calibrate OTP Generation Targets (4)`
   - `[3] Calibrate All Targets (9)`
3. For each target listed in the terminal:
   - Hover your mouse cursor directly over the target button/field in the ERP window (you do **not** need to click back into the terminal).
   - Press **`[SPACE]`** or **`[F8]`** on your keyboard.
   - You will hear a confirmation **beep** and see the captured `(X, Y)` coordinates.
   - Type **`Y`** (or press **`Enter`**) to accept, or **`R`** to retry.
4. **Visual Cursor Replay**: The bot will smoothly glide your mouse across the calibrated points on screen so you can visually verify accuracy.

### Calibrated Targets:
**Bulk Print Targets (5):**
1. **Complaint Number Field** (where the complaint number is entered)
2. **Main Print Button** (opens the print popup)
3. **"FOR HA FIELD" Checkbox**
4. **Popup Print Button** (sends the job to Internet Explorer / Foxit)
5. **Popup Exit Button** (closes the print popup)

**OTP Generation Targets (4):**
6. **OTP Add Button** (opens the OTP module)
7. **Complaint Search (3-Dots) Button** (opens complaint lookup)
8. **Complaint Search Input Field** (field double-clicked and pasted into)
9. **OTP Reason (3-Dots) Button** (double-tap focused to open reason list)

---

## Emergency Stop (Kill-Switch)

You can safely stop the bot at any time during execution using either method:
- **Keyboard Stop**: Hold down the **`[ESC]`** key for 1 second.
- **Mouse Corner Fail-Safe**: Slam your mouse cursor into **any of the 4 corners** of your monitor.

All complaints completed prior to stopping are already recorded in `completed_complaints.txt` and will be automatically skipped when you resume.

---

## Developer Setup & Building from Source

### Prerequisites
- Windows 10 / 11
- Python 3.10+
- Microsoft Print to PDF / Foxit Reader configured as default PDF printer

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
This compiles `main.py` via PyInstaller with `--onedir` into `dist/ERP_Automation_Suite/` containing `ERP_Automation_Suite.exe`, `config.json`, and `complaints.xlsx`.

### Architecture Overview
| File | Responsibility |
| :--- | :--- |
| `main.py` | Entry point, interactive CLI menu, Excel file-lock check, Bulk/Manual Print routing, and Interactive OTP loop. |
| `config_manager.py` | Portable path anchoring (`sys.executable`), Windows Per-Monitor DPI awareness, and 9-target `config.json` validation. |
| `calibration_wizard.py` | Interactive CLI Calibration Wizard with modular sub-menu, global hotkeys (`Space`/`F8`), audio chime, and cursor replay. |
| `engine.py` | Unified RPA execution pipeline for Bulk/Manual Printing and isolated `generate_otp()` sequence with safety aborts. |
| `build.py` | PyInstaller packaging script compiling `--onedir` deployment to `dist/ERP_Automation_Suite/`. |
| `masterbot.py` | Original Phase 2 baseline script (preserved untouched). |
