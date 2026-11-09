import time
import sys
import os
import datetime

# Ensure Unicode output doesn't crash on standard Windows consoles (cp1252)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import pyautogui
import keyboard

try:
    import winsound
except ImportError:
    winsound = None

import config_manager

TARGETS = [
    ("complaint_input", "1. Complaint Number Field (where complaint # is entered)"),
    ("main_print_btn", "2. Main Print Button (triggers the print dialog)"),
    ("ha_field_checkbox", "3. 'FOR HA FIELD' Checkbox"),
    ("popup_print_btn", "4. Popup Print Button (sends report to IE/Foxit)"),
    ("popup_exit_btn", "5. Popup Exit Button (closes the print modal)")
]

PRINT_TARGETS = [
    ("complaint_input", "1. Complaint Number Field (where complaint # is entered)"),
    ("main_print_btn", "2. Main Print Button (triggers the print dialog)"),
    ("ha_field_checkbox", "3. 'FOR HA FIELD' Checkbox"),
    ("popup_print_btn", "4. Popup Print Button (sends report to IE/Foxit)"),
    ("popup_exit_btn", "5. Popup Exit Button (closes the print modal)")
]

OTP_TARGETS = [
    ("otp_add_btn", "1. OTP 'Add' Button (opens the OTP module)"),
    ("otp_complaint_search_btn", "2. Complaint Search (3-Dots) Button (opens complaint search modal)"),
    ("otp_search_input", "3. Search Input Field (double-clicked and complaint # pasted)"),
    ("otp_reason_search_btn", "4. OTP Reason (3-Dots) Button (double-tap focused to open reason list)")
]

ALL_TARGETS = PRINT_TARGETS + OTP_TARGETS
TARGETS = ALL_TARGETS

def play_chime(freq=1000, duration_ms=200):
    """Plays an audible beep to confirm coordinate capture."""
    if winsound:
        try:
            winsound.Beep(freq, duration_ms)
        except Exception:
            pass

def wait_for_capture():
    """
    Listens globally for Space or F8 keypress without needing terminal focus.
    Returns (x, y) coordinates of mouse cursor at moment of keypress.
    Returns (None, None) if Esc is pressed.
    """
    # Wait until keys are released to prevent accidental immediate trigger
    while keyboard.is_pressed('space') or keyboard.is_pressed('f8'):
        time.sleep(0.05)

    while True:
        if keyboard.is_pressed('space') or keyboard.is_pressed('f8'):
            pos = pyautogui.position()
            play_chime()
            # Debounce until key release
            while keyboard.is_pressed('space') or keyboard.is_pressed('f8'):
                time.sleep(0.05)
            return pos.x, pos.y

        if keyboard.is_pressed('esc'):
            while keyboard.is_pressed('esc'):
                time.sleep(0.05)
            return None, None

        time.sleep(0.03)

def replay_cursor_verification(coords, targets=None):
    """
    Tier 1 Verification:
    Smoothly moves mouse cursor through each calibrated coordinate with a pause.
    Allows user to visually confirm alignment with their ERP screen elements.
    """
    if targets is None:
        targets = ALL_TARGETS

    print("\n" + "=" * 55)
    print(" [TIER 1 VERIFICATION] Visual Cursor Replay")
    print("=" * 55)
    print("Please ensure your ERP window is visible.")
    print(f"The cursor will glide across {len(targets)} calibrated target(s) in 3 seconds...")
    time.sleep(3)

    for key, label in targets:
        pt = coords.get(key, {})
        x = pt.get("x")
        y = pt.get("y")
        if x is not None and y is not None:
            print(f" -> Moving to: {label} [X: {x}, Y: {y}]")
            pyautogui.moveTo(x, y, duration=0.8)
            time.sleep(0.5)

    print("\nVisual cursor replay complete!")
    print("=" * 55 + "\n")

