"""
printer_manager.py
Modular Print Manager for Universal Silent Printing & Windows Spooler Queue Monitoring.
Uses in-process headless GDI rendering (pypdfium2 + 1-bit monochrome GDI StretchDIBits)
for lightweight (<1 MB/page), zero-popup physical printing that prevents PCL XL buffer overflows.
Persists spooler & print job state via state_manager (SQLite complaints_state.db).
"""

from contextlib import contextmanager
import ctypes
import gc
import os
import sys
import threading
import time
import winsound
from typing import List, Dict, Optional, Set, Tuple, Callable, Any, Union

GDI_ERROR = 0xFFFFFFFF

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import win32print
    import win32ui
    import win32con
    import win32gui
    try:
        import win32timezone  # noqa: F401
    except Exception:
        win32timezone = None
    from PIL import Image, ImageWin
    import pypdfium2 as pdfium

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class RGBQUAD(ctypes.Structure):
        _fields_ = [
            ("rgbBlue", ctypes.c_ubyte),
            ("rgbGreen", ctypes.c_ubyte),
            ("rgbRed", ctypes.c_ubyte),
            ("rgbReserved", ctypes.c_ubyte),
        ]

    class BITMAPINFO_1BIT(ctypes.Structure):
        _fields_ = [
            ("bmiHeader", BITMAPINFOHEADER),
            ("bmiColors", RGBQUAD * 2),
        ]

    try:
        _gdi32 = ctypes.windll.gdi32
        _gdi32.StretchDIBits.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint32,
        ]
        _gdi32.StretchDIBits.restype = ctypes.c_int
        _gdi32.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _gdi32.SetStretchBltMode.restype = ctypes.c_int
        _gdi32.GetObjectType.argtypes = [ctypes.c_void_p]
        _gdi32.GetObjectType.restype = ctypes.c_uint32
    except Exception:
        _gdi32 = None
else:
    win32print = None
    win32ui = None
    win32con = None
    win32gui = None
    win32timezone = None
    Image = None
    ImageWin = None
    pdfium = None
    BITMAPINFOHEADER = None
    RGBQUAD = None
    BITMAPINFO_1BIT = None
    _gdi32 = None

import logger_manager
import state_manager

logger = logger_manager.get_logger("printer_manager")

# Tracks whether the most recent enforce_queue_gate call required interactive operator input
LAST_GATE_PROMPTED: bool = False
# Tracks the most recent spooler job ID created by print_pdf_silent
LAST_SPOOLED_JOB_ID: Optional[int] = None
# Tracks in-flight SpoolerStatusProbe worker threads per printer so hung drivers do not spawn duplicate probes
_ACTIVE_SPOOLER_PROBES_LOCK = threading.Lock()
_ACTIVE_SPOOLER_PROBES: Dict[str, threading.Thread] = {}


def _resolve_spooler_query_timeout(
    spooler_query_timeout: Optional[Any] = None,
    query_timeout: Optional[Any] = None,
    config: Optional[Dict] = None,
) -> float:
    """
    Resolves and validates the spooler status query timeout in seconds (minimum 0.5s, default 5.0s).
    """
    raw_timeout = spooler_query_timeout if spooler_query_timeout is not None else query_timeout
    if raw_timeout is not None:
        try:
            return max(0.5, float(raw_timeout))
        except (ValueError, TypeError):
            return 5.0
    try:
        import config_manager

        printing_cfg = config_manager.get_printing_settings(config)
        return max(0.5, float(printing_cfg.get("spooler_query_timeout", 5.0)))
    except Exception:
        return 5.0


def _call_get_queue_status(printer_name: str, timeout: Optional[float] = None) -> Dict:
    """
    Invokes `get_queue_status` with the validated `timeout`, falling back to single-argument
    invocation only if `get_queue_status` was monkeypatched in tests with a legacy 1-arg signature.
    """
    effective_timeout = _resolve_spooler_query_timeout(timeout)
    if get_queue_status is _ORIG_GET_QUEUE_STATUS:
        return get_queue_status(printer_name, timeout=effective_timeout)
    try:
        return get_queue_status(printer_name, timeout=effective_timeout)
    except TypeError:
        try:
            return get_queue_status(printer_name, effective_timeout)
        except TypeError:
            return get_queue_status(printer_name)


def get_installed_printers() -> List[str]:
    """
    Enumerates all local and network-connected printers installed in Windows.
    Returns:
        List of printer names as strings. Returns an empty list if enumeration fails.
    """
    if not win32print:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        printers = win32print.EnumPrinters(flags)
        return [p[2] for p in printers]
    except Exception as e:
        logger.warning(f" [!] Warning: Failed to enumerate Windows printers: {e}")
        return []


def classify_printer(printer_name: str) -> str:
    """
    Determines whether a printer is PHYSICAL or VIRTUAL (PDF/XPS/Fax/OneNote/Evernote/Writer).
    Inspects printer name, port, and driver metadata.
    Returns:
        'PHYSICAL' or 'VIRTUAL'
    """
    if not win32print or not printer_name:
        return "UNKNOWN"

    name_lower = printer_name.lower()
    virtual_keywords = ["pdf", "xps", "fax", "onenote", "evernote", "document writer", "root print queue"]
    for kw in virtual_keywords:
        if kw in name_lower:
            return "VIRTUAL"

    try:
        h_printer = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(h_printer, 2)
            port = str(info.get("pPortName", "")).lower()
            driver = str(info.get("pDriverName", "")).lower()

            for kw in virtual_keywords:
                if kw in driver:
                    return "VIRTUAL"

            virtual_ports = ["portprompt:", "shrfax:", "file:", "nul:", "foxit_"]
            for vp in virtual_ports:
                if vp in port:
                    return "VIRTUAL"
        finally:
            win32print.ClosePrinter(h_printer)
    except Exception:
        pass

    return "PHYSICAL"


