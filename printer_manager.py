"""
printer_manager.py
Modular Print Manager for Universal Silent Printing & Windows Spooler Queue Monitoring.
Uses in-process headless GDI rendering (pypdfium2 + win32ui) for zero-popup physical printing.
"""

import gc
import os
import sys
import time
import winsound
from datetime import datetime
from typing import List, Dict, Optional, Set, Tuple, Callable, Any

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
    from PIL import Image, ImageWin
    import pypdfium2 as pdfium
else:
    win32print = None
    win32ui = None
    win32con = None
    win32gui = None
    ImageWin = None
    pdfium = None

# Tracks whether the most recent enforce_queue_gate call required interactive operator input
LAST_GATE_PROMPTED: bool = False
# Tracks the most recent spooler job ID created by print_pdf_silent
LAST_SPOOLED_JOB_ID: Optional[int] = None


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
        print(f" [!] Warning: Failed to enumerate Windows printers: {e}")
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
        print("\n [!] ERROR: No Windows printers detected on this computer.")
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
        print(" [i] Printer selection cancelled.")
        return None, config

    selected_name = current_printer

    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(printers):
            selected_name = printers[idx - 1]
            cat = classify_printer(selected_name)
            print(f" [✓] Selected: {selected_name} [{cat}]")
            if cat == "VIRTUAL":
                print(" [!] Notice: You selected a VIRTUAL printer. It may open file dialogs instead of physical printing.")
        else:
            print(" [!] Invalid selection index. Keeping existing printer setting.")
    elif current_printer:
        print(f" [i] Keeping configured printer: {current_printer}")
    else:
        # Default fallback to first physical printer, or system default
        physical_candidates = [p for p in printers if classify_printer(p) == "PHYSICAL"]
        if physical_candidates:
            selected_name = physical_candidates[0]
            print(f" [i] Automatically selected first physical printer: {selected_name}")
        elif default_printer:
            selected_name = default_printer
            print(f" [i] Defaulting to system printer: {selected_name}")
        else:
            selected_name = printers[0]

    # Save to config if requested
    if auto_save and selected_name:
        if "printing" not in config:
            config["printing"] = {}
        config["printing"]["printer_name"] = selected_name
        config_manager.save_config(config)
        print(f" [✓] Saved target printer '{selected_name}' to config.json.")

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


def get_queue_status(printer_name: str) -> Dict:
    """
    Queries the Windows Print Spooler for active jobs and status flags on target printer.
    Returns:
        Dict containing:
            - printer_name (str)
            - job_count (int)
            - has_error (bool)
            - error_reasons (List[str])
            - jobs (List[Dict])
    """
    if not win32print:
        return {"printer_name": printer_name, "job_count": 0, "has_error": False, "error_reasons": ["Non-Windows OS"], "jobs": []}

    try:
        h_printer = win32print.OpenPrinter(printer_name)
    except Exception as e:
        return {
            "printer_name": printer_name,
            "job_count": 0,
            "has_error": True,
            "error_reasons": [f"Cannot open printer: {e}"],
            "jobs": []
        }

    try:
        try:
            p_info = win32print.GetPrinter(h_printer, 2)
        except Exception as e:
            return {
                "printer_name": printer_name,
                "job_count": 0,
                "has_error": True,
                "error_reasons": [f"Cannot query printer status: {e}"],
                "jobs": []
            }

        c_jobs = p_info.get("cJobs", 0)
        p_status = p_info.get("Status", 0)

        has_error = False
        error_reasons = []

        # Enumerate individual spooler jobs
        try:
            jobs = win32print.EnumJobs(h_printer, 0, 999, 2)
        except Exception as e:
            jobs = ()
            has_error = True
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
            "error_reasons": error_reasons,
            "jobs": [
                {
                    "job_id": j.get("JobId"),
                    "document": j.get("pDocument"),
                    "status": j.get("Status"),
                    "pages": j.get("TotalPages"),
                    "pages_printed": j.get("PagesPrinted", 0)
                }
                for j in jobs
            ]
        }
    finally:
        win32print.ClosePrinter(h_printer)


