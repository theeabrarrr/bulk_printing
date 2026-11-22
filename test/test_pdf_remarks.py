import csv
import hashlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import pymupdf

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config_manager
import main as app_main
import pdf_remarks


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _create_synthetic_form_pdf(
    path: str,
    shift_y: float = 0.0,
    pages: int = 1,
    target_page: int = 0,
    include_table: bool = True,
) -> None:
    """Creates a 612x792 synthetic complaint form PDF matching Oracle ERP line geometry."""
    doc = pymupdf.open()
    for p_idx in range(pages):
        page = doc.new_page(width=612, height=792)
        if p_idx == target_page:
            page.insert_text((364.2, 507.0 + shift_y), "Technician Remarks & Sign", fontsize=8.0, fontname="helv")
            if include_table:
                shape = page.new_shape()
                # Outer table box + vertical divider at x=359.76 + horizontal rules (each as an independent path)
                for p1, p2 in (
                    ((359.76, 493.56 + shift_y), (359.76, 541.68 + shift_y)),
                    ((14.88, 493.56 + shift_y), (603.84, 493.56 + shift_y)),
                    ((14.88, 517.08 + shift_y), (603.84, 517.08 + shift_y)),
                    ((14.88, 530.52 + shift_y), (603.84, 530.52 + shift_y)),
                    ((14.88, 541.68 + shift_y), (603.84, 541.68 + shift_y)),
                ):
                    shape.draw_line(p1, p2)
                    shape.finish(color=(0, 0, 0), width=0.75)
                shape.commit()
        else:
            page.insert_text((50, 50), f"Cover or Attachment Page {p_idx + 1}", fontsize=10.0, fontname="helv")
    doc.save(path)
    doc.close()


def _write_remarks_excel(path: str, rows: list, columns=None) -> None:
    if columns is None:
        columns = ["COMPLAINT_NO", "REMARKS"]
    df = pd.DataFrame(rows, columns=columns)
    df.to_excel(path, index=False)


class TestRemarksConfig(unittest.TestCase):
    def test_default_settings_match_extracted_coordinates(self):
        cfg = config_manager.get_remarks_stamping_settings(config_manager.DEFAULT_CONFIG)
        self.assertEqual(cfg["excel_path"], "complaint_remarks.xlsx")
        self.assertEqual(cfg["complaint_column"], "COMPLAINT_NO")
        self.assertEqual(cfg["remarks_column"], "REMARKS")
        self.assertEqual(cfg["input_dir"], "Today_Complaints")
        self.assertEqual(cfg["output_dir"], "Stamped_Complaints")
        self.assertEqual(
            cfg["rect"],
            {"x0": 360.48, "y0": 516.70, "x1": 603.15, "y1": 645.76},
        )
        self.assertEqual(cfg["padding"], 4.0)
        self.assertTrue(cfg["dynamic_anchor"])
        self.assertAlmostEqual(cfg["anchor_reference_y"], 517.08)
        self.assertEqual(cfg["footer_margin"], 32.0)
        self.assertEqual(cfg["fontname"], "helv")
        self.assertEqual(cfg["max_fontsize"], 9.5)
        self.assertEqual(cfg["min_fontsize"], 6.0)
        self.assertEqual(cfg["fontsize_step"], 0.5)
        self.assertEqual(cfg["lineheight"], 1.2)

    def test_invalid_values_fall_back_safely(self):
        bad_cfg = {
            "remarks_stamping": {
                "excel_path": "   ",
                "output_dir": "",
                "rect": {"x0": 500, "y0": 500, "x1": 300, "y1": 200},
                "padding": -10,
                "dynamic_anchor": None,
                "anchor_reference_y": -5,
                "footer_margin": "not-a-number",
                "min_fontsize": 1.0,
                "max_fontsize": 4.0,
                "fontsize_step": 0,
                "lineheight": 0.2,
            }
        }
        parsed = config_manager.get_remarks_stamping_settings(bad_cfg)
        self.assertEqual(parsed["excel_path"], "complaint_remarks.xlsx")
        self.assertEqual(parsed["output_dir"], "Stamped_Complaints")
        self.assertEqual(parsed["rect"], config_manager.DEFAULT_CONFIG["remarks_stamping"]["rect"])
        self.assertEqual(parsed["padding"], 0.0)
        self.assertTrue(parsed["dynamic_anchor"])
        self.assertEqual(parsed["anchor_reference_y"], 517.08)
        self.assertEqual(parsed["footer_margin"], 32.0)
        self.assertEqual(parsed["min_fontsize"], 2.0)
        self.assertEqual(parsed["max_fontsize"], 4.0)
        self.assertEqual(parsed["fontsize_step"], 0.5)
        self.assertEqual(parsed["lineheight"], 0.8)

    def test_load_config_populates_missing_section_without_mutating_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write('{"settings": {"output_dir": "Today_Complaints"}}')
            loaded = config_manager.load_config(cfg_path)
            loaded["remarks_stamping"]["rect"]["x0"] = 999.0
            self.assertAlmostEqual(
                config_manager.DEFAULT_CONFIG["remarks_stamping"]["rect"]["x0"],
                360.48,
            )