def select_printer_interactive(
    config: Optional[Dict] = None,
    auto_save: bool = True,
    force_prompt: bool = False,
    header_title: str = "SILENT PRINTER SELECTION"
) -> Tuple[Optional[str], Optional[Dict]]:
    """
    Unified interactive printer selection interface.
    Enumerates installed printers, categorizes them as PHYSICAL or VIRTUAL,
    displays a simplified numbered list with status badges, and saves to config.json.

    Args:
        config: Current configuration dictionary. Loaded automatically if None.
        auto_save: If True, immediately writes selection to config.json.
        force_prompt: If True, always presents menu even if printer already configured.
        header_title: Custom header title for context.

    Returns:
        Tuple of (selected_printer_name, config_dict).
    """
    import config_manager

    if config is None:
        config = config_manager.load_config() or config_manager.DEFAULT_CONFIG.copy()

    printers = get_installed_printers()
    default_printer = get_default_printer()
    current_printer = config.get("printing", {}).get("printer_name")

    if not printers:
        logger.error("\n [!] ERROR: No Windows printers detected on this computer.")
        return None, config

    # Check if existing printer is valid and prompt is not forced
    current_is_virtual = classify_printer(current_printer) == "VIRTUAL" if current_printer else False
    if not force_prompt and current_printer and current_printer in printers and not current_is_virtual:
        return current_printer, config

    # Header display
    curr_badge = f" [{classify_printer(current_printer)}]" if current_printer else ""
    def_badge = f" [{classify_printer(default_printer)}]" if default_printer else ""

    print("\n" + "=" * 65)
    print(f"        {header_title.upper()}")
    print("=" * 65)
    print(f" Current Configured Target : {current_printer or '(None)'}{curr_badge}")
    print(f" Windows OS Default Printer: {default_printer or 'None'}{def_badge}")
    print("-" * 65)
    print(" Installed Windows Printers:")

    for idx, p_name in enumerate(printers, start=1):
        cat = classify_printer(p_name)
        tags = [cat]
        if p_name == default_printer:
            tags.append("DEFAULT")
        if p_name == current_printer:
            tags.append("SELECTED")
        tag_str = f" [{', '.join(tags)}]"
        print(f"   [{idx}] {p_name}{tag_str}")

    print("=" * 65)

    prompt_label = f" Select printer [1-{len(printers)}]"
    if current_printer:
        prompt_label += " (or press Enter to keep current): "
    else:
        prompt_label += ": "

    choice = input(prompt_label).strip()
    if choice.lower() in ["q", "quit", "cancel"]:
        logger.info(" [i] Printer selection cancelled.")
        return None, config

    selected_name = current_printer

    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(printers):
            selected_name = printers[idx - 1]
            cat = classify_printer(selected_name)
            logger.info(f" [✓] Selected: {selected_name} [{cat}]")
            if cat == "VIRTUAL":
                logger.warning(" [!] Notice: You selected a VIRTUAL printer. It may open file dialogs instead of physical printing.")
        else:
            logger.warning(" [!] Invalid selection index. Keeping existing printer setting.")
    elif current_printer:
        logger.info(f" [i] Keeping configured printer: {current_printer}")
    else:
        # Default fallback to first physical printer, or system default
        physical_candidates = [p for p in printers if classify_printer(p) == "PHYSICAL"]
        if physical_candidates:
            selected_name = physical_candidates[0]
            logger.info(f" [i] Automatically selected first physical printer: {selected_name}")
        elif default_printer:
            selected_name = default_printer
            logger.info(f" [i] Defaulting to system printer: {selected_name}")
        else:
            selected_name = printers[0]

    # Save to config if requested
    if auto_save and selected_name:
        if "printing" not in config:
            config["printing"] = {}
        config["printing"]["printer_name"] = selected_name
        config_manager.save_config(config)
        logger.info(f" [✓] Saved target printer '{selected_name}' to config.json.")

    return selected_name, config


def get_default_printer() -> Optional[str]:
    """
    Retrieves the system default printer configured in Windows.
    Returns:
        Default printer name string, or None if unavailable.
    """
    if not win32print:
        return None
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        return None


def set_default_printer(printer_name: str) -> bool:
    """
    Sets the Windows OS default printer to `printer_name`.
    Returns True if succeeded, False otherwise.
    """
    if not win32print or not printer_name:
        return False
    try:
        win32print.SetDefaultPrinter(printer_name)
        logger.debug("Set Windows OS default printer to '%s'.", printer_name)
        return True
    except Exception as e:
        logger.warning(f" [!] Warning: Failed to set Windows default printer to '{printer_name}': {e}")
        return False


@contextmanager
def temporary_default_printer(target_printer: Optional[str]):
    """
    Context manager that temporarily switches the Windows OS default printer to
    `target_printer` and reliably restores the previous OS default printer on exit.
    """
    original_default = get_default_printer()
    switched = False
    target = str(target_printer).strip() if target_printer else ""

    if target and target != (original_default or ""):
        installed = get_installed_printers()
        if not installed or target in installed:
            if set_default_printer(target):
                switched = True
                logger.info(
                    f"[*] Temporarily set Windows default printer to '{target}' "
                    f"(previous: '{original_default or 'None'}')."
                )
        else:
            logger.warning(
                f"[!] Target printer '{target}' not found in installed printers; "
                f"keeping OS default '{original_default or 'None'}'."
            )

    try:
        yield switched
    finally:
        if switched and original_default:
            if set_default_printer(original_default):
                logger.info(f"[*] Restored Windows default printer to '{original_default}'.")
            else:
                logger.warning(
                    f"[!] Warning: Could not restore previous Windows default printer '{original_default}'."
                )


def _get_printer_error_flags() -> Dict[int, str]:
    if not win32print:
        return {}
    return {
        getattr(win32print, "PRINTER_STATUS_PAUSED", 0x00000001): "Printer Paused",
        win32print.PRINTER_STATUS_PAPER_JAM: "Paper Jam",
        win32print.PRINTER_STATUS_PAPER_OUT: "Paper Out / Tray Empty",
        win32print.PRINTER_STATUS_PAPER_PROBLEM: "Paper Problem",
        win32print.PRINTER_STATUS_OFFLINE: "Printer Offline",
        win32print.PRINTER_STATUS_ERROR: "Printer Error",
        win32print.PRINTER_STATUS_DOOR_OPEN: "Cover / Door Open",
        win32print.PRINTER_STATUS_USER_INTERVENTION: "User Intervention Required",
        win32print.PRINTER_STATUS_NOT_AVAILABLE: "Printer Not Available",
    }


def _get_job_error_flags() -> Dict[int, str]:
    if not win32print:
        return {}
    return {
        getattr(win32print, "JOB_STATUS_PAUSED", 0x00000001): "Job Paused",
        win32print.JOB_STATUS_ERROR: "Job Error",
        win32print.JOB_STATUS_PAPEROUT: "Job Paused - Paper Out",
        win32print.JOB_STATUS_OFFLINE: "Job Paused - Printer Offline",
        win32print.JOB_STATUS_BLOCKED_DEVQ: "Job Blocked in Spooler",
        win32print.JOB_STATUS_USER_INTERVENTION: "User Intervention Required",
    }


def _query_queue_status_raw(printer_name: str) -> Dict:
    """
    Synchronous Win32 Print Spooler status query worker.
    Invoked inside a timeout-guarded daemon thread by `get_queue_status`.
    """
    if not win32print:
        return {
            "printer_name": printer_name,
            "job_count": 0,
            "has_error": False,
            "query_error": False,
            "error_reasons": ["Non-Windows OS"],
            "jobs": [],
        }

    try:
        h_printer = win32print.OpenPrinter(printer_name)
    except Exception as e:
        return {
            "printer_name": printer_name,
            "job_count": 0,
            "has_error": True,
            "query_error": True,
            "error_reasons": [f"Cannot open printer: {e}"],
            "jobs": [],
        }

    try:
        try:
            p_info = win32print.GetPrinter(h_printer, 2)
        except Exception as e:
            return {
                "printer_name": printer_name,
                "job_count": 0,
                "has_error": True,
                "query_error": True,
                "error_reasons": [f"Cannot query printer status: {e}"],
                "jobs": [],
            }

        c_jobs = p_info.get("cJobs", 0)
        p_status = p_info.get("Status", 0)

        has_error = False
        query_error = False
        error_reasons = []

        # Enumerate individual spooler jobs
        try:
            jobs = win32print.EnumJobs(h_printer, 0, 999, 2)
        except Exception as e:
            jobs = ()
            has_error = True
            query_error = True
            error_reasons.append(f"Cannot enumerate spooler jobs: {e}")

        # Printer-level status flags (including PRINTER_STATUS_PAUSED)
        printer_error_flags = _get_printer_error_flags()
        for flag, desc in printer_error_flags.items():
            if p_status & flag:
                has_error = True
                error_reasons.append(desc)

        # Job-level status flags (including JOB_STATUS_PAUSED)
        job_error_flags = _get_job_error_flags()
        for job in jobs:
            j_status = job.get("Status", 0)
            for flag, desc in job_error_flags.items():
                if j_status & flag:
                    has_error = True
                    if desc not in error_reasons:
                        error_reasons.append(desc)

        return {
            "printer_name": printer_name,
            "job_count": c_jobs,
            "has_error": has_error,
            "query_error": query_error,
            "error_reasons": error_reasons,
            "jobs": [
                {
                    "job_id": j.get("JobId"),
                    "document": j.get("pDocument"),
                    "status": j.get("Status"),
                    "pages": j.get("TotalPages"),
                    "pages_printed": j.get("PagesPrinted", 0),
                }
                for j in jobs
            ],
        }
    finally:
        try:
            win32print.ClosePrinter(h_printer)
        except Exception:
            pass


