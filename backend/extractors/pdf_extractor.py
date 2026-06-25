"""
PDF extractor. Real text + table extraction via pdfplumber.
Used for protocol documents, SAPs, and lab notes -- extracts candidate
analysis parameters with real page-number provenance, per the SAP
extraction sub-harness design (no inference; exact-string match only).
"""
import io
import re
import pdfplumber

# Parameters worth scanning for in a protocol/SAP-style document.
# Each pattern captures the literal value as written -- no inference.
PARAMETER_PATTERNS = {
    "alpha": re.compile(r"\balpha\s*[=:]\s*(0?\.\d+)", re.IGNORECASE),
    "power": re.compile(r"\bpower\s*[=:]\s*(\d{1,3}%|0?\.\d+)", re.IGNORECASE),
    "sample_size": re.compile(r"\b(?:sample size|n\s*=)\s*[=:]?\s*(\d+)", re.IGNORECASE),
    "primary_endpoint": re.compile(r"\bprimary endpoint[s]?\s*[:\-]?\s*([A-Za-z0-9 ,\-\(\)]{3,60})", re.IGNORECASE),
    "ecog": re.compile(r"\bECOG\b[^.\n]{0,80}", re.IGNORECASE),
    "stratification": re.compile(r"\bstratif(?:y|ied|ication)\b[^.\n]{0,100}", re.IGNORECASE),
}


def extract_pdf(content: bytes, filename: str) -> dict:
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception as e:
        return {"status": "error", "filename": filename, "error": f"Failed to open PDF: {e}"}

    pages_text = []
    all_tables = []
    found_params = {key: [] for key in PARAMETER_PATTERNS}

    for i, page in enumerate(pdf.pages):
        page_num = i + 1
        text = page.extract_text() or ""
        pages_text.append(text)

        for key, pattern in PARAMETER_PATTERNS.items():
            for m in pattern.finditer(text):
                snippet = m.group(0).strip()
                found_params[key].append({
                    "page": page_num,
                    "matched_text": snippet[:160],
                })

        try:
            tables = page.extract_tables()
        except Exception:
            tables = []
        for t_idx, table in enumerate(tables):
            if table and len(table) > 1:
                all_tables.append({
                    "page": page_num,
                    "table_index": t_idx,
                    "n_rows": len(table),
                    "n_cols": len(table[0]) if table[0] else 0,
                    "preview": table[:5],
                })

    pdf.close()

    full_text = "\n".join(pages_text)
    n_chars = len(full_text.strip())

    return {
        "status": "ok",
        "filename": filename,
        "format": "pdf",
        "n_pages": len(pages_text),
        "n_chars_extracted": n_chars,
        "is_text_extractable": n_chars > 20,
        "extracted_parameters": found_params,
        "tables_found": all_tables,
        "text_preview": full_text[:1500],
    }


if __name__ == "__main__":
    # build a tiny test PDF with reportlab if available, else skip
    try:
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(72, 700, "Statistical Analysis Plan -- RASolute 302 (simulated)")
        c.drawString(72, 680, "Primary endpoint: Overall survival (OS) in the ITT population.")
        c.drawString(72, 660, "The primary analysis will use alpha = 0.025 (two-sided).")
        c.drawString(72, 640, "Power = 90% to detect a hazard ratio of 0.65.")
        c.drawString(72, 620, "Stratification by ECOG performance status (0 vs 1) at randomization.")
        c.save()
        content = buf.getvalue()
        import json
        result = extract_pdf(content, "test_sap.pdf")
        print(json.dumps(result, indent=2))
    except ImportError:
        print("reportlab not installed -- skipping self-test PDF generation")