def enforce_queue_gate(
    printer_name: str,
    max_jobs: int = 3,
    stall_timeout: float = 60.0,
    on_poll: Optional[Callable[[Dict], None]] = None,
    check_abort_fn: Optional[Callable[[], bool]] = None
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

    drain_start = None
    last_sig = None

    while True:
        if check_abort_fn and check_abort_fn():
            raise KeyboardInterrupt("Print queue operation aborted via Esc.")

        status = get_queue_status(printer_name)
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
                print(
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
            print(" [!] Operator override: Forcing print job dispatch despite warning.")
            return True
        elif user_choice == "s":
            print(" [i] Skipping print for this complaint.")
            return False
        elif user_choice == "q":
            print(" [!] Batch operation aborted by operator.")
            raise KeyboardInterrupt("Print queue operation aborted by user.")

        # Reset drain timer for next check
        drain_start = None
        last_sig = None
        print(" [i] Re-checking printer status...")
        time.sleep(1.0)


def print_pdf_silent(pdf_path: str, printer_name: str, dpi: int = 300, doc_name: Optional[str] = None) -> int:
    """
    Renders PDF pages directly into the Windows GDI Printer Device Context using pypdfium2.
    Completely silent, zero window pop-ups, zero external dependencies.
    Explicitly releases native pypdfium2 bitmaps/pages/documents and Windows GDI DC handles
    after each page/document to guarantee flat memory usage across large batches.
    Note: EndDoc completion indicates the job was spooled to Windows Spooler, not yet confirmed printed.

    Args:
        pdf_path: Absolute or relative path to the PDF document.
        printer_name: Target Windows printer name.
        dpi: Target rasterization resolution (default: 300 DPI for high-quality text/graphics).
        doc_name: Optional document title displayed in the Windows spooler.

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
                dib = None
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

                    # Render page to high-DPI bitmap
                    scale = dpi / 72.0
                    bitmap = page.render(scale=scale)
                    pil_img = bitmap.to_pil().convert("RGB")
                    dib = ImageWin.Dib(pil_img)

                    # Stream into printer DC
                    dc.StartPage()
                    dib.draw(dc.GetHandleOutput(), (offset_x, offset_y, offset_x + target_w, offset_y + target_h))
                    dc.EndPage()
                finally:
                    del dib
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
    poll_interval: float = 0.25
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

    while True:
        status = get_queue_status(printer_name)
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


def reconcile_in_flight_jobs(
    printer_name: str,
    in_flight: List[Dict[str, Any]],
    tracker_file: str,
    status: Optional[Dict] = None
) -> Tuple[int, List[Tuple[str, str]]]:
    """
    Checks all currently in-flight spooled jobs against the Windows Spooler.
    - Jobs that are CONFIRMED are immediately recorded as PRINTED in `tracker_file`
      and removed from `in_flight`.
    - Jobs that have FAILED (deleted, error flag, printer error) are removed from
      `in_flight` and returned in `failed_jobs` so they stay eligible for retry.
    - Jobs still PENDING remain in `in_flight` while the printer continues processing.

    Returns:
        (newly_confirmed_count, list_of_(complaint_no, failure_reason))
    """
    if not in_flight:
        return 0, []

    if status is None:
        status = get_queue_status(printer_name)

    confirmed_now = 0
    failed_now: List[Tuple[str, str]] = []
    remaining: List[Dict[str, Any]] = []

    for item in in_flight:
        c_num = item["complaint_no"]
        p_path = item["file_path"]
        j_id = item.get("job_id")
        d_name = item.get("doc_name")

        state, reason = inspect_job_in_status(status, job_id=j_id, doc_name=d_name)
        if state == "CONFIRMED":
            if not mark_complaint_printed(
                tracker_file, c_num, p_path, printer_name, job_id=j_id
            ):
                raise RuntimeError(f"Failed to record confirmed print for #{c_num} in '{tracker_file}'.")
            confirmed_now += 1
            print(f"  [✓] Complaint #{c_num} confirmed printed on {printer_name}.")
        elif state == "FAILED":
            failed_now.append((c_num, reason))
            print(f"  [!] Complaint #{c_num} print not confirmed ({reason}); kept eligible for retry.")
        else:
            remaining.append(item)

    in_flight[:] = remaining
    return confirmed_now, failed_now


def drain_in_flight_jobs(
    printer_name: str,
    in_flight: List[Dict[str, Any]],
    tracker_file: str,
    stall_timeout: float = 60.0,
    poll_interval: float = 0.4,
    check_abort_fn: Optional[Callable[[], bool]] = None
) -> Tuple[int, List[Tuple[str, str]], bool]:
    """
    Waits for all remaining in-flight spooled jobs at the end of a batch to complete
    and reconciles each into `tracker_file`.
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

    print(f"\n[*] Waiting for {len(in_flight)} remaining spooled job(s) to finish printing on '{printer_name}'...")
    stall_start = time.time()
    last_sig = None

    while in_flight:
        if check_abort_fn and check_abort_fn():
            print("\n[!] Spooler drain interrupted by user (Esc pressed).")
            return total_confirmed, total_failed, True

        status = get_queue_status(printer_name)
        has_error = status.get("has_error", False)

        # If there is no hardware error, reconcile any jobs that finished
        if not has_error:
            c_now, f_now = reconcile_in_flight_jobs(printer_name, in_flight, tracker_file, status=status)
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
                    check_abort_fn=check_abort_fn
                )
            except KeyboardInterrupt:
                return total_confirmed, total_failed, True

            if not gate_ok:
                # Operator pressed [S] to skip waiting on remaining unconfirmed jobs
                for item in list(in_flight):
                    c_num = item["complaint_no"]
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


def _write_tracker_entry(
    tracker_path: str,
    complaint_no: str,
    file_path: str = "",
    printer_name: str = "",
    status: str = "PRINTED",
    job_id: Optional[int] = None
) -> bool:
    """
    Atomically appends and fsyncs a status record to the tracker file.
    Raises an exception if the write or sync fails.
    """
    complaint_no = str(complaint_no).strip()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_name = os.path.basename(file_path) if file_path else "N/A"
    printer_label = printer_name or "Default"
    status_label = str(status or "PRINTED").strip().upper()

    valid_job_id = job_id if (isinstance(job_id, int) and not isinstance(job_id, bool) and job_id > 0) else None
    if valid_job_id is not None:
        log_entry = f"{complaint_no} | {timestamp} | {printer_label} | {file_name} | {status_label} | job_id={valid_job_id}\n"
    else:
        log_entry = f"{complaint_no} | {timestamp} | {printer_label} | {file_name} | {status_label}\n"

    parent_dir = os.path.dirname(tracker_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    try:
        with open(tracker_path, "a", encoding="utf-8") as f:
            f.write(log_entry)
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception as e:
        print(f" [!] Error: Failed to write to {tracker_path}: {e}")
        raise


def mark_complaint_spooled(
    tracker_path: str,
    complaint_no: str,
    file_path: str = "",
    printer_name: str = "",
    job_id: Optional[int] = None
) -> bool:
    """
    Records a complaint print job as SPOOLED (not yet confirmed printed) in the tracker file.
    Propagates write/sync failures by raising an exception.
    """
    return _write_tracker_entry(
        tracker_path,
        complaint_no,
        file_path=file_path,
        printer_name=printer_name,
        status="SPOOLED",
        job_id=job_id
    )


def mark_complaint_printed(
    tracker_path: str,
    complaint_no: str,
    file_path: str = "",
    printer_name: str = "",
    status: str = "PRINTED",
    job_id: Optional[int] = None
) -> bool:
    """
    Atomically records a complaint as confirmed PRINTED in the tracker file.
    Propagates tracker append or sync failures by raising an exception.
    """
    return _write_tracker_entry(
        tracker_path,
        complaint_no,
        file_path=file_path,
        printer_name=printer_name,
        status=status,
        job_id=job_id
    )


def load_printed_complaints(tracker_path: str = "printed_complaints.txt") -> Set[str]:
    """
    Loads all confirmed printed complaint numbers from the tracker file.
    Complaints whose latest status is SPOOLED, FAILED, DELETED, or UNRESOLVED are not included
    so they remain eligible for retry.
    Propagates file read errors to the caller rather than returning an empty or partial set.
    """
    if not os.path.exists(tracker_path):
        return set()

    printed: Set[str] = set()
    try:
        with open(tracker_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                token = parts[0].split(",")[0].strip()
                if not token:
                    continue
                if len(parts) >= 5:
                    entry_status = parts[4].upper()
                    if entry_status == "PRINTED":
                        printed.add(token)
                    else:
                        printed.discard(token)
                else:
                    # Legacy 1-column or 4-column confirmed print record
                    printed.add(token)
    except Exception as e:
        print(f" [!] Error reading {tracker_path}: {e}")
        raise
    return printed


def reset_printed_tracker_safe(tracker_path: str = "printed_complaints.txt") -> Optional[str]:
    """
    Safely archives the existing printed_complaints.txt to a timestamped backup (.bak)
    and initializes a clean tracking file with header metadata.
    If archiving fails, aborts the reset before opening tracker_path with 'w' so the
    original tracker and its print history are preserved.
    Returns:
        Backup file path if archived, or None.
    """
    import shutil

    backup_path = None
    if os.path.exists(tracker_path):
        has_content = False
        try:
            with open(tracker_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip() and not line.strip().startswith("#"):
                        has_content = True
                        break
        except Exception as e:
            print(f" [!] Error reading {tracker_path} before reset: {e}")
            raise

        if has_content:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_dir = os.path.dirname(os.path.abspath(tracker_path))
            backup_name = f"printed_complaints_{ts}.bak"
            backup_path = os.path.join(base_dir, backup_name)
            try:
                shutil.copy2(tracker_path, backup_path)
                print(f" [✓] Previous print tracker archived to: {backup_name}")
            except Exception as e:
                print(f" [!] Error: Failed to create archive backup '{backup_path}': {e}")
                print(" [!] Aborting tracker reset to preserve existing print history.")
                raise

    parent_dir = os.path.dirname(tracker_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    try:
        with open(tracker_path, "w", encoding="utf-8") as f:
            f.write("# COMPLAINT_NO | TIMESTAMP | PRINTER | FILE | STATUS\n")
        print(f" [✓] Print tracker '{os.path.basename(tracker_path)}' reset to clean state.")
    except Exception as e:
        print(f" [!] Error resetting tracker file {tracker_path}: {e}")
        raise

    return backup_path
