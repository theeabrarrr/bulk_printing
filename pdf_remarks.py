"""
pdf_remarks.py
Batch Technician Remarks PDF Stamping Engine for the Oracle ERP Automation Suite.

Reads COMPLAINT_NO and REMARKS from an Excel workbook, locates matching PDF job cards
recursively inside an input directory, detects the target Technician Remarks bounding box
(with dynamic vertical border anchoring for layout-shifted forms), inserts wrapped/auto-scaled
text transparently using PyMuPDF without altering existing form elements, mirrors subfolders
into a separate output directory, and exports CSV reports for missing or truncated records.
"""

import csv
from datetime import datetime
import os
import re
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import pymupdf

import config_manager
import logger_manager

logger = logger_manager.get_logger("pdf_remarks")

_NULL_TOKENS = {"nan", "none", "null", "n/a", "na", ""}
_EXCEL_FORMULA_ERROR_RE = re.compile(r"^#[A-Z][A-Z0-9_/!?.]*$")


def _resolve_column_name(df_columns, target_col: str) -> Optional[str]:
    """Resolves a column name by exact, stripped, then case-insensitive stripped match."""
    if target_col in df_columns:
        return target_col
    target_clean = str(target_col).strip()
    for col in df_columns:
        if str(col).strip() == target_clean:
            return col
    for col in df_columns:
        if str(col).strip().upper() == target_clean.upper():
            return col
    return None


