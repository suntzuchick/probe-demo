"""
Probe extraction agent. Single entry point: takes (filename, bytes),
detects real format via magic bytes, dispatches to the matching
real parser, returns a structured ExtractionResult.

This is the one place in the pipeline where ambiguous, messy input
becomes structured candidate output -- the agent's job, same shape
as the harness design's LLM containment boundary, except here the
"agent" is a deterministic dispatcher + per-format parser, not a
model call. (A model-backed classifier can be swapped in behind the
same interface later without changing anything downstream.)
"""
import hashlib
import time

from format_detect import detect_format
from tabular import extract_csv
from excel import extract_xlsx
from json_extractor import extract_json
from pdf_extractor import extract_pdf
from image_extractor import extract_image


def extract(filename: str, content: bytes) -> dict:
    t0 = time.time()
    fmt = detect_format(filename, content)
    file_hash = hashlib.sha256(content).hexdigest()

    if fmt == "csv":
        result = extract_csv(content, filename, sep=",")
    elif fmt == "tsv":
        result = extract_csv(content, filename, sep="\t")
    elif fmt == "xlsx":
        result = extract_xlsx(content, filename)
    elif fmt == "json":
        result = extract_json(content, filename)
    elif fmt == "pdf":
        result = extract_pdf(content, filename)
    elif fmt in ("image_png", "image_jpeg"):
        result = extract_image(content, filename)
    else:
        result = {
            "status": "error",
            "filename": filename,
            "error": f"Unsupported or unrecognized format ('{fmt}'). "
                     f"Supported: CSV, TSV, XLSX, PDF, PNG/JPEG images, JSON.",
        }

    result["detected_format"] = fmt
    result["file_hash"] = file_hash
    result["file_size_bytes"] = len(content)
    result["extraction_time_ms"] = round((time.time() - t0) * 1000, 1)
    result["filename"] = filename
    return result


if __name__ == "__main__":
    import json
    samples = [
        ("dm.csv", b"USUBJID,AGE,SEX\nRVMD-001,58,M\n"),
        ("meta.json", b'{"plate_id": "PL1", "readout_type": "RLU"}'),
        ("notes.txt", b"this is a plain note file with no structure"),
    ]
    for fn, content in samples:
        r = extract(fn, content)
        print(fn, "->", r["status"], "| format:", r["detected_format"], "| time:", r["extraction_time_ms"], "ms")
