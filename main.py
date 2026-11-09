import os
import sys
import time
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

    engine.run_batch(records, config=config, start_delay=5)


def run_manual_mode(config):
    """Prompts for a single complaint number and optional technician name, then runs engine."""
    print("\n" + "-" * 50)
    print("         MANUAL SINGLE-COMPLAINT MODE")
    print("-" * 50)
    complaint_no = input("  Enter Complaint Number (or press [ENTER] to cancel): ").strip()
    if not complaint_no:
        print("  [!] Manual mode cancelled.")
        return

    tech_name = input("  Enter Technician Name [default: Manual_Single]: ").strip()
    if not tech_name:
        tech_name = "Manual_Single"

    print("\n🚀 Starting in 5 seconds! Please ensure your ERP window is open and maximized...")
    for i in range(5, 0, -1):
        if engine.check_abort():
            print("\n[!] Aborted during countdown.")
            return
        print(f"  Countdown: {i}...")
        time.sleep(1.0)

    print(f"\n👉 Processing Single Complaint #: {complaint_no} (Tech: {tech_name})")
    # record_progress=False avoids state contamination with bulk execution
    success, msg = engine.process_complaint(complaint_no, tech_name, config=config, record_progress=False)
    if success:
        print(f"\n[+] Single complaint '{complaint_no}' processed successfully!")
        print("    (Note: Bulk progress tracking was not updated to prevent skipping in bulk batches.)")
    else:
        print(f"\n[!] Single complaint failed or stopped: {msg}")


def main():
    """Main CLI entry point and router."""
    config_manager.set_dpi_awareness()

    print("\n" + "=" * 60)
    print("      ORACLE ERP BULK PDF PRINTER & SORTER (v3.0)")
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
        print("  [2] Manual Mode (Process single complaint #)")
        print("  [3] Recalibrate Coordinates (Setup Wizard)")
        print("  [4] Exit")
        print("-" * 60)

        choice = input("Select an option [1-4]: ").strip()

        if choice == "1":
            run_bulk_mode(config)
        elif choice == "2":
            run_manual_mode(config)
        elif choice == "3":
            calibration_wizard.run_calibration_wizard()
        elif choice == "4" or choice.lower() in ["exit", "q", "quit"]:
            print("\nExiting ERP Bulk Printer. Goodbye!")
            break
        else:
            print("\n[!] Invalid choice. Please enter 1, 2, 3, or 4.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[!] Application terminated by user.")
        sys.exit(0)