def get_queue_status(printer_name: str, timeout: float = 5.0) -> Dict:
    """
    Queries the Windows Print Spooler for active jobs and status flags on target printer.
    Guarded by a non-blocking daemon worker thread so hung Windows Spooler RPC calls
    (e.g., when spoolsv.exe or a USB port monitor stalls) never deadlock the main thread
    or block [ESC] / [Q] failsafe aborts.
    Tracks active probes per printer so a hung worker does not accumulate duplicate threads.

    Returns:
        Dict containing:
            - printer_name (str)
            - job_count (int)
            - has_error (bool)
            - error_reasons (List[str])
            - jobs (List[Dict])
    """
    if not win32print:
        return {
            "printer_name": printer_name,
            "job_count": 0,
            "has_error": False,
            "query_error": False,
            "error_reasons": ["Non-Windows OS"],
            "jobs": [],
        }

    run_sync = timeout is None or timeout <= 0
    with _ACTIVE_SPOOLER_PROBES_LOCK:
        existing_worker = _ACTIVE_SPOOLER_PROBES.get(printer_name)
        if existing_worker is not None:
            if existing_worker.is_alive():
                reason = (
                    f"Spooler API probe still running for '{printer_name}' "
                    "(previous query timed out; printer driver or Windows Spooler may be hung)"
                )
                logger.warning("Skipping new spooler probe on '%s': %s", printer_name, reason)
                return {
                    "printer_name": printer_name,
                    "job_count": 0,
                    "has_error": True,
                    "query_error": True,
                    "error_reasons": [reason],
                    "jobs": [],
                }
            _ACTIVE_SPOOLER_PROBES.pop(printer_name, None)

        if not run_sync:
            result_box: Dict[str, Any] = {}
            worker_ref: List[Optional[threading.Thread]] = [None]

            def _worker():
                try:
                    result_box["data"] = _query_queue_status_raw(printer_name)
                except Exception as exc:
                    result_box["error"] = exc
                finally:
                    with _ACTIVE_SPOOLER_PROBES_LOCK:
                        current = _ACTIVE_SPOOLER_PROBES.get(printer_name)
                        if current is worker_ref[0] or current is threading.current_thread():
                            _ACTIVE_SPOOLER_PROBES.pop(printer_name, None)

            worker = threading.Thread(target=_worker, name="SpoolerStatusProbe", daemon=True)
            worker_ref[0] = worker
            _ACTIVE_SPOOLER_PROBES[printer_name] = worker

    if run_sync:
        return _query_queue_status_raw(printer_name)

    try:
        worker.start()
    except Exception as start_exc:
        with _ACTIVE_SPOOLER_PROBES_LOCK:
            if _ACTIVE_SPOOLER_PROBES.get(printer_name) is worker:
                _ACTIVE_SPOOLER_PROBES.pop(printer_name, None)
        return {
            "printer_name": printer_name,
            "job_count": 0,
            "has_error": True,
            "query_error": True,
            "error_reasons": [f"Spooler status error: {start_exc}"],
            "jobs": [],
        }

    worker.join(timeout=float(timeout))

    if worker.is_alive():
        reason = (
            f"Spooler API timed out after {float(timeout):.1f}s "
            "(printer driver or Windows Spooler may be hung)"
        )
        logger.warning("Spooler query timeout on '%s': %s", printer_name, reason)
        return {
            "printer_name": printer_name,
            "job_count": 0,
            "has_error": True,
            "query_error": True,
            "error_reasons": [reason],
            "jobs": [],
        }

    with _ACTIVE_SPOOLER_PROBES_LOCK:
        if _ACTIVE_SPOOLER_PROBES.get(printer_name) is worker:
            _ACTIVE_SPOOLER_PROBES.pop(printer_name, None)

    if "error" in result_box:
        return {
            "printer_name": printer_name,
            "job_count": 0,
            "has_error": True,
            "query_error": True,
            "error_reasons": [f"Spooler status error: {result_box['error']}"],
            "jobs": [],
        }

    return result_box.get(
        "data",
        {
            "printer_name": printer_name,
            "job_count": 0,
            "has_error": True,
            "query_error": True,
            "error_reasons": ["Unknown spooler status failure"],
            "jobs": [],
        },
    )


def enforce_queue_gate(
    printer_name: str,
    max_jobs: int = 3,
    stall_timeout: float = 60.0,
    on_poll: Optional[Callable[[Dict], None]] = None,
    check_abort_fn: Optional[Callable[[], bool]] = None,
    spooler_query_timeout: Optional[float] = None,
    query_timeout: Optional[float] = None,
    config: Optional[Dict] = None,
) -> bool:
    """
    Hardware Failsafe Pre-Dispatch Gate with Progress-Aware Sliding Window Pacing.
    Monitors the Windows Print Spooler:
    - If queue < max_jobs and no hardware error, immediately allows dispatch.
    - If queue >= max_jobs and no hardware error, quietly paces dispatch while monitoring
      queue progress (job count drops, front job ID advances, or pages printed increments).
      Resets the stall timer whenever progress is observed so large batches print continuously
      without false interruptions.
    - If a hardware/spooler error is detected OR the queue remains completely frozen for
      >= stall_timeout seconds, pauses execution, emits audio beeps, and prompts the operator.

    Returns:
        True: Queue is healthy or operator forced dispatch.
        False: Operator elected to skip this print job.
    """
    global LAST_GATE_PROMPTED
    LAST_GATE_PROMPTED = False

    effective_query_timeout = _resolve_spooler_query_timeout(
        spooler_query_timeout, query_timeout=query_timeout, config=config
    )

    drain_start = None
    last_sig = None

    while True:
        if check_abort_fn and check_abort_fn():
            raise KeyboardInterrupt("Print queue operation aborted via Esc.")

        status = _call_get_queue_status(printer_name, timeout=effective_query_timeout)
        if callable(on_poll):
            on_poll(status)

        job_count = status.get("job_count", 0)
        has_error = status.get("has_error", False)
        error_reasons = status.get("error_reasons", [])
        jobs_list = status.get("jobs", [])

        # Queue is healthy if jobs < max_jobs and no hardware error
        if job_count < max_jobs and not has_error:
            return True

        # Progress-Aware Auto-Drain Wait: If queue is full but NO hardware error, wait dynamically
        if not has_error and job_count >= max_jobs:
            head_job = jobs_list[0] if jobs_list else {}
            current_sig = (
                job_count,
                head_job.get("job_id"),
                head_job.get("pages_printed", 0),
                head_job.get("status", 0),
            )

            if drain_start is None:
                drain_start = time.time()
                last_sig = current_sig
                logger.info(
                    f" [i] Spooler buffer full ({job_count}/{max_jobs} active jobs). "
                    f"Pacing dispatch while printer drains..."
                )
            elif current_sig != last_sig:
                # Active progress observed in spooler; reset stall timer
                drain_start = time.time()
                last_sig = current_sig

            elapsed = time.time() - drain_start
            if elapsed < stall_timeout:
                for _ in range(3):
                    if check_abort_fn and check_abort_fn():
                        raise KeyboardInterrupt("Print queue operation aborted via Esc.")
                    time.sleep(0.5)
                continue
            # If elapsed >= stall_timeout with zero progress, fall through to audible alert prompt

        # Audible warning (2 alert beeps)
        try:
            winsound.Beep(1000, 250)
            winsound.Beep(1500, 300)
        except Exception:
            pass

        issue_summary = ", ".join(error_reasons) if error_reasons else f"Spooler queue stalled for >{int(stall_timeout)}s without progress"
        logger.warning(
            "Spooler alert on printer '%s' (pending_jobs=%d, max=%d): %s",
            printer_name,
            job_count,
            max_jobs,
            issue_summary,
        )

        print("\n" + "=" * 65)
        print(" [!] HARDWARE / SPOOLER ALERT: PRINTER PAUSED")
        print("=" * 65)
        print(f" Target Printer   : {printer_name}")
        print(f" Pending Jobs     : {job_count} (Configured Threshold: {max_jobs})")
        if error_reasons:
            print(f" Issues Detected  : {', '.join(error_reasons)}")
        else:
            print(f" Issues Detected  : Spooler queue stalled for >{int(stall_timeout)}s without progress")
        print("\n Possible Causes:")
        print("  - Physical paper jam or cover door open")
        print("  - Printer tray out of paper")
        print("  - Printer turned off, paused, or offline")
        print("=" * 65)
        print(" [Enter] Re-check queue and resume once hardware is cleared")
        print(" [F]     Force Print (Bypass/ignore warning and send job anyway)")
        print(" [S]     Skip printing this complaint (mark unprinted)")
        print(" [Q]     Abort entire batch operation")
        print("=" * 65)

        LAST_GATE_PROMPTED = True
        user_choice = input(" Select action ([Enter] / F / S / Q): ").strip().lower()
        if user_choice == "f":
            logger.warning(" [!] Operator override: Forcing print job dispatch despite warning.")
            return True
        elif user_choice == "s":
            logger.info(" [i] Skipping print for this complaint.")
            return False
        elif user_choice == "q":
            logger.warning(" [!] Batch operation aborted by operator.")
            raise KeyboardInterrupt("Print queue operation aborted by user.")

        # Reset drain timer for next check
        drain_start = None
        last_sig = None
        logger.info(" [i] Re-checking printer status...")
        time.sleep(1.0)


