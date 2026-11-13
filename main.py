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
import logger_manager
import state_manager
import calibration_wizard
import engine
import printer_manager

logger = logger_manager.get_logger("main")


def ensure_runtime_files(config):
    """
    Ensures output directory, centralized rotating log directory, and the SQLite
    state database ('complaints_state.db') exist, migrates any legacy flat-file
    trackers ('completed_complaints.txt', 'printed_complaints.txt'), and executes
    the idempotent Startup Disk-to-DB Sync against Today_Complaints/.
    """
    logger_manager.setup_logging(config)
    settings = config.get("settings", config_manager.DEFAULT_CONFIG["settings"])
    printing_cfg = config_manager.get_printing_settings(config)

    output_dir = config_manager.resolve_path(settings.get("output_dir", "Today_Complaints"))
    db_path = config_manager.get_database_path(config)
    progress_file = config_manager.resolve_path(settings.get("progress_file", "completed_complaints.txt"))
    printed_file = config_manager.resolve_path(printing_cfg.get("printed_tracker_file", "printed_complaints.txt"))

    os.makedirs(output_dir, exist_ok=True)
    state_manager.init_db(db_path, output_dir=output_dir, sync_disk=False)

    if os.path.exists(progress_file) or os.path.exists(printed_file):
        try:
            state_manager.migrate_legacy_txt_files(
                db_path=db_path,
                completed_txt_path=progress_file,
                printed_txt_path=printed_file,
                output_dir=output_dir,
                archive_after_migrate=True,
            )
        except Exception as e:
            logger.error(f"[!] Warning: Legacy tracker file migration encountered an error: {e}", exc_info=True)

    state_manager.sync_disk_to_db(db_path, output_dir=output_dir, announce=True)


def _select_validated_physical_printer(config, initial_printer=None, force_prompt=True):
    """
    Prompts the operator to select a printer and validates via classify_printer
    that the chosen target is not VIRTUAL before proceeding with physical printing.
    Returns (target_printer: Optional[str], config: dict).
    """
    target_printer = initial_printer
    first_pass = True

    while first_pass or not target_printer or printer_manager.classify_printer(target_printer) == "VIRTUAL":
        first_pass = False
        prev_printer = target_printer
        target_printer, config = printer_manager.select_printer_interactive(
            config=config, auto_save=True, force_prompt=force_prompt
        )
        if not target_printer:
            return None, config

        if printer_manager.classify_printer(target_printer) == "VIRTUAL":
            logger.warning(f"\n[!] Notice: '{target_printer}' is a VIRTUAL printer and cannot be used for physical printing.")
            try:
                retry_ans = input("    Select a different physical printer? (Y/n): ").strip().lower()
            except (EOFError, StopIteration, KeyboardInterrupt):
                return None, config
            if retry_ans in ["n", "no", "q", "quit", "cancel"]:
                return None, config
            if target_printer == prev_printer and retry_ans == "":
                # Continue loop to prompt again
                pass
            force_prompt = True

    return target_printer, config


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
            logger.warning(f"\n[!] ERROR: '{excel_path}' is currently open in Microsoft Excel or another program.")
            ans = input("    Please close the Excel file and press [ENTER] to retry (or type 'q' to cancel): ").strip().lower()
            if ans == "q":
                return False
        except Exception:
            # For read-only file systems or other non-lock states, proceed to pandas read
            return True


def _resolve_column_name(df_columns, target_col):
    """
    Resolves a column name in df_columns by exact match first, then stripped match,
    then case-insensitive stripped match. Returns the matched column name or None.
    """
    if target_col in df_columns:
        return target_col
    target_clean = str(target_col).strip()
    for col in df_columns:
        if str(col).strip() == target_clean:
            return col
    for col in df_columns:
        if str(col).strip().upper() == target_clean.upper():
            return col
    return None


