import os
import re
import sys
import time

# Ensure Unicode output doesn't crash on standard Windows consoles (cp1252)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import pandas as pd
import config_manager
import calibration_wizard
import engine


def ensure_runtime_files(config):
    """Ensures output directory and progress tracking file exist."""
    settings = config.get("settings", config_manager.DEFAULT_CONFIG["settings"])
    output_dir = config_manager.resolve_path(settings.get("output_dir", "Today_Complaints"))
    progress_file = config_manager.resolve_path(settings.get("progress_file", "completed_complaints.txt"))

    os.makedirs(output_dir, exist_ok=True)
    if not os.path.exists(progress_file):
        with open(progress_file, "w", encoding="utf-8") as f:
            pass


def check_excel_lock(excel_path):
    """
    Checks if the Excel file is locked by another process (e.g., open in Microsoft Excel).
    Prompts user to close it if locked.
    """
    while True:
        try:
            with open(excel_path, "r+b"):
                return True
        except PermissionError:
            print(f"\n[!] ERROR: '{excel_path}' is currently open in Microsoft Excel or another program.")
            ans = input("    Please close the Excel file and press [ENTER] to retry (or type 'q' to cancel): ").strip().lower()
            if ans == "q":
                return False
        except Exception:
            # For read-only file systems or other non-lock states, proceed to pandas read
            return True


def run_bulk_mode(config):
    """Loads complaints.xlsx, performs pre-flight checks, and dispatches batch to engine."""
    settings = config.get("settings", config_manager.DEFAULT_CONFIG["settings"])
    excel_rel = settings.get("excel_path", "complaints.xlsx")
    excel_path = config_manager.resolve_path(excel_rel)
    complaint_col = settings.get("complaint_column", "COMPLAINT_NO")
    tech_col = settings.get("technician_column", "TECHNICIAN_NAME")

    if not os.path.exists(excel_path):
        print(f"\n[!] Error: Excel file not found at:\n    {excel_path}")
        print("    Please place the Excel file at the path shown above and try again.")
        return

    if not check_excel_lock(excel_path):
        print("\n[!] Bulk mode cancelled.")
        return

    try:
        # Enforce string dtype to prevent precision loss or scientific notation on complaint IDs
        df = pd.read_excel(excel_path, dtype={complaint_col: str, tech_col: str})
    except Exception:
        try:
            df = pd.read_excel(excel_path, dtype=str)
        except Exception as e:
            print(f"\n[!] Failed to read Excel file: {e}")
            return

    if complaint_col not in df.columns:
        print(f"\n[!] Error: Required column '{complaint_col}' not found in '{excel_rel}'.")
        print(f"    Available columns: {list(df.columns)}")
        return

    df = df.dropna(subset=[complaint_col])

    records = []
    for _, row in df.iterrows():
        c_num = str(row[complaint_col]).strip()
        # Handle float strings like '282634892.0' if previously converted
        if c_num.endswith(".0"):
            c_num = c_num[:-2]
        if not c_num or c_num.lower() == "nan":
            continue
        t_name = str(row[tech_col]).strip() if (tech_col in df.columns and pd.notna(row[tech_col])) else "Unknown"
        records.append((c_num, t_name))

    if not records:
        print("\n[!] No valid complaint records found in the Excel file.")
        return

    # Prompt user for folder routing preference
    organize_by_tech = True
    while True:
        choice = input("\n[->] Organize PDFs into separate Technician folders? (Y/N) [Default: Y]: ").strip().lower()
        if choice in ["", "y", "yes"]:
            organize_by_tech = True
            break
        elif choice in ["n", "no"]:
            organize_by_tech = False
            break
        else:
            print("    Please enter 'Y' for yes or 'N' for no.")

    engine.run_batch(records, config=config, start_delay=5, organize_by_tech=organize_by_tech)


