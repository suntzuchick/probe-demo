"""
Format detection for the Probe extraction agent.
Uses magic bytes / content sniffing, not file extensions, per the
ingestion-layer design (extensions lie; bytes don't).
"""
import json
import zipfile


def detect_format(filename: str, content: bytes) -> str:
    """
    Returns one of: "csv", "tsv", "xlsx", "pdf", "image_png", "image_jpeg",
    "json", "unknown"
    """
    head = content[:8]

    if head[:4] == b"%PDF":
        return "pdf"

    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image_png"

    if head[:3] == b"\xff\xd8\xff":
        return "image_jpeg"

    # XLSX is a zip archive with specific internal structure
    if head[:2] == b"PK":
        try:
            with zipfile.ZipFile.__new__(zipfile.ZipFile) as _:
                pass
        except Exception:
            pass
        try:
            import io
            zf = zipfile.ZipFile(io.BytesIO(content))
            names = zf.namelist()
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
            if any(n.startswith("word/") for n in names):
                return "docx"
        except Exception:
            pass
        return "zip_unknown"

    # JSON: try parsing
    stripped = content.strip()
    if stripped[:1] in (b"{", b"["):
        try:
            json.loads(content.decode("utf-8", errors="strict"))
            return "json"
        except Exception:
            pass

    # text-based: distinguish csv vs tsv by sniffing first line
    try:
        text_head = content[:4096].decode("utf-8", errors="strict")
        first_line = text_head.split("\n", 1)[0]
        if "\t" in first_line and first_line.count("\t") >= first_line.count(","):
            return "tsv"
        if "," in first_line or filename.lower().endswith(".csv"):
            return "csv"
        return "text_unknown"
    except UnicodeDecodeError:
        return "binary_unknown"


if __name__ == "__main__":
    tests = [
        ("a.csv", b"USUBJID,AGE,SEX\n001,45,M\n"),
        ("a.json", b'{"USUBJID": "001", "AGE": 45}'),
        ("a.pdf", b"%PDF-1.4 fake header"),
        ("a.png", b"\x89PNG\r\n\x1a\nrest"),
    ]
    for fn, content in tests:
        print(fn, "->", detect_format(fn, content))
