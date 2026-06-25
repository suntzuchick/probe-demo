"""
Excel (XLSX) extractor. Real openpyxl/pandas parsing.
Handles multi-sheet workbooks -- a common real-world pattern where each
sheet is a different assay run, plate, or domain.
"""
import io
import pandas as pd
from sdtm_mapping import map_columns, is_plate_layout
from tabular import _reshape_plate


def extract_xlsx(content: bytes, filename: str) -> dict:
    try:
        xls = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    except Exception as e:
        return {"status": "error", "filename": filename, "error": f"Failed to open workbook: {e}"}

    sheet_names = xls.sheet_names
    if not sheet_names:
        return {"status": "error", "filename": filename, "error": "Workbook has no sheets."}

    sheets_out = []
    for sheet in sheet_names:
        try:
            df = xls.parse(sheet, dtype=str, keep_default_na=False)
        except Exception as e:
            sheets_out.append({"sheet_name": sheet, "status": "error", "error": str(e)})
            continue

        if df.empty or len(df.columns) == 0:
            sheets_out.append({"sheet_name": sheet, "status": "empty"})
            continue

        columns = [str(c) for c in df.columns]
        first_col_values = df[df.columns[0]].tolist()[:20]
        plate_detected = is_plate_layout(columns, first_col_values)

        if plate_detected:
            df.columns = columns
            reshaped = _reshape_plate(df)
            mapping = map_columns(list(reshaped.columns))
            sheets_out.append({
                "sheet_name": sheet,
                "status": "ok",
                "detected_layout": "plate_map",
                "n_rows_raw": len(df),
                "n_wells_reshaped": len(reshaped),
                "columns": list(reshaped.columns),
                "mapping": mapping,
                "preview": reshaped.head(6).to_dict(orient="records"),
            })
        else:
            mapping = map_columns(columns)
            sheets_out.append({
                "sheet_name": sheet,
                "status": "ok",
                "detected_layout": "long_format",
                "n_rows": len(df),
                "n_cols": len(columns),
                "columns": columns,
                "mapping": mapping,
                "preview": df.head(6).to_dict(orient="records"),
            })

    return {
        "status": "ok",
        "filename": filename,
        "format": "xlsx",
        "n_sheets": len(sheet_names),
        "sheet_names": sheet_names,
        "sheets": sheets_out,
    }


if __name__ == "__main__":
    import openpyxl
    from openpyxl import Workbook

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Demographics"
    ws1.append(["USUBJID", "AGE", "SEX", "ARMCD"])
    ws1.append(["RVMD-001", 58, "M", "DARA"])
    ws1.append(["RVMD-002", 63, "F", "CHEMO"])

    ws2 = wb.create_sheet("Plate1_Viability")
    ws2.append(["Row", 1, 2, 3, 4])
    ws2.append(["A", 0.95, 0.91, 0.20, 0.18])
    ws2.append(["B", 0.93, 0.89, 0.22, 0.15])

    buf = io.BytesIO()
    wb.save(buf)
    content = buf.getvalue()

    result = extract_xlsx(content, "assay_workbook.xlsx")
    import json
    print(json.dumps(result, indent=2)[:2000])