def validate_excel_dataframe(df, complaint_col="COMPLAINT_NO", tech_col="TECHNICIAN_NAME", source_label="complaints.xlsx"):
    """
    Strictly validates and sanitizes an Excel DataFrame of complaint records.
    Rules enforced:
      1. Normalizes column headers (strips whitespace & supports case-insensitive matching).
      2. Requires `complaint_col` to exist; if `tech_col` is missing, defaults technicians to 'Unknown'.
      3. Strips whitespace and float '.0' suffixes from COMPLAINT_NO.
      4. Rejects blank/NaN rows, Excel formula errors (#REF!, #N/A, #VALUE!, etc.), and any
         non-numeric COMPLAINT_NO (must be strictly numeric digits), logging exact 1-based Excel row numbers.
      5. Deduplicates repeated COMPLAINT_NO values (retains first occurrence and logs warning).
      6. Sanitizes TECHNICIAN_NAME and falls back to 'Unknown' when missing or blank.

    Returns:
        (records: Optional[List[Tuple[str, str]]], summary: Dict[str, Any])
        Returns (None, summary) if the required complaint column is missing.
    """
    summary = {
        "source_label": source_label,
        "total_rows": 0,
        "valid_records": 0,
        "empty_rows": 0,
        "invalid_rows": 0,
        "duplicate_rows": 0,
        "unknown_tech_rows": 0,
        "missing_complaint_col": False,
        "missing_tech_col": False,
        "invalid_details": [],
        "duplicate_details": [],
    }

    if df is None or len(df.columns) == 0:
        summary["missing_complaint_col"] = True
        logger.error(f"\n[!] Error: '{source_label}' contains no columns or is empty.")
        return None, summary

    resolved_c_col = _resolve_column_name(df.columns, complaint_col)
    if resolved_c_col is None:
        summary["missing_complaint_col"] = True
        logger.error(f"\n[!] Error: Required column '{complaint_col}' not found in '{source_label}'.")
        logger.error(f"    Available columns: {list(df.columns)}")
        return None, summary

    resolved_t_col = _resolve_column_name(df.columns, tech_col)
    if resolved_t_col is None:
        summary["missing_tech_col"] = True
        logger.warning(
            f"  [!] Warning: Column '{tech_col}' not found in '{source_label}'. "
            "All records will default to technician 'Unknown'."
        )

    summary["total_rows"] = len(df)
    records = []
    seen_complaints = {}
    null_tokens = {"nan", "none", "null", "n/a", "na", ""}

    for row_idx, (_, row) in enumerate(df.iterrows()):
        excel_row = row_idx + 2  # 1-based row index in Excel (Row 1 is header)
        raw_c = row[resolved_c_col]

        if pd.isna(raw_c):
            summary["empty_rows"] += 1
            logger.debug("Row %d: Skipped empty/NaN complaint cell.", excel_row)
            continue

        c_str = str(raw_c).strip()
        if not c_str or c_str.lower() in null_tokens:
            summary["empty_rows"] += 1
            logger.debug("Row %d: Skipped blank/null complaint value '%s'.", excel_row, c_str)
            continue

        # Strip float '.0' suffix if Excel serialized an integer as float
        if re.fullmatch(r"\d+\.0+", c_str):
            c_str = c_str.split(".", 1)[0]

        # Enforce strict numeric digits check (also catches #REF!, #VALUE!, alphanumeric typos)
        if not c_str.isdigit():
            summary["invalid_rows"] += 1
            summary["invalid_details"].append((excel_row, c_str))
            logger.warning(
                f"  [!] Excel Row {excel_row}: Skipping invalid COMPLAINT_NO '{c_str}' (must contain numeric digits only)."
            )
            continue

        # Deduplicate repeated complaint IDs within the sheet
        if c_str in seen_complaints:
            first_row = seen_complaints[c_str]
            summary["duplicate_rows"] += 1
            summary["duplicate_details"].append((excel_row, c_str, first_row))
            logger.warning(
                f"  [!] Excel Row {excel_row}: Skipping duplicate COMPLAINT_NO '{c_str}' (already queued from Row {first_row})."
            )
            continue

        # Extract and sanitize Technician Name
        if resolved_t_col is not None and pd.notna(row[resolved_t_col]):
            raw_t = str(row[resolved_t_col]).strip()
            if not raw_t or raw_t.lower() in null_tokens or raw_t.startswith("#"):
                t_name = "Unknown"
                summary["unknown_tech_rows"] += 1
                logger.warning(
                    f"  [!] Excel Row {excel_row}: Missing/invalid TECHNICIAN_NAME for Complaint #{c_str}; defaulting to 'Unknown'."
                )
            else:
                t_name = engine.sanitize_technician_name(raw_t)
                if t_name == "Unknown":
                    summary["unknown_tech_rows"] += 1
                    logger.warning(
                        f"  [!] Excel Row {excel_row}: TECHNICIAN_NAME '{raw_t}' sanitized to empty for Complaint #{c_str}; defaulting to 'Unknown'."
                    )
        else:
            t_name = "Unknown"
            summary["unknown_tech_rows"] += 1
            if not summary["missing_tech_col"]:
                logger.warning(
                    f"  [!] Excel Row {excel_row}: Blank TECHNICIAN_NAME for Complaint #{c_str}; defaulting to 'Unknown'."
                )

        seen_complaints[c_str] = excel_row
        records.append((c_str, t_name))

    summary["valid_records"] = len(records)
    return records, summary


def validate_and_load_excel_records(excel_path, complaint_col="COMPLAINT_NO", tech_col="TECHNICIAN_NAME"):
    """
    Reads an Excel file from disk and runs `validate_excel_dataframe`, printing
    the Pre-Flight Validation Summary.
    Returns (records: Optional[List[Tuple[str, str]]], summary: Dict[str, Any]).
    """
    source_label = os.path.basename(excel_path)
    try:
        df = pd.read_excel(excel_path, dtype=str)
    except Exception as e:
        logger.error(f"\n[!] Failed to read Excel file '{excel_path}': {e}", exc_info=True)
        return None, {"source_label": source_label, "read_error": str(e)}

    records, summary = validate_excel_dataframe(
        df, complaint_col=complaint_col, tech_col=tech_col, source_label=source_label
    )
    if records is None:
        return None, summary

    print("\n" + "=" * 55)
    print("         EXCEL PRE-FLIGHT VALIDATION SUMMARY")
    print("=" * 55)
    print(f" Source File            : {summary['source_label']}")
    print(f" Total Rows Scanned     : {summary['total_rows']}")
    print(f" Valid Unique Records   : {summary['valid_records']}")
    print(f" Skipped (Empty/Blank)  : {summary['empty_rows']}")
    print(f" Skipped (Invalid ID)   : {summary['invalid_rows']}")
    print(f" Skipped (Duplicates)   : {summary['duplicate_rows']}")
    print(f" Fallback Tech (Unknown): {summary['unknown_tech_rows']}")
    print("=" * 55)

    logger.info(
        "Excel pre-flight validation (%s): total=%d, valid=%d, empty=%d, invalid=%d, duplicates=%d, unknown_tech=%d.",
        summary["source_label"],
        summary["total_rows"],
        summary["valid_records"],
        summary["empty_rows"],
        summary["invalid_rows"],
        summary["duplicate_rows"],
        summary["unknown_tech_rows"],
    )
    return records, summary


