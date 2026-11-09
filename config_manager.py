import os
import sys
import json
import ctypes
import pyautogui

DEFAULT_CONFIG = {
    "screen_metadata": {
        "calibrated_resolution": [1366, 768],
        "calibration_timestamp": "2026-11-08 18:30:00"
    },
    "coordinates": {
        "complaint_input": {"x": 118, "y": 160},
        "main_print_btn": {"x": 1196, "y": 363},
        "ha_field_checkbox": {"x": 764, "y": 419},
        "popup_print_btn": {"x": 671, "y": 493},
        "popup_exit_btn": {"x": 749, "y": 493}
    },
    "timings": {
        "focus_delay": 0.5,
        "record_load_seconds": 3.0,
        "popup_wait_seconds": 2.0,
        "checkbox_wait_seconds": 1.0,
        "browser_pdf_load_seconds": 8.0,
        "foxit_print_dialog_seconds": 2.5,
        "save_dialog_seconds": 3.0,
        "file_save_processing_seconds": 4.0,
        "window_switch_seconds": 2.0,
        "popup_close_seconds": 1.0
    },
    "settings": {
        "excel_path": "complaints.xlsx",
        "complaint_column": "COMPLAINT_NO",
        "technician_column": "TECHNICIAN_NAME",
        "output_dir": "Today_Complaints",
        "progress_file": "completed_complaints.txt"
    }
}

def set_dpi_awareness():
    """Enforce Per-Monitor DPI awareness on Windows to prevent coordinate distortion."""
    if sys.platform == "win32":
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

def get_base_dir():
    """
    Returns base directory where the script or frozen .exe is located.
    Ensures portable resolution of files when packaged with PyInstaller.
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def get_config_path(filename="config.json"):
    """Returns absolute path to the configuration file."""
    return os.path.join(get_base_dir(), filename)

def resolve_path(relative_or_absolute_path):
    """Resolves relative file/folder path against the application base directory."""
    if os.path.isabs(relative_or_absolute_path):
        return relative_or_absolute_path
    return os.path.join(get_base_dir(), relative_or_absolute_path)

def load_config(config_path=None):
    """
    Loads and validates config.json.
    Falls back to default schema for any missing sections.
    Returns None if file does not exist.
    """
    if config_path is None:
        config_path = get_config_path()

    if not os.path.exists(config_path):
        return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[Warning] Failed to read config file ({e}).")
        return None

    # Merge missing sections or keys with defaults
    for section, defaults in DEFAULT_CONFIG.items():
        if section not in cfg or not isinstance(cfg[section], dict):
            cfg[section] = defaults.copy()
        else:
            for k, v in defaults.items():
                if k not in cfg[section]:
                    cfg[section][k] = v

    return cfg

def save_config(config_data, config_path=None):
    """Saves configuration data dictionary to config.json."""
    if config_path is None:
        config_path = get_config_path()

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)
    return True

def check_resolution_drift(config):
    """
    Checks if current screen resolution differs from calibrated resolution in config.
    Returns (has_drift, current_res, calibrated_res).
    """
    set_dpi_awareness()
    current_size = pyautogui.size()
    current_res = [current_size.width, current_size.height]

    meta = config.get("screen_metadata", {})
    calibrated_res = meta.get("calibrated_resolution", [current_size.width, current_size.height])

    has_drift = (current_res[0] != calibrated_res[0] or current_res[1] != calibrated_res[1])
    return has_drift, current_res, calibrated_res

if __name__ == "__main__":
    set_dpi_awareness()
    print("Testing config_manager...")
    base_dir = get_base_dir()
    print(f"Base Directory: {base_dir}")
    cfg = load_config()
    if cfg:
        print("Config successfully loaded.")
        drift, curr, calib = check_resolution_drift(cfg)
        print(f"Resolution check: Current={curr}, Calibrated={calib}, Drift Detected={drift}")
    else:
        print("Config file not found or failed to load.")
