"""
printer_manager.py
Modular Print Manager for Universal Silent Printing & Windows Spooler Queue Monitoring.
Uses in-process headless GDI rendering (pypdfium2 + win32ui) for zero-popup physical printing.
"""

import os
import sys
import time
import winsound
from datetime import datetime
from typing import List, Dict, Optional, Set, Tuple

if sys.platform == "win32":
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


def get_installed_printers() -> List[str]:
    """
    Enumerates all local and network-connected printers installed in Windows.
    Returns:
        List of printer names as strings.
    """
    if not win32print:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    printers = win32print.EnumPrinters(flags)
    return [p[2] for p in printers]


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
        p_info = win32print.GetPrinter(h_printer, 2)
        c_jobs = p_info.get("cJobs", 0)
        p_status = p_info.get("Status", 0)

        # Enumerate individual spooler jobs
        try:
            jobs = win32print.EnumJobs(h_printer, 0, 999, 2)
        except Exception:
            jobs = ()

        has_error = False
        error_reasons = []

        # Printer-level status flags
        printer_error_flags = {
            win32print.PRINTER_STATUS_PAPER_JAM: "Paper Jam",
            win32print.PRINTER_STATUS_PAPER_OUT: "Paper Out / Tray Empty",
            win32print.PRINTER_STATUS_PAPER_PROBLEM: "Paper Problem",
            win32print.PRINTER_STATUS_OFFLINE: "Printer Offline",
            win32print.PRINTER_STATUS_ERROR: "Printer Error",
            win32print.PRINTER_STATUS_DOOR_OPEN: "Cover / Door Open",
            win32print.PRINTER_STATUS_USER_INTERVENTION: "User Intervention Required",
            win32print.PRINTER_STATUS_NOT_AVAILABLE: "Printer Not Available"
        }
        for flag, desc in printer_error_flags.items():
            if p_status & flag:
                has_error = True
                error_reasons.append(desc)

        # Job-level status flags
        job_error_flags = {
            win32print.JOB_STATUS_ERROR: "Job Error",
            win32print.JOB_STATUS_PAPEROUT: "Job Paused - Paper Out",
            win32print.JOB_STATUS_OFFLINE: "Job Paused - Printer Offline",
            win32print.JOB_STATUS_BLOCKED_DEVQ: "Job Blocked in Spooler",
            win32print.JOB_STATUS_USER_INTERVENTION: "User Intervention Required"
        }
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
                    "pages": j.get("TotalPages")
                }
                for j in jobs
            ]
        }
    finally:
        win32print.ClosePrinter(h_printer)


def enforce_queue_gate(printer_name: str, max_jobs: int = 3) -> bool:
    """
    Hardware Failsafe Pre-Dispatch Gate.
    Monitors the Windows Print Spooler. If queue >= max_jobs or a hardware error is detected,
    pauses execution, emits audio beeps, and prompts the user until resolved.

    Returns:
        True: Queue is healthy, safe to dispatch print job.
        False: Operator elected to skip this print job.
    """
    while True:
        status = get_queue_status(printer_name)
        job_count = status.get("job_count", 0)
        has_error = status.get("has_error", False)
        error_reasons = status.get("error_reasons", [])

        # Queue is healthy if jobs < max_jobs and no hardware error
        if job_count < max_jobs and not has_error:
            return True

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
            print(" Issues Detected  : Queue capacity saturated (Jobs pending processing)")
        print("\n Possible Causes:")
        print("  - Physical paper jam or cover door open")
        print("  - Printer tray out of paper")
        print("  - Printer turned off or offline")
        print("=" * 65)
        print(" [Enter] Re-check queue and resume once hardware is cleared")
        print(" [S]     Skip printing this complaint (mark unprinted)")
        print(" [Q]     Abort entire batch operation")
        print("=" * 65)

        user_choice = input(" Select action ([Enter] / S / Q): ").strip().lower()
        if user_choice == "s":
            print(" [i] Skipping print for this complaint.")
            return False
        elif user_choice == "q":
            print(" [!] Batch operation aborted by operator.")
            raise KeyboardInterrupt("Print queue operation aborted by user.")

        print(" [i] Re-checking printer status...")
        time.sleep(1.0)


def print_pdf_silent(pdf_path: str, printer_name: str, dpi: int = 300, doc_name: Optional[str] = None) -> bool:
    """
    Renders PDF pages directly into the Windows GDI Printer Device Context using pypdfium2.
    Completely silent, zero window pop-ups, zero external dependencies.

    Args:
        pdf_path: Absolute or relative path to the PDF document.
        printer_name: Target Windows printer name.
        dpi: Target rasterization resolution (default: 300 DPI for high-quality text/graphics).
        doc_name: Optional document title displayed in the Windows spooler.

    Returns:
        True if successfully queued to Windows spooler, raises RuntimeError on failure.
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF file does not exist: {pdf_path}")

    if not win32ui or not win32print or not pdfium:
        raise RuntimeError("Windows printing dependencies (pywin32, pypdfium2) are not loaded.")

    if not doc_name:
        doc_name = os.path.basename(pdf_path)

    # Open PDF document
    pdf = pdfium.PdfDocument(pdf_path)
    total_pages = len(pdf)
    if total_pages == 0:
        raise ValueError(f"PDF contains 0 pages: {pdf_path}")

    # Create printer device context
    dc = win32ui.CreateDC()
    try:
        dc.CreatePrinterDC(printer_name)
    except Exception as e:
        raise RuntimeError(f"Failed to create Printer Device Context for '{printer_name}': {e}")

    try:
        # Printable area of target printer (pixels at printer resolution)
        printable_w = dc.GetDeviceCaps(win32con.HORZRES)
        printable_h = dc.GetDeviceCaps(win32con.VERTRES)
        printer_aspect = printable_w / printable_h

        # Start document spool
        dc.StartDoc(doc_name)

        for page_idx in range(total_pages):
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

        dc.EndDoc()
        return True
    except Exception as e:
        try:
            dc.AbortDoc()
        except Exception:
            pass
        raise RuntimeError(f"Error streaming PDF '{pdf_path}' to printer '{printer_name}': {e}")
    finally:
        del dc


def load_printed_complaints(tracker_path: str = "printed_complaints.txt") -> Set[str]:
    """
    Loads all previously printed complaint numbers from the tracker file.
    Returns:
        Set of complaint number strings.
    """
    if not os.path.exists(tracker_path):
        return set()

    printed = set()
    try:
        with open(tracker_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Split by comma or pipe if metadata format is used
                token = line.split("|")[0].split(",")[0].strip()
                if token:
                    printed.add(token)
    except Exception as e:
        print(f" [!] Warning reading {tracker_path}: {e}")
    return printed


def mark_complaint_printed(
    tracker_path: str,
    complaint_no: str,
    file_path: str = "",
    printer_name: str = ""
) -> None:
    """
    Atomically records a complaint as printed in the tracker file.
    """
    complaint_no = str(complaint_no).strip()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_name = os.path.basename(file_path) if file_path else "N/A"
    printer_label = printer_name or "Default"

    # Line format: COMPLAINT_NO | TIMESTAMP | PRINTER | FILE
    log_entry = f"{complaint_no} | {timestamp} | {printer_label} | {file_name}\n"

    try:
        with open(tracker_path, "a", encoding="utf-8") as f:
            f.write(log_entry)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f" [!] Warning: Failed to write to {tracker_path}: {e}")
