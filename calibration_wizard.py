import time
import sys
import os
import datetime
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

def replay_cursor_verification(coords):
    """
    Tier 1 Verification:
    Smoothly moves mouse cursor through each calibrated coordinate with a pause.
    Allows user to visually confirm alignment with their ERP screen elements.
    """
    print("\n" + "=" * 55)
    print(" [TIER 1 VERIFICATION] Visual Cursor Replay")
    print("=" * 55)
    print("Please ensure your ERP window is visible.")
    print("The cursor will glide across all 5 calibrated targets in 3 seconds...")
    time.sleep(3)

    for key, label in TARGETS:
        pt = coords.get(key, {})
        x = pt.get("x")
        y = pt.get("y")
        if x is not None and y is not None:
            print(f" -> Moving to: {label} [X: {x}, Y: {y}]")
            pyautogui.moveTo(x, y, duration=0.8)
            time.sleep(0.5)

    print("\nVisual cursor replay complete!")
    print("=" * 55 + "\n")

def run_calibration_wizard():
    """
    Interactive CLI Calibration Wizard.
    Guides user through capturing the 5 essential ERP UI targets using global hotkeys.
    """
    config_manager.set_dpi_awareness()
    cfg = config_manager.load_config()
    if cfg is None:
        cfg = config_manager.DEFAULT_CONFIG.copy()

    screen_w, screen_h = pyautogui.size()

    print("\n" + "=" * 60)
    print("    ORACLE ERP BULK PRINTER - CALIBRATION WIZARD")
    print("=" * 60)
    print(f"Current Screen Resolution: {screen_w} x {screen_h}")
    print("Instructions:")
    print("  1. Position your mouse cursor over the requested ERP button/field.")
    print("  2. Press [SPACE] or [F8] on your keyboard to capture coordinates.")
    print("  3. An audio chime will confirm capture.")
    print("  4. Press [ESC] at any time to cancel calibration.")
    print("=" * 60 + "\n")

    new_coords = {}

    for key, label in TARGETS:
        while True:
            current_val = cfg.get("coordinates", {}).get(key, {"x": None, "y": None})
            print(f"\nTarget: {label}")
            if current_val.get("x") is not None and current_val.get("y") is not None:
                print(f"  Current value: (x={current_val['x']}, y={current_val['y']})")
            print("  >> Hover mouse over target and press [SPACE] or [F8] (or [ESC] to abort)...")

            x, y = wait_for_capture()

            if x is None and y is None:
                print("\n[!] Calibration aborted by user.")
                return False

            print(f"  Captured: X = {x}, Y = {y}")
            choice = input("  Accept this coordinate? [Y/n/r (retry)]: ").strip().lower()

            if choice in ["", "y", "yes"]:
                new_coords[key] = {"x": int(x), "y": int(y)}
                break
            elif choice in ["r", "retry"]:
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
    replay_cursor_verification(new_coords)

    # Prompt user if they want to re-run replay or exit
    while True:
        ans = input("Would you like to repeat the visual cursor replay? [y/N]: ").strip().lower()
        if ans in ["y", "yes"]:
            replay_cursor_verification(new_coords)
        else:
            break

    # Tier 2 Verification: Optional Single Complaint Test Run (PRD Section 3.1)
    print("\n" + "=" * 55)
    print(" [TIER 2 VERIFICATION] Optional Single-Complaint Test")
    print("=" * 55)
    tier2_ans = input("Would you like to dispatch an optional single-complaint test run? [y/N]: ").strip().lower()
    if tier2_ans in ["y", "yes"]:
        test_no = input("  Enter Complaint Number for test run [default: 282636629]: ").strip()
        if not test_no:
            test_no = "282636629"
        print(f"\n🚀 Running Tier 2 verification test with complaint #{test_no}...")
        import engine
        engine.process_complaint(test_no, "Calibration_Test", config=cfg, record_progress=False)

    print("\nCalibration Wizard finished successfully!")
    return True

if __name__ == "__main__":
    run_calibration_wizard()