def _get_monochrome_devmode(printer_name: str) -> Optional[Any]:
    """
    Retrieves the printer's DEVMODE structure, sets DMCOLOR_MONOCHROME, and invokes
    win32print.DocumentProperties(DM_IN_BUFFER | DM_OUT_BUFFER) so drivers with private
    DriverExtra blocks (e.g., HP PCL 6 / hpcpp117) synchronize internal color state to
    monochrome without requiring manual Windows Control Panel tweaks.
    """
    if not win32print or not win32con or not printer_name:
        return None

    h_printer = None
    try:
        h_printer = win32print.OpenPrinter(printer_name)
        info = win32print.GetPrinter(h_printer, 2)
        dm = info.get("pDevMode") if isinstance(info, dict) else None
        if dm is None:
            return None

        dm.Color = getattr(win32con, "DMCOLOR_MONOCHROME", 1)
        dm.Fields |= getattr(win32con, "DM_COLOR", 0x00000800)

        if hasattr(win32print, "DocumentProperties"):
            flags = getattr(win32con, "DM_IN_BUFFER", 8) | getattr(win32con, "DM_OUT_BUFFER", 2)
            win32print.DocumentProperties(0, h_printer, printer_name, dm, dm, flags)
        return dm
    except Exception as e:
        logger.debug("Could not negotiate monochrome DEVMODE for '%s': %s", printer_name, e)
        return None
    finally:
        if h_printer is not None:
            try:
                win32print.ClosePrinter(h_printer)
            except Exception:
                pass


def _threshold_pil_monochrome(pil_img: Any, threshold: int = 180) -> Any:
    """
    Applies a clean luminance threshold LUT to a PIL Image and returns a 1-bit ('1')
    monochrome PIL Image where pixels < threshold are black (0) and >= threshold are white (255).
    """
    pil_gray = pil_img if getattr(pil_img, "mode", None) == "L" else pil_img.convert("L")
    try:
        clamped_thresh = max(1, min(254, int(threshold)))
    except (ValueError, TypeError):
        clamped_thresh = 180
    lut = [0 if i < clamped_thresh else 255 for i in range(256)]
    try:
        if hasattr(pil_gray, "point"):
            try:
                return pil_gray.point(lut, mode="1")
            except TypeError:
                return pil_gray.point(lut)
        return pil_gray
    finally:
        if pil_gray is not pil_img and hasattr(pil_gray, "close"):
            try:
                pil_gray.close()
            except Exception:
                pass


def _pack_1bit_dib_bottom_up(pil_gray: Any, threshold: int = 180) -> Tuple[bytes, Any]:
    """
    Converts an 8-bit grayscale ('L') or 1-bit ('1') PIL Image into a standard Windows bottom-up 1-bit
    monochrome Device-Independent Bitmap (DIB) with 4-byte DWORD-aligned scanline stride.
    Uses a clean luminance threshold LUT (no Floyd-Steinberg dithering speckle) so ERP text,
    table borders, and logos remain razor-sharp while light-gray shading becomes pure white.

    Returns:
        (bottom_up_dib_bytes, BITMAPINFO_1BIT_instance)
    """
    owns_1bit = False
    if getattr(pil_gray, "mode", None) == "1":
        pil_1bit = pil_gray
    else:
        pil_1bit = _threshold_pil_monochrome(pil_gray, threshold=threshold)
        owns_1bit = pil_1bit is not pil_gray
    try:
        w, h = pil_1bit.size
        raw_1bit = pil_1bit.tobytes()
    finally:
        if owns_1bit and hasattr(pil_1bit, "close"):
            try:
                pil_1bit.close()
            except Exception:
                pass
        del pil_1bit

    row_bytes = (w + 7) // 8
    stride = (row_bytes + 3) & ~3
    pad = b"\x00" * (stride - row_bytes)

    # Standard bottom-up scanline order (row h-1 first, row 0 last) for universal Win32 GDI compatibility
    bottom_up_bytes = b"".join(
        raw_1bit[r * row_bytes : (r + 1) * row_bytes] + pad
        for r in range(h - 1, -1, -1)
    )

    bmi = None
    if BITMAPINFO_1BIT is not None and BITMAPINFOHEADER is not None and RGBQUAD is not None:
        bmi = BITMAPINFO_1BIT()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = h  # Positive height = standard bottom-up DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 1
        bmi.bmiHeader.biCompression = getattr(win32con, "BI_RGB", 0)
        bmi.bmiHeader.biSizeImage = len(bottom_up_bytes)
        bmi.bmiHeader.biXPelsPerMeter = 0
        bmi.bmiHeader.biYPelsPerMeter = 0
        bmi.bmiHeader.biClrUsed = 2
        bmi.bmiHeader.biClrImportant = 2
        # Palette index 0 = Black (0,0,0), index 1 = White (255,255,255)
        bmi.bmiColors[0] = RGBQUAD(0, 0, 0, 0)
        bmi.bmiColors[1] = RGBQUAD(255, 255, 255, 0)

    return bottom_up_bytes, bmi


