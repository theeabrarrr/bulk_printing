import os
import sys
import time
import re
import ctypes

# Ensure Unicode output doesn't crash on standard Windows consoles (cp1252)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import pyautogui
import keyboard
import pyperclip
from PIL import Image, ImageChops, ImageGrab

try:
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32process = None

import config_manager
import logger_manager
import state_manager

logger = logger_manager.get_logger("engine")

# Allowed foreground process names for Oracle Forms running in Edge IE Mode / Java Applet
ERP_ALLOWED_PROCESS_NAMES = {
    "jp2launcher.exe",
    "java.exe",
    "javaw.exe",
    "msedge.exe",
    "iexplore.exe",
}

# Process names corresponding to our CLI console / terminal host
CONSOLE_PROCESS_NAMES = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "conhost.exe",
    "windowsterminal.exe",
    "openconsole.exe",
    "erp_automation_suite.exe",
    "python.exe",
    "pythonw.exe",
}

# Ensure failsafe is active (slam mouse into any corner to emergency abort)
pyautogui.FAILSAFE = True

# Global latch for ESC key press so brief taps during sleeps are never missed
_ABORT_REQUESTED = False
_ESC_HOOK_REGISTERED = False


def _on_esc_pressed(event=None):
    global _ABORT_REQUESTED
    _ABORT_REQUESTED = True


def _ensure_esc_listener():
    global _ESC_HOOK_REGISTERED
    if not _ESC_HOOK_REGISTERED:
        try:
            keyboard.on_press_key('esc', _on_esc_pressed, suppress=False)
            _ESC_HOOK_REGISTERED = True
        except Exception as e:
            logger.debug("Could not register global ESC listener hook: %s", e)


def reset_abort():
    """Clears the latched ESC abort state before starting a new run."""
    global _ABORT_REQUESTED
    _ensure_esc_listener()
    _ABORT_REQUESTED = False


def check_abort():
    """Checks if Esc is currently pressed or was latched for immediate abort."""
    global _ABORT_REQUESTED
    _ensure_esc_listener()
    try:
        if keyboard.is_pressed('esc'):
            logger.debug("ESC key detected via keyboard.is_pressed('esc').")
            return True
    except Exception:
        pass
    if _ABORT_REQUESTED:
        _ABORT_REQUESTED = False
        logger.debug("Latched ESC abort signal consumed.")
        return True
    return False


def sleep_interruptible(seconds, poll_interval=0.05):
    """
    Sleeps for `seconds` in short `poll_interval` chunks using time.monotonic(),
    checking `check_abort()` on every tick so [ESC] aborts within ~50ms.
    Returns True if aborted by [ESC], False if the full duration elapsed normally.
    """
    try:
        total = max(0.0, float(seconds))
    except (ValueError, TypeError):
        total = 0.0

    if check_abort():
        return True
    if total <= 0.0:
        return False

    step = max(0.01, float(poll_interval))
    deadline = time.monotonic() + total
    while True:
        if check_abort():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        time.sleep(min(step, remaining))
    return check_abort()


def _get_process_name_by_pid(pid):
    """
    Resolves the lowercase executable filename (e.g., 'jp2launcher.exe', 'msedge.exe')
    for a Windows PID using Win32 QueryFullProcessImageNameW. Returns '' on failure.
    """
    if not pid or sys.platform != "win32":
        return ""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h_proc:
            return ""
        try:
            buf_len = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(buf_len)):
                return os.path.basename(buf.value).strip().lower()
        finally:
            kernel32.CloseHandle(h_proc)
    except Exception:
        pass
    return ""