def normalize_remark_text(raw_text: Any) -> str:
    """
    Normalizes line endings, whitespace variants, and typographic punctuation
    so Base-14 PDF fonts render cleanly while preserving exact letter casing.
    """
    if raw_text is None or pd.isna(raw_text):
        return ""
    text = str(raw_text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\t", "  ")
    text = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return text.strip()


def validate_remarks_dataframe(
    df: Optional[pd.DataFrame],
    complaint_col: str = "COMPLAINT_NO",
    remarks_col: str = "REMARKS",
    source_label: str = "complaint_remarks.xlsx",
) -> Tuple[Optional[List[Tuple[str, str, int]]], Dict[str, Any]]:
    """
    Validates and sanitizes an Excel DataFrame of complaint remarks.

    Rules enforced:
      - Normalizes headers (stripped & case-insensitive fallback); requires both columns.
      - Strips whitespace and float '.0' suffixes from COMPLAINT_NO; rejects non-numeric/formula IDs.
      - Skips blank/null/formula-error REMARKS rows with a warning (never stamps empty text).
      - For duplicate COMPLAINT_NO rows, keeps the LAST non-empty remark and logs both row numbers.

    Returns:
        (records, summary) where records is List[(complaint_no, remark_text, excel_row)]
        or None if required columns are missing.
    """
    summary: Dict[str, Any] = {
        "source_label": source_label,
        "total_rows": 0,
        "valid_records": 0,
        "empty_rows": 0,
        "invalid_rows": 0,
        "blank_remarks_rows": 0,
        "duplicate_rows": 0,
        "missing_complaint_col": False,
        "missing_remarks_col": False,
        "invalid_details": [],
        "blank_remark_details": [],
        "duplicate_details": [],
    }

    if df is None or len(df.columns) == 0:
        summary["missing_complaint_col"] = True
        summary["missing_remarks_col"] = True
        logger.error(f"\n[!] Error: '{source_label}' contains no columns or is empty.")
        return None, summary

    resolved_c_col = _resolve_column_name(df.columns, complaint_col)
    resolved_r_col = _resolve_column_name(df.columns, remarks_col)

    if resolved_c_col is None:
        summary["missing_complaint_col"] = True
        logger.error(f"\n[!] Error: Required column '{complaint_col}' not found in '{source_label}'.")
    if resolved_r_col is None:
        summary["missing_remarks_col"] = True
        logger.error(f"\n[!] Error: Required column '{remarks_col}' not found in '{source_label}'.")
    if summary["missing_complaint_col"] or summary["missing_remarks_col"]:
        logger.error(f"    Available columns: {list(df.columns)}")
        return None, summary

    summary["total_rows"] = len(df)
    records: List[Tuple[str, str, int]] = []
    record_idx_by_id: Dict[str, int] = {}
    last_row_by_id: Dict[str, int] = {}

    for row_idx, (_, row) in enumerate(df.iterrows()):
        excel_row = row_idx + 2
        raw_c = row[resolved_c_col]

        if pd.isna(raw_c):
            summary["empty_rows"] += 1
            logger.debug("Row %d: Skipped empty/NaN complaint cell.", excel_row)
            continue

        c_str = str(raw_c).strip()
        if not c_str or c_str.lower() in _NULL_TOKENS:
            summary["empty_rows"] += 1
            logger.debug("Row %d: Skipped blank/null complaint value '%s'.", excel_row, c_str)
            continue

        if re.fullmatch(r"\d+\.0+", c_str):
            c_str = c_str.split(".", 1)[0]

        if not c_str.isdigit():
            summary["invalid_rows"] += 1
            summary["invalid_details"].append((excel_row, c_str))
            logger.warning(
                f"  [!] Excel Row {excel_row}: Skipping invalid COMPLAINT_NO '{c_str}' (must contain numeric digits only)."
            )
            continue

        raw_r = row[resolved_r_col]
        remark_text = normalize_remark_text(raw_r)
        if (
            not remark_text
            or remark_text.lower() in _NULL_TOKENS
            or bool(_EXCEL_FORMULA_ERROR_RE.fullmatch(remark_text))
        ):
            summary["blank_remarks_rows"] += 1
            summary["blank_remark_details"].append((excel_row, c_str))
            logger.warning(
                f"  [!] Excel Row {excel_row}: Blank/invalid REMARKS for Complaint #{c_str}; skipping."
            )
            continue

        if c_str in last_row_by_id:
            prev_row = last_row_by_id[c_str]
            summary["duplicate_rows"] += 1
            summary["duplicate_details"].append((excel_row, c_str, prev_row))
            records[record_idx_by_id[c_str]] = (c_str, remark_text, excel_row)
            last_row_by_id[c_str] = excel_row
            logger.warning(
                f"  [!] Excel Row {excel_row}: Duplicate COMPLAINT_NO '{c_str}' "
                f"(previously at Row {prev_row}); keeping remark from Row {excel_row}."
            )
            continue

        record_idx_by_id[c_str] = len(records)
        last_row_by_id[c_str] = excel_row
        records.append((c_str, remark_text, excel_row))

    summary["valid_records"] = len(records)
    return records, summary


def validate_and_load_remarks_excel(
    excel_path: str,
    complaint_col: str = "COMPLAINT_NO",
    remarks_col: str = "REMARKS",
) -> Tuple[Optional[List[Tuple[str, str, int]]], Dict[str, Any]]:
    """
    Reads an Excel workbook of technician remarks and runs strict pre-flight validation.
    """
    source_label = os.path.basename(excel_path)
    try:
        df = pd.read_excel(excel_path, dtype=str)
    except Exception as e:
        logger.error(f"\n[!] Failed to read remarks Excel file '{excel_path}': {e}", exc_info=True)
        return None, {"source_label": source_label, "read_error": str(e)}

    records, summary = validate_remarks_dataframe(
        df, complaint_col=complaint_col, remarks_col=remarks_col, source_label=source_label
    )
    if records is None:
        return None, summary

    print("\n" + "=" * 58)
    print("      REMARKS EXCEL PRE-FLIGHT VALIDATION SUMMARY")
    print("=" * 58)
    print(f" Source File              : {summary['source_label']}")
    print(f" Total Rows Scanned       : {summary['total_rows']}")
    print(f" Valid Unique Records     : {summary['valid_records']}")
    print(f" Skipped (Empty ID)       : {summary['empty_rows']}")
    print(f" Skipped (Invalid ID)     : {summary['invalid_rows']}")
    print(f" Skipped (Blank Remark)   : {summary['blank_remarks_rows']}")
    print(f" Duplicates (Kept Last)   : {summary['duplicate_rows']}")
    print("=" * 58)

    logger.info(
        "Remarks Excel validation (%s): total=%d, valid=%d, empty=%d, invalid=%d, blank_remarks=%d, duplicates=%d.",
        summary["source_label"],
        summary["total_rows"],
        summary["valid_records"],
        summary["empty_rows"],
        summary["invalid_rows"],
        summary["blank_remarks_rows"],
        summary["duplicate_rows"],
    )
    return records, summary


def find_target_page_index(doc: pymupdf.Document) -> int:
    """
    Returns the 0-based index of the page containing the Technician Remarks block.
    Falls back to page 0 for single-page forms or when no explicit match is found.
    """
    if len(doc) == 0:
        raise ValueError("PDF contains no pages.")
    if len(doc) == 1:
        return 0

    for idx, page in enumerate(doc):
        norm_text = re.sub(r"\s+", " ", page.get_text("text")).lower()
        if "technician remarks" in norm_text or ("technician" in norm_text and "remarks" in norm_text):
            return idx

    for idx, page in enumerate(doc):
        for drawing in page.get_drawings():
            r = drawing["rect"]
            if r.width <= 1.5 and 30.0 <= r.height <= 80.0 and 335.0 <= r.x0 <= 385.0:
                return idx

    return 0


def detect_target_rect(
    page: pymupdf.Page,
    settings: Dict[str, Any],
) -> Tuple[pymupdf.Rect, float, bool]:
    """
    Computes the padded target PyMuPDF Rect for Technician Remarks on `page`.

    When `dynamic_anchor` is enabled, detects the Technician Remarks vertical divider
    and its upper horizontal rule line to compensate for wrapped upper-form shifts
    (clamped safely above the page footer), falling back to the configured static rect.

    Returns:
        (padded_rect, delta_y, anchored)
    """
    rect_cfg = settings["rect"]
    x0 = float(rect_cfg["x0"])
    y0 = float(rect_cfg["y0"])
    x1 = float(rect_cfg["x1"])
    y1 = float(rect_cfg["y1"])

    pad = float(settings.get("padding", 4.0))
    footer_margin = float(settings.get("footer_margin", 32.0))
    anchor_ref_y = float(settings.get("anchor_reference_y", 517.08))
    page_w = float(page.rect.width)
    page_h = float(page.rect.height)

    delta_y = 0.0
    anchored = False

    if settings.get("dynamic_anchor", True):
        drawings = page.get_drawings()
        vlines = [
            d["rect"]
            for d in drawings
            if d["rect"].width <= 1.5
            and 30.0 <= d["rect"].height <= 80.0
            and abs(d["rect"].x0 - x0) <= 25.0
            and 300.0 <= d["rect"].y0 <= max(320.0, page_h - 100.0)
        ]
        if vlines:
            best_vline = min(
                vlines,
                key=lambda r: (abs(r.x0 - x0), abs(r.y0 - (anchor_ref_y - 23.52))),
            )
            matching_hlines = [
                d["rect"]
                for d in drawings
                if d["rect"].height <= 1.5
                and d["rect"].width >= 250.0
                and d["rect"].x0 <= best_vline.x0 + 5.0
                and d["rect"].x1 >= best_vline.x0 + 100.0
                and (best_vline.y0 + 10.0) < d["rect"].y0 < (best_vline.y1 - 10.0)
            ]
            if matching_hlines:
                detected_top_y = min(r.y0 for r in matching_hlines)
                delta_y = float(detected_top_y - anchor_ref_y)
                anchored = True

    max_y1 = max(0.0, page_h - footer_margin)
    sy0 = max(0.0, y0 + delta_y)
    sy1 = min(max_y1, y1 + delta_y)

    # Guard against extreme or malformed anchor shifts collapsing the text box
    if (sy1 - sy0) < (2.0 * pad + 12.0):
        delta_y = 0.0
        anchored = False
        sy0 = max(0.0, y0)
        sy1 = min(max_y1, y1)

    px0 = max(0.0, x0 + pad)
    py0 = max(0.0, sy0 + pad)
    px1 = min(page_w, x1 - pad)
    py1 = min(page_h, sy1 - pad)

    if px1 <= px0 or py1 <= py0:
        px0, py0, px1, py1 = x0, sy0, x1, sy1

    return pymupdf.Rect(px0, py0, px1, py1), round(delta_y, 2), anchored


def _measure_textbox_fit(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    text: str,
    fontsize: float,
    fontname: str = "helv",
    lineheight: float = 1.2,
) -> float:
    """
    Side-effect-free dry run using an uncommitted PyMuPDF Shape.
    Returns >= 0 when all text fits inside `rect`, or < 0 on overflow.
    """
    shape = page.new_shape()
    return float(
        shape.insert_textbox(
            rect,
            text,
            fontsize=fontsize,
            fontname=fontname,
            color=(0, 0, 0),
            align=pymupdf.TEXT_ALIGN_LEFT,
            lineheight=lineheight,
        )
    )


def _truncate_to_fit(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    text: str,
    fontsize: float,
    fontname: str = "helv",
    lineheight: float = 1.2,
) -> str:
    """
    Binary-searches the longest prefix of `text` that fits inside `rect` with '...' appended,
    preferring a clean word boundary whenever one exists.
    """
    ellipsis = "..."
    if _measure_textbox_fit(page, rect, ellipsis, fontsize, fontname, lineheight) < 0:
        return ""

    lo = 0
    hi = len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if _measure_textbox_fit(page, rect, candidate, fontsize, fontname, lineheight) >= 0:
            lo = mid
        else:
            hi = mid - 1

    if lo <= 0:
        return ellipsis

    prefix = text[:lo]
    if lo < len(text) and not text[lo].isspace():
        match = re.search(r"\s+\S*$", prefix)
        if match and match.start() > 0:
            word_cut = prefix[: match.start()].rstrip()
            if word_cut:
                candidate = word_cut + ellipsis
                if _measure_textbox_fit(page, rect, candidate, fontsize, fontname, lineheight) >= 0:
                    return candidate

    cleaned_prefix = prefix.rstrip()
    return (cleaned_prefix + ellipsis) if cleaned_prefix else ellipsis


def fit_remark_text(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    text: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Finds the largest font size from [max_fontsize .. min_fontsize] that fits `text`
    inside `rect`, falling back to word-boundary truncation with '...' at `min_fontsize`.
    """
    fontname = settings.get("fontname", "helv")
    max_fs = float(settings.get("max_fontsize", 9.5))
    min_fs = float(settings.get("min_fontsize", 6.0))
    step = float(settings.get("fontsize_step", 0.5))
    lineheight = float(settings.get("lineheight", 1.2))

    candidate_sizes: List[float] = []
    fs = max_fs
    while fs > min_fs + 1e-6:
        candidate_sizes.append(round(fs, 3))
        fs -= step
    candidate_sizes.append(round(min_fs, 3))

    last_rc = -1.0
    for size in candidate_sizes:
        rc = _measure_textbox_fit(page, rect, text, size, fontname=fontname, lineheight=lineheight)
        last_rc = rc
        if rc >= 0:
            return {
                "text": text,
                "fontsize": size,
                "scaled": size < (max_fs - 1e-6),
                "truncated": False,
                "rc": rc,
            }

    truncated_text = _truncate_to_fit(
        page, rect, text, min_fs, fontname=fontname, lineheight=lineheight
    )
    final_rc = (
        _measure_textbox_fit(
            page, rect, truncated_text, min_fs, fontname=fontname, lineheight=lineheight
        )
        if truncated_text
        else last_rc
    )
    return {
        "text": truncated_text,
        "fontsize": round(min_fs, 3),
        "scaled": True,
        "truncated": True,
        "rc": final_rc,
    }


def stamp_pdf_remark(
    input_pdf_path: str,
    output_pdf_path: str,
    remark_text: str,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Stamps `remark_text` transparently into the Technician Remarks area of `input_pdf_path`
    and writes the updated PDF atomically to `output_pdf_path`.
    """
    if settings is None:
        settings = config_manager.get_remarks_stamping_settings()

    abs_in = os.path.abspath(input_pdf_path)
    abs_out = os.path.abspath(output_pdf_path)
    if os.path.normcase(abs_in) == os.path.normcase(abs_out):
        raise ValueError("Output PDF path must differ from input PDF path to protect source files.")

    if not os.path.isfile(abs_in) or os.path.getsize(abs_in) == 0:
        raise FileNotFoundError(f"Input PDF is missing or empty: '{abs_in}'")

    out_parent = os.path.dirname(abs_out)
    if out_parent:
        os.makedirs(out_parent, exist_ok=True)

    tmp_path: Optional[str] = None
    doc = pymupdf.open(abs_in)
    try:
        if doc.is_encrypted and not doc.authenticate(""):
            raise ValueError(f"PDF is password-protected/encrypted: '{abs_in}'")

        page_idx = find_target_page_index(doc)
        page = doc[page_idx]
        rect, delta_y, anchored = detect_target_rect(page, settings)
        fit = fit_remark_text(page, rect, remark_text, settings)

        if fit["text"]:
            shape = page.new_shape()
            shape.insert_textbox(
                rect,
                fit["text"],
                fontsize=fit["fontsize"],
                fontname=settings.get("fontname", "helv"),
                color=(0, 0, 0),
                align=pymupdf.TEXT_ALIGN_LEFT,
                lineheight=float(settings.get("lineheight", 1.2)),
            )
            shape.commit(overlay=True)

        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".stamp_",
            suffix=".tmp.pdf",
            dir=out_parent or ".",
        )
        os.close(tmp_fd)
        doc.save(tmp_path, garbage=0, deflate=True)
        doc.close()
        doc = None

        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise RuntimeError(f"Saved stamped PDF is empty: '{tmp_path}'")

        os.replace(tmp_path, abs_out)
        tmp_path = None

        return {
            "page_index": page_idx,
            "rect": (round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2)),
            "delta_y": delta_y,
            "anchored": anchored,
            "fontsize": fit["fontsize"],
            "scaled": fit["scaled"],
            "truncated": fit["truncated"],
            "output_path": abs_out,
        }
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def validate_io_directories(input_dir: str, output_dir: str) -> Tuple[str, str]:
    """
    Validates that `input_dir` and `output_dir` are distinct and non-nested so source PDFs
    cannot be overwritten or recursively re-scanned. Returns (abs_input_dir, abs_output_dir).
    """
    abs_in = os.path.abspath(input_dir)
    abs_out = os.path.abspath(output_dir)
    norm_in = os.path.normcase(abs_in)
    norm_out = os.path.normcase(abs_out)

    if norm_in == norm_out:
        raise ValueError(
            f"Output folder ('{abs_out}') cannot be the same as Input folder ('{abs_in}')."
        )

    try:
        common = os.path.commonpath([norm_in, norm_out])
        if common in (norm_in, norm_out):
            raise ValueError(
                f"Input folder ('{abs_in}') and Output folder ('{abs_out}') cannot be nested inside each other."
            )
    except ValueError as exc:
        # Different drive letters (e.g. C: vs D:) raise ValueError in commonpath and are never nested
        if "don't have the same drive" not in str(exc) and "no common path" not in str(exc).lower():
            raise

    return abs_in, abs_out


