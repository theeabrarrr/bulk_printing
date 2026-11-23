import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config_manager
import engine
import main as app_main
import printer_manager
import state_manager


def _make_test_config(base_dir: str, printer_name: str = "HP LaserJet 400 M401 PCL 6") -> dict:
    cfg = {
        "coordinates": {
            "complaint_input": {"x": 100, "y": 100},
            "main_print_btn": {"x": 200, "y": 200},
            "ha_field_checkbox": {"x": 300, "y": 300},
            "popup_print_btn": {"x": 400, "y": 400},
            "popup_exit_btn": {"x": 500, "y": 500},
        },
        "timings": {
            "focus_delay": 0.0,
            "record_load_seconds": 0.0,
            "popup_wait_seconds": 0.0,
            "checkbox_wait_seconds": 0.0,
            "browser_pdf_load_seconds": 0.0,
            "foxit_print_dialog_seconds": 0.0,
            "direct_print_spool_seconds": 0.05,
            "save_dialog_seconds": 0.0,
            "file_save_processing_seconds": 0.0,
            "window_switch_seconds": 0.0,
            "popup_close_seconds": 0.0,
        },
        "settings": {
            "excel_path": os.path.join(base_dir, "complaints.xlsx"),
            "complaint_column": "COMPLAINT_NO",
            "technician_column": "TECHNICIAN_NAME",
            "output_dir": os.path.join(base_dir, "Today_Complaints"),
            "progress_file": os.path.join(base_dir, "completed_complaints.txt"),
            "database_file": os.path.join(base_dir, "complaints_state.db"),
        },
        "printing": {
            "printer_name": printer_name,
            "max_spool_queue": 3,
            "enable_live_print": False,
            "printed_tracker_file": os.path.join(base_dir, "printed_complaints.txt"),
            "print_dpi": 200,
            "color_mode": "monochrome",
            "monochrome_threshold": 180,
            "spooler_query_timeout": 1.0,
        },
    }
    return cfg


class TestDefaultPrinterGuard(unittest.TestCase):
    def test_temporary_default_printer_switches_and_restores_on_exit_and_error(self):
        calls = []
        current_default = ["Microsoft Print to PDF"]

        def fake_get_default():
            return current_default[0]

        def fake_set_default(name):
            calls.append(name)
            current_default[0] = name
            return True

        with patch.object(printer_manager, "get_default_printer", side_effect=fake_get_default), \
             patch.object(printer_manager, "set_default_printer", side_effect=fake_set_default), \
             patch.object(
                 printer_manager,
                 "get_installed_printers",
                 return_value=["Microsoft Print to PDF", "HP LaserJet 400 M401 PCL 6"],
             ):
            with printer_manager.temporary_default_printer("HP LaserJet 400 M401 PCL 6") as switched:
                self.assertTrue(switched)
                self.assertEqual(current_default[0], "HP LaserJet 400 M401 PCL 6")

            self.assertEqual(current_default[0], "Microsoft Print to PDF")
            self.assertEqual(calls, ["HP LaserJet 400 M401 PCL 6", "Microsoft Print to PDF"])

            calls.clear()
            with self.assertRaises(RuntimeError):
                with printer_manager.temporary_default_printer("HP LaserJet 400 M401 PCL 6"):
                    self.assertEqual(current_default[0], "HP LaserJet 400 M401 PCL 6")
                    raise RuntimeError("boom")

            self.assertEqual(current_default[0], "Microsoft Print to PDF")
            self.assertEqual(calls, ["HP LaserJet 400 M401 PCL 6", "Microsoft Print to PDF"])

    def test_temporary_default_printer_noop_when_already_default_or_missing(self):
        with patch.object(printer_manager, "get_default_printer", return_value="Microsoft Print to PDF"), \
             patch.object(printer_manager, "set_default_printer") as mock_set, \
             patch.object(printer_manager, "get_installed_printers", return_value=["Microsoft Print to PDF"]):
            with printer_manager.temporary_default_printer("Microsoft Print to PDF") as switched:
                self.assertFalse(switched)
            with printer_manager.temporary_default_printer("Nonexistent Printer") as switched2:
                self.assertFalse(switched2)
            mock_set.assert_not_called()