class TestRemarksExcelValidation(unittest.TestCase):
    def test_validation_handles_duplicates_blanks_invalid_ids_and_case(self):
        df = pd.DataFrame(
            {
                " complaint_no ": [
                    "282634716.0",
                    None,
                    "ABC123",
                    "#REF!",
                    "282634717",
                    "282634718",
                    "282634716",
                    "282634718",
                    "282634719",
                ],
                "remarks": [
                    "First remark",
                    "Orphan remark",
                    "Bad ID remark",
                    "Formula ID remark",
                    "   ",
                    "Valid initial remark",
                    "Updated LAST remark\r\nLine Two — ‘Test’",
                    "N/A",
                    "#1 compressor replaced",
                ],
            }
        )
        with self.assertLogs("erp_automation.pdf_remarks", level="WARNING") as cm:
            records, summary = pdf_remarks.validate_remarks_dataframe(df)

        self.assertIsNotNone(records)
        self.assertEqual(summary["total_rows"], 9)
        self.assertEqual(summary["empty_rows"], 1)
        self.assertEqual(summary["invalid_rows"], 2)
        self.assertEqual(summary["blank_remarks_rows"], 2)
        self.assertEqual(summary["duplicate_rows"], 1)
        self.assertEqual(summary["valid_records"], 3)

        by_id = {cid: (text, row) for cid, text, row in records}
        self.assertEqual(by_id["282634716"], ("Updated LAST remark\nLine Two - 'Test'", 8))
        self.assertEqual(by_id["282634718"], ("Valid initial remark", 7))
        self.assertEqual(by_id["282634719"], ("#1 compressor replaced", 10))
        self.assertTrue(any("Row 8" in msg and "Row 2" in msg for msg in cm.output))

    def test_missing_columns_returns_none(self):
        df = pd.DataFrame({"COMPLAINT_NO": ["123456"], "OTHER_COL": ["abc"]})
        records, summary = pdf_remarks.validate_remarks_dataframe(df)
        self.assertIsNone(records)
        self.assertFalse(summary["missing_complaint_col"])
        self.assertTrue(summary["missing_remarks_col"])