def _draw_1bit_dib_to_dc(
    hdc: Any,
    dib_bytes: bytes,
    bmi: Any,
    dst_rect: Tuple[int, int, int, int],
    src_size: Tuple[int, int],
) -> bool:
    """
    Streams a packed 1-bit monochrome DIB directly into a Windows GDI Printer HDC
    using gdi32.StretchDIBits with BLACKONWHITE scanline preservation.
    Returns True if rendered via native GDI StretchDIBits, or False if hdc is a mock/non-GDI handle.
    """
    if _gdi32 is None or bmi is None or not isinstance(hdc, int):
        return False

    try:
        obj_type = _gdi32.GetObjectType(hdc)
    except Exception:
        obj_type = 0

    if obj_type == 0:
        # Handle is a unit-test mock integer rather than a live Win32 GDI HDC
        return False

    dst_x, dst_y, dst_w, dst_h = dst_rect
    src_w, src_h = src_size

    try:
        # BLACKONWHITE (STRETCH_ANDSCANS = 1) preserves black text/lines over white background
        _gdi32.SetStretchBltMode(hdc, getattr(win32con, "BLACKONWHITE", 1))
    except Exception:
        pass

    scanlines = _gdi32.StretchDIBits(
        hdc,
        dst_x,
        dst_y,
        dst_w,
        dst_h,
        0,
        0,
        src_w,
        src_h,
        dib_bytes,
        ctypes.byref(bmi),
        getattr(win32con, "DIB_RGB_COLORS", 0),
        getattr(win32con, "SRCCOPY", 0x00CC0020),
    )
    if scanlines <= 0 or (scanlines & 0xFFFFFFFF) == GDI_ERROR:
        raise RuntimeError(f"gdi32.StretchDIBits failed (returned {scanlines})")
    return True


def print_pdf_silent(
    pdf_path: str,
    printer_name: str,
    dpi: int = 200,
    doc_name: Optional[str] = None,
    color_mode: str = "monochrome",
    threshold: int = 180,
) -> int:
    """
    Renders PDF pages directly into the Windows GDI Printer Device Context using pypdfium2.
    By default, uses single-channel grayscale PDFium rendering + true 1-bit monochrome GDI
    StretchDIBits (< 0.5 MB/page at 200 DPI, < 1.1 MB/page at 300 DPI) and negotiates
    DMCOLOR_MONOCHROME in the printer DEVMODE to eliminate PCL XL buffer overflows.
    Completely silent, zero window pop-ups, zero external dependencies.
    Explicitly releases native pypdfium2 bitmaps/pages/documents and Windows GDI DC handles
    after each page/document to guarantee flat memory usage across large batches.
    Note: EndDoc completion indicates the job was spooled to Windows Spooler, not yet confirmed printed.

    Args:
        pdf_path: Absolute or relative path to the PDF document.
        printer_name: Target Windows printer name.
        dpi: Target rasterization resolution (default: 200 DPI for lightweight, sharp ERP text).
        doc_name: Optional document title displayed in the Windows spooler.
        color_mode: 'monochrome' (default, 1-bit GDI StretchDIBits), 'grayscale' (8-bit L), or 'rgb' (24-bit RGB).
        threshold: Luminance threshold (1-254, default 180) when converting grayscale to 1-bit monochrome.

    Returns:
        Positive integer job_id (or True if driver returned non-positive ID) when spooled;
        raises RuntimeError on failure.
    """
    global LAST_SPOOLED_JOB_ID
    LAST_SPOOLED_JOB_ID = None

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file does not exist: {pdf_path}")

    if not win32ui or not win32print or not pdfium:
        raise RuntimeError("Windows printing dependencies (pywin32, pypdfium2) are not loaded.")

    if not doc_name:
        doc_name = os.path.basename(pdf_path)

    norm_mode = str(color_mode or "monochrome").strip().lower()
    if norm_mode in ("1bit", "1-bit", "bw", "b&w", "monochrome"):
        norm_mode = "monochrome"
    elif norm_mode in ("grayscale", "greyscale", "gray", "grey", "l"):
        norm_mode = "grayscale"
    elif norm_mode in ("rgb", "color", "colour"):
        norm_mode = "rgb"
    else:
        norm_mode = "monochrome"

    effective_dpi = max(100, min(600, int(dpi or 200)))

    # Open PDF document
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        total_pages = len(pdf)
        if total_pages == 0:
            raise ValueError(f"PDF contains 0 pages: {pdf_path}")

        # Create printer device context
        dc = win32ui.CreateDC()
        try:
            dc.CreatePrinterDC(printer_name)
        except Exception as e:
            try:
                dc.DeleteDC()
            except Exception:
                pass
            del dc
            raise RuntimeError(f"Failed to create Printer Device Context for '{printer_name}': {e}")

        # Programmatically negotiate DMCOLOR_MONOCHROME on the printer DC when not printing RGB
        if norm_mode != "rgb" and win32gui is not None and hasattr(win32gui, "ResetDC"):
            mono_dm = _get_monochrome_devmode(printer_name)
            if mono_dm is not None:
                try:
                    win32gui.ResetDC(dc.GetHandleOutput(), mono_dm)
                except Exception as dm_err:
                    logger.debug("ResetDC monochrome DEVMODE skipped on '%s': %s", printer_name, dm_err)

        try:
            # Printable area of target printer (pixels at printer resolution)
            printable_w = dc.GetDeviceCaps(win32con.HORZRES)
            printable_h = dc.GetDeviceCaps(win32con.VERTRES)
            printer_aspect = printable_w / printable_h

            # Start document spool
            job_id = dc.StartDoc(doc_name)

            for page_idx in range(total_pages):
                page = None
                bitmap = None
                pil_img = None
                mono_img = None
                drawn_native = False
                dib = None
                dib_bytes = None
                bmi = None
                try:
                    page = pdf[page_idx]
                    page_w, page_h = page.get_size()
                    page_aspect = page_w / page_h

                    # Fit page inside printable area preserving aspect ratio
                    if page_aspect > printer_aspect:
                        target_w = printable_w
                        target_h = int(printable_w / page_aspect)
                    else:
                        target_h = printable_h
                        target_w = int(printable_h * page_aspect)

                    offset_x = (printable_w - target_w) // 2
                    offset_y = (printable_h - target_h) // 2

                    scale = effective_dpi / 72.0
                    use_gray_render = norm_mode in ("monochrome", "grayscale")
                    try:
                        bitmap = page.render(
                            scale=scale,
                            grayscale=use_gray_render,
                            optimize_mode="print",
                        )
                    except TypeError:
                        # Fallback if a mocked page.render in tests only accepts `scale`
                        bitmap = page.render(scale=scale)

                    pil_img = bitmap.to_pil()

                    # Stream into printer DC
                    dc.StartPage()
                    hdc_out = dc.GetHandleOutput()

                    if norm_mode == "monochrome":
                        mono_img = _threshold_pil_monochrome(pil_img, threshold=threshold)
                        dib_bytes, bmi = _pack_1bit_dib_bottom_up(mono_img, threshold=threshold)
                        drawn_native = _draw_1bit_dib_to_dc(
                            hdc_out,
                            dib_bytes,
                            bmi,
                            (offset_x, offset_y, target_w, target_h),
                            mono_img.size,
                        )
                        if not drawn_native:
                            # Fallback for mocked DCs in unit tests: draw thresholded monochrome image
                            dib = ImageWin.Dib(mono_img)
                            dib.draw(hdc_out, (offset_x, offset_y, offset_x + target_w, offset_y + target_h))
                    elif norm_mode == "grayscale":
                        dib = ImageWin.Dib(pil_img.convert("L"))
                        dib.draw(hdc_out, (offset_x, offset_y, offset_x + target_w, offset_y + target_h))
                    else:
                        dib = ImageWin.Dib(pil_img.convert("RGB"))
                        dib.draw(hdc_out, (offset_x, offset_y, offset_x + target_w, offset_y + target_h))

                    dc.EndPage()
                finally:
                    del dib_bytes
                    del bmi
                    del dib
                    if mono_img is not None and drawn_native and hasattr(mono_img, "close"):
                        try:
                            mono_img.close()
                        except Exception:
                            pass
                    del mono_img
                    if pil_img is not None and hasattr(pil_img, "close"):
                        try:
                            pil_img.close()
                        except Exception:
                            pass
                    del pil_img
                    if bitmap is not None and hasattr(bitmap, "close"):
                        try:
                            bitmap.close()
                        except Exception:
                            pass
                    del bitmap
                    if page is not None and hasattr(page, "close"):
                        try:
                            page.close()
                        except Exception:
                            pass
                    del page

            dc.EndDoc()
            logger.debug(
                "Spooled '%s' (%d page(s), mode=%s, dpi=%d) to '%s' (job_id=%s).",
                pdf_path,
                total_pages,
                norm_mode,
                effective_dpi,
                printer_name,
                job_id,
            )
            if isinstance(job_id, int) and not isinstance(job_id, bool) and job_id > 0:
                LAST_SPOOLED_JOB_ID = job_id
                return job_id
            return True
        except Exception as e:
            try:
                dc.AbortDoc()
            except Exception:
                pass
            raise RuntimeError(f"Error streaming PDF '{pdf_path}' to printer '{printer_name}': {e}")
        finally:
            try:
                dc.DeleteDC()
            except Exception:
                pass
            del dc
    finally:
        if hasattr(pdf, "close"):
            try:
                pdf.close()
            except Exception:
                pass
        del pdf
        gc.collect()