def run_manual_mode(config):
    """
    Prompts for one or multiple complaint numbers (comma or space-separated),
    a batch/technician folder name, and processes each sequentially.
    """
    print("\n" + "-" * 50)
    print("       MANUAL COMPLAINT PRINT MODE (BATCH/SINGLE)")
    print("-" * 50)
    raw_input_str = input("  Enter Complaint Number(s) (comma/space-separated, or [ENTER] to cancel): ").strip()
    if not raw_input_str:
        print("  [!] Manual mode cancelled.")
        return

    # Parse and split tokens by commas and whitespace
    raw_tokens = [tok.strip() for tok in re.split(r'[\s,]+', raw_input_str) if tok.strip()]
    if not raw_tokens:
        print("  [!] No valid complaint numbers provided. Manual mode cancelled.")
        return

    # Deduplicate preserving input order
    seen = set()
    complaints = []
    has_duplicates = False
    for num in raw_tokens:
        if num in seen:
            has_duplicates = True
        else:
            seen.add(num)
            complaints.append(num)

    if has_duplicates:
        print(f"  [!] Notice: Duplicate numbers detected and removed. ({len(complaints)} unique record(s))")

    tech_name = input("  Enter Batch / Technician Name [default: Manual_Batch]: ").strip()
    if not tech_name:
        tech_name = "Manual_Batch"

    print(f"\n[*] Manual Batch Queued: {len(complaints)} complaint(s) for folder '{tech_name}'.")
    print("[*] Starting in 5 seconds! Please ensure your ERP window is open and maximized...")
    for i in range(5, 0, -1):
        if engine.check_abort():
            print("\n[!] Aborted during countdown.")
            return
        print(f"  Countdown: {i}...")
        time.sleep(1.0)

    stats = {
        "total": len(complaints),
        "completed": 0,
        "failed": 0,
        "aborted": False
    }

    for idx, complaint_no in enumerate(complaints, 1):
        if engine.check_abort():
            print("\n[!] Manual batch processing stopped by user (Esc pressed).")
            stats["aborted"] = True
            break

        print(f"\n[->] [{idx}/{len(complaints)}] Processing Complaint #: {complaint_no} (Batch: {tech_name})")
        # record_progress=False avoids state contamination with bulk execution
        success, msg = engine.process_complaint(
            complaint_no, tech_name, config=config, record_progress=False, organize_by_tech=True
        )

        if success:
            stats["completed"] += 1
            print(f"  [+] Complaint #{complaint_no} processed successfully!")
        else:
            if "aborted" in msg.lower() or "failsafe" in msg.lower():
                print(f"  [!] Execution aborted: {msg}")
                stats["aborted"] = True
                break
            else:
                print(f"  [!] Complaint #{complaint_no} failed or stopped: {msg}")
                stats["failed"] += 1

    print("\n" + "=" * 50)
    print("        MANUAL BATCH EXECUTION SUMMARY")
    print("=" * 50)
    print(f" Total entered:    {stats['total']}")
    print(f" Completed now:    {stats['completed']}")
    print(f" Failed:           {stats['failed']}")
    print(f" Interrupted:      {stats['aborted']}")
    print("=" * 50)
    print(" (Note: Bulk progress tracking was not updated to prevent skipping in bulk batches.)\n")


def run_otp_mode(config):
    """
    Interactive continuous loop for OTP Generation.
    Prompts user for complaint number, displays countdown, and invokes engine.generate_otp().
    Continues looping until user enters empty string or 'q'.
    """
    print("\n" + "=" * 55)
    print("         INTERACTIVE OTP GENERATION MODE")
    print("=" * 55)
    print("Instructions:")
    print("  1. Ensure your Oracle Forms ERP window is visible and maximized.")
    print("  2. Enter or paste the Complaint Number to generate its OTP.")
    print("  3. Press [ESC] at any time during execution to emergency abort.")
    print("  4. Press [ENTER] without input (or type 'q') to return to Main Menu.")
    print("=" * 55)

    while True:
        complaint_no = input("\n[->] Enter Complaint Number (or press [ENTER] to return): ").strip()
        if not complaint_no or complaint_no.lower() in ["q", "quit", "exit"]:
            print("Returning to main menu...")
            break

        print("\n[*] Starting OTP Generation in 3 seconds! Switch to Oracle Forms window...")
        countdown_aborted = False
        for i in range(3, 0, -1):
            if engine.check_abort():
                print("\n[!] Aborted during countdown.")
                countdown_aborted = True
                break
            print(f"  Countdown: {i}...")
            time.sleep(1.0)

        if countdown_aborted:
            continue

        success, msg = engine.generate_otp(complaint_no, config=config)
        if success:
            print(f"\n[+] Success: {msg}")
        else:
            print(f"\n[!] Notice: {msg}")
            if "aborted" in msg.lower() or "failsafe" in msg.lower():
                print("    Execution paused due to abort signal.")