class TestDynamicAnchorDetection(unittest.TestCase):
    def setUp(self):
        self.settings = config_manager.get_remarks_stamping_settings(config_manager.DEFAULT_CONFIG)

    def test_standard_and_shifted_forms_detect_exact_delta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            std_pdf = os.path.join(tmpdir, "std.pdf")
            shifted_pdf = os.path.join(tmpdir, "shifted.pdf")
            shifted_big_pdf = os.path.join(tmpdir, "shifted_big.pdf")
            plain_pdf = os.path.join(tmpdir, "plain.pdf")

            _create_synthetic_form_pdf(std_pdf, shift_y=0.0)
            _create_synthetic_form_pdf(shifted_pdf, shift_y=4.92)
            _create_synthetic_form_pdf(shifted_big_pdf, shift_y=125.0)
            _create_synthetic_form_pdf(plain_pdf, shift_y=0.0, include_table=False)

            with pymupdf.open(std_pdf) as d:
                rect, delta, anchored = pdf_remarks.detect_target_rect(d[0], self.settings)
                self.assertTrue(anchored)
                self.assertAlmostEqual(delta, 0.0, places=2)
                self.assertAlmostEqual(rect.x0, 364.48, places=2)
                self.assertAlmostEqual(rect.y0, 520.70, places=2)
                self.assertAlmostEqual(rect.x1, 599.15, places=2)
                self.assertAlmostEqual(rect.y1, 641.76, places=2)

            with pymupdf.open(shifted_pdf) as d:
                rect, delta, anchored = pdf_remarks.detect_target_rect(d[0], self.settings)
                self.assertTrue(anchored)
                self.assertAlmostEqual(delta, 4.92, places=2)
                self.assertAlmostEqual(rect.y0, 525.62, places=2)
                self.assertAlmostEqual(rect.y1, 646.68, places=2)

            with pymupdf.open(shifted_big_pdf) as d:
                rect, delta, anchored = pdf_remarks.detect_target_rect(d[0], self.settings)
                self.assertTrue(anchored)
                self.assertAlmostEqual(delta, 125.0, places=1)
                self.assertAlmostEqual(
                    rect.y1,
                    d[0].rect.height - self.settings["footer_margin"] - self.settings["padding"],
                    places=2,
                )

            with pymupdf.open(plain_pdf) as d:
                rect, delta, anchored = pdf_remarks.detect_target_rect(d[0], self.settings)
                self.assertFalse(anchored)
                self.assertEqual(delta, 0.0)
                self.assertAlmostEqual(rect.y0, 520.70, places=2)


