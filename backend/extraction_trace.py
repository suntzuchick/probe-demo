"""
Converts a real extraction result (from extractors/agent.py) into an
ordered list of log lines that narrate, field by field, what the agent
actually did -- for the live terminal-style extraction screen. Every
line is derived directly from real computed values (column names,
real confidence scores, real row counts); nothing here is scripted
or pre-written copy standing in for the actual result.
"""


def build_trace(extraction_result: dict) -> list:
    lines = []
    fname = extraction_result.get("filename", "unknown")
    fmt = extraction_result.get("detected_format", "unknown")
    size = extraction_result.get("file_size_bytes", 0)
    t_ms = extraction_result.get("extraction_time_ms", 0)

    lines.append({"level": "info", "text": f"reading {fname} ({size} bytes)"})
    lines.append({"level": "info", "text": f"format detected via content signature: {fmt}"})

    if extraction_result.get("status") == "error":
        lines.append({"level": "error", "text": extraction_result.get("error", "extraction failed")})
        return lines

    if fmt in ("csv", "tsv"):
        lines.extend(_trace_tabular(extraction_result))
    elif fmt == "xlsx":
        lines.extend(_trace_xlsx(extraction_result))
    elif fmt == "pdf":
        lines.extend(_trace_pdf(extraction_result))
    elif fmt in ("image_png", "image_jpeg"):
        lines.extend(_trace_image(extraction_result))
    elif fmt == "json":
        lines.extend(_trace_json(extraction_result))

    lines.append({"level": "done", "text": f"extraction complete in {t_ms}ms"})
    return lines


def _trace_mapping_lines(mapping):
    lines = []
    for m in mapping:
        action = m["action"]
        pct = round(m["confidence"] * 100, 1)
        if action == "AUTO_MAP":
            lines.append({
                "level": "match",
                "text": f"column \"{m['source_label']}\" \u2192 scored against {len(m['candidates'])} candidate variables \u2192 best match {m['top_match']} ({pct}%) \u2014 auto-mapped, above 85% threshold",
            })
        elif action == "SURFACE_TO_USER":
            cands = ", ".join(f"{c['var']} ({round(c['score']*100,1)}%)" for c in m["candidates"][:2])
            lines.append({
                "level": "review",
                "text": f"column \"{m['source_label']}\" \u2192 best match {m['top_match']} ({pct}%) \u2014 below 85% auto-map threshold, holding for confirmation. candidates: {cands}",
            })
        else:
            lines.append({
                "level": "reject",
                "text": f"column \"{m['source_label']}\" \u2192 no candidate scored above 55% (best: {m['top_match']} at {pct}%) \u2014 rejected, no vocabulary match",
            })
    return lines


def _trace_tabular(result):
    lines = []
    layout = result.get("detected_layout")
    if layout == "plate_map":
        lines.append({"level": "info", "text": f"layout check: first column values match row-letter pattern (A-P), remaining headers are numeric \u2192 plate-grid layout detected"})
        lines.append({"level": "info", "text": f"reshaping {result.get('n_rows_raw')} grid rows \u00d7 columns \u2192 {result.get('n_wells_reshaped')} long-format well records"})
    else:
        lines.append({"level": "info", "text": f"layout check: no row-letter/numeric-grid pattern \u2192 long-format table, {result.get('n_rows')} rows \u00d7 {result.get('n_cols')} columns"})
    lines.append({"level": "info", "text": f"scoring {len(result.get('columns', []))} column headers against SDTM/assay vocabulary"})
    lines.extend(_trace_mapping_lines(result.get("mapping", [])))
    return lines


def _trace_xlsx(result):
    lines = []
    lines.append({"level": "info", "text": f"workbook contains {result.get('n_sheets')} sheet(s): {', '.join(result.get('sheet_names', []))}"})
    for sheet in result.get("sheets", []):
        if sheet.get("status") == "empty":
            lines.append({"level": "info", "text": f"sheet \"{sheet['sheet_name']}\" \u2014 empty, skipping"})
            continue
        layout = sheet.get("detected_layout")
        lines.append({"level": "info", "text": f"sheet \"{sheet['sheet_name']}\": " +
                       (f"plate-grid layout, reshaped to {sheet.get('n_wells_reshaped')} wells" if layout == "plate_map"
                        else f"long-format, {sheet.get('n_rows')} rows \u00d7 {sheet.get('n_cols')} columns")})
        lines.extend(_trace_mapping_lines(sheet.get("mapping", [])))
    return lines


def _trace_pdf(result):
    lines = []
    lines.append({"level": "info", "text": f"{result.get('n_pages')} page(s), {result.get('n_chars_extracted')} characters of text extracted"})
    if not result.get("is_text_extractable"):
        lines.append({"level": "review", "text": "very little extractable text -- this PDF may be a scanned image without an OCR layer"})
    params = result.get("extracted_parameters", {})
    any_found = False
    for key, hits in params.items():
        for hit in hits:
            any_found = True
            lines.append({"level": "match", "text": f"pattern \"{key}\" matched on page {hit['page']}: \"{hit['matched_text']}\""})
    if not any_found:
        lines.append({"level": "info", "text": "no recognized SAP-style parameters (alpha, power, endpoint, stratification) found in text"})
    n_tables = len(result.get("tables_found", []))
    if n_tables:
        lines.append({"level": "info", "text": f"{n_tables} table structure(s) detected via cell-boundary analysis"})
    return lines


def _trace_image(result):
    lines = []
    w, h = result.get("image_size", (0, 0))
    lines.append({"level": "info", "text": f"image size {w}\u00d7{h} \u2014 converting to grayscale for OCR"})
    conf = result.get("ocr_mean_confidence")
    lines.append({"level": "info", "text": f"Tesseract OCR pass complete, mean character confidence {conf}%"})
    n_chars = result.get("n_chars_extracted", 0)
    if n_chars < 10:
        lines.append({"level": "review", "text": "very little text recognized -- image may be low-resolution, low-contrast, or non-textual"})
    else:
        lines.append({"level": "match", "text": f"{n_chars} characters recognized"})
    return lines


def _trace_json(result):
    lines = []
    if result.get("shape") == "record_list":
        lines.append({"level": "info", "text": f"parsed as a list of {result.get('n_records')} record objects, {len(result.get('columns', []))} distinct keys"})
        lines.extend(_trace_mapping_lines(result.get("mapping", [])))
    else:
        lines.append({"level": "info", "text": f"parsed as a nested metadata object, flattened to {result.get('n_fields')} leaf fields"})
        lines.extend(_trace_mapping_lines(result.get("mapping", [])))
    return lines