def discover_input_pdfs(input_dir: str) -> Dict[str, List[str]]:
    """
    Recursively scans `input_dir` for PDF files, mapping stripped filename stem
    (complaint number) -> sorted list of absolute PDF paths.
    """
    pdf_map: Dict[str, List[str]] = {}
    if not input_dir or not os.path.isdir(input_dir):
        return pdf_map

    for root, dirs, files in os.walk(input_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fname in sorted(files):
            if fname.startswith("._") or not fname.lower().endswith(".pdf"):
                continue
            stem = os.path.splitext(fname)[0].strip()
            if not stem:
                continue
            pdf_map.setdefault(stem, []).append(os.path.abspath(os.path.join(root, fname)))

    return pdf_map


def export_remarks_report_csv(
    issues: List[Dict[str, Any]],
    log_dir: str,
) -> Optional[str]:
    """
    Exports missing PDFs, truncation notices, and stamping errors to a timestamped CSV
    in `log_dir`. Returns the absolute CSV path, or None when `issues` is empty.
    """
    if not issues:
        return None

    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.abspath(os.path.join(log_dir, f"remarks_report_{ts}.csv"))

    fieldnames = ["COMPLAINT_NO", "STATUS", "EXCEL_ROW", "DETAILS", "PDF_PATH"]
    with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in issues:
            writer.writerow({k: item.get(k, "") for k in fieldnames})

    return report_path


def run_remarks_batch(
    excel_path: str,
    input_dir: str,
    output_dir: str,
    config: Optional[Dict[str, Any]] = None,
    check_abort_fn: Optional[Callable[[], bool]] = None,
    preloaded_records: Optional[List[Tuple[str, str, int]]] = None,
    preloaded_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Executes batch Technician Remarks PDF stamping from `excel_path` against PDFs in `input_dir`,
    writing matched stamped PDFs to mirrored paths inside `output_dir`.
    """
    if config is None:
        config = config_manager.load_config() or config_manager.DEFAULT_CONFIG

    settings = config_manager.get_remarks_stamping_settings(config)
    log_cfg = config_manager.get_logging_settings(config)
    log_dir = config_manager.resolve_path(log_cfg.get("log_dir", "logs"))

    abs_in, abs_out = validate_io_directories(input_dir, output_dir)
    if not os.path.isdir(abs_in):
        raise FileNotFoundError(f"Input PDF folder does not exist: '{abs_in}'")

    if preloaded_records is not None:
        records = preloaded_records
        summary = preloaded_summary or {}
    else:
        records, summary = validate_and_load_remarks_excel(
            excel_path,
            complaint_col=settings["complaint_column"],
            remarks_col=settings["remarks_column"],
        )

    stats: Dict[str, Any] = {
        "total_excel_records": len(records) if records is not None else 0,
        "matched_complaints": 0,
        "stamped": 0,
        "auto_scaled": 0,
        "truncated": 0,
        "missing_pdfs": 0,
        "failed": 0,
        "unmatched_source_pdfs": 0,
        "aborted": False,
        "report_path": None,
        "issues": [],
        "output_dir": abs_out,
        "excel_summary": summary,
    }

    if records is None or not records:
        return stats

    pdf_map = discover_input_pdfs(abs_in)
    excel_ids = {cid for cid, _, _ in records}
    stats["unmatched_source_pdfs"] = sum(
        len(paths) for cid, paths in pdf_map.items() if cid not in excel_ids
    )

    os.makedirs(abs_out, exist_ok=True)
    logger.info(
        "\n[*] Starting Remarks Stamping: %d Excel record(s) | %d PDF file(s) found in '%s'",
        len(records),
        sum(len(v) for v in pdf_map.values()),
        abs_in,
    )

    for idx, (complaint_no, remark_text, excel_row) in enumerate(records, 1):
        if check_abort_fn and check_abort_fn():
            logger.warning("\n[!] Remarks stamping aborted by user (Esc pressed).")
            stats["aborted"] = True
            break

        src_paths = pdf_map.get(complaint_no)
        if not src_paths:
            stats["missing_pdfs"] += 1
            msg = f"No matching '{complaint_no}.pdf' found in '{abs_in}'."
            logger.warning(f"  [!] [{idx}/{len(records)}] Missing PDF for #{complaint_no} (Row {excel_row}).")
            stats["issues"].append(
                {
                    "COMPLAINT_NO": complaint_no,
                    "STATUS": "MISSING_PDF",
                    "EXCEL_ROW": excel_row,
                    "DETAILS": msg,
                    "PDF_PATH": "",
                }
            )
            continue

        stats["matched_complaints"] += 1
        if len(src_paths) > 1:
            logger.info(
                f"  [i] [{idx}/{len(records)}] Found {len(src_paths)} PDF copies for #{complaint_no}; stamping all matched copies."
            )

        for src_pdf in src_paths:
            rel_path = os.path.relpath(src_pdf, abs_in)
            dst_pdf = os.path.join(abs_out, rel_path)
            try:
                res = stamp_pdf_remark(src_pdf, dst_pdf, remark_text, settings=settings)
                stats["stamped"] += 1
                if res["scaled"]:
                    stats["auto_scaled"] += 1
                if res["truncated"]:
                    stats["truncated"] += 1
                    warn_msg = (
                        f"Remark exceeded box even at min fontsize ({res['fontsize']} pt) and was truncated with '...'."
                    )
                    logger.warning(
                        f"  [!] [{idx}/{len(records)}] #{complaint_no}: {warn_msg}"
                    )
                    stats["issues"].append(
                        {
                            "COMPLAINT_NO": complaint_no,
                            "STATUS": "TRUNCATED",
                            "EXCEL_ROW": excel_row,
                            "DETAILS": warn_msg,
                            "PDF_PATH": dst_pdf,
                        }
                    )
                else:
                    scale_note = f" (auto-scaled to {res['fontsize']}pt)" if res["scaled"] else f" ({res['fontsize']}pt)"
                    shift_note = f" [shifted {res['delta_y']:+.2f}pt]" if abs(res["delta_y"]) >= 0.5 else ""
                    logger.info(
                        f"  [+] [{idx}/{len(records)}] Stamped #{complaint_no}{scale_note}{shift_note} -> {rel_path}"
                    )
            except Exception as exc:
                stats["failed"] += 1
                logger.error(
                    f"  [!] [{idx}/{len(records)}] Failed to stamp '{src_pdf}' for #{complaint_no}: {exc}",
                    exc_info=True,
                )
                stats["issues"].append(
                    {
                        "COMPLAINT_NO": complaint_no,
                        "STATUS": "ERROR",
                        "EXCEL_ROW": excel_row,
                        "DETAILS": str(exc),
                        "PDF_PATH": src_pdf,
                    }
                )

    if stats["issues"]:
        stats["report_path"] = export_remarks_report_csv(stats["issues"], log_dir)

    logger.info(
        "Remarks stamping summary: records=%d, matched=%d, stamped=%d, auto_scaled=%d, truncated=%d, missing_pdfs=%d, failed=%d, unmatched_input_pdfs=%d, aborted=%s",
        stats["total_excel_records"],
        stats["matched_complaints"],
        stats["stamped"],
        stats["auto_scaled"],
        stats["truncated"],
        stats["missing_pdfs"],
        stats["failed"],
        stats["unmatched_source_pdfs"],
        stats["aborted"],
    )

    print("\n" + "=" * 60)
    print("           REMARKS STAMPING BATCH SUMMARY")
    print("=" * 60)
    print(f" Valid Excel Records    : {stats['total_excel_records']}")
    print(f" Matched Complaints     : {stats['matched_complaints']}")
    print(f" PDFs Stamped           : {stats['stamped']}")
    print(f"   - Auto-Scaled        : {stats['auto_scaled']}")
    print(f"   - Truncated (...)    : {stats['truncated']}")
    print(f" Missing PDFs           : {stats['missing_pdfs']}")
    print(f" Failed / Corrupt PDFs  : {stats['failed']}")
    print(f" Unmatched Input PDFs   : {stats['unmatched_source_pdfs']} (ignored)")
    print(f" Output Directory       : {stats['output_dir']}")
    if stats["report_path"]:
        print(f" Issues CSV Report      : {stats['report_path']}")
    if stats["aborted"]:
        print(" Interrupted            : True (ESC pressed)")
    print("=" * 60 + "\n")

    return stats