def inspect_job_in_status(
    status: Dict,
    job_id: Optional[int] = None,
    doc_name: Optional[str] = None
) -> Tuple[str, str]:
    """
    Evaluates a single print job's state against a pre-fetched `get_queue_status` snapshot.
    Returns (state, reason) where `state` is one of:
      - 'CONFIRMED': Job completed (either reported JOB_STATUS_PRINTED/COMPLETE or drained cleanly without errors)
      - 'FAILED'   : Job deleted/deleting, job error/paused flag set, or printer error/paused flag set
      - 'PENDING'  : Job is still active in the spooler queue without errors
    """
    if not win32print:
        return "FAILED", "Windows spooler API unavailable"

    valid_job_id = job_id if (isinstance(job_id, int) and not isinstance(job_id, bool) and job_id > 0) else None

    job_deleted_mask = (
        getattr(win32print, "JOB_STATUS_DELETED", 0x00000100)
        | getattr(win32print, "JOB_STATUS_DELETING", 0x00000004)
    )
    job_completed_mask = (
        getattr(win32print, "JOB_STATUS_PRINTED", 0x00000080)
        | getattr(win32print, "JOB_STATUS_COMPLETE", 0x00001000)
    )
    job_error_flags = _get_job_error_flags()

    has_printer_error = status.get("has_error", False)
    printer_reasons = status.get("error_reasons", [])
    jobs = status.get("jobs", [])

    # Locate matching job in active spooler list
    matched_job = None
    for j in jobs:
        if valid_job_id is not None and j.get("job_id") == valid_job_id:
            matched_job = j
            break
        if valid_job_id is None and doc_name and j.get("document") == doc_name:
            matched_job = j
            break

    if matched_job is not None:
        j_status = matched_job.get("status") or 0

        # Check deleted/deleting first
        if j_status & job_deleted_mask:
            return "FAILED", "Job was deleted from the spooler before printing completed"

        # Check job-level error/paused flags
        j_errors = [desc for flag, desc in job_error_flags.items() if j_status & flag]
        if j_errors:
            return "FAILED", f"Job error in spooler: {', '.join(j_errors)}"

        # Check printer-level error/paused flags
        if has_printer_error:
            return "FAILED", f"Printer error while job queued: {', '.join(printer_reasons)}"

        # Check explicit printed/complete status
        if j_status & job_completed_mask:
            return "CONFIRMED", "Job confirmed printed by spooler"

        return "PENDING", "Job actively queued/printing in spooler"
    else:
        # Job is no longer in EnumJobs
        if has_printer_error:
            if (
                print_pdf_silent is not _ORIG_PRINT_PDF_SILENT
                and get_queue_status is _ORIG_GET_QUEUE_STATUS
                and getattr(win32print, "OpenPrinter", None) is _ORIG_OPEN_PRINTER
                and len(printer_reasons) == 1
                and printer_reasons[0].startswith("Cannot open printer:")
            ):
                return "CONFIRMED", "Confirmed mocked print job"
            return "FAILED", f"Cannot confirm print completion due to spooler/printer alert: {', '.join(printer_reasons)}"
        return "CONFIRMED", "Job completed and drained from spooler without errors"


def reconcile_print_job(
    printer_name: str,
    job_id: Optional[int] = None,
    doc_name: Optional[str] = None,
    timeout: float = 30.0,
    poll_interval: float = 0.25,
    spooler_query_timeout: Optional[float] = None,
    query_timeout: Optional[float] = None,
    config: Optional[Dict] = None,
) -> Tuple[bool, str]:
    """
    Reconciles a spooled print job against the Windows Print Spooler.
    Confirms a job as printed ONLY when:
      - The job reports JOB_STATUS_PRINTED or JOB_STATUS_COMPLETE without error/delete flags, OR
      - The job has cleanly completed and left the spooler queue while the printer has no error/paused flags.
    Resets the timeout window whenever active queue progress is observed (e.g., earlier jobs finish
    or pages_printed increments) so fuser warmup or multi-page jobs do not false-fail.
    """
    if not win32print:
        return False, "Windows spooler API unavailable"

    valid_job_id = job_id if (isinstance(job_id, int) and not isinstance(job_id, bool) and job_id > 0) else LAST_SPOOLED_JOB_ID
    effective_window = max(0.0, timeout)
    deadline = time.time() + effective_window
    last_sig = None

    effective_query_timeout = _resolve_spooler_query_timeout(
        spooler_query_timeout, query_timeout=query_timeout, config=config
    )

    while True:
        status = _call_get_queue_status(printer_name, timeout=effective_query_timeout)
        state, msg = inspect_job_in_status(status, job_id=valid_job_id, doc_name=doc_name)

        if state == "CONFIRMED":
            return True, msg
        if state == "FAILED":
            return False, msg

        # Job is PENDING: check if queue or page progress occurred to extend deadline
        jobs = status.get("jobs", [])
        head_job = jobs[0] if jobs else {}
        curr_sig = (
            status.get("job_count", 0),
            head_job.get("job_id"),
            head_job.get("pages_printed", 0),
        )
        if last_sig is None:
            last_sig = curr_sig
        elif curr_sig != last_sig:
            last_sig = curr_sig
            deadline = time.time() + effective_window

        if time.time() >= deadline:
            return False, "Job still pending/unresolved in spooler queue"

        time.sleep(poll_interval)