def main():
    """Main CLI entry point and router."""
    config_manager.set_dpi_awareness()

    print("\n" + "=" * 60)
    print("      ORACLE ERP AUTOMATION SUITE (v4.0)")
    print("=" * 60)

    config_path = config_manager.get_config_path()
    if not os.path.exists(config_path):
        print("\n[!] 'config.json' not found! First-time setup is required.")
        ans = input("Would you like to launch the Calibration Wizard now? [Y/n]: ").strip().lower()
        if ans in ["", "y", "yes"]:
            calibrated = calibration_wizard.run_calibration_wizard()
            if not calibrated:
                print("[!] Using default fallback coordinates.")
                config = config_manager.DEFAULT_CONFIG.copy()
                config_manager.save_config(config)
            else:
                config = config_manager.load_config()
        else:
            config = config_manager.DEFAULT_CONFIG.copy()
            config_manager.save_config(config)
    else:
        try:
            config = config_manager.load_config()
        except Exception as e:
            print(f"\n[!] Fatal: Cannot launch application because 'config.json' is corrupted: {e}")
            print("    Please correct the syntax in 'config.json' or delete it to re-initialize.")
            input("\nPress [ENTER] to exit...")
            return

    ensure_runtime_files(config)

    while True:
        # Reload config on each menu cycle in case recalibration occurred
        try:
            loaded_cfg = config_manager.load_config()
            if loaded_cfg:
                config = loaded_cfg
        except Exception as e:
            print(f"\n[!] Warning: Failed to reload 'config.json' ({e}). Using active configuration.")

        # Check for screen resolution drift
        has_drift, curr_res, calib_res = config_manager.check_resolution_drift(config)
        if has_drift:
            print("\n" + "!" * 60)
            print(f" [WARNING] SCREEN RESOLUTION DRIFT DETECTED!")
            print(f" Current Resolution:    {curr_res[0]} x {curr_res[1]}")
            print(f" Calibrated Resolution: {calib_res[0]} x {calib_res[1]}")
            print(" Coordinates may not align with ERP buttons. Select [3] to recalibrate.")
            print("!" * 60)

        print("\n" + "-" * 60)
        print(" MAIN MENU")
        print("-" * 60)
        print("  [1] Bulk Mode (Process complaints.xlsx)")
        print("  [2] Manual Mode (Process single/batch complaint #s)")
        print("  [3] Recalibrate Coordinates (Setup Wizard)")
        print("  [4] Generate OTP (Interactive Mode)")
        print("  [5] Exit")
        print("-" * 60)

        choice = input("Select an option [1-5]: ").strip()

        if choice == "1":
            run_bulk_mode(config)
        elif choice == "2":
            run_manual_mode(config)
        elif choice == "3":
            calibration_wizard.run_calibration_wizard()
        elif choice == "4":
            run_otp_mode(config)
        elif choice == "5" or choice.lower() in ["exit", "q", "quit"]:
            print("\nExiting ERP Automation Suite. Goodbye!")
            break
        else:
            print("\n[!] Invalid choice. Please enter 1, 2, 3, 4, or 5.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[!] Application terminated by user.")
        sys.exit(0)