class TestWaitForDirectPrintSpool(unittest.TestCase):
    def test_detects_new_job_id_ignoring_prior_jobs(self):
        snapshots = [
            {"printer_name": "HP", "job_count": 1, "has_error": False, "query_error": False, "error_reasons": [], "jobs": [{"job_id": 10, "status": 0}]},
            {"printer_name": "HP", "job_count": 2, "has_error": False, "query_error": False, "error_reasons": [], "jobs": [{"job_id": 10, "status": 0}, {"job_id": 11, "status": 0}]},
        ]
        idx = [0]

        def fake_status(printer_name, timeout=5.0):
            s = snapshots[min(idx[0], len(snapshots) - 1)]
            idx[0] += 1
            return s

        with patch.object(printer_manager, "get_queue_status", side_effect=fake_status):
            ok, job_id, msg = printer_manager.wait_for_direct_print_spool(
                "HP", prior_job_ids={10}, timeout=0.3, min_wait=0.05, poll_interval=0.05
            )
        self.assertTrue(ok)
        self.assertEqual(job_id, 11)
        self.assertIn("11", msg)

    def test_fast_drained_job_succeeds_cleanly(self):
        clean_status = {"printer_name": "HP", "job_count": 0, "has_error": False, "query_error": False, "error_reasons": [], "jobs": []}
        with patch.object(printer_manager, "get_queue_status", return_value=clean_status):
            ok, job_id, _ = printer_manager.wait_for_direct_print_spool(
                "HP", prior_job_ids=set(), timeout=0.1, min_wait=0.05, poll_interval=0.05
            )
        self.assertTrue(ok)
        self.assertIsNone(job_id)

    def test_printer_hardware_error_fails_fast(self):
        err_status = {
            "printer_name": "HP",
            "job_count": 0,
            "has_error": True,
            "query_error": False,
            "error_reasons": ["Paper Out / Tray Empty"],
            "jobs": [],
        }
        with patch.object(printer_manager, "get_queue_status", return_value=err_status):
            ok, job_id, msg = printer_manager.wait_for_direct_print_spool(
                "HP", prior_job_ids=set(), timeout=0.3, min_wait=0.05, poll_interval=0.05
            )
        self.assertFalse(ok)
        self.assertIsNone(job_id)
        self.assertIn("Paper Out", msg)

    def test_transient_query_error_does_not_false_fail(self):
        q_err_status = {
            "printer_name": "HP",
            "job_count": 0,
            "has_error": True,
            "query_error": True,
            "error_reasons": ["Spooler API timed out"],
            "jobs": [],
        }
        with patch.object(printer_manager, "get_queue_status", return_value=q_err_status):
            ok, job_id, _ = printer_manager.wait_for_direct_print_spool(
                "HP", prior_job_ids=set(), timeout=0.1, min_wait=0.05, poll_interval=0.05
            )
        self.assertTrue(ok)
        self.assertIsNone(job_id)


class TestStateResetDirectVsDownloaded(unittest.TestCase):
    def test_reset_deletes_direct_print_rows_and_downgrades_pdf_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "complaints_state.db")
            pdf_path = os.path.join(tmpdir, "Today_Complaints", "Ali", "1001.pdf")
            os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
            with open(pdf_path, "wb") as f:
                f.write(b"%PDF-1.4\n%test\n")

            state_manager.init_db(db_path, output_dir=os.path.join(tmpdir, "Today_Complaints"), sync_disk=False)
            state_manager.mark_complaint_downloaded(db_path, "1001", technician="Ali", file_path=pdf_path)
            state_manager.mark_complaint_printed(db_path, "1001", file_path=pdf_path, printer_name="HP", job_id=7, technician="Ali")
            state_manager.mark_complaint_printed(db_path, "2002", file_path="", printer_name="HP", job_id=8, technician="Bilal")
            state_manager.mark_complaint_spooled(db_path, "2003", file_path="", printer_name="HP", job_id=9, technician="Bilal")

            backup_path = state_manager.reset_printed_tracker_safe(db_path)
            self.assertIsNotNone(backup_path)
            self.assertTrue(os.path.exists(backup_path))

            rec_pdf = state_manager.get_complaint_record("1001", db_path)
            self.assertIsNotNone(rec_pdf)
            self.assertEqual(rec_pdf["status"], "DOWNLOADED")
            self.assertIsNone(rec_pdf["job_id"])

            self.assertIsNone(state_manager.get_complaint_record("2002", db_path))
            self.assertIsNone(state_manager.get_complaint_record("2003", db_path))

            bconn = sqlite3.connect(backup_path)
            try:
                bconn.row_factory = sqlite3.Row
                rows = {r["complaint_id"]: r["status"] for r in bconn.execute("SELECT complaint_id, status FROM complaints")}
            finally:
                bconn.close()
            self.assertEqual(rows, {"1001": "PRINTED", "2002": "PRINTED", "2003": "SPOOLED"})


