import io
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config_manager
import engine
import main


def _make_patch(val, size=(240, 160)):
    """Creates a synthetic grayscale PIL Image filled with intensity `val`."""
    return Image.new("L", size, color=int(val))


class TestOtpModeAndGuards(unittest.TestCase):
    def setUp(self):
        engine.reset_abort()

    def tearDown(self):
        engine.reset_abort()

    def test_timings_and_otp_guard_defaults_and_fallbacks(self):
        t_defaults = config_manager.get_timings_settings({})
        self.assertEqual(t_defaults["otp_start_countdown_seconds"], 3.0)
        self.assertEqual(t_defaults["otp_window_open_seconds"], 1.5)
        self.assertEqual(t_defaults["otp_search_modal_seconds"], 1.5)
        self.assertEqual(t_defaults["otp_input_focus_delay_seconds"], 0.3)
        self.assertEqual(t_defaults["otp_post_paste_delay_seconds"], 0.5)
        self.assertEqual(t_defaults["otp_query_wait_seconds"], 2.5)
        self.assertEqual(t_defaults["otp_modal_close_timeout_seconds"], 3.0)
        self.assertEqual(t_defaults["otp_post_select_delay_seconds"], 1.5)
        self.assertEqual(t_defaults["otp_reason_focus_seconds"], 1.0)
        self.assertEqual(t_defaults["otp_enter_delay_seconds"], 0.4)
        self.assertEqual(t_defaults["otp_between_complaints_seconds"], 1.5)

        malformed_cfg = {
            "timings": {
                "otp_query_wait_seconds": "invalid",
                "otp_post_paste_delay_seconds": None,
                "otp_enter_delay_seconds": True,
                "otp_modal_close_timeout_seconds": -5,
                "otp_start_countdown_seconds": 0,
                "otp_between_complaints_seconds": -2,
            },
            "settings": {
                "otp_verify_screen_transitions": "false",
                "otp_screen_diff_threshold": 9.9,
            },
        }
        t_clean = config_manager.get_timings_settings(malformed_cfg)
        self.assertEqual(t_clean["otp_query_wait_seconds"], 2.5)
        self.assertEqual(t_clean["otp_post_paste_delay_seconds"], 0.5)
        self.assertEqual(t_clean["otp_enter_delay_seconds"], 0.4)
        self.assertEqual(t_clean["otp_modal_close_timeout_seconds"], 3.0)
        self.assertEqual(t_clean["otp_start_countdown_seconds"], 0.0)
        self.assertEqual(t_clean["otp_between_complaints_seconds"], 1.5)

        g_clean = config_manager.get_otp_guard_settings(malformed_cfg)
        self.assertFalse(g_clean["otp_verify_screen_transitions"])
        self.assertEqual(g_clean["otp_screen_diff_threshold"], 0.03)

    def test_sanitize_otp_complaint_inputs(self):
        raw = "COMPLAINT_NO, 282634716.0  282634892, #REF! 282634716 282635000.00 #N/A"
        valid, invalid, dupes = main.sanitize_otp_complaint_inputs(raw)
        self.assertEqual(valid, ["282634716", "282634892", "282635000"])
        self.assertEqual(invalid, ["COMPLAINT_NO", "#REF!", "#N/A"])
        self.assertEqual(dupes, ["282634716"])

    def test_capture_region_patch_clamps_to_screen_bounds(self):
        captured_bboxes = []

        def fake_grab(bbox=None, all_screens=True):
            captured_bboxes.append(bbox)
            w = max(1, bbox[2] - bbox[0])
            h = max(1, bbox[3] - bbox[1])
            return Image.new("RGB", (w, h), color=(120, 120, 120))

        with patch("engine.pyautogui.size", return_value=(1366, 768)), patch(
            "engine.ImageGrab.grab", side_effect=fake_grab
        ):
            p_top_left = engine._capture_region_patch(10, 5, half_w=120, half_h=80)
            p_bottom_right = engine._capture_region_patch(1360, 765, half_w=120, half_h=80)

        self.assertIsNotNone(p_top_left)
        self.assertIsNotNone(p_bottom_right)
        self.assertEqual(len(captured_bboxes), 2)
        for left, top, right, bottom in captured_bboxes:
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, 1366)
            self.assertLessEqual(bottom, 768)
            self.assertGreater(right, left)
            self.assertGreater(bottom, top)

    def test_verify_erp_foreground_edge_ie_and_java_compatibility(self):
        expected_ctx = {
            "hwnd": 101,
            "pid": 4000,
            "process_name": "msedge.exe",
            "title": "Oracle Applications - Edge",
            "is_console": False,
        }

        # 1. Child Java Applet LOV window with different PID (jp2launcher.exe) -> ALLOWED
        java_child_ctx = {
            "hwnd": 202,
            "pid": 5555,
            "process_name": "jp2launcher.exe",
            "title": "Complaints",
            "is_console": False,
        }
        with patch("engine.win32gui", object()), patch("engine.win32process", object()), patch(
            "engine.capture_foreground_window_context", return_value=java_child_ctx
        ):
            ok, msg = engine.verify_erp_foreground(expected_ctx)
            self.assertTrue(ok, msg)

        # 2. Oracle Forms title match with unknown process name -> ALLOWED
        title_match_ctx = {
            "hwnd": 203,
            "pid": 6666,
            "process_name": "custom_host.exe",
            "title": "Oracle Forms Runtime",
            "is_console": False,
        }
        with patch("engine.win32gui", object()), patch("engine.win32process", object()), patch(
            "engine.capture_foreground_window_context", return_value=title_match_ctx
        ):
            ok, msg = engine.verify_erp_foreground(expected_ctx)
            self.assertTrue(ok, msg)

        # 3. Focus shifted to our CLI Console -> REJECTED
        console_ctx = {
            "hwnd": 303,
            "pid": 7777,
            "process_name": "powershell.exe",
            "title": "Windows PowerShell",
            "is_console": True,
        }
        with patch("engine.win32gui", object()), patch("engine.win32process", object()), patch(
            "engine.capture_foreground_window_context", return_value=console_ctx
        ):
            ok, msg = engine.verify_erp_foreground(expected_ctx)
            self.assertFalse(ok)
            self.assertIn("console", msg.lower())

        # 4. Focus shifted to File Explorer -> REJECTED
        explorer_ctx = {
            "hwnd": 404,
            "pid": 8888,
            "process_name": "explorer.exe",
            "title": "File Explorer",
            "is_console": False,
        }
        with patch("engine.win32gui", object()), patch("engine.win32process", object()), patch(
            "engine.capture_foreground_window_context", return_value=explorer_ctx
        ):
            ok, msg = engine.verify_erp_foreground(expected_ctx)
            self.assertFalse(ok)
            self.assertIn("lost focus", msg.lower())

    def test_generate_otp_happy_path_sequence_and_timings(self):
        # Sequence of synthetic patches returned by _capture_region_patch:
        # 1. main_screen_search_btn_patch (val=20)
        # 2. Step 1 poll at search_btn -> changed to OTP sub-window (val=80)
        # 3. Step 2 baseline subwindow_center_patch at input_pt (val=80)
        # 4. Step 2 baseline subwindow_reason_patch at reason_btn (val=90)
        # 5. Step 2 poll at input_pt -> changed to Complaint LOV open (val=160)
        # 6. Step 4 pre_enter2_lov_patch at input_pt (val=160)
        # 7. Step 4A poll at input_pt -> LOV closed back to subwindow (val=80)
        # 8. Step 4B poll at reason_btn -> matches subwindow_reason_patch (val=90)
        # 9. Step 5 pre_reason_center_patch at input_pt (val=80)
        # 10. Step 5 poll at input_pt -> Reason LOV opened (val=200)
        # 11. Step 6 poll at search_btn -> restored to main_screen_search_btn_patch (val=20)
        patches = [
            _make_patch(20),
            _make_patch(80),
            _make_patch(80),
            _make_patch(90),
            _make_patch(160),
            _make_patch(160),
            _make_patch(80),
            _make_patch(90),
            _make_patch(80),
            _make_patch(200),
            _make_patch(20),
        ]

        events = []
        sleeps = []

        def fake_sleep(sec, poll_interval=0.05):
            sleeps.append(round(float(sec), 2))
            events.append(("sleep", round(float(sec), 2)))
            return False

        cfg = config_manager.DEFAULT_CONFIG

        with patch("engine._capture_region_patch", side_effect=patches), patch(
            "engine.sleep_interruptible", side_effect=fake_sleep
        ), patch("engine.verify_erp_foreground", return_value=(True, "")), patch(
            "engine.pyautogui.click", side_effect=lambda x, y: events.append(("click", x, y))
        ), patch(
            "engine.pyautogui.doubleClick", side_effect=lambda x, y: events.append(("doubleClick", x, y))
        ), patch(
            "engine.pyautogui.hotkey", side_effect=lambda *k: events.append(("hotkey", k))
        ), patch(
            "engine.pyautogui.press", side_effect=lambda k: events.append(("press", k))
        ), patch(
            "engine.pyperclip.copy", side_effect=lambda v: events.append(("copy", v))
        ):
            ok, msg = engine.generate_otp("282634716", config=cfg)

        self.assertTrue(ok, msg)
        self.assertIn("282634716", msg)

        # Verify key ordering: paste -> post_paste_delay (0.5) -> Enter #1 -> query_wait (2.5) -> Enter #2
        action_only = [e for e in events if e[0] != "sleep"]
        self.assertEqual(
            action_only,
            [
                ("click", 200, 200),       # Step 1: Add
                ("click", 350, 250),       # Step 2: Complaint 3-dots
                ("doubleClick", 500, 300), # Step 3: Search input
                ("copy", "282634716"),
                ("hotkey", ("ctrl", "v")),
                ("press", "enter"),        # Step 4: Enter #1 (query)
                ("press", "enter"),        # Step 4: Enter #2 (select/close)
                ("click", 350, 350),       # Step 5: Reason focus click
                ("click", 350, 350),       # Step 5: Reason activate click
                ("press", "enter"),        # Step 6: Enter 1/4
                ("press", "enter"),        # Step 6: Enter 2/4
                ("press", "enter"),        # Step 6: Enter 3/4
                ("press", "enter"),        # Step 6: Enter 4/4
            ],
        )
        self.assertIn(2.5, sleeps)  # otp_query_wait_seconds
        self.assertIn(0.5, sleeps)  # otp_post_paste_delay_seconds
        self.assertEqual(sleeps.count(0.4), 4)  # 4x otp_enter_delay_seconds in Step 6

    def test_generate_otp_halts_when_complaint_lov_stays_stuck_at_step4(self):
        # Step 1 & 2 succeed, but after Enter #2 the patch at input_pt stays at val=160 (stuck LOV!)
        patches = [
            _make_patch(20),   # main_screen_search_btn_patch
            _make_patch(80),   # Step 1 changed
            _make_patch(80),   # subwindow_center_patch
            _make_patch(90),   # subwindow_reason_patch
            _make_patch(160),  # Step 2 Complaint LOV opened
            _make_patch(160),  # Step 4 pre_enter2_lov_patch
            _make_patch(160),  # Step 4A poll: still 160 (stuck!)
            _make_patch(160),  # Step 4A poll: still 160 (stuck!)
        ]

        clicks = []
        presses = []

        # Force timeout loop in _wait_for_region_change to expire after 1 poll
        clock = [0.0]

        def fake_monotonic():
            clock[0] += 2.0
            return clock[0]

        with patch("engine._capture_region_patch", side_effect=patches), patch(
            "engine.time.monotonic", side_effect=fake_monotonic
        ), patch("engine.sleep_interruptible", return_value=False), patch(
            "engine.verify_erp_foreground", return_value=(True, "")
        ), patch(
            "engine.pyautogui.click", side_effect=lambda x, y: clicks.append((x, y))
        ), patch(
            "engine.pyautogui.doubleClick"
        ), patch(
            "engine.pyautogui.hotkey"
        ), patch(
            "engine.pyautogui.press", side_effect=lambda k: presses.append(k)
        ), patch(
            "engine.pyperclip.copy"
        ):
            ok, msg = engine.generate_otp("282634716", config=config_manager.DEFAULT_CONFIG)

        self.assertFalse(ok)
        self.assertIn("Step 4/6", msg)
        # Must NOT have clicked Reason 3-dots (350, 350) or pressed Step 6 Enter x4
        self.assertNotIn((350, 350), clicks)
        self.assertEqual(len(presses), 2)  # Only Enter #1 and Enter #2 from Step 4

    def test_generate_otp_halts_when_oracle_alert_blocks_reason_lov_at_step5(self):
        # Step 1, 2, 4A, 4B succeed, but Step 5 double-tap on Reason 3-dots fails to open Reason LOV (patch stays 80)
        patches = [
            _make_patch(20),   # main_screen_search_btn_patch
            _make_patch(80),   # Step 1 changed
            _make_patch(80),   # subwindow_center_patch
            _make_patch(90),   # subwindow_reason_patch
            _make_patch(160),  # Step 2 Complaint LOV opened
            _make_patch(160),  # Step 4 pre_enter2_lov_patch
            _make_patch(80),   # Step 4A LOV closed
            _make_patch(90),   # Step 4B sub-window reason btn visible
            _make_patch(80),   # Step 5 pre_reason_center_patch
            _make_patch(80),   # Step 5 poll: unchanged (blocked by alert!)
            _make_patch(80),   # Step 5 poll: unchanged
        ]

        presses = []
        clock = [0.0]

        def fake_monotonic():
            clock[0] += 1.5
            return clock[0]

        with patch("engine._capture_region_patch", side_effect=patches), patch(
            "engine.time.monotonic", side_effect=fake_monotonic
        ), patch("engine.sleep_interruptible", return_value=False), patch(
            "engine.verify_erp_foreground", return_value=(True, "")
        ), patch("engine.pyautogui.click"), patch("engine.pyautogui.doubleClick"), patch(
            "engine.pyautogui.hotkey"
        ), patch(
            "engine.pyautogui.press", side_effect=lambda k: presses.append(k)
        ), patch(
            "engine.pyperclip.copy"
        ):
            ok, msg = engine.generate_otp("282634716", config=config_manager.DEFAULT_CONFIG)

        self.assertFalse(ok)
        self.assertIn("Step 5/6", msg)
        # Step 6 Enter x4 must NEVER have fired!
        self.assertEqual(len(presses), 2)

    def test_sleep_interruptible_aborts_immediately_on_esc(self):
        engine._ABORT_REQUESTED = True
        start = time.monotonic()
        aborted = engine.sleep_interruptible(5.0)
        elapsed = time.monotonic() - start
        self.assertTrue(aborted)
        self.assertLess(elapsed, 0.25)

    def test_run_otp_mode_halts_batch_and_prints_remaining_ids(self):
        called_ids = []

        def fake_generate_otp(num, cfg, expected_fg=None):
            called_ids.append(num)
            if num == "1002":
                return False, "Halted at Step 4/6 for #1002: Complaint Search LOV stayed open."
            return True, f"OTP generated successfully for complaint #{num}"

        buf = io.StringIO()
        with patch(
            "builtins.input",
            side_effect=["COMPLAINT_NO, 1001.0, 1002, 1003, 1004, 1002", "q"],
        ), patch("main.engine.generate_otp", side_effect=fake_generate_otp), patch(
            "main.engine.sleep_interruptible", return_value=False
        ), patch(
            "main.engine.capture_foreground_window_context",
            return_value={"hwnd": 1, "pid": 999, "process_name": "msedge.exe", "title": "Oracle", "is_console": False},
        ), patch(
            "main._play_otp_alert_chime"
        ) as mock_chime, redirect_stdout(
            buf
        ):
            main.run_otp_mode(config_manager.DEFAULT_CONFIG)

        output = buf.getvalue()
        # Only 1001 and 1002 should have been attempted; 1003 and 1004 must NOT run after 1002 halts
        self.assertEqual(called_ids, ["1001", "1002"])
        mock_chime.assert_called_once()
        self.assertIn("OTP BATCH EXECUTION SUMMARY (HALTED)", output)
        self.assertIn("1002 1003 1004", output)

    def test_run_otp_mode_halts_if_console_still_focused_after_countdown(self):
        called_ids = []
        buf = io.StringIO()
        with patch("builtins.input", side_effect=["2001 2002", "q"]), patch(
            "main.engine.generate_otp", side_effect=lambda n, c, **kw: called_ids.append(n)
        ), patch("main.engine.sleep_interruptible", return_value=False), patch(
            "main.engine.capture_foreground_window_context",
            return_value={
                "hwnd": 10,
                "pid": os.getpid(),
                "process_name": "powershell.exe",
                "title": "Windows PowerShell",
                "is_console": True,
            },
        ), patch("main._play_otp_alert_chime") as mock_chime, redirect_stdout(buf):
            main.run_otp_mode(config_manager.DEFAULT_CONFIG)

        output = buf.getvalue()
        self.assertEqual(called_ids, [])
        mock_chime.assert_called_once()
        self.assertIn("2001 2002", output)


if __name__ == "__main__":
    unittest.main()
