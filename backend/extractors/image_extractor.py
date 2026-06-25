"""
Image extractor. Real OCR via pytesseract (Tesseract engine).
Used for scanned protocol pages, photographed lab notebooks, or
photographed plate maps where no digital file exists.
"""
import io
from PIL import Image
import pytesseract


def extract_image(content: bytes, filename: str) -> dict:
    try:
        img = Image.open(io.BytesIO(content))
    except Exception as e:
        return {"status": "error", "filename": filename, "error": f"Failed to open image: {e}"}

    try:
        # convert to grayscale for more reliable OCR on photographs/scans
        gray = img.convert("L")
        text = pytesseract.image_to_string(gray)
    except Exception as e:
        return {"status": "error", "filename": filename, "error": f"OCR failed: {e}"}

    try:
        ocr_data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT)
        confidences = [int(c) for c in ocr_data.get("conf", []) if str(c).lstrip("-").isdigit() and int(c) >= 0]
        mean_conf = round(sum(confidences) / len(confidences), 1) if confidences else 0.0
    except Exception:
        mean_conf = None

    n_chars = len(text.strip())

    return {
        "status": "ok",
        "filename": filename,
        "format": "image",
        "image_size": img.size,
        "n_chars_extracted": n_chars,
        "ocr_mean_confidence": mean_conf,
        "is_likely_useful": n_chars > 10,
        "extracted_text": text.strip()[:2000],
    }


if __name__ == "__main__":
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (700, 200), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), "Plate ID: PL-0042", fill="black")
    draw.text((20, 60), "Assay: Cell viability, 96-well", fill="black")
    draw.text((20, 100), "Readout: Luminescence (RLU)", fill="black")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    content = buf.getvalue()

    import json
    result = extract_image(content, "plate_label_photo.png")
    print(json.dumps(result, indent=2))
