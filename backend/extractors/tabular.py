"""
Tabular (CSV/TSV) extractor. Real pandas parsing -- no simulation.
Detects plate-map layout vs long-format table and reshapes accordingly.
"""
import io
import pandas as pd
from sdtm_mapping import map_columns, is_plate_layout


def extract_csv(content: bytes, filename: str, sep: str = ",") -> dict:
    try:
        df = pd.read_csv(io.BytesIO(content), sep=sep, dtype=str, keep_default_na=False)
    except Exception as e:
        return {
            "status": "error",
            "filename": filename,
            "error": f"Failed to parse as delimited text: {e}",
        }

    if df.empty or len(df.columns) == 0:
        return {"status": "error", "filename": filename, "error": "No columns detected in file."}

    columns = list(df.columns)
    first_col_values = df[columns[0]].tolist()[:20] if len(df) > 0 else []

    plate_detected = is_plate_layout(columns, first_col_values)

    if plate_detected:
        reshaped = _reshape_plate(df)
        mapping = map_columns(list(reshaped.columns))
        preview = reshaped.head(8).to_dict(orient="records")
        return {
            "status": "ok",
            "filename": filename,
            "detected_layout": "plate_map",
            "n_rows_raw": len(df),
            "n_wells_reshaped": len(reshaped),
            "columns": list(reshaped.columns),
            "mapping": mapping,
            "preview": preview,
            "raw_preview": df.head(8).to_dict(orient="records"),
        }

    mapping = map_columns(columns)
    preview = df.head(8).to_dict(orient="records")

    return {
        "status": "ok",
        "filename": filename,
        "detected_layout": "long_format",
        "n_rows": len(df),
        "n_cols": len(columns),
        "columns": columns,
        "mapping": mapping,
        "preview": preview,
    }


def _reshape_plate(df: pd.DataFrame) -> pd.DataFrame:
    """Reshape a wide plate-grid (rows=A-H/A-P, cols=1-12/1-24) into
    long format: one record per well with WELLID, ROW, COL, READOUT."""
    row_col = df.columns[0]
    records = []
    for _, row in df.iterrows():
        row_label = str(row[row_col]).strip().upper()
        for col in df.columns[1:]:
            try:
                col_num = int(str(col).strip())
            except ValueError:
                continue
            well_id = f"{row_label}{col_num:02d}"
            value = row[col]
            records.append({"WELLID": well_id, "ROW": row_label, "COL": col_num, "READOUT": value})
    return pd.DataFrame(records)


if __name__ == "__main__":
    sample_csv = b"USUBJID,AGE,SEX,AETOXGR\nRVMD-001,58,M,2\nRVMD-002,63,F,3\n"
    result = extract_csv(sample_csv, "test.csv")
    import json
    print(json.dumps(result, indent=2))

    plate_csv = (
        b"Row,1,2,3,4,5,6,7,8,9,10,11,12\n"
        b"A,0.12,0.15,0.11,0.13,1.2,1.4,1.1,1.3,0.9,0.95,0.88,0.92\n"
        b"B,0.10,0.14,0.12,0.16,1.1,1.3,1.0,1.2,0.85,0.91,0.80,0.89\n"
    )
    result2 = extract_csv(plate_csv, "plate.csv")
    print(json.dumps(result2, indent=2)[:800])