def run_bulk_mode(config):
    """Loads complaints.xlsx, performs pre-flight checks and strict validation, and dispatches batch to engine."""
    engine.reset_abort()
    settings = config.get("settings", config_manager.DEFAULT_CONFIG["settings"])
    excel_rel = settings.get("excel_path", "complaints.xlsx")
    excel_path = config_manager.resolve_path(excel_rel)
    complaint_col = settings.get("complaint_column", "COMPLAINT_NO")
    tech_col = settings.get("technician_column", "TECHNICIAN_NAME")

    if not os.path.exists(excel_path):
        logger.error(f"\n[!] Error: Excel file not found at:\n    {excel_path}")
        print("    Please place the Excel file at the path shown above and try again.")
        return

    try:
        if not check_excel_lock(excel_path):
            logger.info("\n[!] Bulk mode cancelled.")
            return

        records, _ = validate_and_load_excel_records(
            excel_path, complaint_col=complaint_col, tech_col=tech_col
        )
        if records is None:
            return

        if not records:
            logger.warning("\n[!] No valid complaint records found in the Excel file.")
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

        # Prompt user for Live Printing preference
        configured_printer, is_fallback = config_manager.get_configured_printer(config)
        fallback_hint = " (Auto-Detected)" if is_fallback else ""
        p_cat = f" [{printer_manager.classify_printer(configured_printer)}]" if configured_printer else ""
        printer_label = f"{configured_printer}{p_cat}{fallback_hint}" if configured_printer else "None (PDF only)"

        live_choice = input(f"\n[->] Enable Live Physical Printing to '{printer_label}'? (y/N) [or C to change]: ").strip().lower()
        if live_choice == "c":
            configured_printer, config = _select_validated_physical_printer(config, initial_printer=configured_printer, force_prompt=True)
            if configured_printer:
                live_choice = input(f"    Enable Live Physical Printing to '{configured_printer}'? (y/N) [Default: N]: ").strip().lower()
            else:
                live_choice = "n"

        if live_choice in ["y", "yes"]:
            if not configured_printer or printer_manager.classify_printer(configured_printer) == "VIRTUAL":
                configured_printer, config = _select_validated_physical_printer(config, initial_printer=configured_printer, force_prompt=True)
            if not configured_printer:
                logger.warning("    [!] Warning: No physical printer selected. Live printing disabled.")
                live_print = False
            else:
                live_print = True
                logger.info(f"    [+] Live physical printing enabled for '{configured_printer}'.")
        else:
            live_print = False

        engine.run_batch(records, config=config, start_delay=5, organize_by_tech=organize_by_tech, live_print=live_print)
    except KeyboardInterrupt:
        logger.warning("\n[!] Bulk mode interrupted by user (Ctrl+C). Returning to main menu.")