def configure_printer_menu(cfg=None):
    """
    Interactive workflow to detect, select, and configure the target Windows printer
    and spooler queue thresholds.
    """
    import printer_manager as pm

    if cfg is None:
        cfg = config_manager.load_config() or config_manager.DEFAULT_CONFIG.copy()

    printers = pm.get_installed_printers()
    default_printer = pm.get_default_printer()
    current_printer = cfg.get("printing", {}).get("printer_name")
    current_max_queue = cfg.get("printing", {}).get("max_spool_queue", 3)

    print("\n" + "=" * 60)
    print("      SILENT PRINTER & SPOOLER CONFIGURATION")
    print("=" * 60)
    print(f"Windows Default Printer  : {default_printer or 'None detected'}")
    print(f"Current Configured Target: {current_printer or '(Dynamic OS Default)'}")
    print(f"Current Queue Threshold  : {current_max_queue} pending jobs")
    print("-" * 60)
    print("Installed Windows Printers:")
    for idx, p_name in enumerate(printers, start=1):
        tags = []
        if p_name == default_printer:
            tags.append("DEFAULT")
        if p_name == current_printer:
            tags.append("SELECTED")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        print(f"  [{idx}] {p_name}{tag_str}")

    print("  [0] Dynamic OS Default (auto-detect system default)")
    print("=" * 60)

    choice = input(f"\nSelect printer [0-{len(printers)}] (or press Enter to keep current): ").strip()
    selected_name = current_printer

    if choice == "0":
        selected_name = None
        print(f" [✓] Set target printer to Dynamic OS Default ({default_printer}).")
    elif choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(printers):
            selected_name = printers[idx - 1]
            print(f" [✓] Selected target printer: {selected_name}")
        else:
            print(" [!] Invalid index. Keeping existing printer setting.")
    else:
        print(" [i] Keeping current printer setting.")

    # Queue threshold configuration
    q_input = input(f"\nEnter max spooler queue jobs before hardware alert [default: {current_max_queue}]: ").strip()
    if q_input.isdigit() and int(q_input) > 0:
        max_q = int(q_input)
    else:
        max_q = current_max_queue

    # Optional test print / connectivity verification
    print("\n" + "-" * 60)
    target_to_test = selected_name or default_printer
    if target_to_test:
        test_ans = input(f"Would you like to test spooler connectivity for '{target_to_test}'? [y/N]: ").strip().lower()
        if test_ans in ["y", "yes"]:
            print(f" [*] Querying Windows Spooler for '{target_to_test}'...")
            q_status = pm.get_queue_status(target_to_test)
            print(f"     Status: {'OK' if not q_status['has_error'] else 'ALERT'}")
            print(f"     Active Spooler Jobs: {q_status['job_count']}")
            if q_status['has_error']:
                print(f"     Errors: {', '.join(q_status['error_reasons'])}")
            else:
                print("     [+] Printer device is connected and responsive!")

    # Save to config
    if "printing" not in cfg:
        cfg["printing"] = {}
    cfg["printing"]["printer_name"] = selected_name
    cfg["printing"]["max_spool_queue"] = max_q
    config_manager.save_config(cfg)
    print("\n[+] Printer configuration successfully saved to config.json!")
    return True

