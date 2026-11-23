import os
import sys
import time
import re

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
import config_manager
import logger_manager
import state_manager

logger = logger_manager.get_logger("engine")

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


def generate_otp(complaint_no, config=None):
    """
    Executes the automated OTP Generation workflow inside Oracle Forms:
    1. Click 'Add' button to open the OTP module.
    2. Click 3-dots next to Complaint No. to open search module.
    3. Double-click search input, then paste complaint number via pyperclip.
    4. Press Enter to search/select, then Enter again to confirm/close search module.
    5. Click 3-dots next to OTP Reason (click once to focus window, wait 1s, click 3-dots).
    6. Press Enter 4 times with micro-delays to confirm reason and return to primary screen.

    Returns (success: bool, message: str).
    """
    if config is None:
        config = config_manager.load_config()
        if config is None:
            config = config_manager.DEFAULT_CONFIG

    coords = config.get("coordinates", config_manager.DEFAULT_CONFIG["coordinates"])
    timings = config.get("timings", config_manager.DEFAULT_CONFIG["timings"])

    num = str(complaint_no).strip()
    if not num:
        return False, "Complaint number cannot be empty."

    # Early abort check
    if check_abort():
        return False, "OTP generation aborted by user (Esc pressed)."

    try:
        # Step 1: Open OTP module by clicking "Add" button
        logger.info("\n[OTP] Step 1/6: Opening OTP module (Click 'Add')...")
        add_pt = coords.get("otp_add_btn", {"x": 200, "y": 200})
        pyautogui.click(add_pt["x"], add_pt["y"])
        time.sleep(timings.get("otp_window_open_seconds", 1.5))

        if check_abort():
            return False, "OTP generation aborted by user (Esc pressed)."

        # Step 2: Open Complaint search module by clicking 3-dots icon
        logger.info("[OTP] Step 2/6: Opening Complaint lookup (Click 3-dots)...")
        search_btn = coords.get("otp_complaint_search_btn", {"x": 350, "y": 250})
        pyautogui.click(search_btn["x"], search_btn["y"])
        time.sleep(timings.get("otp_search_modal_seconds", 1.2))

        if check_abort():
            return False, "OTP generation aborted by user (Esc pressed)."

        # Step 3: Double-click search input to highlight text and paste complaint number
        logger.info(f"[OTP] Step 3/6: Highlighting and pasting Complaint #{num}...")
        input_pt = coords.get("otp_search_input", {"x": 500, "y": 300})
        pyautogui.doubleClick(input_pt["x"], input_pt["y"])
        time.sleep(0.2)
        pyperclip.copy(num)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)

        if check_abort():
            return False, "OTP generation aborted by user (Esc pressed)."

        # Step 4: Press Enter to search/select, then Enter again to confirm/close
        logger.info("[OTP] Step 4/6: Selecting complaint and closing lookup (Enter x2)...")
        enter_delay = timings.get("otp_enter_delay_seconds", 0.3)
        pyautogui.press('enter')
        time.sleep(enter_delay)
        pyautogui.press('enter')
        time.sleep(timings.get("otp_window_open_seconds", 1.5))

        if check_abort():
            return False, "OTP generation aborted by user (Esc pressed)."

        # Step 5: Click 3-dots next to OTP Reason (focus click, delay, then activate click)
        logger.info("[OTP] Step 5/6: Focusing and opening OTP Reason lookup (Double-tap)...")
        reason_btn = coords.get("otp_reason_search_btn", {"x": 350, "y": 350})
        # Focus click
        pyautogui.click(reason_btn["x"], reason_btn["y"])
        time.sleep(timings.get("otp_reason_focus_seconds", 1.0))

        if check_abort():
            return False, "OTP generation aborted by user (Esc pressed)."

        # Activate click
        pyautogui.click(reason_btn["x"], reason_btn["y"])
        time.sleep(timings.get("otp_search_modal_seconds", 1.2))

        if check_abort():
            return False, "OTP generation aborted by user (Esc pressed)."

        # Step 6: Press Enter 4 times with micro-delays to confirm reason and return
        logger.info("[OTP] Step 6/6: Confirming OTP reason and committing (Enter x4)...")
        for i in range(1, 5):
            if check_abort():
                return False, "OTP generation aborted by user (Esc pressed)."
            pyautogui.press('enter')
            time.sleep(enter_delay)

        time.sleep(timings.get("otp_window_open_seconds", 1.5))
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