def run_manual_mode(config):
    """
    Prompts for one or multiple complaint numbers (comma or space-separated),
    a batch/technician folder name, and processes each sequentially.
    """
    engine.reset_abort()
    print("\n" + "-" * 50)
    print("       MANUAL COMPLAINT PRINT MODE (BATCH/SINGLE)")
    print("-" * 50)
    try:
        raw_input_str = input("  Enter Complaint Number(s) (comma/space-separated, or [ENTER] to cancel): ").strip()
        if not raw_input_str:
            logger.info("  [!] Manual mode cancelled.")
            return

        # Parse and split tokens by commas and whitespace
        raw_tokens = [tok.strip() for tok in re.split(r'[\s,]+', raw_input_str) if tok.strip()]
        if not raw_tokens:
            logger.warning("  [!] No valid complaint numbers provided. Manual mode cancelled.")
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
            logger.info(f"  [!] Notice: Duplicate numbers detected and removed. ({len(complaints)} unique record(s))")

        tech_name = input("  Enter Batch / Technician Name [default: Manual_Batch]: ").strip()
        if not tech_name:
            tech_name = "Manual_Batch"

        # Prompt user for Live Printing preference
        configured_printer, is_fallback = config_manager.get_configured_printer(config)
        fallback_hint = " (Auto-Detected)" if is_fallback else ""
        p_cat = f" [{printer_manager.classify_printer(configured_printer)}]" if configured_printer else ""
        printer_label = f"{configured_printer}{p_cat}{fallback_hint}" if configured_printer else "None (PDF only)"

        live_choice = input(f"  Enable Live Physical Printing to '{printer_label}'? (y/N) [or C to change]: ").strip().lower()
        if live_choice == "c":
            configured_printer, config = _select_validated_physical_printer(config, initial_printer=configured_printer, force_prompt=True)
            if configured_printer:
                live_choice = input(f"  Enable Live Physical Printing to '{configured_printer}'? (y/N) [Default: N]: ").strip().lower()
            else:
                live_choice = "n"

        if live_choice in ["y", "yes"] and (not configured_printer or printer_manager.classify_printer(configured_printer) == "VIRTUAL"):
            configured_printer, config = _select_validated_physical_printer(config, initial_printer=configured_printer, force_prompt=True)

        live_print = (live_choice in ["y", "yes"]) and bool(configured_printer)
        if live_print:
            logger.info(f"  [+] Live physical printing enabled for '{configured_printer}'.")

        logger.info(f"\n[*] Manual Batch Queued: {len(complaints)} complaint(s) for folder '{tech_name}'.")
        logger.info("[*] Starting in 5 seconds! Please ensure your ERP window is open and maximized...")
        for i in range(5, 0, -1):
            if engine.check_abort():
                logger.warning("\n[!] Aborted during countdown.")
                return
            print(f"  Countdown: {i}...")
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.warning("\n[!] Manual mode cancelled by user (Ctrl+C).")
        return

    stats = {
        "total": len(complaints),
        "completed": 0,
        "failed": 0,
        "aborted": False
    }

    try:
        for idx, complaint_no in enumerate(complaints, 1):
            if engine.check_abort():
                logger.warning("\n[!] Manual batch processing stopped by user (Esc pressed).")
                stats["aborted"] = True
                break

            logger.info(f"\n[->] [{idx}/{len(complaints)}] Processing Complaint #: {complaint_no} (Batch: {tech_name})")
            # record_progress=False avoids state contamination with bulk execution
            success, msg = engine.process_complaint(
                complaint_no, tech_name, config=config, record_progress=False, organize_by_tech=True, live_print=live_print
            )

            if success:
                stats["completed"] += 1
                logger.info(f"  [+] Complaint #{complaint_no} processed successfully!")
            else:
                if "aborted" in msg.lower() or "failsafe" in msg.lower():
                    logger.warning(f"  [!] Execution aborted: {msg}")
                    stats["aborted"] = True
                    break
                else:
                    logger.error(f"  [!] Complaint #{complaint_no} failed or stopped: {msg}")
                    stats["failed"] += 1
    except KeyboardInterrupt:
        logger.warning("\n[!] Manual batch interrupted by user (Ctrl+C).")
        stats["aborted"] = True

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
    Prompts user for complaint numbers (single or comma/space-separated),
    displays countdown once, and sequentially invokes engine.generate_otp().
    Continues looping until user enters empty string or 'q'.
    """
    engine.reset_abort()
    print("\n" + "=" * 55)
    print("         INTERACTIVE OTP GENERATION MODE")
    print("=" * 55)
    print("Instructions:")
    print("  1. Ensure your Oracle Forms ERP window is visible and maximized.")
    print("  2. Enter or paste Complaint Number(s) (comma/space-separated) to generate OTPs.")
    print("  3. Press [ESC] at any time during execution to emergency abort.")
    print("  4. Press [ENTER] without input (or type 'q') to return to Main Menu.")
    print("=" * 55)

    try:
        while True:
            engine.reset_abort()
            raw_input = input("\n[->] Enter Complaint Number(s) (or press [ENTER] to return): ").strip()
            if not raw_input or raw_input.lower() in ["q", "quit", "exit"]:
                print("Returning to main menu...")
                break

            complaint_list = [c.strip() for c in re.split(r'[\s,]+', raw_input) if c.strip()]
            if not complaint_list:
                continue

            logger.info(f"\n[*] Starting OTP Generation for {len(complaint_list)} complaint(s) in 3 seconds! Switch to Oracle Forms window...")
            countdown_aborted = False
            for i in range(3, 0, -1):
                if engine.check_abort():
                    logger.warning("\n[!] Aborted during countdown.")
                    countdown_aborted = True
                    break
                print(f"  Countdown: {i}...")
                time.sleep(1.0)

            if countdown_aborted:
                continue

            for idx, num in enumerate(complaint_list, 1):
                if engine.check_abort():
                    logger.warning("\n[!] OTP Generation stopped by user (Esc pressed).")
                    break

                logger.info(f"\n[->] [{idx}/{len(complaint_list)}] Generating OTP for Complaint #{num}...")
                success, msg = engine.generate_otp(num, config)
                if success:
                    logger.info(f"[+] Success: {msg}")
                else:
                    logger.warning(f"[!] Notice: {msg}")
                    if "aborted" in msg.lower() or "failsafe" in msg.lower():
                        logger.warning("    Execution stopped due to abort signal.")
                        break

                time.sleep(1.0)
    except KeyboardInterrupt:
        logger.warning("\n[!] OTP mode interrupted by user (Ctrl+C). Returning to main menu...")


def _discover_technician_folders(output_dir, printed_set):
    """
    Scans output_dir for Technician subdirectories (and any root-level PDFs).
    Returns a list of dicts:
        {
            "name": str,
            "path": str,
            "pdf_files": List[str],
            "total": int,
            "printed": int,
            "pending": List[Tuple[str, str, str]]  # (complaint_no, pdf_path, folder_name)
        }
    """
    folders = []

    if not os.path.exists(output_dir):
        return folders

    # 1. Check root-level PDFs directly inside output_dir
    try:
        entries = sorted(os.listdir(output_dir))
    except Exception:
        return folders

    root_pdfs = []
    subdirs = []
    for entry in entries:
        full_path = os.path.join(output_dir, entry)
        if os.path.isfile(full_path):
            if entry.lower().endswith(".pdf") and not entry.startswith("._"):
                root_pdfs.append(full_path)
        elif os.path.isdir(full_path):
            subdirs.append((entry, full_path))

    def _build_folder_record(folder_name, folder_path, pdf_list):
        sorted_pdfs = sorted(pdf_list)
        printed_cnt = 0
        pending_list = []
        for p_path in sorted_pdfs:
            base_name = os.path.splitext(os.path.basename(p_path))[0]
            if base_name in printed_set:
                printed_cnt += 1
            else:
                pending_list.append((base_name, p_path, folder_name))
        return {
            "name": folder_name,
            "path": folder_path,
            "pdf_files": sorted_pdfs,
            "total": len(sorted_pdfs),
            "printed": printed_cnt,
            "pending": pending_list,
        }

    if root_pdfs:
        folders.append(_build_folder_record("(Root Folder)", output_dir, root_pdfs))

    # 2. Scan each technician subdirectory
    for sub_name, sub_path in subdirs:
        sub_pdfs = []
        for root, _, files in os.walk(sub_path):
            for f in files:
                if f.lower().endswith(".pdf") and not f.startswith("._"):
                    sub_pdfs.append(os.path.join(root, f))
        if sub_pdfs:
            folders.append(_build_folder_record(sub_name, sub_path, sub_pdfs))

    return folders


def run_batch_print_mode(config):
    """
    Offline Batch Printing (Technician-Wise & Sliding-Window Spooling):
    Scans Today_Complaints/ for Technician subfolders, displays a live status
    table ([Total | Printed | Pending]), allows single/multi/all folder selection,
    and dispatches PDFs using the Sliding-Window Spooler Pipeline backed by SQLite.
    Loops back to the Technician Folder Selection menu after each batch until
    the operator exits to the Main Menu.
    """
    engine.reset_abort()
    settings = config.get("settings", config_manager.DEFAULT_CONFIG["settings"])
    printing_settings = config_manager.get_printing_settings(config)
    target_printer, is_fallback = config_manager.get_configured_printer(config)

    try:
        # Check target printer; if unset or virtual, prompt and validate physical printer selection
        if not target_printer or printer_manager.classify_printer(target_printer) == "VIRTUAL":
            logger.info("\n[!] Notice: Target printer is unset or virtual. Please select a physical printer:")
            target_printer, config = _select_validated_physical_printer(
                config, initial_printer=target_printer, force_prompt=True
            )
            if not target_printer:
                logger.warning("[!] Batch printing cancelled: No physical printer selected.")
                return
            is_fallback = False

        output_dir = config_manager.resolve_path(settings.get("output_dir", "Today_Complaints"))
        db_path = config_manager.get_database_path(config)
        max_queue = printing_settings.get("max_spool_queue", 3)

        if not os.path.exists(output_dir):
            logger.warning(f"\n[!] Output directory '{output_dir}' does not exist. No PDFs to print.")
            return

        while True:
            engine.reset_abort()

            # Load SQLite tracker state on each menu cycle so counts stay up to date
            try:
                printed_set = printer_manager.load_printed_complaints(db_path)
            except Exception as e:
                logger.error(f"\n[!] Error: Cannot read SQLite print tracker '{db_path}': {e}", exc_info=True)
                print("    Batch printing aborted to prevent duplicate physical prints.")
                return

            folders = _discover_technician_folders(output_dir, printed_set)
            if not folders:
                logger.warning(f"\n[!] No PDF documents found in '{output_dir}'.")
                return

            total_all = sum(f["total"] for f in folders)
            printed_all = sum(f["printed"] for f in folders)
            pending_all = sum(len(f["pending"]) for f in folders)

            p_cat = printer_manager.classify_printer(target_printer)
            fallback_str = " (Auto-Detected)" if is_fallback else ""

            print("\n" + "=" * 70)
            print("        OFFLINE BATCH PRINTING - SELECT TECHNICIAN FOLDER")
            print("=" * 70)
            print(f" Target Printer   : {target_printer} [{p_cat}]{fallback_str}")
            print(f" Base Directory   : {output_dir}")
            print(f" State Database   : {os.path.basename(db_path)} (SQLite WAL)")
            print(f" Spooler Window   : Up to {max_queue} active jobs (Sliding-Window Pipeline)")
            print("-" * 70)
            print(f"  {'#':<5} {'Technician Folder':<34} {'Total':>7} {'Printed':>9} {'Pending':>9}")
            print("-" * 70)
            for idx, f_info in enumerate(folders, 1):
                idx_label = f"[{idx}]"
                f_name = f_info["name"]
                if len(f_name) > 32:
                    f_name = f_name[:29] + "..."
                print(
                    f"  {idx_label:<5} {f_name:<34} {f_info['total']:>7} "
                    f"{f_info['printed']:>9} {len(f_info['pending']):>9}"
                )
            print("-" * 70)
            print(
                f"  {'[A]':<5} {'All Folders (Combined)':<34} {total_all:>7} "
                f"{printed_all:>9} {pending_all:>9}"
            )
            print("-" * 70)
            print("  [R] Reset Global Print Tracker   [C] Change Printer   [Q] Main Menu")
            print("=" * 70)

            sel_raw = input(
                f"Select folder(s) (1-{len(folders)}, e.g. 1 or 1,2 | A=All | R=Reset | C=Printer | [Enter]/Q=Menu): "
            ).strip()

            if not sel_raw or sel_raw.lower() in ["q", "quit", "exit"]:
                print("Returning to main menu...")
                return

            low_sel = sel_raw.lower()
            if low_sel == "c":
                new_printer, config = _select_validated_physical_printer(
                    config, initial_printer=target_printer, force_prompt=True
                )
                if new_printer:
                    target_printer = new_printer
                    is_fallback = False
                continue

            if low_sel == "r":
                confirm_r = input(
                    " [!] Archive and reset the ENTIRE global print tracker for all folders? (y/N): "
                ).strip().lower()
                if confirm_r in ["y", "yes"]:
                    try:
                        printer_manager.reset_printed_tracker_safe(db_path)
                    except Exception as e:
                        logger.error(f" [!] Tracker reset aborted: {e}", exc_info=True)
                else:
                    logger.info(" [i] Tracker reset cancelled.")
                continue

            selected_folders = []
            if low_sel in ["a", "all"]:
                selected_folders = list(folders)
                scope_label = f"All Folders ({len(folders)} folder(s))"
            else:
                tokens = [t.strip() for t in re.split(r"[\s,]+", sel_raw) if t.strip()]
                seen_indices = set()
                invalid_token = False
                for tok in tokens:
                    if not tok.isdigit():
                        invalid_token = True
                        break
                    f_idx = int(tok)
                    if f_idx < 1 or f_idx > len(folders):
                        invalid_token = True
                        break
                    if f_idx not in seen_indices:
                        seen_indices.add(f_idx)
                        selected_folders.append(folders[f_idx - 1])

                if invalid_token or not selected_folders:
                    logger.warning(
                        f"\n[!] Invalid selection '{sel_raw}'. Please enter folder number(s) between 1 and {len(folders)}, 'A', 'R', 'C', or 'Q'."
                    )
                    continue

                if len(selected_folders) == 1:
                    scope_label = selected_folders[0]["name"]
                else:
                    scope_label = ", ".join(f["name"] for f in selected_folders)

            # Aggregate PDFs for the selected folder(s)
            all_selected_jobs = []
            pending_jobs = []
            already_printed_count = 0

            for f_info in selected_folders:
                already_printed_count += f_info["printed"]
                pending_jobs.extend(f_info["pending"])
                for p_path in f_info["pdf_files"]:
                    base_name = os.path.splitext(os.path.basename(p_path))[0]
                    all_selected_jobs.append((base_name, p_path, f_info["name"]))

            p_cat = printer_manager.classify_printer(target_printer)
            fallback_str = " (Auto-Detected)" if is_fallback else ""
            print("\n" + "=" * 65)
            print(f"        SELECTED BATCH SUMMARY: {scope_label}")
            print("=" * 65)
            print(f" Target Printer   : {target_printer} [{p_cat}]{fallback_str}")
            print(f" Selected Folder  : {scope_label}")
            print(f" Total PDFs Found : {len(all_selected_jobs)}")
            print(f" Already Printed  : {already_printed_count}")
            print(f" Pending to Print : {len(pending_jobs)}")
            print(f" Spooler Window   : {max_queue} active jobs")
            print("=" * 65)

            # Scoped action prompt when previously printed documents exist in selection
            if already_printed_count > 0 and pending_jobs:
                print(
                    f"\n [i] Notice: {already_printed_count} of {len(all_selected_jobs)} document(s) in '{scope_label}' were previously printed."
                )
                print(
                    " Options: [Enter] Print pending only | [A] Force re-print all in selection | [C] Change printer | [B] Back to folder list"
                )
                act = input(" Select action ([Enter] / A / C / B): ").strip().lower()
                if act == "b":
                    continue
                elif act == "c":
                    new_printer, config = _select_validated_physical_printer(
                        config, initial_printer=target_printer, force_prompt=True
                    )
                    if not new_printer:
                        logger.info("[!] Printer change cancelled.")
                        continue
                    target_printer = new_printer
                    is_fallback = False
                elif act == "a":
                    pending_jobs = list(all_selected_jobs)
                    logger.info(f" [*] Queuing all {len(pending_jobs)} documents in '{scope_label}' for re-printing...")
            elif not pending_jobs:
                print(f"\n[✓] All PDFs in '{scope_label}' have already been printed according to tracker!")
                print(
                    " Options: [A] Force re-print all in selection | [C] Change printer | [Enter]/[B] Back to folder list"
                )
                reprint_ans = input(" Select action (A / C / [Enter]): ").strip().lower()
                if reprint_ans == "a":
                    pending_jobs = list(all_selected_jobs)
                    logger.info(f" [*] Queuing all {len(pending_jobs)} documents in '{scope_label}' for re-printing...")
                elif reprint_ans == "c":
                    new_printer, config = _select_validated_physical_printer(
                        config, initial_printer=target_printer, force_prompt=True
                    )
                    if new_printer:
                        target_printer = new_printer
                        is_fallback = False
                    continue
                else:
                    continue

            confirm = input(
                f"\nProceed with printing {len(pending_jobs)} document(s) from '{scope_label}' to '{target_printer}'? (Y/n) [C=Printer, B=Back]: "
            ).strip().lower()
            if confirm == "b":
                continue
            if confirm == "c":
                new_printer, config = _select_validated_physical_printer(
                    config, initial_printer=target_printer, force_prompt=True
                )
                if not new_printer:
                    logger.info(" [!] Printer selection cancelled.")
                    continue
                target_printer = new_printer
                is_fallback = False
                confirm = input(
                    f"Proceed with printing {len(pending_jobs)} document(s) to '{target_printer}'? (Y/n) [Default: Y]: "
                ).strip().lower()

            if confirm not in ["", "y", "yes"]:
                logger.info(" [i] Batch printing cancelled for current selection. Returning to folder menu...")
                continue

            logger.info("\n[*] Starting batch printing in 3 seconds! Press [ESC] at any time to abort...")
            countdown_aborted = False
            for i in range(3, 0, -1):
                if engine.check_abort():
                    logger.warning("\n[!] Aborted during countdown.")
                    countdown_aborted = True
                    break
                print(f"  Countdown: {i}...")
                time.sleep(1.0)

            if countdown_aborted:
                continue

            stats = {
                "total": len(pending_jobs),
                "printed": 0,
                "skipped": 0,
                "failed": 0,
                "aborted": False,
            }
            in_flight = []

            def _on_poll_reconcile(spool_status):
                c_done, f_done = printer_manager.reconcile_in_flight_jobs(
                    target_printer, in_flight, db_path, status=spool_status
                )
                stats["printed"] += c_done
                stats["failed"] += len(f_done)

            for idx, (c_num, p_path, f_name) in enumerate(pending_jobs, 1):
                if engine.check_abort():
                    logger.warning("\n[!] Batch printing stopped by user (Esc pressed).")
                    stats["aborted"] = True
                    break

                # Opportunistically reconcile any in-flight jobs that finished before this iteration
                try:
                    c_done, f_done = printer_manager.reconcile_in_flight_jobs(
                        target_printer, in_flight, db_path
                    )
                    stats["printed"] += c_done
                    stats["failed"] += len(f_done)
                except Exception as rec_err:
                    logger.error(f"  [!] Tracker error while reconciling in-flight jobs: {rec_err}", exc_info=True)
                    stats["aborted"] = True
                    break

                logger.info(
                    f"\n[->] [{idx}/{len(pending_jobs)}] Printing: {os.path.basename(p_path)} "
                    f"(Complaint #{c_num} | Folder: {f_name})"
                )

                # Sliding-Window Spooler Queue Gate: keeps up to max_queue jobs buffered
                try:
                    gate_ok = printer_manager.enforce_queue_gate(
                        target_printer,
                        max_jobs=max_queue,
                        on_poll=_on_poll_reconcile,
                        check_abort_fn=engine.check_abort,
                    )
                except KeyboardInterrupt:
                    stats["aborted"] = True
                    break
                except Exception as gate_err:
                    logger.error(f"  [!] Error during queue gate reconciliation: {gate_err}", exc_info=True)
                    stats["aborted"] = True
                    break

                if not gate_ok:
                    logger.info(f"  [i] Skipped Complaint #{c_num} by user request.")
                    stats["skipped"] += 1
                    continue

                try:
                    doc_name = f"Complaint_{c_num}.pdf"
                    job_id = printer_manager.print_pdf_silent(p_path, target_printer, doc_name=doc_name)
                    if not job_id:
                        raise RuntimeError("Silent print did not return a valid spool job.")
                    printer_manager.mark_complaint_spooled(
                        db_path, c_num, p_path, target_printer, job_id=job_id
                    )
                    in_flight.append(
                        {
                            "complaint_no": c_num,
                            "file_path": p_path,
                            "job_id": job_id,
                            "doc_name": doc_name,
                        }
                    )
                    logger.info(
                        f"  [*] Complaint #{c_num} spooled to {target_printer} "
                        f"(Sliding window: {len(in_flight)}/{max_queue} active)."
                    )
                    time.sleep(0.25)  # Micro-throttle between spool dispatches
                except KeyboardInterrupt:
                    logger.warning("\n[!] Batch printing interrupted by user (Ctrl+C).")
                    stats["aborted"] = True
                    break
                except Exception as e:
                    logger.error(f"  [!] Failed to spool {os.path.basename(p_path)}: {e}", exc_info=True)
                    stats["failed"] += 1

            # Final Drain Pass: reconcile all remaining spooled jobs in the sliding window
            if in_flight and not stats["aborted"]:
                try:
                    c_drained, f_drained, drain_aborted = printer_manager.drain_in_flight_jobs(
                        target_printer,
                        in_flight,
                        db_path,
                        check_abort_fn=engine.check_abort,
                    )
                    stats["printed"] += c_drained
                    stats["failed"] += len(f_drained)
                    if drain_aborted:
                        stats["aborted"] = True
                except KeyboardInterrupt:
                    logger.warning("\n[!] Final spooler drain interrupted by user.")
                    stats["aborted"] = True
                except Exception as drain_err:
                    logger.error(f"\n[!] Error during final spooler drain: {drain_err}", exc_info=True)
                    stats["failed"] += len(in_flight)
                    in_flight.clear()

            logger.info(
                "Offline batch printing summary (%s): total=%d, printed=%d, skipped=%d, failed=%d, aborted=%s",
                scope_label,
                stats["total"],
                stats["printed"],
                stats["skipped"],
                stats["failed"],
                stats["aborted"],
            )
            print("\n" + "=" * 55)
            print(f"        BATCH PRINTING SUMMARY ({scope_label})")
            print("=" * 55)
            print(f" Total Queued:         {stats['total']}")
            print(f" Confirmed Printed:    {stats['printed']}")
            print(f" Skipped:              {stats['skipped']}")
            print(f" Failed:               {stats['failed']}")
            print(f" Interrupted:          {stats['aborted']}")
            print("=" * 55)
    except KeyboardInterrupt:
        logger.warning("\n[!] Batch printing cancelled by user (Ctrl+C).")
        return


def main():
    """Main CLI entry point and router."""
    config_manager.set_dpi_awareness()
    logger_manager.setup_logging()

    print("\n" + "=" * 60)
    print("      ORACLE ERP AUTOMATION SUITE (v6.0)")
    print("=" * 60)

    config_path = config_manager.get_config_path()
    if not os.path.exists(config_path):
        logger.warning("\n[!] 'config.json' not found! First-time setup is required.")
        ans = input("Would you like to launch the Calibration Wizard now? [Y/n]: ").strip().lower()
        if ans in ["", "y", "yes"]:
            calibrated = calibration_wizard.run_calibration_wizard()
            if not calibrated:
                logger.info("[!] Using default fallback coordinates.")
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
            logger.critical(f"\n[!] Fatal: Cannot launch application because 'config.json' is corrupted: {e}", exc_info=True)
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
            logger.warning(f"\n[!] Warning: Failed to reload 'config.json' ({e}). Using active configuration.")

        # Check for screen resolution drift
        has_drift, curr_res, calib_res = config_manager.check_resolution_drift(config)
        if has_drift:
            logger.warning(
                "Screen resolution drift detected: current=%dx%d, calibrated=%dx%d",
                curr_res[0],
                curr_res[1],
                calib_res[0],
                calib_res[1],
            )
            print("\n" + "!" * 60)
            print(" [WARNING] SCREEN RESOLUTION DRIFT DETECTED!")
            print(f" Current Resolution:    {curr_res[0]} x {curr_res[1]}")
            print(f" Calibrated Resolution: {calib_res[0]} x {calib_res[1]}")
            print(" Coordinates may not align with ERP buttons. Select [3] to recalibrate.")
            print("!" * 60)

        configured_p, is_fb = config_manager.get_configured_printer(config)
        p_badge = f" [{printer_manager.classify_printer(configured_p)}]" if configured_p else ""
        fb_note = " (Auto-Detected)" if is_fb else ""

        print("\n" + "=" * 60)
        print(" MAIN MENU")
        print(f" Target Printer: {configured_p or 'None'}{p_badge}{fb_note}")
        print("=" * 60)
        print("  [1] Bulk Mode (Process complaints.xlsx)")
        print("  [2] Manual Mode (Process single/batch complaint #s)")
        print("  [3] Recalibrate Coordinates (Setup Wizard)")
        print("  [4] Generate OTP (Interactive Mode)")
        print("  [5] Batch Print Offline PDFs (Today_Complaints)")
        print("  [6] Select / Change Target Printer")
        print("  [7] Exit")
        print("=" * 60)

        choice = input("Select an option [1-7]: ").strip()

        if choice == "1":
            run_bulk_mode(config)
        elif choice == "2":
            run_manual_mode(config)
        elif choice == "3":
            try:
                calibration_wizard.run_calibration_wizard()
            except KeyboardInterrupt:
                logger.warning("\n[!] Calibration wizard interrupted. Returning to main menu.")
        elif choice == "4":
            run_otp_mode(config)
        elif choice == "5":
            run_batch_print_mode(config)
        elif choice == "6":
            try:
                printer_manager.select_printer_interactive(config=config, auto_save=True, force_prompt=True)
            except KeyboardInterrupt:
                logger.warning("\n[!] Printer selection cancelled.")
        elif choice == "7" or choice.lower() in ["exit", "q", "quit"]:
            logger.info("\nExiting ERP Automation Suite. Goodbye!")
            break
        else:
            logger.warning("\n[!] Invalid choice. Please enter 1, 2, 3, 4, 5, 6, or 7.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n\n[!] Application terminated by user.")
        sys.exit(0)
