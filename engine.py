import os
import sys
import time
import re
import pyautogui
import keyboard
import config_manager

# Ensure failsafe is active (slam mouse into any corner to emergency abort)
pyautogui.FAILSAFE = True

def sanitize_technician_name(raw_name):
    """Sanitizes technician name for Windows folder path compatibility."""
    if not raw_name or not str(raw_name).strip() or str(raw_name).strip().lower() == "nan":
        return "Unknown"
    cleaned = re.sub(r'[\\/*?:"<>|]', "", str(raw_name).strip())
    return cleaned if cleaned else "Unknown"

def get_completed_complaints(progress_file_path):
    """Reads completed complaints tracking file and returns a set of IDs."""
    if not os.path.exists(progress_file_path):
        return set()
    try:
        with open(progress_file_path, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except Exception as e:
        print(f"[Warning] Failed to read {progress_file_path}: {e}")
        return set()

def record_completed_complaint(progress_file_path, complaint_no):
    """Appends complaint number to the tracking file."""
    try:
        with open(progress_file_path, "a", encoding="utf-8") as f:
            f.write(str(complaint_no).strip() + "\n")
    except Exception as e:
        print(f"[Warning] Failed to write {complaint_no} to {progress_file_path}: {e}")

def check_abort():
    """Checks if Esc is pressed for immediate abort."""
    return keyboard.is_pressed('esc')

def process_complaint(complaint_no, tech_name, config=None, record_progress=True):
    """
    Executes the battle-tested RPA printing sequence for a single complaint.
    Returns (success: bool, message: str).
    """
    if config is None:
        config = config_manager.load_config()
        if config is None:
            config = config_manager.DEFAULT_CONFIG

    coords = config.get("coordinates", config_manager.DEFAULT_CONFIG["coordinates"])
    timings = config.get("timings", config_manager.DEFAULT_CONFIG["timings"])
    settings = config.get("settings", config_manager.DEFAULT_CONFIG["settings"])

    num = str(complaint_no).strip()
    tech = sanitize_technician_name(tech_name)

    # Prepare technician destination folder
    output_base = config_manager.resolve_path(settings.get("output_dir", "Today_Complaints"))
    tech_folder = os.path.join(output_base, tech)
    os.makedirs(tech_folder, exist_ok=True)

    progress_file = config_manager.resolve_path(settings.get("progress_file", "completed_complaints.txt"))

    # Early abort check
    if check_abort():
        return False, "Process aborted by user (Esc pressed)."

    try:
        # Step A: Focus & Enter Complaint Number
        input_pt = coords.get("complaint_input", {"x": 118, "y": 160})
        pyautogui.click(x=input_pt["x"], y=input_pt["y"])
        time.sleep(timings.get("focus_delay", 0.5))

        pyautogui.doubleClick(x=input_pt["x"], y=input_pt["y"])
        time.sleep(timings.get("focus_delay", 0.5))

        pyautogui.press('backspace')
        pyautogui.write(num)
        pyautogui.press('enter')
        time.sleep(timings.get("record_load_seconds", 3.0))

        if check_abort():
            return False, "Process aborted by user (Esc pressed)."

        # Step B: Main Print Click
        main_print = coords.get("main_print_btn", {"x": 1196, "y": 363})
        pyautogui.click(x=main_print["x"], y=main_print["y"])
        time.sleep(timings.get("popup_wait_seconds", 2.0))

        if check_abort():
            return False, "Process aborted by user (Esc pressed)."

        # Step C: HA FIELD Checkbox
        ha_checkbox = coords.get("ha_field_checkbox", {"x": 764, "y": 419})
        pyautogui.click(x=ha_checkbox["x"], y=ha_checkbox["y"])
        time.sleep(timings.get("checkbox_wait_seconds", 1.0))

        # Step D: Popup Print Click
        popup_print = coords.get("popup_print_btn", {"x": 671, "y": 493})
        pyautogui.click(x=popup_print["x"], y=popup_print["y"])

        # Step E: Wait for PDF to load in browser
        print(f"⏳ Waiting for {num} PDF to load in browser...")
        pdf_load_wait = timings.get("browser_pdf_load_seconds", 8.0)
        # Sleep in intervals to allow responsive Esc abort
        elapsed = 0.0
        while elapsed < pdf_load_wait:
            if check_abort():
                return False, "Process aborted by user (Esc pressed)."
            time.sleep(0.5)
            elapsed += 0.5

        # Step F: Print & Save as PDF
        print(f"🖨️ Pressing Ctrl+P for {tech}...")
        pyautogui.hotkey('ctrl', 'p')
        time.sleep(timings.get("foxit_print_dialog_seconds", 2.5))

        # Clicks 'OK' on the Foxit Print menu
        pyautogui.press('enter')
        time.sleep(timings.get("save_dialog_seconds", 3.0))

        # Type absolute path and save in technician's folder
        file_path = os.path.join(tech_folder, f"{num}.pdf")
        pyautogui.write(file_path)
        time.sleep(1.0)
        pyautogui.press('enter') # Save
        time.sleep(timings.get("file_save_processing_seconds", 4.0))

        # Step G: Cleanup & Record
        print("🔄 Switching back to ERP window using Alt+Tab...")
        pyautogui.hotkey('alt', 'tab')
        time.sleep(timings.get("window_switch_seconds", 2.0))

        # Close the Print Module popup
        print("❌ Closing the Print Module...")
        popup_exit = coords.get("popup_exit_btn", {"x": 749, "y": 493})
        pyautogui.click(x=popup_exit["x"], y=popup_exit["y"])
        time.sleep(timings.get("popup_close_seconds", 1.0))

        # Write to tracking file if requested
        if record_progress:
            record_completed_complaint(progress_file, num)

        print(f"✅ {num} successfully saved in {tech}'s folder!")
        time.sleep(1.0)
        return True, f"{num} completed successfully."

    except pyautogui.FailSafeException:
        print("\n[!] Emergency FailSafe triggered (mouse in screen corner)!")
        return False, "Aborted by FailSafe"
    except Exception as e:
        print(f"\n[!] Error during processing of complaint {num}: {e}")
        return False, str(e)

def run_batch(records_iterable, config=None, start_delay=5):
    """
    Executes batch processing for an iterable of (complaint_no, tech_name) records.
    Returns summary statistics dict: {'total', 'completed', 'skipped', 'failed', 'aborted'}.
    """
    config_manager.set_dpi_awareness()
    if config is None:
        config = config_manager.load_config()
        if config is None:
            config = config_manager.DEFAULT_CONFIG

    settings = config.get("settings", config_manager.DEFAULT_CONFIG["settings"])
    progress_file = config_manager.resolve_path(settings.get("progress_file", "completed_complaints.txt"))
    completed_set = get_completed_complaints(progress_file)

    records = list(records_iterable)
    stats = {
        "total": len(records),
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "aborted": False
    }

    print(f"\n🚀 Batch Execution Initialized: {len(records)} total records.")
    print(f"Auto-resume: {len(completed_set)} already completed records found.")
    print(f"Starting in {start_delay} seconds! Please ensure your ERP window is open and maximized...")
    
    for i in range(start_delay, 0, -1):
        if check_abort():
            print("\n[!] Aborted during countdown.")
            stats["aborted"] = True
            return stats
        print(f"  Countdown: {i}...")
        time.sleep(1.0)

    for idx, (complaint_no, tech_name) in enumerate(records, 1):
        num = str(complaint_no).strip()
        tech = sanitize_technician_name(tech_name)

        if check_abort():
            print("\n[!] Batch processing stopped by User (Esc pressed).")
            stats["aborted"] = True
            break

        if num in completed_set:
            print(f"⏩ [{idx}/{len(records)}] {num} already done. Skipping...")
            stats["skipped"] += 1
            continue

        print(f"\n👉 [{idx}/{len(records)}] Processing Complaint #: {num} (Tech: {tech})")
        success, msg = process_complaint(num, tech, config=config, record_progress=True)

        if success:
            completed_set.add(num)
            stats["completed"] += 1
        else:
            if "aborted" in msg.lower() or "failsafe" in msg.lower():
                stats["aborted"] = True
                break
            stats["failed"] += 1

    print("\n" + "=" * 50)
    print("           BATCH EXECUTION SUMMARY")
    print("=" * 50)
    print(f" Total records:    {stats['total']}")
    print(f" Completed now:    {stats['completed']}")
    print(f" Skipped:          {stats['skipped']}")
    print(f" Failed:           {stats['failed']}")
    print(f" Interrupted:      {stats['aborted']}")
    print("=" * 50 + "\n")

    return stats

if __name__ == "__main__":
    print("engine.py loaded. Testing functions...")
    test_tech = sanitize_technician_name("Ameer/Hamza*Test?")
    print(f"Sanitization test: 'Ameer/Hamza*Test?' -> '{test_tech}'")
    assert test_tech == "AmeerHamzaTest", "Sanitization failed!"
    print("All engine utility checks passed successfully!")