def run_calibration_wizard():
    """
    Interactive CLI Calibration Wizard.
    Provides a modular sub-menu to calibrate Bulk Print targets (5),
    OTP Generation targets (4), or All targets (9).
    """
    config_manager.set_dpi_awareness()
    cfg = config_manager.load_config()
    if cfg is None:
        cfg = config_manager.DEFAULT_CONFIG.copy()

    screen_w, screen_h = pyautogui.size()

    print("\n" + "=" * 60)
    print("    ORACLE ERP AUTOMATION SUITE - CALIBRATION WIZARD")
    print("=" * 60)
    print(f"Current Screen Resolution: {screen_w} x {screen_h}")
    print("\nSelect Calibration Scope:")
    print("  [1] Calibrate Bulk Print Targets (5 targets)")
    print("  [2] Calibrate OTP Generation Targets (4 targets)")
    print("  [3] Calibrate All Targets (9 targets)")
    print("  [4] Configure Physical Printer & Spooler")
    print("  [5] Return to Main Menu")

    choice = input("\nEnter choice [1-5]: ").strip()
    if choice == "1":
        selected_targets = PRINT_TARGETS
        scope_name = "Bulk Print"
    elif choice == "2":
        selected_targets = OTP_TARGETS
        scope_name = "OTP Generation"
    elif choice == "3":
        selected_targets = ALL_TARGETS
        scope_name = "All Automation"
    elif choice == "4":
        configure_printer_menu(cfg)
        return True
    elif choice in ["5", "q", "exit", ""]:
        print("Returning to main menu...")
        return False
    else:
        print("[!] Invalid selection. Returning to main menu.")
        return False

    print("\n" + "=" * 60)
    print(f"    CALIBRATING: {scope_name.upper()} ({len(selected_targets)} Targets)")
    print("=" * 60)
    print("Instructions:")
    print("  1. Position your mouse cursor over the requested ERP button/field.")
    print("  2. Press [SPACE] or [F8] on your keyboard to capture coordinates.")
    print("  3. An audio chime will confirm capture.")
    print("  4. Press [ESC] at any time to cancel calibration.")
    print("=" * 60 + "\n")

    new_coords = cfg.get("coordinates", {}).copy()

    for key, label in selected_targets:
        while True:
            current_val = new_coords.get(key, {"x": None, "y": None})
            print(f"\nTarget: {label}")
            if current_val.get("x") is not None and current_val.get("y") is not None:
                print(f"  Current value: (x={current_val['x']}, y={current_val['y']})")
            print("  >> Hover mouse over target and press [SPACE] or [F8] (or [ESC] to abort)...")

            x, y = wait_for_capture()

            if x is None and y is None:
                print("\n[!] Calibration aborted by user.")
                return False

            print(f"  Captured: X = {x}, Y = {y}")
            choice_sub = input("  Accept this coordinate? [Y/n/r (retry)]: ").strip().lower()

            if choice_sub in ["", "y", "yes"]:
                new_coords[key] = {"x": int(x), "y": int(y)}
                break
            elif choice_sub in ["r", "retry"]:
                print("  Retrying target capture...")
                continue
            else:
                print("  Retrying target capture...")
                continue

    # Update config with new coordinates and screen metadata
    cfg["coordinates"] = new_coords
    cfg["screen_metadata"] = {
        "calibrated_resolution": [screen_w, screen_h],
        "calibration_timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    config_manager.save_config(cfg)
    print("\n[+] Coordinates and screen metadata successfully saved to config.json!")

    # Tier 1 Verification: Visual Cursor Replay
    replay_cursor_verification(new_coords, selected_targets)

    # Prompt user if they want to repeat replay or proceed
    while True:
        ans = input("Would you like to repeat the visual cursor replay? [y/N]: ").strip().lower()
        if ans in ["y", "yes"]:
            replay_cursor_verification(new_coords, selected_targets)
        else:
            break

    # Tier 2 Verification: Optional Single Complaint Test Run (for print targets)
    if choice in ["1", "3"]:
        print("\n" + "=" * 55)
        print(" [TIER 2 VERIFICATION] Optional Single-Complaint Test")
        print("=" * 55)
        tier2_ans = input("Would you like to dispatch an optional single-complaint test run? [y/N]: ").strip().lower()
        if tier2_ans in ["y", "yes"]:
            test_no = input("  Enter Complaint Number for test run [default: 282636629]: ").strip()
            if not test_no:
                test_no = "282636629"
            print(f"\n[*] Running Tier 2 verification test with complaint #{test_no}...")
            import engine
            engine.process_complaint(test_no, "Calibration_Test", config=cfg, record_progress=False)

    print("\nCalibration Wizard finished successfully!")
    return True

if __name__ == "__main__":
    run_calibration_wizard()