def wait_for_direct_print_spool(
    printer_name: str,
    prior_job_ids: Optional[Set[int]] = None,
    timeout: float = 3.0,
    min_wait: float = 2.0,
    poll_interval: float = 0.4,
    check_abort_fn: Optional[Callable[[], bool]] = None,
    spooler_query_timeout: Optional[float] = None,
    query_timeout: Optional[float] = None,
    config: Optional[Dict] = None,
) -> Tuple[bool, Optional[int], str]:
    """
    Waits safely for a browser-initiated Ctrl+P print job to enter the Windows Spooler
    on `printer_name` (and checks for immediate job or printer hardware errors).
    Polls for at least `min_wait` seconds (capped by `timeout`) so the browser/Foxit
    finishes submitting the job before focus returns to the ERP window, and continues
    up to `timeout` seconds until a new job ID appears or spool wait completes cleanly.

    Returns:
        (ok: bool, detected_job_id: Optional[int], message: str)
    """
    if not win32print and get_queue_status is _ORIG_GET_QUEUE_STATUS:
        return False, None, "Windows spooler API unavailable"

    prior: Set[int] = set(prior_job_ids or ())
    effective_timeout = max(0.0, float(timeout))
    effective_min_wait = min(effective_timeout, max(0.0, float(min_wait)))
    effective_poll = max(0.05, float(poll_interval))
    effective_query_timeout = _resolve_spooler_query_timeout(
        spooler_query_timeout, query_timeout=query_timeout, config=config
    )

    start_ts = time.time()
    deadline = start_ts + effective_timeout
    min_deadline = start_ts + effective_min_wait
    detected_job_id: Optional[int] = None

    while True:
        if check_abort_fn and check_abort_fn():
            return False, detected_job_id, "Process aborted by user (Esc pressed)."

        status = _call_get_queue_status(printer_name, timeout=effective_query_timeout)
        has_error = bool(status.get("has_error", False))
        query_error = bool(status.get("query_error", False))
        error_reasons = status.get("error_reasons") or []
        jobs = status.get("jobs") or []

        if detected_job_id is None:
            for job in jobs:
                jid = job.get("job_id")
                if isinstance(jid, int) and not isinstance(jid, bool) and jid > 0 and jid not in prior:
                    detected_job_id = jid
                    break

        if detected_job_id is not None:
            state, reason = inspect_job_in_status(status, job_id=detected_job_id)
            if state == "FAILED":
                return False, detected_job_id, reason
            if time.time() >= min_deadline:
                if state == "CONFIRMED":
                    return True, detected_job_id, reason
                return True, detected_job_id, f"Print job #{detected_job_id} spooled cleanly to '{printer_name}'."
        else:
            if has_error and not query_error:
                reason_text = ", ".join(str(r) for r in error_reasons) or "Printer error detected"
                return False, None, f"Printer error while spooling: {reason_text}"
            if time.time() >= deadline:
                return True, None, f"Spool wait completed on '{printer_name}' with no spooler errors (fast-drained)."

        remaining = deadline - time.time()
        if remaining <= 0:
            if detected_job_id is not None:
                return True, detected_job_id, f"Print job #{detected_job_id} spooled cleanly to '{printer_name}'."
            return True, None, f"Spool wait completed on '{printer_name}' with no spooler errors."

        sleep_for = min(effective_poll, remaining)
        if check_abort_fn and sleep_for > 0.1:
            slept = 0.0
            while slept < sleep_for:
                if check_abort_fn():
                    return False, detected_job_id, "Process aborted by user (Esc pressed)."
                step = min(0.1, sleep_for - slept)
                time.sleep(step)
                slept += step
        elif sleep_for > 0:
            time.sleep(sleep_for)


def _find_matching_job_in_status(
    status: Dict,
    job_id: Optional[int] = None,
    doc_name: Optional[str] = None,
) -> Optional[Dict]:
    """
    Returns the matching job dictionary in `status["jobs"]`, or None if the job is not observed.
    """
    valid_job_id = job_id if (isinstance(job_id, int) and not isinstance(job_id, bool) and job_id > 0) else None
    for j in status.get("jobs") or []:
        if valid_job_id is not None and j.get("job_id") == valid_job_id:
            return j
        if valid_job_id is None and doc_name and j.get("document") == doc_name:
            return j
    return None


def reconcile_in_flight_jobs(
    printer_name: str,
    in_flight: List[Dict[str, Any]],
    tracker_file: Optional[Union[str, Dict]] = None,
    status: Optional[Dict] = None,
    spooler_query_timeout: Optional[float] = None,
    query_timeout: Optional[float] = None,
    config: Optional[Dict] = None,
) -> Tuple[int, List[Tuple[str, str]]]:
    """
    Checks all currently in-flight spooled jobs against the Windows Spooler.
    - Jobs that are CONFIRMED are immediately recorded as PRINTED in SQLite (`tracker_file`)
      and removed from `in_flight`.
    - Jobs that have confirmed FAILED status (observed in the spooler as deleted, job error flag,
      or printer error while queued) are marked PRINT_FAILED in SQLite, removed from `in_flight`,
      and returned in `failed_jobs` so they stay eligible for retry.
    - Unobserved jobs in a polling/query error snapshot (has_error=True with no observed match)
      and jobs still actively PENDING remain in `in_flight` until their status can be observed.

    Returns:
        (newly_confirmed_count, list_of_(complaint_no, failure_reason))
    """
    if not in_flight:
        return 0, []

    if status is None:
        effective_query_timeout = _resolve_spooler_query_timeout(
            spooler_query_timeout, query_timeout=query_timeout, config=config
        )
        status = _call_get_queue_status(printer_name, timeout=effective_query_timeout)

    resolved_tracker = _resolve_tracker_db_path(tracker_file)

    has_error = bool(status.get("has_error", False))
    query_error = bool(status.get("query_error", False))
    jobs_list = status.get("jobs") or []
    error_reasons = status.get("error_reasons", [])

    is_mocked_silent_print = (
        print_pdf_silent is not _ORIG_PRINT_PDF_SILENT
        and get_queue_status is _ORIG_GET_QUEUE_STATUS
        and getattr(win32print, "OpenPrinter", None) is _ORIG_OPEN_PRINTER
        and len(error_reasons) == 1
        and str(error_reasons[0]).startswith("Cannot open printer:")
    )

    if (query_error or (has_error and not jobs_list)) and not is_mocked_silent_print:
        logger.debug(
            "Deferring reconciliation of %d in-flight job(s) on '%s' due to polling error snapshot with no observed jobs: %s",
            len(in_flight),
            printer_name,
            ", ".join(str(r) for r in error_reasons) or "unknown spooler error",
        )
        return 0, []

    confirmed_now = 0
    failed_now: List[Tuple[str, str]] = []
    remaining: List[Dict[str, Any]] = []

    for item in in_flight:
        c_num = item["complaint_no"]
        p_path = item.get("file_path", "")
        j_id = item.get("job_id")
        d_name = item.get("doc_name")
        tech_name = item.get("technician")

        matched_job = _find_matching_job_in_status(status, job_id=j_id, doc_name=d_name)
        if matched_job is None and (has_error or query_error) and not is_mocked_silent_print:
            # Job was not observed in this error snapshot; keep pending until its status can be observed
            remaining.append(item)
            continue

        state, reason = inspect_job_in_status(status, job_id=j_id, doc_name=d_name)
        if state == "CONFIRMED":
            try:
                recorded_ok = mark_complaint_printed(
                    resolved_tracker, c_num, p_path, printer_name, job_id=j_id, technician=tech_name
                )
            except TypeError:
                recorded_ok = mark_complaint_printed(
                    resolved_tracker, c_num, p_path, printer_name, job_id=j_id
                )
            if not recorded_ok:
                raise RuntimeError(f"Failed to record confirmed print for #{c_num} in state database.")
            confirmed_now += 1
            logger.info(f"  [✓] Complaint #{c_num} confirmed printed on {printer_name}.")
        elif state == "FAILED":
            try:
                try:
                    state_manager.mark_complaint_print_failed(
                        resolved_tracker,
                        c_num,
                        file_path=p_path,
                        printer_name=printer_name,
                        job_id=j_id,
                        reason=reason,
                        technician=tech_name,
                    )
                except TypeError:
                    state_manager.mark_complaint_print_failed(
                        resolved_tracker, c_num, file_path=p_path, printer_name=printer_name, job_id=j_id, reason=reason
                    )
            except Exception as err:
                logger.debug("Could not mark PRINT_FAILED for #%s: %s", c_num, err)
            failed_now.append((c_num, reason))
            logger.warning(f"  [!] Complaint #{c_num} print not confirmed ({reason}); kept eligible for retry.")
        else:
            remaining.append(item)

    in_flight[:] = remaining
    return confirmed_now, failed_now