class TestTextFitAndStamping(unittest.TestCase):
    def setUp(self):
        self.settings = config_manager.get_remarks_stamping_settings(config_manager.DEFAULT_CONFIG)

    def test_short_autoscaled_and_truncated_stamping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src.pdf")
            _create_synthetic_form_pdf(src, shift_y=0.0)

            out_short = os.path.join(tmpdir, "out_short.pdf")
            res_short = pdf_remarks.stamp_pdf_remark(
                src, out_short, "Cold tap replaced and job ok", self.settings
            )
            self.assertEqual(res_short["fontsize"], 9.5)
            self.assertFalse(res_short["scaled"])
            self.assertFalse(res_short["truncated"])
            with pymupdf.open(out_short) as d:
                txt = d[0].get_text("text")
                self.assertIn("Cold tap replaced and job ok", txt)
                self.assertIn("Technician Remarks & Sign", txt)

            medium_text = " ".join(
                [f"Checked compressor suction pressure and relay point #{i}." for i in range(1, 16)]
            )
            out_med = os.path.join(tmpdir, "out_med.pdf")
            res_med = pdf_remarks.stamp_pdf_remark(src, out_med, medium_text, self.settings)
            self.assertTrue(res_med["scaled"])
            self.assertFalse(res_med["truncated"])
            self.assertGreaterEqual(res_med["fontsize"], 6.0)
            self.assertLess(res_med["fontsize"], 9.5)
            with pymupdf.open(out_med) as d:
                norm_txt = " ".join(d[0].get_text("text").split())
                self.assertIn("point #15.", norm_txt)

            extreme_text = " ".join(
                [f"Step{i}: Inspected capacitor, fan motor, thermostat, and wiring harness." for i in range(1, 70)]
            )
            out_long = os.path.join(tmpdir, "out_long.pdf")
            res_long = pdf_remarks.stamp_pdf_remark(src, out_long, extreme_text, self.settings)
            self.assertTrue(res_long["scaled"])
            self.assertTrue(res_long["truncated"])
            self.assertEqual(res_long["fontsize"], 6.0)
            with pymupdf.open(out_long) as d:
                box_txt = " ".join(d[0].get_text("text", clip=pymupdf.Rect(*res_long["rect"])).split())
                self.assertTrue(box_txt.endswith("..."))
                self.assertFalse(box_txt.endswith("Step..."))

            unbroken_token = "X" * 2500
            out_tok = os.path.join(tmpdir, "out_tok.pdf")
            res_tok = pdf_remarks.stamp_pdf_remark(src, out_tok, unbroken_token, self.settings)
            self.assertTrue(res_tok["truncated"])
            with pymupdf.open(out_tok) as d:
                box_txt = " ".join(d[0].get_text("text", clip=pymupdf.Rect(*res_tok["rect"])).split())
                self.assertTrue(box_txt.endswith("..."))

    def test_multipage_targets_technician_remarks_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "multi.pdf")
            out = os.path.join(tmpdir, "multi_out.pdf")
            _create_synthetic_form_pdf(src, pages=3, target_page=1)
            res = pdf_remarks.stamp_pdf_remark(src, out, "Stamped on page two", self.settings)
            self.assertEqual(res["page_index"], 1)
            with pymupdf.open(out) as d:
                self.assertNotIn("Stamped on page two", d[0].get_text())
                self.assertIn("Stamped on page two", d[1].get_text())
                self.assertNotIn("Stamped on page two", d[2].get_text())

    def test_same_path_and_encrypted_pdf_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src.pdf")
            _create_synthetic_form_pdf(src)
            with self.assertRaises(ValueError):
                pdf_remarks.stamp_pdf_remark(src, src, "Test", self.settings)

            enc = os.path.join(tmpdir, "enc.pdf")
            with pymupdf.open(src) as d:
                d.save(enc, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
            with self.assertRaises(ValueError):
                pdf_remarks.stamp_pdf_remark(enc, os.path.join(tmpdir, "enc_out.pdf"), "Test", self.settings)


class TestRemarksBatch(unittest.TestCase):
    def test_directory_safety_guards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            in_dir = os.path.join(tmpdir, "in")
            nested = os.path.join(in_dir, "out")
            os.makedirs(nested, exist_ok=True)
            with self.assertRaises(ValueError):
                pdf_remarks.validate_io_directories(in_dir, in_dir)
            with self.assertRaises(ValueError):
                pdf_remarks.validate_io_directories(in_dir, nested)
            with self.assertRaises(ValueError):
                pdf_remarks.validate_io_directories(nested, in_dir)

    def test_recursive_mirroring_unmatched_ignored_overwrite_and_clean_run_no_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            in_dir = os.path.join(tmpdir, "Today_Complaints")
            out_dir = os.path.join(tmpdir, "Stamped_Complaints")
            log_dir = os.path.join(tmpdir, "logs")
            excel_path = os.path.join(tmpdir, "complaint_remarks.xlsx")

            os.makedirs(os.path.join(in_dir, "TechA"), exist_ok=True)
            os.makedirs(os.path.join(in_dir, "TechB", "Sub"), exist_ok=True)
            pdf_a = os.path.join(in_dir, "TechA", "1001.pdf")
            pdf_b = os.path.join(in_dir, "TechB", "Sub", "1002.pdf")
            pdf_flat = os.path.join(in_dir, "1003.pdf")
            pdf_unmatched = os.path.join(in_dir, "TechA", "9999.pdf")

            _create_synthetic_form_pdf(pdf_a, shift_y=0.0)
            _create_synthetic_form_pdf(pdf_b, shift_y=4.92)
            _create_synthetic_form_pdf(pdf_flat, shift_y=0.0)
            _create_synthetic_form_pdf(pdf_unmatched, shift_y=0.0)

            orig_hash_a = _file_sha256(pdf_a)
            _write_remarks_excel(
                excel_path,
                [
                    ["1001", "Initial remark A"],
                    ["1002", "Shifted form remark B"],
                    ["1003", "Flat folder remark C"],
                ],
            )
            cfg = {"logging": {"log_dir": log_dir}}
            stats1 = pdf_remarks.run_remarks_batch(excel_path, in_dir, out_dir, config=cfg)

            self.assertEqual(stats1["stamped"], 3)
            self.assertEqual(stats1["missing_pdfs"], 0)
            self.assertEqual(stats1["unmatched_source_pdfs"], 1)
            self.assertIsNone(stats1["report_path"])
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "TechA", "1001.pdf")))
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "TechB", "Sub", "1002.pdf")))
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "1003.pdf")))
            self.assertFalse(os.path.exists(os.path.join(out_dir, "TechA", "9999.pdf")))
            self.assertEqual(_file_sha256(pdf_a), orig_hash_a)

            # Re-run with updated remark -> clean atomic overwrite
            _write_remarks_excel(excel_path, [["1001", "Updated remark A v2"]])
            stats2 = pdf_remarks.run_remarks_batch(excel_path, in_dir, out_dir, config=cfg)
            self.assertEqual(stats2["stamped"], 1)
            with pymupdf.open(os.path.join(out_dir, "TechA", "1001.pdf")) as d:
                txt = d[0].get_text()
                self.assertIn("Updated remark A v2", txt)
                self.assertNotIn("Initial remark A", txt)

    def test_missing_corrupt_and_abort_export_csv_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            in_dir = os.path.join(tmpdir, "in")
            out_dir = os.path.join(tmpdir, "out")
            log_dir = os.path.join(tmpdir, "logs")
            excel_path = os.path.join(tmpdir, "remarks.xlsx")
            os.makedirs(in_dir, exist_ok=True)

            valid_pdf = os.path.join(in_dir, "2001.pdf")
            corrupt_pdf = os.path.join(in_dir, "2002.pdf")
            _create_synthetic_form_pdf(valid_pdf)
            with open(corrupt_pdf, "wb") as f:
                f.write(b"not-a-valid-pdf")

            _write_remarks_excel(
                excel_path,
                [
                    ["2001", "OK remark"],
                    ["2002", "Corrupt target"],
                    ["2003", "Missing PDF target"],
                ],
            )
            stats = pdf_remarks.run_remarks_batch(
                excel_path, in_dir, out_dir, config={"logging": {"log_dir": log_dir}}
            )
            self.assertEqual(stats["stamped"], 1)
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(stats["missing_pdfs"], 1)
            self.assertIsNotNone(stats["report_path"])
            self.assertTrue(os.path.isfile(stats["report_path"]))

            with open(stats["report_path"], "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            statuses = {r["COMPLAINT_NO"]: r["STATUS"] for r in rows}
            self.assertEqual(statuses["2002"], "ERROR")
            self.assertEqual(statuses["2003"], "MISSING_PDF")

            # Abort mid-run stops remaining items
            stats_abort = pdf_remarks.run_remarks_batch(
                excel_path,
                in_dir,
                out_dir,
                config={"logging": {"log_dir": log_dir}},
                check_abort_fn=lambda: True,
            )
            self.assertTrue(stats_abort["aborted"])
            self.assertEqual(stats_abort["stamped"], 0)


class TestMainIntegration(unittest.TestCase):
    def test_cli_helpers_and_batch_print_source_switch(self):
        self.assertEqual(app_main._strip_quotes('"C:\\Temp\\file.xlsx"'), "C:\\Temp\\file.xlsx")
        self.assertEqual(app_main._strip_quotes("plain.xlsx"), "plain.xlsx")

        with tempfile.TemporaryDirectory() as tmpdir:
            today_dir = os.path.join(tmpdir, "Today_Complaints")
            stamped_dir = os.path.join(tmpdir, "Stamped_Complaints")
            os.makedirs(os.path.join(today_dir, "Tech1"), exist_ok=True)
            os.makedirs(os.path.join(stamped_dir, "Tech1"), exist_ok=True)
            _create_synthetic_form_pdf(os.path.join(today_dir, "Tech1", "3001.pdf"))
            _create_synthetic_form_pdf(os.path.join(stamped_dir, "Tech1", "3001.pdf"))

            self.assertTrue(app_main._directory_has_pdfs(today_dir))
            self.assertFalse(app_main._directory_has_pdfs(os.path.join(tmpdir, "missing")))

            cfg = {
                "settings": {"output_dir": today_dir, "database_file": os.path.join(tmpdir, "state.db")},
                "printing": {"printer_name": "HP Test Printer"},
                "remarks_stamping": {"output_dir": stamped_dir},
            }
            # Choose source 2 (Stamped_Complaints), then switch with 's' back to Today_Complaints, then 'q'
            with patch("builtins.input", side_effect=["2", "s", "q"]), \
                 patch.object(app_main.printer_manager, "classify_printer", return_value="PHYSICAL"), \
                 patch.object(app_main.printer_manager, "load_printed_complaints", return_value=set()):
                app_main.run_batch_print_mode(cfg)


_SAMPLE_EXCEL = os.path.join(PROJECT_ROOT, "test", "sample_data", "complaint_remarks.xlsx")
_SAMPLE_PDF = os.path.join(PROJECT_ROOT, "test", "sample_data", "282634716.pdf")
_SHIFTED_REAL_PDF = os.path.join(
    PROJECT_ROOT,
    "dist",
    "ERP_Automation_Suite",
    "Today_Complaints",
    "Ameer Hamza",
    "282634911.pdf",
)


class TestSampleDataIntegration(unittest.TestCase):
    @unittest.skipUnless(
        os.path.isfile(_SAMPLE_EXCEL) and os.path.isfile(_SAMPLE_PDF),
        "Sample data files not present in test/sample_data/",
    )
    def test_sample_data_stamps_exact_box_and_preserves_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            in_dir = os.path.join(tmpdir, "in")
            out_dir = os.path.join(tmpdir, "out")
            log_dir = os.path.join(tmpdir, "logs")
            os.makedirs(in_dir, exist_ok=True)
            dst_sample = os.path.join(in_dir, "282634716.pdf")
            with open(_SAMPLE_PDF, "rb") as s, open(dst_sample, "wb") as d:
                d.write(s.read())

            stats = pdf_remarks.run_remarks_batch(
                _SAMPLE_EXCEL,
                in_dir,
                out_dir,
                config={"logging": {"log_dir": log_dir}},
            )
            self.assertEqual(stats["stamped"], 1)
            self.assertEqual(stats["missing_pdfs"], 0)
            self.assertIsNone(stats["report_path"])

            out_pdf = os.path.join(out_dir, "282634716.pdf")
            with pymupdf.open(_SAMPLE_PDF) as orig_doc, pymupdf.open(out_pdf) as new_doc:
                self.assertEqual(len(new_doc), len(orig_doc))
                self.assertEqual(len(new_doc[0].get_drawings()), len(orig_doc[0].get_drawings()))
                box_clip = pymupdf.Rect(360.48, 516.70, 603.15, 645.76)
                extracted = " ".join(new_doc[0].get_text("text", clip=box_clip).split())
                self.assertIn("cold tap + hot tap + smart block cap replaced and installed, job ok", extracted)

    @unittest.skipUnless(
        os.path.isfile(_SHIFTED_REAL_PDF),
        "Shifted real PDF sample not present in dist/",
    )
    def test_real_shifted_pdf_anchors_at_4_92_pt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = os.path.join(tmpdir, "282634911_stamped.pdf")
            settings = config_manager.get_remarks_stamping_settings(config_manager.DEFAULT_CONFIG)
            res = pdf_remarks.stamp_pdf_remark(
                _SHIFTED_REAL_PDF,
                out_pdf,
                "Compressor relay replaced, gas pressure checked and verified OK.",
                settings=settings,
            )
            self.assertTrue(res["anchored"])
            self.assertAlmostEqual(res["delta_y"], 4.92, delta=0.2)
            with pymupdf.open(out_pdf) as d:
                box_clip = pymupdf.Rect(*res["rect"])
                extracted = " ".join(d[0].get_text("text", clip=box_clip).split())
                self.assertIn("Compressor relay replaced", extracted)


if __name__ == "__main__":
    unittest.main()