class TestEngineDirectAndSavePdfModes(unittest.TestCase):
    def test_save_pdf_mode_backward_compatible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_test_config(tmpdir)
            copied = []

            def fake_copy(val):
                copied.append(val)

            def fake_hotkey(*keys):
                pass

            def fake_press(key):
                # When Save As Enter is pressed, simulate PDF written by Microsoft Print to PDF
                if key == "enter" and copied:
                    target_pdf = copied[-1]
                    os.makedirs(os.path.dirname(target_pdf), exist_ok=True)
                    with open(target_pdf, "wb") as f:
                        f.write(b"%PDF-1.4\n%saved\n")

            with patch.object(engine.pyautogui, "click"), \
                 patch.object(engine.pyautogui, "doubleClick"), \
                 patch.object(engine.pyautogui, "write"), \
                 patch.object(engine.pyautogui, "press", side_effect=fake_press), \
                 patch.object(engine.pyautogui, "hotkey", side_effect=fake_hotkey), \
                 patch.object(engine.pyperclip, "copy", side_effect=fake_copy), \
                 patch.object(engine, "check_abort", return_value=False), \
                 patch.object(engine.time, "sleep"):
                ok, msg = engine.process_complaint("12345", "Ameer Hamza", config=cfg, record_progress=True, direct_print=False)

            self.assertTrue(ok, msg)
            expected_pdf = os.path.abspath(os.path.join(tmpdir, "Today_Complaints", "Ameer Hamza", "12345.pdf"))
            self.assertEqual(copied, [expected_pdf])
            rec = state_manager.get_complaint_record("12345", cfg)
            self.assertEqual(rec["status"], "DOWNLOADED")
            self.assertEqual(rec["file_path"], expected_pdf)

    def test_direct_print_mode_no_pdf_save_and_tracks_in_flight_to_printed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_test_config(tmpdir, printer_name="HP LaserJet 400 M401 PCL 6")
            db_path = cfg["settings"]["database_file"]
            state_manager.init_db(db_path, output_dir=cfg["settings"]["output_dir"], sync_disk=False)
            # Pre-mark one complaint already printed; should be skipped in direct print AND in save PDF mode
            state_manager.mark_complaint_printed(db_path, "1001", file_path="", printer_name="HP LaserJet 400 M401 PCL 6", technician="TechA")

            hotkeys = []
            default_switches = []
            current_default = ["Microsoft Print to PDF"]

            def fake_get_default():
                return current_default[0]

            def fake_set_default(name):
                default_switches.append(name)
                current_default[0] = name
                return True

            # Queue status transitions: before Ctrl+P empty; during wait job 77 active; during drain job 77 completed
            status_calls = [0]

            def fake_queue_status(printer_name, timeout=5.0):
                status_calls[0] += 1
                if status_calls[0] <= 2:
                    return {"printer_name": printer_name, "job_count": 0, "has_error": False, "query_error": False, "error_reasons": [], "jobs": []}
                if status_calls[0] <= 5:
                    return {
                        "printer_name": printer_name,
                        "job_count": 1,
                        "has_error": False,
                        "query_error": False,
                        "error_reasons": [],
                        "jobs": [{"job_id": 77, "document": "Complaint 1002", "status": 0, "pages": 1, "pages_printed": 0}],
                    }
                return {"printer_name": printer_name, "job_count": 0, "has_error": False, "query_error": False, "error_reasons": [], "jobs": []}

            with patch.object(printer_manager, "get_default_printer", side_effect=fake_get_default), \
                 patch.object(printer_manager, "set_default_printer", side_effect=fake_set_default), \
                 patch.object(printer_manager, "get_installed_printers", return_value=["Microsoft Print to PDF", "HP LaserJet 400 M401 PCL 6"]), \
                 patch.object(printer_manager, "classify_printer", side_effect=lambda n: "VIRTUAL" if "PDF" in n else "PHYSICAL"), \
                 patch.object(printer_manager, "get_queue_status", side_effect=fake_queue_status), \
                 patch.object(engine.pyautogui, "click"), \
                 patch.object(engine.pyautogui, "doubleClick"), \
                 patch.object(engine.pyautogui, "write"), \
                 patch.object(engine.pyautogui, "press"), \
                 patch.object(engine.pyautogui, "hotkey", side_effect=lambda *k: hotkeys.append(k)), \
                 patch.object(engine.pyperclip, "copy") as mock_clip, \
                 patch.object(engine, "check_abort", return_value=False), \
                 patch.object(engine.time, "sleep"), \
                 patch.object(printer_manager.time, "sleep"):
                stats = engine.run_batch(
                    [("1001", "TechA"), ("1002", "TechB")],
                    config=cfg,
                    start_delay=0,
                    direct_print=True,
                    target_printer="HP LaserJet 400 M401 PCL 6",
                )

            self.assertEqual(stats["total"], 2)
            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(stats["completed"], 1)
            self.assertEqual(stats["failed"], 0)
            self.assertFalse(stats["aborted"])
            mock_clip.assert_not_called()
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Today_Complaints", "TechB", "1002.pdf")))
            self.assertEqual(default_switches, ["HP LaserJet 400 M401 PCL 6", "Microsoft Print to PDF"])
            self.assertEqual(current_default[0], "Microsoft Print to PDF")
            self.assertIn(("ctrl", "p"), hotkeys)
            self.assertIn(("alt", "tab"), hotkeys)
            rec_1002 = state_manager.get_complaint_record("1002", cfg)
            self.assertEqual(rec_1002["status"], "PRINTED")
            self.assertEqual(rec_1002["technician"], "TechB")
            self.assertEqual(rec_1002["file_path"], "")

            # Re-running in Save PDF mode also skips direct-printed complaint 1002
            with patch.object(printer_manager, "get_default_printer", side_effect=fake_get_default), \
                 patch.object(printer_manager, "set_default_printer", side_effect=fake_set_default), \
                 patch.object(printer_manager, "get_installed_printers", return_value=["Microsoft Print to PDF", "HP LaserJet 400 M401 PCL 6"]), \
                 patch.object(engine, "process_complaint") as mock_proc:
                save_stats = engine.run_batch([("1002", "TechB")], config=cfg, start_delay=0, direct_print=False)
            self.assertEqual(save_stats["skipped"], 1)
            mock_proc.assert_not_called()

    def test_direct_print_existing_disk_pdf_and_spool_failure_recording(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_test_config(tmpdir, printer_name="HP LaserJet 400 M401 PCL 6")
            existing_pdf = os.path.join(tmpdir, "Today_Complaints", "TechA", "3001.pdf")
            os.makedirs(os.path.dirname(existing_pdf), exist_ok=True)
            with open(existing_pdf, "wb") as f:
                f.write(b"%PDF-1.4\n%existing\n")

            with patch.object(printer_manager, "get_default_printer", return_value="Microsoft Print to PDF"), \
                 patch.object(printer_manager, "set_default_printer", return_value=True), \
                 patch.object(printer_manager, "get_installed_printers", return_value=["Microsoft Print to PDF", "HP LaserJet 400 M401 PCL 6"]), \
                 patch.object(printer_manager, "classify_printer", return_value="PHYSICAL"), \
                 patch.object(engine, "_dispatch_live_print", return_value=(True, "ok")) as mock_live, \
                 patch.object(engine, "process_complaint") as mock_proc:
                stats = engine.run_batch([("3001", "TechA")], config=cfg, start_delay=0, direct_print=True)
            self.assertEqual(stats["completed"], 1)
            mock_live.assert_called_once()
            mock_proc.assert_not_called()

            # Now simulate spool failure during process_complaint direct_print=True
            with patch.object(printer_manager, "classify_printer", return_value="PHYSICAL"), \
                 patch.object(printer_manager, "enforce_queue_gate", return_value=True), \
                 patch.object(printer_manager, "get_queue_status", return_value={"jobs": [], "has_error": False, "query_error": False, "error_reasons": []}), \
                 patch.object(printer_manager, "wait_for_direct_print_spool", return_value=(False, None, "Paper Jam")), \
                 patch.object(engine.pyautogui, "click"), \
                 patch.object(engine.pyautogui, "doubleClick"), \
                 patch.object(engine.pyautogui, "write"), \
                 patch.object(engine.pyautogui, "press"), \
                 patch.object(engine.pyautogui, "hotkey"), \
                 patch.object(engine, "check_abort", return_value=False), \
                 patch.object(engine.time, "sleep"):
                ok, msg = engine.process_complaint("3002", "TechA", config=cfg, record_progress=True, direct_print=True)
            self.assertFalse(ok)
            self.assertIn("Paper Jam", msg)
            rec = state_manager.get_complaint_record("3002", cfg)
            self.assertEqual(rec["status"], "PRINT_FAILED")
            self.assertEqual(rec["technician"], "TechA")

    def test_direct_print_in_flight_spooled_then_drained_to_printed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_test_config(tmpdir, printer_name="HP LaserJet 400 M401 PCL 6")
            cfg["timings"]["direct_print_spool_seconds"] = 0.01
            db_path = cfg["settings"]["database_file"]
            state_manager.init_db(db_path, output_dir=cfg["settings"]["output_dir"], sync_disk=False)

            active_snap = {
                "printer_name": "HP LaserJet 400 M401 PCL 6",
                "job_count": 1,
                "has_error": False,
                "query_error": False,
                "error_reasons": [],
                "jobs": [{"job_id": 88, "document": "Complaint 5001", "status": 0, "pages": 1, "pages_printed": 0}],
            }
            empty_snap = {
                "printer_name": "HP LaserJet 400 M401 PCL 6",
                "job_count": 0,
                "has_error": False,
                "query_error": False,
                "error_reasons": [],
                "jobs": [],
            }
            # 1: gate empty, 2: prior snapshot empty, 3-4: wait_for_direct_print_spool sees job 88,
            # 5: cur_status in process_complaint sees job 88 still PENDING -> SPOOLED + in_flight,
            # 6+: drain_in_flight_jobs sees empty -> CONFIRMED -> PRINTED
            snapshots = [empty_snap, empty_snap, active_snap, active_snap, active_snap, empty_snap]
            idx = [0]

            def fake_q(printer_name, timeout=5.0):
                s = snapshots[min(idx[0], len(snapshots) - 1)]
                idx[0] += 1
                return s

            seen_statuses = []
            orig_spooled = state_manager.mark_complaint_spooled

            def spy_spooled(*args, **kwargs):
                seen_statuses.append("SPOOLED")
                return orig_spooled(*args, **kwargs)

            with patch.object(printer_manager, "get_default_printer", return_value="Microsoft Print to PDF"), \
                 patch.object(printer_manager, "set_default_printer", return_value=True), \
                 patch.object(printer_manager, "get_installed_printers", return_value=["Microsoft Print to PDF", "HP LaserJet 400 M401 PCL 6"]), \
                 patch.object(printer_manager, "classify_printer", return_value="PHYSICAL"), \
                 patch.object(printer_manager, "get_queue_status", side_effect=fake_q), \
                 patch.object(state_manager, "mark_complaint_spooled", side_effect=spy_spooled), \
                 patch.object(engine.pyautogui, "click"), \
                 patch.object(engine.pyautogui, "doubleClick"), \
                 patch.object(engine.pyautogui, "write"), \
                 patch.object(engine.pyautogui, "press"), \
                 patch.object(engine.pyautogui, "hotkey"), \
                 patch.object(engine, "check_abort", return_value=False):
                stats = engine.run_batch(
                    [("5001", "TechZ")],
                    config=cfg,
                    start_delay=0,
                    direct_print=True,
                    target_printer="HP LaserJet 400 M401 PCL 6",
                )

            self.assertEqual(seen_statuses, ["SPOOLED"])
            self.assertEqual(stats["completed"], 1)
            self.assertEqual(stats["failed"], 0)
            rec = state_manager.get_complaint_record("5001", cfg)
            self.assertEqual(rec["status"], "PRINTED")
            self.assertEqual(rec["job_id"], 88)
            self.assertEqual(rec["technician"], "TechZ")
            self.assertEqual(rec["file_path"], "")


class TestMainCliWorkflowPrompt(unittest.TestCase):
    def test_bulk_mode_default_save_pdf_vs_direct_print_prompts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_test_config(tmpdir, printer_name="HP LaserJet 400 M401 PCL 6")
            pd.DataFrame([{"COMPLAINT_NO": "1001", "TECHNICIAN_NAME": "Tech1"}]).to_excel(
                cfg["settings"]["excel_path"], index=False
            )

            # Case 1: Enter -> Save PDF mode (1), Y organize, N live print
            with patch("builtins.input", side_effect=["", "y", "n"]), \
                 patch.object(printer_manager, "classify_printer", return_value="PHYSICAL"), \
                 patch.object(engine, "run_batch") as mock_batch:
                app_main.run_bulk_mode(cfg)
            mock_batch.assert_called_once()
            _, kwargs1 = mock_batch.call_args
            self.assertFalse(kwargs1.get("direct_print", False))
            self.assertTrue(kwargs1.get("organize_by_tech"))
            self.assertFalse(kwargs1.get("live_print"))

            # Case 2: '2' -> Direct Browser Print, Enter to confirm target printer
            with patch("builtins.input", side_effect=["2", ""]), \
                 patch.object(printer_manager, "classify_printer", return_value="PHYSICAL"), \
                 patch.object(printer_manager, "get_installed_printers", return_value=["HP LaserJet 400 M401 PCL 6"]), \
                 patch.object(engine, "run_batch") as mock_batch2:
                app_main.run_bulk_mode(cfg)
            mock_batch2.assert_called_once()
            _, kwargs2 = mock_batch2.call_args
            self.assertTrue(kwargs2.get("direct_print"))
            self.assertEqual(kwargs2.get("target_printer"), "HP LaserJet 400 M401 PCL 6")
            self.assertFalse(kwargs2.get("live_print"))

    def test_manual_mode_direct_print_dispatches_and_restores_default_printer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _make_test_config(tmpdir, printer_name="HP LaserJet 400 M401 PCL 6")
            switches = []
            cur_def = ["Microsoft Print to PDF"]

            def fake_get_def():
                return cur_def[0]

            def fake_set_def(name):
                switches.append(name)
                cur_def[0] = name
                return True

            with patch("builtins.input", side_effect=["4001 4002", "2", "", "TestBatch"]), \
                 patch.object(printer_manager, "get_default_printer", side_effect=fake_get_def), \
                 patch.object(printer_manager, "set_default_printer", side_effect=fake_set_def), \
                 patch.object(printer_manager, "get_installed_printers", return_value=["Microsoft Print to PDF", "HP LaserJet 400 M401 PCL 6"]), \
                 patch.object(printer_manager, "classify_printer", return_value="PHYSICAL"), \
                 patch.object(engine, "process_complaint", return_value=(True, "ok")) as mock_proc, \
                 patch.object(app_main.time, "sleep"):
                app_main.run_manual_mode(cfg)

            self.assertEqual(mock_proc.call_count, 2)
            for call in mock_proc.call_args_list:
                self.assertTrue(call.kwargs.get("direct_print"))
                self.assertFalse(call.kwargs.get("record_progress"))
                self.assertEqual(call.kwargs.get("target_printer"), "HP LaserJet 400 M401 PCL 6")
            self.assertEqual(switches, ["HP LaserJet 400 M401 PCL 6", "Microsoft Print to PDF"])
            self.assertEqual(cur_def[0], "Microsoft Print to PDF")


if __name__ == "__main__":
    unittest.main()