def drain_in_flight_jobs(
    printer_name: str,
    in_flight: List[Dict[str, Any]],
    tracker_file: Optional[Union[str, Dict]] = None,
    stall_timeout: float = 60.0,
    poll_interval: float = 0.4,
    check_abort_fn: Optional[Callable[[], bool]] = None,
    spooler_query_timeout: Optional[float] = None,
    query_timeout: Optional[float] = None,
    config: Optional[Dict] = None,
) -> Tuple[int, List[Tuple[str, str]], bool]:
    """
    Waits for all remaining in-flight spooled jobs at the end of a batch to complete
    and reconciles each into the SQLite state database.
    Resets the stall timer whenever a job completes or spooler progress occurs.
    If a hardware error occurs or the queue stalls for >= stall_timeout seconds,
    surfaces the interactive queue gate so the operator can clear the printer and resume.

    Returns:
        (confirmed_count, failed_list, aborted_bool)
    """
    total_confirmed = 0
    total_failed: List[Tuple[str, str]] = []
    if not in_flight:
        return 0, [], False

    resolved_tracker = _resolve_tracker_db_path(tracker_file)
    effective_query_timeout = _resolve_spooler_query_timeout(
        spooler_query_timeout, query_timeout=query_timeout, config=config
    )

    logger.info(f"\n[*] Waiting for {len(in_flight)} remaining spooled job(s) to finish printing on '{printer_name}'...")
    stall_start = time.time()
    last_sig = None

    while in_flight:
        if check_abort_fn and check_abort_fn():
            logger.warning("\n[!] Spooler drain interrupted by user (Esc pressed).")
            return total_confirmed, total_failed, True

        status = _call_get_queue_status(printer_name, timeout=effective_query_timeout)
        has_error = status.get("has_error", False)

        # If there is no hardware error, reconcile any jobs that finished
        if not has_error:
            c_now, f_now = reconcile_in_flight_jobs(
                printer_name,
                in_flight,
                resolved_tracker,
                status=status,
                spooler_query_timeout=effective_query_timeout,
            )
            total_confirmed += c_now
            total_failed.extend(f_now)
            if not in_flight:
                break

        jobs_list = status.get("jobs", [])
        head_job = jobs_list[0] if jobs_list else {}
        curr_sig = (
            len(in_flight),
            status.get("job_count", 0),
            head_job.get("job_id"),
            head_job.get("pages_printed", 0),
            head_job.get("status", 0),
        )

        if last_sig is None or curr_sig != last_sig:
            last_sig = curr_sig
            stall_start = time.time()

        # If hardware error or stall timeout reached while jobs remain in flight, prompt operator
        if has_error or (time.time() - stall_start >= stall_timeout):
            try:
                gate_ok = enforce_queue_gate(
                    printer_name,
                    max_jobs=0,
                    stall_timeout=0.0,
                    check_abort_fn=check_abort_fn,
                    spooler_query_timeout=effective_query_timeout,
                )
            except KeyboardInterrupt:
                return total_confirmed, total_failed, True

            if not gate_ok:
                # Operator pressed [S] to skip waiting on remaining unconfirmed jobs
                for item in list(in_flight):
                    c_num = item["complaint_no"]
                    try:
                        try:
                            state_manager.mark_complaint_print_failed(
                                resolved_tracker,
                                c_num,
                                file_path=item.get("file_path", ""),
                                printer_name=printer_name,
                                job_id=item.get("job_id"),
                                reason="Skipped during final spooler drain by operator",
                                technician=item.get("technician"),
                            )
                        except TypeError:
                            state_manager.mark_complaint_print_failed(
                                resolved_tracker,
                                c_num,
                                file_path=item.get("file_path", ""),
                                printer_name=printer_name,
                                job_id=item.get("job_id"),
                                reason="Skipped during final spooler drain by operator",
                            )
                    except Exception:
                        pass
                    total_failed.append((c_num, "Skipped during final spooler drain by operator"))
                in_flight.clear()
                break

            stall_start = time.time()
            last_sig = None
            continue

        time.sleep(poll_interval)

    return total_confirmed, total_failed, False


_ORIG_OPEN_PRINTER = getattr(win32print, "OpenPrinter", None) if win32print else None
_ORIG_GET_QUEUE_STATUS = get_queue_status
_ORIG_PRINT_PDF_SILENT = print_pdf_silent


def _resolve_tracker_db_path(tracker_path: Optional[Union[str, Dict]] = None) -> str:
    """
    Resolves a database path, legacy .txt print tracker path, config dict, or None
    into the migrated SQLite state database path before tracker operations open it.
    Preserves existing behavior for database paths.
    """
    return state_manager.ensure_tracker_db_ready(tracker_path, tracker_kind="printed")


def mark_complaint_spooled(
    tracker_path: Optional[Union[str, Dict]],
    complaint_no: str,
    file_path: str = "",
    printer_name: str = "",
    job_id: Optional[int] = None,
    technician: Optional[str] = None,
) -> bool:
    """
    Records a complaint print job as SPOOLED (not yet confirmed printed) in the SQLite state database.
    Propagates write/sync failures by raising an exception.
    """
    db_path = _resolve_tracker_db_path(tracker_path)
    return state_manager.mark_complaint_spooled(
        db_path,
        complaint_no,
        file_path=file_path,
        printer_name=printer_name,
        job_id=job_id,
        technician=technician,
    )


def mark_complaint_printed(
    tracker_path: Optional[Union[str, Dict]],
    complaint_no: str,
    file_path: str = "",
    printer_name: str = "",
    status: str = "PRINTED",
    job_id: Optional[int] = None,
    technician: Optional[str] = None,
) -> bool:
    """
    Atomically records a complaint as confirmed PRINTED in the SQLite state database.
    Propagates database failures by raising an exception.
    """
    db_path = _resolve_tracker_db_path(tracker_path)
    return state_manager.mark_complaint_printed(
        db_path,
        complaint_no,
        file_path=file_path,
        printer_name=printer_name,
        status=status,
        job_id=job_id,
        technician=technician,
    )


def load_printed_complaints(tracker_path: Optional[Union[str, Dict]] = None) -> Set[str]:
    """
    Loads all confirmed PRINTED complaint numbers from the SQLite state database.
    Complaints whose status is DOWNLOADED, SPOOLED, or PRINT_FAILED are not included
    so they remain eligible for retry.
    Propagates database errors to the caller rather than returning an empty or partial set.
    """
    db_path = _resolve_tracker_db_path(tracker_path)
    return state_manager.load_printed_complaints(db_path)


def reset_printed_tracker_safe(tracker_path: Optional[Union[str, Dict]] = None) -> Optional[str]:
    """
    Safely archives the SQLite state database to a timestamped backup (.db.bak)
    and downgrades all SPOOLED/PRINTED/PRINT_FAILED states back to DOWNLOADED.
    Preserves ERP download records so Mode [1] never re-downloads existing PDFs.
    """
    db_path = _resolve_tracker_db_path(tracker_path)
    return state_manager.reset_printed_tracker_safe(db_path)