def capture_foreground_window_context():
    """
    Captures metadata about the currently active Windows foreground window:
    {'hwnd', 'pid', 'process_name', 'title', 'is_console'}.
    Returns None if win32gui is unavailable or no foreground window exists.
    """
    if win32gui is None or win32process is None:
        return None
    try:
        hwnd = int(win32gui.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        pid = int(pid or 0)
        title = str(win32gui.GetWindowText(hwnd) or "").strip()
        proc_name = _get_process_name_by_pid(pid)

        console_hwnd = 0
        if sys.platform == "win32":
            try:
                console_hwnd = int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
            except Exception:
                console_hwnd = 0

        title_low = title.lower()
        is_console = (
            pid == os.getpid()
            or (console_hwnd != 0 and hwnd == console_hwnd)
            or proc_name in CONSOLE_PROCESS_NAMES
            or "oracle erp automation suite" in title_low
            or title_low.startswith(
                (
                    "command prompt",
                    "windows powershell",
                    "administrator: command prompt",
                    "administrator: windows powershell",
                )
            )
        )
        return {
            "hwnd": hwnd,
            "pid": pid,
            "process_name": proc_name,
            "title": title,
            "is_console": bool(is_console),
        }
    except Exception as e:
        logger.debug("Could not capture foreground window context: %s", e)
        return None


def verify_erp_foreground(expected_ctx):
    """
    Verifies that the active foreground window still belongs to Oracle Forms / Edge IE Mode / Java.
    Allows windows whose PID matches `expected_ctx['pid']`, OR whose process name is in
    ERP_ALLOWED_PROCESS_NAMES ({'jp2launcher.exe', 'java.exe', 'javaw.exe', 'msedge.exe', 'iexplore.exe'}),
    OR whose window title contains 'Oracle' or 'Forms', while immediately halting if focus shifts
    to the CLI console, Explorer, or any unrelated application.
    Returns (is_ok: bool, error_message: str).
    """
    if expected_ctx is None or win32gui is None or win32process is None:
        return True, ""

    curr = capture_foreground_window_context()
    if curr is None:
        return False, "No active foreground window detected (ERP window lost focus)."

    curr_pid = curr.get("pid", 0)
    curr_proc = (curr.get("process_name") or "").lower()
    curr_title = curr.get("title") or ""
    curr_title_low = curr_title.lower()

    if curr.get("is_console") or curr_pid == os.getpid() or curr_proc in CONSOLE_PROCESS_NAMES:
        label = curr_title or curr_proc or f"PID {curr_pid}"
        return False, f"ERP window lost focus to CLI console ('{label}')."

    expected_pid = expected_ctx.get("pid")
    if (
        (expected_pid and curr_pid == expected_pid)
        or (curr_proc in ERP_ALLOWED_PROCESS_NAMES)
        or ("oracle" in curr_title_low or "forms" in curr_title_low)
    ):
        return True, ""

    label = curr_title or curr_proc or f"PID {curr_pid}"
    proc_detail = f" [{curr_proc}]" if curr_proc else ""
    return False, f"ERP window lost focus (active window: '{label}'{proc_detail})."


def _capture_region_patch(center_x, center_y, half_w=120, half_h=80):
    """
    Captures a grayscale ('L') screenshot patch around (center_x, center_y), clamping
    the bounding box within [0, screen_width] and [0, screen_height] so patches near
    screen edges never go out of bounds on 1366x768 displays.
    Returns Optional[PIL.Image.Image].
    """
    try:
        size = pyautogui.size()
        screen_w = max(1, int(getattr(size, "width", size[0])))
        screen_h = max(1, int(getattr(size, "height", size[1])))
    except Exception:
        screen_w, screen_h = 1366, 768

    cx = int(center_x)
    cy = int(center_y)
    hw = max(8, int(half_w))
    hh = max(8, int(half_h))

    left = max(0, min(screen_w - 1, cx - hw))
    top = max(0, min(screen_h - 1, cy - hh))
    right = max(left + 1, min(screen_w, cx + hw))
    bottom = max(top + 1, min(screen_h, cy + hh))
    bbox = (left, top, right, bottom)

    try:
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        except TypeError:
            img = ImageGrab.grab(bbox=bbox)
        return img.convert("L")
    except Exception as e:
        logger.debug("Screen patch capture failed at bbox=%s: %s", bbox, e)
        return None


def _compute_patch_diff_ratio(patch_a, patch_b, pixel_delta_threshold=18):
    """
    Computes the fraction of pixels (0.0 to 1.0) between two grayscale patches whose
    intensity differs by at least `pixel_delta_threshold` (out of 255).
    Ignores minor blinking caret changes (<0.1%) while reliably detecting modal transitions (>3%).
    """
    if patch_a is None or patch_b is None:
        return 0.0
    try:
        a = patch_a.convert("L") if patch_a.mode != "L" else patch_a
        b = patch_b.convert("L") if patch_b.mode != "L" else patch_b
        if a.size != b.size:
            b = b.resize(a.size)
        diff = ImageChops.difference(a, b)
        hist = diff.histogram()
        total_px = max(1, a.width * a.height)
        thresh_idx = max(1, min(255, int(pixel_delta_threshold)))
        changed_px = sum(hist[thresh_idx:])
        return float(changed_px) / float(total_px)
    except Exception as e:
        logger.debug("Error computing patch diff ratio: %s", e)
        return 0.0


def _wait_for_region_change(
    center_pt,
    baseline_patch,
    timeout_seconds,
    threshold=0.03,
    min_settle=0.25,
    expected_fg=None,
    poll_interval=0.1,
    half_w=120,
    half_h=80,
):
    """
    Polls until the screen region centered at `center_pt` changes by at least `threshold`
    relative to `baseline_patch`. Checks `check_abort()` and `verify_erp_foreground()` on each tick.
    Returns (status: str, diff_ratio: float, error_msg: str) where status is one of:
    'CHANGED', 'ABORTED', 'FOCUS_LOST', 'TIMEOUT'.
    """
    if baseline_patch is None:
        if sleep_interruptible(timeout_seconds):
            return "ABORTED", 0.0, "OTP generation aborted by user (Esc pressed)."
        fg_ok, fg_msg = verify_erp_foreground(expected_fg)
        if not fg_ok:
            return "FOCUS_LOST", 0.0, fg_msg
        return "CHANGED", 1.0, ""

    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    last_ratio = 0.0

    while True:
        if check_abort():
            return "ABORTED", last_ratio, "OTP generation aborted by user (Esc pressed)."
        fg_ok, fg_msg = verify_erp_foreground(expected_fg)
        if not fg_ok:
            return "FOCUS_LOST", last_ratio, fg_msg

        live_patch = _capture_region_patch(center_pt["x"], center_pt["y"], half_w=half_w, half_h=half_h)
        if live_patch is not None:
            last_ratio = _compute_patch_diff_ratio(baseline_patch, live_patch)
            if last_ratio >= threshold:
                if min_settle > 0.0 and sleep_interruptible(min_settle):
                    return "ABORTED", last_ratio, "OTP generation aborted by user (Esc pressed)."
                fg_ok, fg_msg = verify_erp_foreground(expected_fg)
                if not fg_ok:
                    return "FOCUS_LOST", last_ratio, fg_msg
                return "CHANGED", last_ratio, ""

        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        if sleep_interruptible(min(float(poll_interval), remaining)):
            return "ABORTED", last_ratio, "OTP generation aborted by user (Esc pressed)."

    return "TIMEOUT", last_ratio, f"Screen region at ({center_pt['x']}, {center_pt['y']}) did not change (diff={last_ratio:.3f} < {threshold:.3f})."


def _wait_for_region_restore(
    center_pt,
    target_patch,
    timeout_seconds,
    max_diff_threshold=0.08,
    min_settle=0.2,
    expected_fg=None,
    poll_interval=0.1,
    half_w=120,
    half_h=80,
):
    """
    Polls until the screen region centered at `center_pt` returns to matching `target_patch`
    (diff_ratio <= max_diff_threshold). Checks `check_abort()` and `verify_erp_foreground()` on each tick.
    Returns (status: str, diff_ratio: float, error_msg: str) where status is one of:
    'RESTORED', 'ABORTED', 'FOCUS_LOST', 'TIMEOUT'.
    """
    if target_patch is None:
        if sleep_interruptible(timeout_seconds):
            return "ABORTED", 0.0, "OTP generation aborted by user (Esc pressed)."
        fg_ok, fg_msg = verify_erp_foreground(expected_fg)
        if not fg_ok:
            return "FOCUS_LOST", 0.0, fg_msg
        return "RESTORED", 0.0, ""

    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    last_ratio = 1.0

    while True:
        if check_abort():
            return "ABORTED", last_ratio, "OTP generation aborted by user (Esc pressed)."
        fg_ok, fg_msg = verify_erp_foreground(expected_fg)
        if not fg_ok:
            return "FOCUS_LOST", last_ratio, fg_msg

        live_patch = _capture_region_patch(center_pt["x"], center_pt["y"], half_w=half_w, half_h=half_h)
        if live_patch is not None:
            last_ratio = _compute_patch_diff_ratio(target_patch, live_patch)
            if last_ratio <= max_diff_threshold:
                if min_settle > 0.0 and sleep_interruptible(min_settle):
                    return "ABORTED", last_ratio, "OTP generation aborted by user (Esc pressed)."
                fg_ok, fg_msg = verify_erp_foreground(expected_fg)
                if not fg_ok:
                    return "FOCUS_LOST", last_ratio, fg_msg
                return "RESTORED", last_ratio, ""

        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        if sleep_interruptible(min(float(poll_interval), remaining)):
            return "ABORTED", last_ratio, "OTP generation aborted by user (Esc pressed)."

    return "TIMEOUT", last_ratio, f"Screen region at ({center_pt['x']}, {center_pt['y']}) did not restore to baseline (diff={last_ratio:.3f} > {max_diff_threshold:.3f})."


def restore_erp_focus(timings=None):
    """Restores foreground focus to the ERP window using the existing Alt+Tab mechanism."""
    if timings is None:
        timings = config_manager.DEFAULT_CONFIG["timings"]
    logger.info("[*] Switching back to ERP window using Alt+Tab...")
    pyautogui.hotkey('alt', 'tab')
    time.sleep(timings.get("window_switch_seconds", 2.0))


def sanitize_technician_name(raw_name):
    """Sanitizes technician name for Windows folder path compatibility."""
    if not raw_name or not str(raw_name).strip() or str(raw_name).strip().lower() == "nan":
        return "Unknown"
    cleaned = re.sub(r'[\\/*?:"<>|]', "", str(raw_name).strip())
    return cleaned if cleaned else "Unknown"


def get_completed_complaints(progress_file_path=None):
    """Reads completed complaints from the SQLite state database and returns a set of IDs."""
    try:
        return state_manager.get_completed_complaints(progress_file_path)
    except Exception as e:
        logger.warning("[Warning] Failed to read completed complaints from '%s': %s", progress_file_path, e)
        return set()


def record_completed_complaint(progress_file_path, complaint_no, technician="Unknown", file_path=""):
    """Records a completed complaint download in the SQLite state database."""
    try:
        state_manager.mark_complaint_downloaded(
            progress_file_path,
            complaint_no,
            technician=technician,
            file_path=file_path,
        )
    except Exception as e:
        logger.warning("[Warning] Failed to write complaint #%s to state database '%s': %s", complaint_no, progress_file_path, e)


def _dispatch_live_print(num, file_path, config, timings, target_printer=None):
    """
    Executes queue-gated silent physical printing and job reconciliation for a saved PDF.
    Ensures the ERP window is restored to the foreground after any interactive queue gate prompt.
    Returns (printed_ok: bool, message: str).
    """
    import printer_manager as pm

    printing_cfg = config_manager.get_printing_settings(config)
    if target_printer:
        is_fallback = False
    else:
        target_printer, is_fallback = config_manager.get_configured_printer(config)
    db_path = state_manager.ensure_tracker_db_ready(config, tracker_kind="printed")
    max_queue = printing_cfg.get("max_spool_queue", 3)
    spooler_query_timeout = printing_cfg.get("spooler_query_timeout", 5.0)

    if not target_printer:
        logger.warning("[!] Warning: No Windows printer available for live printing.")
        return False, f"No Windows printer available for live printing of #{num}."

    fallback_note = " (System Default)" if is_fallback else ""
    logger.info(f"[*] Live Print: Checking spooler queue on '{target_printer}'{fallback_note}...")

    try:
        try:
            gate_ok = pm.enforce_queue_gate(
                target_printer,
                max_jobs=max_queue,
                spooler_query_timeout=spooler_query_timeout,
            )
        except TypeError:
            gate_ok = pm.enforce_queue_gate(target_printer, max_jobs=max_queue)
    except KeyboardInterrupt:
        logger.warning("Live print aborted by user during print queue gate for complaint #%s.", num)
        return False, "Process aborted by user during print queue gate."

    gate_was_interactive = bool(getattr(pm, "LAST_GATE_PROMPTED", False)) or not gate_ok

    if not gate_ok:
        if gate_was_interactive:
            restore_erp_focus(timings)
        logger.info(f"[i] Live printing skipped for complaint #{num} by operator.")
        return False, f"Live printing skipped for complaint #{num} by operator."

    try:
        doc_name = f"Complaint_{num}.pdf"
        logger.info(f"[*] Sending '{num}.pdf' to '{target_printer}'...")
        print_dpi = printing_cfg.get("print_dpi", 200)
        color_mode = printing_cfg.get("color_mode", "monochrome")
        mono_threshold = printing_cfg.get("monochrome_threshold", 180)
        try:
            job_id = pm.print_pdf_silent(
                file_path,
                target_printer,
                dpi=print_dpi,
                doc_name=doc_name,
                color_mode=color_mode,
                threshold=mono_threshold,
            )
        except TypeError:
            job_id = pm.print_pdf_silent(file_path, target_printer, doc_name=doc_name)
        if not job_id:
            raise RuntimeError("Silent print did not return a valid spool job.")
        pm.mark_complaint_spooled(
            db_path, num, file_path, target_printer, job_id=job_id
        )
        try:
            confirmed, status_msg = pm.reconcile_print_job(
                target_printer,
                job_id=job_id,
                doc_name=doc_name,
                spooler_query_timeout=spooler_query_timeout,
            )
        except TypeError:
            confirmed, status_msg = pm.reconcile_print_job(
                target_printer, job_id=job_id, doc_name=doc_name
            )
        if not confirmed:
            raise RuntimeError(f"Print job not confirmed ({status_msg})")
        if not pm.mark_complaint_printed(
            db_path, num, file_path, target_printer, job_id=job_id
        ):
            raise RuntimeError(f"Failed to record confirmed print in '{db_path}'")
        logger.info(f"[✓] Complaint #{num} confirmed printed on '{target_printer}'.")
        return True, f"{num} completed and printed successfully."
    except Exception as print_err:
        try:
            state_manager.mark_complaint_print_failed(
                db_path,
                num,
                file_path=file_path,
                printer_name=target_printer,
                reason=str(print_err),
            )
        except Exception as state_err:
            logger.debug("Could not mark PRINT_FAILED for #%s: %s", num, state_err)
        logger.error(f"[!] Warning: Silent physical printing failed for #{num}: {print_err}", exc_info=True)
        return False, f"PDF saved, but physical printing failed for #{num}: {print_err}"
    finally:
        if gate_was_interactive:
            restore_erp_focus(timings)


def process_complaint(
    complaint_no,
    tech_name,
    config=None,
    record_progress=True,
    organize_by_tech=True,
    live_print=False,
    direct_print=False,
    target_printer=None,
    in_flight=None,
):
    """
    Executes the battle-tested RPA printing sequence for a single complaint.
    When `direct_print=True`, skips saving PDF to disk and sends Ctrl+P directly to
    `target_printer` (with spooler queue gating and win32print spool verification).
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

    output_base = config_manager.resolve_path(settings.get("output_dir", "Today_Complaints"))
    dest_folder = os.path.join(output_base, tech) if organize_by_tech else os.path.join(output_base, "All_PDFs")
    if not direct_print:
        # Prepare destination folder with explicit error handling
        try:
            os.makedirs(dest_folder, exist_ok=True)
        except OSError as e:
            logger.error(f"\n[!] Failed to create destination folder '{dest_folder}': {e}", exc_info=True)
            return False, f"Destination folder creation failed: {e}"

    db_path = state_manager.ensure_tracker_db_ready(
        config, tracker_kind="printed" if direct_print else "completed", output_dir=output_base
    )

    # Early abort check
    if check_abort():
        logger.info("Process aborted by user (Esc pressed) before starting complaint #%s.", num)
        return False, "Process aborted by user (Esc pressed)."

    resolved_printer = None
    max_queue = 3
    spooler_query_timeout = 5.0
    pm = None

    if direct_print:
        import printer_manager as pm

        printing_cfg = config_manager.get_printing_settings(config)
        max_queue = printing_cfg.get("max_spool_queue", 3)
        spooler_query_timeout = printing_cfg.get("spooler_query_timeout", 5.0)
        resolved_printer = str(target_printer or "").strip()
        if not resolved_printer:
            resolved_printer, _ = config_manager.get_configured_printer(config)

        if not resolved_printer:
            logger.warning("[!] Warning: No Windows printer available for direct browser printing.")
            return False, f"No Windows printer available for direct browser printing of #{num}."

        if pm.classify_printer(resolved_printer) == "VIRTUAL":
            logger.warning(
                f"[!] Warning: '{resolved_printer}' is a VIRTUAL printer and cannot be used for direct physical printing."
            )
            return False, f"Target printer '{resolved_printer}' is VIRTUAL and cannot be used for direct printing."

        def _gate_on_poll(spool_status):
            if in_flight:
                try:
                    try:
                        pm.reconcile_in_flight_jobs(
                            resolved_printer,
                            in_flight,
                            db_path,
                            status=spool_status,
                            spooler_query_timeout=spooler_query_timeout,
                        )
                    except TypeError:
                        pm.reconcile_in_flight_jobs(resolved_printer, in_flight, db_path, status=spool_status)
                except Exception as rec_err:
                    logger.debug("Queue gate poll reconcile skipped for #%s: %s", num, rec_err)

        try:
            try:
                gate_ok = pm.enforce_queue_gate(
                    resolved_printer,
                    max_jobs=max_queue,
                    on_poll=_gate_on_poll if in_flight is not None else None,
                    check_abort_fn=check_abort,
                    spooler_query_timeout=spooler_query_timeout,
                )
            except TypeError:
                gate_ok = pm.enforce_queue_gate(resolved_printer, max_jobs=max_queue)
        except KeyboardInterrupt:
            if getattr(pm, "LAST_GATE_PROMPTED", False):
                restore_erp_focus(timings)
            logger.warning("Direct print aborted by user during print queue gate for complaint #%s.", num)
            return False, "Process aborted by user during print queue gate."

        gate_was_interactive = bool(getattr(pm, "LAST_GATE_PROMPTED", False)) or not gate_ok
        if gate_was_interactive:
            restore_erp_focus(timings)
        if not gate_ok:
            logger.info(f"[i] Direct browser printing skipped for complaint #{num} by operator.")
            return False, f"Direct browser printing skipped for complaint #{num} by operator."

    # Runtime Defense-in-Depth: when progress tracking is enabled (Bulk Mode),
    # if a valid non-empty PDF already exists on disk, backfill SQLite and skip ERP GUI download.
    if record_progress and not direct_print:
        existing_pdf = _find_saved_pdf_for_complaint(num, tech, output_base, organize_by_tech)
        if existing_pdf:
            record_completed_complaint(db_path, num, technician=tech, file_path=existing_pdf)
            logger.info(f"[>>] Existing PDF found on disk for #{num} ({tech}); synced to database and skipping ERP download.")
            if live_print:
                print_ok, print_msg = _dispatch_live_print(num, existing_pdf, config, timings)
                if not print_ok:
                    return False, print_msg
                return True, f"{num} synced from disk and printed successfully."
            return True, f"{num} already exists on disk (synced to database)."

    try:
        # Step A: Focus & Enter Complaint Number
        input_pt = coords.get("complaint_input", {"x": 118, "y": 160})
        logger.debug("Step A: Entering complaint #%s at (%d, %d).", num, input_pt["x"], input_pt["y"])
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
        logger.debug("Step B: Clicking main_print_btn at (%d, %d).", main_print["x"], main_print["y"])
        pyautogui.click(x=main_print["x"], y=main_print["y"])
        time.sleep(timings.get("popup_wait_seconds", 2.0))

        if check_abort():
            return False, "Process aborted by user (Esc pressed)."

        # Step C: HA FIELD Checkbox
        ha_checkbox = coords.get("ha_field_checkbox", {"x": 764, "y": 419})
        logger.debug("Step C: Clicking ha_field_checkbox at (%d, %d).", ha_checkbox["x"], ha_checkbox["y"])
        pyautogui.click(x=ha_checkbox["x"], y=ha_checkbox["y"])
        time.sleep(timings.get("checkbox_wait_seconds", 1.0))

        if check_abort():
            return False, "Process aborted by user (Esc pressed)."

        # Step D: Popup Print Click
        popup_print = coords.get("popup_print_btn", {"x": 671, "y": 493})
        logger.debug("Step D: Clicking popup_print_btn at (%d, %d).", popup_print["x"], popup_print["y"])
        pyautogui.click(x=popup_print["x"], y=popup_print["y"])

        # Step E: Wait for PDF to load in browser
        logger.info(f"⏳ Waiting for {num} PDF to load in browser...")
        pdf_load_wait = timings.get("browser_pdf_load_seconds", 8.0)
        # Sleep in intervals to allow responsive Esc abort
        elapsed = 0.0
        while elapsed < pdf_load_wait:
            if check_abort():
                return False, "Process aborted by user (Esc pressed)."
            time.sleep(0.5)
            elapsed += 0.5

        if direct_print:
            # Step F (Direct Browser Print): snapshot active spool jobs, press Ctrl+P -> Enter,
            # wait for win32print spool verification without opening any Save As modal.
            prior_status = pm._call_get_queue_status(resolved_printer, timeout=spooler_query_timeout)
            prior_job_ids = {
                j.get("job_id")
                for j in (prior_status.get("jobs") or [])
                if isinstance(j.get("job_id"), int)
                and not isinstance(j.get("job_id"), bool)
                and j.get("job_id") > 0
            }

            logger.info(f"[*] Direct Print: Pressing Ctrl+P for #{num} -> '{resolved_printer}'...")
            pyautogui.hotkey('ctrl', 'p')
            time.sleep(timings.get("foxit_print_dialog_seconds", 2.5))

            if check_abort():
                return False, "Process aborted by user (Esc pressed)."

            # Clicks 'OK' / Print on the Foxit Print menu (defaults to physical printer)
            pyautogui.press('enter')

            spool_wait_timeout = timings.get("direct_print_spool_seconds", 3.0)
            try:
                spool_ok, detected_job_id, spool_msg = pm.wait_for_direct_print_spool(
                    resolved_printer,
                    prior_job_ids=prior_job_ids,
                    timeout=spool_wait_timeout,
                    min_wait=min(2.0, float(spool_wait_timeout)),
                    check_abort_fn=check_abort,
                    spooler_query_timeout=spooler_query_timeout,
                )
            except TypeError:
                spool_ok, detected_job_id, spool_msg = pm.wait_for_direct_print_spool(
                    resolved_printer,
                    prior_job_ids=prior_job_ids,
                    timeout=spool_wait_timeout,
                )

            if not spool_ok and "aborted" in spool_msg.lower():
                return False, spool_msg

            if check_abort():
                return False, "Process aborted by user (Esc pressed)."

            # Step G: Return focus to ERP and close Print Module popup
            restore_erp_focus(timings)

            logger.info("[*] Closing the Print Module...")
            popup_exit = coords.get("popup_exit_btn", {"x": 749, "y": 493})
            pyautogui.click(x=popup_exit["x"], y=popup_exit["y"])
            time.sleep(timings.get("popup_close_seconds", 1.0))

            if not spool_ok:
                if record_progress:
                    try:
                        state_manager.mark_complaint_print_failed(
                            db_path,
                            num,
                            file_path="",
                            printer_name=resolved_printer,
                            job_id=detected_job_id,
                            reason=spool_msg,
                            technician=tech,
                        )
                    except Exception as state_err:
                        logger.debug("Could not mark PRINT_FAILED for #%s: %s", num, state_err)
                logger.error(f"[!] Direct print spool verification failed for #{num}: {spool_msg}")
                return False, f"Direct browser printing failed for #{num}: {spool_msg}"

            if record_progress:
                still_active = False
                matched_doc_name = None
                if detected_job_id is not None and in_flight is not None:
                    cur_status = pm._call_get_queue_status(resolved_printer, timeout=spooler_query_timeout)
                    matched_job = pm._find_matching_job_in_status(cur_status, job_id=detected_job_id)
                    if matched_job is not None:
                        j_state, j_reason = pm.inspect_job_in_status(cur_status, job_id=detected_job_id)
                        if j_state == "FAILED":
                            try:
                                state_manager.mark_complaint_print_failed(
                                    db_path,
                                    num,
                                    file_path="",
                                    printer_name=resolved_printer,
                                    job_id=detected_job_id,
                                    reason=j_reason,
                                    technician=tech,
                                )
                            except Exception as state_err:
                                logger.debug("Could not mark PRINT_FAILED for #%s: %s", num, state_err)
                            logger.error(f"[!] Direct print job #{detected_job_id} failed for #{num}: {j_reason}")
                            return False, f"Direct browser printing failed for #{num}: {j_reason}"
                        if j_state == "PENDING":
                            still_active = True
                            matched_doc_name = matched_job.get("document")

                if still_active:
                    pm.mark_complaint_spooled(
                        db_path,
                        num,
                        file_path="",
                        printer_name=resolved_printer,
                        job_id=detected_job_id,
                        technician=tech,
                    )
                    in_flight.append(
                        {
                            "complaint_no": num,
                            "file_path": "",
                            "job_id": detected_job_id,
                            "doc_name": matched_doc_name,
                            "technician": tech,
                        }
                    )
                    logger.info(
                        f"[*] Complaint #{num} spooled directly to '{resolved_printer}' "
                        f"(Job #{detected_job_id}; sliding window: {len(in_flight)}/{max_queue})."
                    )
                else:
                    pm.mark_complaint_printed(
                        db_path,
                        num,
                        file_path="",
                        printer_name=resolved_printer,
                        job_id=detected_job_id,
                        technician=tech,
                    )
                    logger.info(
                        f"[✓] Complaint #{num} direct-printed on '{resolved_printer}' (recorded in SQLite)."
                    )
            else:
                logger.info(f"[✓] Complaint #{num} sent directly to '{resolved_printer}'!")

            time.sleep(1.0)
            return True, f"{num} direct-printed successfully on '{resolved_printer}'."

        # Step F: Print & Save as PDF
        logger.info(f"[*] Pressing Ctrl+P for {tech}...")
        pyautogui.hotkey('ctrl', 'p')
        time.sleep(timings.get("foxit_print_dialog_seconds", 2.5))

        if check_abort():
            return False, "Process aborted by user (Esc pressed)."

        # Clicks 'OK' on the Foxit Print menu
        pyautogui.press('enter')
        time.sleep(timings.get("save_dialog_seconds", 3.0))

        if check_abort():
            return False, "Process aborted by user (Esc pressed)."

        # Unicode-safe path insertion via pyperclip clipboard injection
        file_path = os.path.abspath(os.path.join(dest_folder, f"{num}.pdf"))
        file_existed_prior = os.path.exists(file_path)

        pyperclip.copy(file_path)
        time.sleep(0.3)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(1.0)
        pyautogui.press('enter')  # Save trigger

        # Dynamic modal handling: If file already existed, Windows Save As displays
        # 'Confirm Save As: ... already exists. Do you want to replace it? [Yes] [No]'
        # Pause briefly and confirm overwrite if the file existed prior
        time.sleep(1.2)
        if file_existed_prior:
            logger.debug("Confirming overwrite for existing PDF '%s'.", file_path)
            pyautogui.press('y')
            time.sleep(0.3)
            pyautogui.press('enter')

        # Sleep in intervals for file saving to allow responsive Esc abort
        save_wait = timings.get("file_save_processing_seconds", 4.0)
        elapsed = 0.0
        while elapsed < save_wait:
            if check_abort():
                return False, "Process aborted by user (Esc pressed)."
            time.sleep(0.5)
            elapsed += 0.5

        # Step G: Cleanup & Record
        restore_erp_focus(timings)

        # Close the Print Module popup
        logger.info("[*] Closing the Print Module...")
        popup_exit = coords.get("popup_exit_btn", {"x": 749, "y": 493})
        pyautogui.click(x=popup_exit["x"], y=popup_exit["y"])
        time.sleep(timings.get("popup_close_seconds", 1.0))

        # Mandate physical verification that PDF exists and has non-zero size
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            logger.error(f"\n[!] Verification Failed: '{file_path}' was not created or is empty.")
            return False, f"PDF file not saved or 0 bytes: {file_path}"

        # Write PDF completion to SQLite state database if requested
        if record_progress:
            record_completed_complaint(db_path, num, technician=tech, file_path=file_path)

        logger.info(f"[+] {num} successfully saved in {tech}'s folder!")

        # Physical Silent Printing if live_print is enabled (tracked in SQLite state database)
        if live_print:
            print_ok, print_msg = _dispatch_live_print(num, file_path, config, timings)
            if not print_ok:
                time.sleep(1.0)
                return False, print_msg

        time.sleep(1.0)
        return True, f"{num} completed successfully."

    except KeyboardInterrupt:
        logger.warning("\n[!] Operation interrupted by user (Ctrl+C).")
        return False, "Process aborted by user (Ctrl+C)."
    except pyautogui.FailSafeException:
        logger.error("\n[!] Emergency FailSafe triggered (mouse in screen corner)!")
        return False, "Aborted by FailSafe"
    except Exception as e:
        logger.error(f"\n[!] Error during processing of complaint {num}: {e}", exc_info=True)
        return False, str(e)


def generate_otp(complaint_no, config=None, expected_fg=None):
    """
    Executes the automated OTP Generation workflow inside Oracle Forms with
    configurable post-paste/query delays, [ESC]-responsive chunked sleeps,
    Edge IE Mode / Java foreground window verification, and zero-calibration
    visual modal transition checks:
    1. Click 'Add' button to open the OTP sub-window (verifies sub-window opened).
    2. Click 3-dots next to Complaint No. to open Complaint LOV modal (verifies LOV opened).
    3. Double-click search input, then paste complaint number via pyperclip.
    4. Press Enter #1 to query LOV, wait `otp_query_wait_seconds`, press Enter #2 to
       select/close, and verify the Complaint LOV closed cleanly without an obstructive alert.
    5. Click 3-dots next to OTP Reason (focus click, wait, activate click) and verify
       the centered OTP Reason LOV modal opened (trapping any validation alert before Enter x4).
    6. Press Enter 4 times with configurable delays to confirm reason and verify the OTP
       sub-window closed back to the primary screen.

    Returns (success: bool, message: str).
    """
    if config is None:
        config = config_manager.load_config()
        if config is None:
            config = config_manager.DEFAULT_CONFIG

    coords = config.get("coordinates", config_manager.DEFAULT_CONFIG["coordinates"])
    timings = config_manager.get_timings_settings(config)
    guard_cfg = config_manager.get_otp_guard_settings(config)
    verify_ui = guard_cfg["otp_verify_screen_transitions"]
    diff_thresh = guard_cfg["otp_screen_diff_threshold"]
    restore_thresh = max(0.08, diff_thresh * 2.5)

    num = str(complaint_no).strip()
    if not num:
        return False, "Complaint number cannot be empty."

    # Early abort & foreground check
    if check_abort():
        return False, "OTP generation aborted by user (Esc pressed)."
    fg_ok, fg_msg = verify_erp_foreground(expected_fg)
    if not fg_ok:
        logger.warning("[OTP] Foreground verification failed before starting #%s: %s", num, fg_msg)
        return False, f"Halted before Step 1/6 for #{num}: {fg_msg}"

    add_pt = coords.get("otp_add_btn", {"x": 200, "y": 200})
    search_btn = coords.get("otp_complaint_search_btn", {"x": 350, "y": 250})
    input_pt = coords.get("otp_search_input", {"x": 500, "y": 300})
    reason_btn = coords.get("otp_reason_search_btn", {"x": 350, "y": 350})

    try:
        # Capture baseline patch on the primary screen before clicking 'Add'
        main_screen_search_btn_patch = (
            _capture_region_patch(search_btn["x"], search_btn["y"]) if verify_ui else None
        )

        # Step 1: Open OTP sub-window by clicking "Add" button
        logger.info("\n[OTP] Step 1/6: Opening OTP module (Click 'Add')...")
        pyautogui.click(add_pt["x"], add_pt["y"])

        if verify_ui and main_screen_search_btn_patch is not None:
            st1, ratio1, err1 = _wait_for_region_change(
                search_btn,
                main_screen_search_btn_patch,
                timeout_seconds=max(timings["otp_window_open_seconds"], 2.0),
                threshold=diff_thresh,
                min_settle=0.3,
                expected_fg=expected_fg,
            )
            logger.debug("[OTP] Step 1 transition status=%s (diff=%.3f).", st1, ratio1)
            if st1 == "ABORTED":
                return False, err1
            if st1 == "FOCUS_LOST":
                return False, f"Halted at Step 1/6 for #{num}: {err1}"
            if st1 == "TIMEOUT":
                return (
                    False,
                    f"Halted at Step 1/6 for #{num}: OTP 'Add' sub-window did not open "
                    "(ERP lagged or screen is blocked by an existing modal/popup).",
                )
        else:
            if sleep_interruptible(timings["otp_window_open_seconds"]):
                return False, "OTP generation aborted by user (Esc pressed)."
            fg_ok, fg_msg = verify_erp_foreground(expected_fg)
            if not fg_ok:
                return False, f"Halted at Step 1/6 for #{num}: {fg_msg}"

        # Step 2: Open Complaint search LOV modal by clicking 3-dots icon
        logger.info("[OTP] Step 2/6: Opening Complaint lookup (Click 3-dots)...")
        subwindow_center_patch = (
            _capture_region_patch(input_pt["x"], input_pt["y"]) if verify_ui else None
        )
        subwindow_reason_patch = (
            _capture_region_patch(reason_btn["x"], reason_btn["y"]) if verify_ui else None
        )

        pyautogui.click(search_btn["x"], search_btn["y"])

        if verify_ui and subwindow_center_patch is not None:
            st2, ratio2, err2 = _wait_for_region_change(
                input_pt,
                subwindow_center_patch,
                timeout_seconds=max(timings["otp_search_modal_seconds"], 2.0),
                threshold=diff_thresh,
                min_settle=0.25,
                expected_fg=expected_fg,
            )
            logger.debug("[OTP] Step 2 transition status=%s (diff=%.3f).", st2, ratio2)
            if st2 == "ABORTED":
                return False, err2
            if st2 == "FOCUS_LOST":
                return False, f"Halted at Step 2/6 for #{num}: {err2}"
            if st2 == "TIMEOUT":
                return (
                    False,
                    f"Halted at Step 2/6 for #{num}: Complaint Search LOV modal did not open.",
                )
        else:
            if sleep_interruptible(timings["otp_search_modal_seconds"]):
                return False, "OTP generation aborted by user (Esc pressed)."
            fg_ok, fg_msg = verify_erp_foreground(expected_fg)
            if not fg_ok:
                return False, f"Halted at Step 2/6 for #{num}: {fg_msg}"

        # Step 3: Double-click search input to highlight text and paste complaint number
        logger.info(f"[OTP] Step 3/6: Highlighting and pasting Complaint #{num}...")
        pyautogui.doubleClick(input_pt["x"], input_pt["y"])
        if sleep_interruptible(timings["otp_input_focus_delay_seconds"]):
            return False, "OTP generation aborted by user (Esc pressed)."

        fg_ok, fg_msg = verify_erp_foreground(expected_fg)
        if not fg_ok:
            return False, f"Halted at Step 3/6 for #{num}: {fg_msg}"

        pyperclip.copy(num)
        pyautogui.hotkey('ctrl', 'v')
        if sleep_interruptible(timings["otp_post_paste_delay_seconds"]):
            return False, "OTP generation aborted by user (Esc pressed)."

        fg_ok, fg_msg = verify_erp_foreground(expected_fg)
        if not fg_ok:
            return False, f"Halted at Step 3/6 for #{num}: {fg_msg}"

        # Step 4: Press Enter #1 to query LOV, wait otp_query_wait_seconds, then Enter #2 to select/close
        query_wait = timings["otp_query_wait_seconds"]
        modal_close_timeout = timings["otp_modal_close_timeout_seconds"]
        logger.info(
            f"[OTP] Step 4/6: Querying Complaint #{num} (Enter #1 -> wait {query_wait:.1f}s -> Enter #2)..."
        )
        pyautogui.press('enter')
        if sleep_interruptible(query_wait):
            return False, "OTP generation aborted by user (Esc pressed)."

        fg_ok, fg_msg = verify_erp_foreground(expected_fg)
        if not fg_ok:
            return False, f"Halted at Step 4/6 for #{num}: {fg_msg}"

        # Snapshot open LOV right before Enter #2 so we can verify Enter #2 closes the LOV modal
        pre_enter2_lov_patch = (
            _capture_region_patch(input_pt["x"], input_pt["y"]) if verify_ui else None
        )

        pyautogui.press('enter')

        if verify_ui and pre_enter2_lov_patch is not None:
            # Gate 4A: Confirm the Complaint LOV modal closed after Enter #2
            st4a, ratio4a, err4a = _wait_for_region_change(
                input_pt,
                pre_enter2_lov_patch,
                timeout_seconds=modal_close_timeout,
                threshold=diff_thresh,
                min_settle=0.2,
                expected_fg=expected_fg,
            )
            logger.debug("[OTP] Step 4A LOV-close status=%s (diff=%.3f).", st4a, ratio4a)
            if st4a == "ABORTED":
                return False, err4a
            if st4a == "FOCUS_LOST":
                return False, f"Halted at Step 4/6 for #{num}: {err4a}"
            if st4a == "TIMEOUT":
                return (
                    False,
                    f"Halted at Step 4/6 for #{num}: Complaint Search LOV stayed open after query "
                    "(no matching record found, invalid status, or ERP query lagged past timeout).",
                )

            # Gate 4B: Confirm the underlying OTP sub-window around Reason 3-dots is visible & unobstructed
            if subwindow_reason_patch is not None:
                st4b, ratio4b, err4b = _wait_for_region_restore(
                    reason_btn,
                    subwindow_reason_patch,
                    timeout_seconds=1.2,
                    max_diff_threshold=restore_thresh,
                    min_settle=0.1,
                    expected_fg=expected_fg,
                )
                logger.debug("[OTP] Step 4B sub-window restore status=%s (diff=%.3f).", st4b, ratio4b)
                if st4b == "ABORTED":
                    return False, err4b
                if st4b == "FOCUS_LOST":
                    return False, f"Halted at Step 4/6 for #{num}: {err4b}"
                if st4b == "TIMEOUT":
                    return (
                        False,
                        f"Halted at Step 4/6 for #{num}: Unexpected Oracle popup or modal obscuring OTP sub-window.",
                    )

        if sleep_interruptible(timings["otp_post_select_delay_seconds"]):
            return False, "OTP generation aborted by user (Esc pressed)."

        fg_ok, fg_msg = verify_erp_foreground(expected_fg)
        if not fg_ok:
            return False, f"Halted at Step 4/6 for #{num}: {fg_msg}"

        # Step 5: Click 3-dots next to OTP Reason (focus click, delay, then activate click)
        logger.info("[OTP] Step 5/6: Focusing and opening OTP Reason lookup (Double-tap)...")
        pre_reason_center_patch = (
            _capture_region_patch(input_pt["x"], input_pt["y"]) if verify_ui else None
        )

        # Focus click
        pyautogui.click(reason_btn["x"], reason_btn["y"])
        if sleep_interruptible(timings["otp_reason_focus_seconds"]):
            return False, "OTP generation aborted by user (Esc pressed)."

        fg_ok, fg_msg = verify_erp_foreground(expected_fg)
        if not fg_ok:
            return False, f"Halted at Step 5/6 for #{num}: {fg_msg}"

        # Activate click
        pyautogui.click(reason_btn["x"], reason_btn["y"])

        if verify_ui and pre_reason_center_patch is not None:
            # Gate 5: Confirm centered OTP Reason LOV opened (traps any Oracle validation alert before Enter x4)
            st5, ratio5, err5 = _wait_for_region_change(
                input_pt,
                pre_reason_center_patch,
                timeout_seconds=max(timings["otp_search_modal_seconds"], 2.0),
                threshold=diff_thresh,
                min_settle=0.25,
                expected_fg=expected_fg,
            )
            logger.debug("[OTP] Step 5 Reason-LOV status=%s (diff=%.3f).", st5, ratio5)
            if st5 == "ABORTED":
                return False, err5
            if st5 == "FOCUS_LOST":
                return False, f"Halted at Step 5/6 for #{num}: {err5}"
            if st5 == "TIMEOUT":
                return (
                    False,
                    f"Halted at Step 5/6 for #{num}: OTP Reason LOV modal did not open "
                    "(blocked by an active Oracle Alert or unselected complaint).",
                )
        else:
            if sleep_interruptible(timings["otp_search_modal_seconds"]):
                return False, "OTP generation aborted by user (Esc pressed)."
            fg_ok, fg_msg = verify_erp_foreground(expected_fg)
            if not fg_ok:
                return False, f"Halted at Step 5/6 for #{num}: {fg_msg}"

        # Step 6: Press Enter 4 times with micro-delays to confirm reason and return to primary screen
        logger.info("[OTP] Step 6/6: Confirming OTP reason and committing (Enter x4)...")
        enter_delay = timings["otp_enter_delay_seconds"]
        for i in range(1, 5):
            if check_abort():
                return False, "OTP generation aborted by user (Esc pressed)."
            fg_ok, fg_msg = verify_erp_foreground(expected_fg)
            if not fg_ok:
                return False, f"Halted at Step 6/6 (Enter {i}/4) for #{num}: {fg_msg}"
            pyautogui.press('enter')
            if sleep_interruptible(enter_delay):
                return False, "OTP generation aborted by user (Esc pressed)."

        if verify_ui and main_screen_search_btn_patch is not None:
            # Gate 6: Confirm OTP sub-window closed cleanly back to the primary screen
            st6, ratio6, err6 = _wait_for_region_restore(
                search_btn,
                main_screen_search_btn_patch,
                timeout_seconds=modal_close_timeout,
                max_diff_threshold=restore_thresh,
                min_settle=0.2,
                expected_fg=expected_fg,
            )
            logger.debug("[OTP] Step 6 main-screen restore status=%s (diff=%.3f).", st6, ratio6)
            if st6 == "ABORTED":
                return False, err6
            if st6 == "FOCUS_LOST":
                return False, f"Halted at Step 6/6 for #{num}: {err6}"
            if st6 == "TIMEOUT":
                return (
                    False,
                    f"Halted at Step 6/6 for #{num}: OTP sub-window did not close cleanly after Enter x4 "
                    "(possible Oracle validation alert).",
                )
        else:
            if sleep_interruptible(timings["otp_window_open_seconds"]):
                return False, "OTP generation aborted by user (Esc pressed)."
            fg_ok, fg_msg = verify_erp_foreground(expected_fg)
            if not fg_ok:
                return False, f"Halted at Step 6/6 for #{num}: {fg_msg}"

        logger.info(f"[+] OTP successfully generated for Complaint #{num}!")
        return True, f"OTP generated successfully for complaint #{num}"

    except KeyboardInterrupt:
        logger.warning("\n[!] OTP generation interrupted by user (Ctrl+C).")
        return False, "Aborted by user (Ctrl+C)."
    except pyautogui.FailSafeException:
        logger.error("\n[!] EMERGENCY STOP: PyAutoGUI FailSafe triggered (mouse in corner)!")
        return False, "Aborted by user (FailSafe triggered)."
    except Exception as e:
        logger.error(f"\n[!] Unexpected error during OTP generation: {e}", exc_info=True)
        return False, f"OTP generation failed: {e}"


def _find_saved_pdf_for_complaint(num, tech, output_base, organize_by_tech):
    """Locates an existing saved PDF for a complaint in the expected or alternate output folder."""
    primary_folder = os.path.join(output_base, tech) if organize_by_tech else os.path.join(output_base, "All_PDFs")
    alt_folder = os.path.join(output_base, "All_PDFs") if organize_by_tech else os.path.join(output_base, tech)

    for folder in (primary_folder, alt_folder):
        candidate = os.path.abspath(os.path.join(folder, f"{num}.pdf"))
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return None


def run_batch(
    records_iterable,
    config=None,
    start_delay=5,
    organize_by_tech=True,
    live_print=False,
    direct_print=False,
    target_printer=None,
):
    """
    Executes batch processing for an iterable of (complaint_no, tech_name) records.
    Returns summary statistics dict: {'total', 'completed', 'skipped', 'failed', 'aborted'}.
    """
    import printer_manager as pm

    reset_abort()
    config_manager.set_dpi_awareness()
    if config is None:
        config = config_manager.load_config()
        if config is None:
            config = config_manager.DEFAULT_CONFIG

    settings = config.get("settings", config_manager.DEFAULT_CONFIG["settings"])
    timings = config.get("timings", config_manager.DEFAULT_CONFIG["timings"])
    printing_cfg = config_manager.get_printing_settings(config)
    spooler_query_timeout = printing_cfg.get("spooler_query_timeout", 5.0)
    output_base = config_manager.resolve_path(settings.get("output_dir", "Today_Complaints"))
    db_path = state_manager.ensure_tracker_db_ready(
        config, tracker_kind="printed" if direct_print else "completed", output_dir=output_base
    )
    completed_set = get_completed_complaints(db_path)

    printed_set = set()
    if live_print or direct_print:
        try:
            printed_set = pm.load_printed_complaints(db_path)
        except Exception as e:
            logger.warning(f"[!] Warning: Could not load printed tracker from '{db_path}': {e}")
            printed_set = set()

    resolved_printer = None
    default_printer_target = None
    if direct_print:
        resolved_printer = str(target_printer or "").strip()
        if not resolved_printer:
            resolved_printer, _ = config_manager.get_configured_printer(config)
        if not resolved_printer or pm.classify_printer(resolved_printer) == "VIRTUAL":
            logger.error("[!] Direct Browser Print aborted: No valid physical target printer configured.")
            return {
                "total": len(list(records_iterable)),
                "completed": 0,
                "skipped": 0,
                "failed": 0,
                "aborted": True,
            }
        default_printer_target = resolved_printer
    else:
        installed_printers = pm.get_installed_printers()
        if "Microsoft Print to PDF" in installed_printers:
            default_printer_target = "Microsoft Print to PDF"

    records = list(records_iterable)
    stats = {
        "total": len(records),
        "completed": 0,
        "skipped": 0,
        "failed": 0,
        "aborted": False
    }

    in_flight = []
    spooled_pending = set()

    def _sync_spooled_pending():
        if not spooled_pending:
            return
        active_ids = {item.get("complaint_no") for item in in_flight}
        finished_ids = [cid for cid in list(spooled_pending) if cid not in active_ids]
        for cid in finished_ids:
            spooled_pending.discard(cid)
            rec = state_manager.get_complaint_record(cid, db_path)
            if rec and rec.get("status") == "PRINTED":
                printed_set.add(cid)
                stats["completed"] += 1
            else:
                printed_set.discard(cid)
                stats["failed"] += 1

    logger.info(f"\n[*] Batch Execution Initialized: {len(records)} total records.")
    if direct_print:
        logger.info(f"[*] Workflow Mode: Direct Browser Print (No PDF Save) -> '{resolved_printer}'")
        logger.info(f"Auto-resume (Direct Print): {len(printed_set)} already printed records found.")
    else:
        routing_label = "Per-Technician Folders" if organize_by_tech else "All_PDFs (Unified Folder)"
        logger.info(f"[*] Folder Routing: {routing_label}")
        logger.info(f"Auto-resume: {len(completed_set)} already completed records found.")
    logger.info(f"Starting in {start_delay} seconds! Please ensure your ERP window is open and maximized...")

    try:
        with pm.temporary_default_printer(default_printer_target):
            for i in range(start_delay, 0, -1):
                if check_abort():
                    logger.warning("\n[!] Aborted during countdown.")
                    stats["aborted"] = True
                    return stats
                print(f"  Countdown: {i}...")
                time.sleep(1.0)

            for idx, (complaint_no, tech_name) in enumerate(records, 1):
                num = str(complaint_no).strip()
                tech = sanitize_technician_name(tech_name)

                if check_abort():
                    logger.warning("\n[!] Batch processing stopped by User (Esc pressed).")
                    stats["aborted"] = True
                    break

                if direct_print:
                    if in_flight:
                        try:
                            try:
                                pm.reconcile_in_flight_jobs(
                                    resolved_printer,
                                    in_flight,
                                    db_path,
                                    spooler_query_timeout=spooler_query_timeout,
                                )
                            except TypeError:
                                pm.reconcile_in_flight_jobs(resolved_printer, in_flight, db_path)
                        except Exception as rec_err:
                            logger.warning("Spooler reconciliation warning: %s", rec_err)
                        _sync_spooled_pending()

                    if num in printed_set:
                        logger.info(f"[>>] [{idx}/{len(records)}] {num} already printed. Skipping...")
                        stats["skipped"] += 1
                        continue

                    existing_disk_pdf = _find_saved_pdf_for_complaint(num, tech, output_base, organize_by_tech)
                    if existing_disk_pdf:
                        record_completed_complaint(db_path, num, technician=tech, file_path=existing_disk_pdf)
                        completed_set.add(num)
                        logger.info(
                            f"\n[->] [{idx}/{len(records)}] Existing PDF found on disk for #{num} (Tech: {tech}); printing directly..."
                        )
                        print_ok, print_msg = _dispatch_live_print(
                            num, existing_disk_pdf, config, timings, target_printer=resolved_printer
                        )
                        if print_ok:
                            printed_set.add(num)
                            stats["completed"] += 1
                        else:
                            if "aborted" in print_msg.lower() or "failsafe" in print_msg.lower():
                                stats["aborted"] = True
                                break
                            stats["failed"] += 1
                        continue

                    logger.info(f"\n[->] [{idx}/{len(records)}] Direct-Printing Complaint #: {num} (Tech: {tech})")
                    success, msg = process_complaint(
                        num,
                        tech,
                        config=config,
                        record_progress=True,
                        organize_by_tech=organize_by_tech,
                        live_print=False,
                        direct_print=True,
                        target_printer=resolved_printer,
                        in_flight=in_flight,
                    )
                    _sync_spooled_pending()

                    if success:
                        if any(item.get("complaint_no") == num for item in in_flight):
                            spooled_pending.add(num)
                        else:
                            printed_set.add(num)
                            stats["completed"] += 1
                    else:
                        if "aborted" in msg.lower() or "failsafe" in msg.lower():
                            stats["aborted"] = True
                            break
                        elif "skipped" in msg.lower():
                            stats["skipped"] += 1
                        else:
                            stats["failed"] += 1
                    continue

                if num in completed_set:
                    if not live_print or num in printed_set:
                        logger.info(f"[>>] [{idx}/{len(records)}] {num} already done. Skipping...")
                        stats["skipped"] += 1
                        continue

                    # Complaint PDF was completed previously, but is not marked PRINTED in SQLite.
                    # Retry physical printing from saved PDF if present on disk.
                    saved_pdf = _find_saved_pdf_for_complaint(num, tech, output_base, organize_by_tech)
                    if saved_pdf:
                        logger.info(f"\n[->] [{idx}/{len(records)}] Retrying print for saved PDF #{num} (Tech: {tech})")
                        print_ok, print_msg = _dispatch_live_print(num, saved_pdf, config, timings)
                        if print_ok:
                            printed_set.add(num)
                            stats["completed"] += 1
                        else:
                            if "aborted" in print_msg.lower() or "failsafe" in print_msg.lower():
                                stats["aborted"] = True
                                break
                            stats["failed"] += 1
                        continue

                # Runtime Defense-in-Depth: check if a valid non-empty PDF already exists on disk
                # even if it was not yet registered in completed_set.
                existing_disk_pdf = _find_saved_pdf_for_complaint(num, tech, output_base, organize_by_tech)
                if existing_disk_pdf:
                    record_completed_complaint(db_path, num, technician=tech, file_path=existing_disk_pdf)
                    completed_set.add(num)
                    if not live_print or num in printed_set:
                        logger.info(
                            f"[>>] [{idx}/{len(records)}] {num} already exists on disk (synced to DB). Skipping..."
                        )
                        stats["skipped"] += 1
                        continue

                    logger.info(
                        f"\n[->] [{idx}/{len(records)}] Existing PDF found on disk for #{num} (Tech: {tech}); synced to DB and printing..."
                    )
                    print_ok, print_msg = _dispatch_live_print(num, existing_disk_pdf, config, timings)
                    if print_ok:
                        printed_set.add(num)
                        stats["completed"] += 1
                    else:
                        if "aborted" in print_msg.lower() or "failsafe" in print_msg.lower():
                            stats["aborted"] = True
                            break
                        stats["failed"] += 1
                    continue

                logger.info(f"\n[->] [{idx}/{len(records)}] Processing Complaint #: {num} (Tech: {tech})")
                success, msg = process_complaint(
                    num, tech, config=config, record_progress=True, organize_by_tech=organize_by_tech, live_print=live_print
                )

                # Update in-memory sets based on actual persisted state
                saved_pdf_now = _find_saved_pdf_for_complaint(num, tech, output_base, organize_by_tech)
                if saved_pdf_now:
                    completed_set.add(num)

                if success:
                    completed_set.add(num)
                    if live_print:
                        printed_set.add(num)
                    stats["completed"] += 1
                else:
                    if "aborted" in msg.lower() or "failsafe" in msg.lower():
                        stats["aborted"] = True
                        break
                    stats["failed"] += 1

            # Final Drain Pass for Direct Browser Print sliding-window jobs
            if direct_print and in_flight and not stats["aborted"]:
                try:
                    try:
                        _, _, drain_aborted = pm.drain_in_flight_jobs(
                            resolved_printer,
                            in_flight,
                            db_path,
                            check_abort_fn=check_abort,
                            spooler_query_timeout=spooler_query_timeout,
                        )
                    except TypeError:
                        _, _, drain_aborted = pm.drain_in_flight_jobs(
                            resolved_printer,
                            in_flight,
                            db_path,
                            check_abort_fn=check_abort,
                        )
                    _sync_spooled_pending()
                    if drain_aborted:
                        stats["aborted"] = True
                except KeyboardInterrupt:
                    logger.warning("\n[!] Final spooler drain interrupted by user.")
                    stats["aborted"] = True
                except Exception as drain_err:
                    logger.error(f"\n[!] Error during final spooler drain: {drain_err}", exc_info=True)
                    in_flight.clear()
                    _sync_spooled_pending()
    except KeyboardInterrupt:
        logger.warning("\n[!] Batch processing interrupted by user (Ctrl+C).")
        stats["aborted"] = True

    logger.info(
        "Batch execution summary: total=%d, completed=%d, skipped=%d, failed=%d, aborted=%s",
        stats["total"],
        stats["completed"],
        stats["skipped"],
        stats["failed"],
        stats["aborted"],
    )
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
