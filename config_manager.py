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
        "popup_exit_btn": {"x": 749, "y": 493},
        "otp_add_btn": {"x": 200, "y": 200},
        "otp_complaint_search_btn": {"x": 350, "y": 250},
        "otp_search_input": {"x": 500, "y": 300},
        "otp_reason_search_btn": {"x": 350, "y": 350}
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
        "popup_close_seconds": 1.0,
        "otp_window_open_seconds": 1.5,
        "otp_search_modal_seconds": 1.2,
        "otp_reason_focus_seconds": 1.0,
        "otp_enter_delay_seconds": 0.3
    },
    "settings": {
        "excel_path": "complaints.xlsx",
        "complaint_column": "COMPLAINT_NO",
        "technician_column": "TECHNICIAN_NAME",
        "output_dir": "Today_Complaints",
        "progress_file": "completed_complaints.txt"
    },
    "printing": {
        "printer_name": None,
        "max_spool_queue": 3,
        "enable_live_print": False,
        "printed_tracker_file": "printed_complaints.txt"
    },
    "logging": {
        "log_dir": "logs",
        "log_file": "app.log",
        "max_bytes": 5242880,
        "backup_count": 5,
        "file_level": "DEBUG",
        "console_level": "INFO"
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
    Returns None if the configuration file does not exist (triggering first-time setup).
    Raises json.JSONDecodeError if the file exists but has corrupted/invalid JSON syntax,
    preventing silent overwrites of user settings.
    """
    if config_path is None:
        config_path = get_config_path()

    if not os.path.exists(config_path):
        return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print("\n" + "!" * 65)
        print(" [CRITICAL ERROR] 'config.json' contains invalid JSON syntax!")
        print(f" File path: {config_path}")
        print(f" Error details: {e}")
        print(" To prevent data loss, the bot will NOT overwrite your existing configuration.")
        print(" Please fix the syntax in config.json or delete it to regenerate defaults.")
        print("!" * 65 + "\n")
        raise
    except Exception as e:
        print(f"[!] Unexpected error reading configuration file ({config_path}): {e}")
        raise

    # Deep merge missing sections or keys with defaults
    for section, defaults in DEFAULT_CONFIG.items():
        if section not in cfg or not isinstance(cfg[section], dict):
            cfg[section] = defaults.copy()
        else:
            for k, v in defaults.items():
                if k not in cfg[section]:
                    cfg[section][k] = v
                elif isinstance(v, dict) and isinstance(cfg[section][k], dict):
                    for sub_k, sub_v in v.items():
                        if sub_k not in cfg[section][k] or cfg[section][k][sub_k] is None:
                            cfg[section][k][sub_k] = sub_v

    # Deep schema validation: Ensure all UI coordinate targets exist with integer x and y
    validate_coordinates(cfg)

    return cfg

def validate_coordinates(config):
    """
    Validates that all required UI targets exist in config['coordinates']
    and each contains valid integer 'x' and 'y' values.
    Repairs missing/invalid coordinates from DEFAULT_CONFIG and returns (is_valid: bool, config: dict).
    """
    if "coordinates" not in config or not isinstance(config["coordinates"], dict):
        config["coordinates"] = {k: v.copy() for k, v in DEFAULT_CONFIG["coordinates"].items()}
        return False, config

    coords = config["coordinates"]
    default_coords = DEFAULT_CONFIG["coordinates"]
    repaired = False

    for target, default_pt in default_coords.items():
        if target not in coords or not isinstance(coords[target], dict):
            coords[target] = default_pt.copy()
            repaired = True
        else:
            pt = coords[target]
            for axis in ("x", "y"):
                val = pt.get(axis)
                if val is None or not isinstance(val, (int, float)):
                    pt[axis] = default_pt[axis]
                    repaired = True
                else:
                    pt[axis] = int(val)

    return not repaired, config

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

def get_printing_settings(config=None):
    """
    Returns the printing configuration dictionary, ensuring default fallbacks.
    """
    if config is None:
        config = load_config() or DEFAULT_CONFIG

    printing_cfg = config.get("printing", {})
    default_printing = DEFAULT_CONFIG["printing"]

    merged = {}
    for k, v in default_printing.items():
        merged[k] = printing_cfg.get(k, v)

    # Type safety checks
    try:
        merged["max_spool_queue"] = max(1, int(merged.get("max_spool_queue", 3)))
    except (ValueError, TypeError):
        merged["max_spool_queue"] = 3

    merged["enable_live_print"] = bool(merged.get("enable_live_print", False))
    return merged

def get_logging_settings(config=None):
    """
    Returns the logging configuration dictionary, ensuring default fallbacks and type safety.
    """
    if config is None:
        config = load_config() or DEFAULT_CONFIG

    logging_cfg = config.get("logging", {})
    default_logging = DEFAULT_CONFIG["logging"]

    merged = {}
    for k, v in default_logging.items():
        merged[k] = logging_cfg.get(k, v)

    try:
        merged["max_bytes"] = max(1024, int(merged.get("max_bytes", 5242880)))
    except (ValueError, TypeError):
        merged["max_bytes"] = 5242880

    try:
        merged["backup_count"] = max(1, int(merged.get("backup_count", 5)))
    except (ValueError, TypeError):
        merged["backup_count"] = 5

    merged["log_dir"] = str(merged.get("log_dir") or "logs").strip()
    merged["log_file"] = str(merged.get("log_file") or "app.log").strip()
    merged["file_level"] = str(merged.get("file_level") or "DEBUG").strip().upper()
    merged["console_level"] = str(merged.get("console_level") or "INFO").strip().upper()
    return merged

def get_configured_printer(config=None):
    """
    Resolves the target printer name.
    1. Returns config["printing"]["printer_name"] if specified and installed.
    2. If unconfigured or disconnected, prioritizes physical/hardware printers first.
    3. Falls back to the Windows system default printer if no physical printer is found.
    Returns:
        (printer_name: str or None, is_fallback: bool)
    """
    import printer_manager as pm

    settings = get_printing_settings(config)
    configured = settings.get("printer_name")
    installed = pm.get_installed_printers()

    if configured and configured in installed:
        return configured, False

    # Prefer physical hardware printers over virtual software printers
    physical_printers = [p for p in installed if pm.classify_printer(p) == "PHYSICAL"]
    if physical_printers:
        return physical_printers[0], True

    # Fallback to system default
    default_p = pm.get_default_printer()
    if default_p:
        return default_p, True

    if installed:
        return installed[0], True

    return None, False

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
