"""
JSON extractor. Real json.loads parsing. Flattens nested metadata
structures and maps keys against the same SDTM/assay vocabulary.
Handles both a single record and a list of records.
"""
import json
from sdtm_mapping import map_columns


def _flatten(obj, prefix=""):
    """Flatten nested dict/list structure into dotted-key leaf values."""
    flat = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            flat.update(_flatten(v, key))
    elif isinstance(obj, list):
        if len(obj) > 0 and all(isinstance(x, dict) for x in obj):
            # list of records -- treat as rows, not a single flat object
            flat[prefix or "_root_list"] = obj
        else:
            flat[prefix or "_root_value"] = obj
    else:
        flat[prefix or "_value"] = obj
    return flat


def extract_json(content: bytes, filename: str) -> dict:
    try:
        data = json.loads(content.decode("utf-8"))
    except Exception as e:
        return {"status": "error", "filename": filename, "error": f"Invalid JSON: {e}"}

    if isinstance(data, list) and len(data) > 0 and all(isinstance(x, dict) for x in data):
        # list-of-records: treat like a table
        all_keys = []
        for row in data:
            for k in row.keys():
                if k not in all_keys:
                    all_keys.append(k)
        mapping = map_columns(all_keys)
        return {
            "status": "ok",
            "filename": filename,
            "format": "json",
            "shape": "record_list",
            "n_records": len(data),
            "columns": all_keys,
            "mapping": mapping,
            "preview": data[:8],
        }

    if isinstance(data, dict):
        flat = _flatten(data)
        leaf_keys = [k for k, v in flat.items() if not isinstance(v, (list, dict))]
        mapping = map_columns(leaf_keys)
        return {
            "status": "ok",
            "filename": filename,
            "format": "json",
            "shape": "metadata_object",
            "n_fields": len(leaf_keys),
            "fields": leaf_keys,
            "mapping": mapping,
            "preview": {k: flat[k] for k in leaf_keys[:20]},
        }

    return {"status": "error", "filename": filename, "error": "Unsupported JSON shape (expected object or list of records)."}


if __name__ == "__main__":
    sample = json.dumps([
        {"usubjid": "RVMD-001", "age": 58, "sex": "M", "armcd": "DARA"},
        {"usubjid": "RVMD-002", "age": 63, "sex": "F", "armcd": "CHEMO"},
    ]).encode()
    print(json.dumps(extract_json(sample, "subjects.json"), indent=2)[:1200])

    print("---")
    meta = json.dumps({
        "protocol": {"study_id": "RASOLUTE302-SIM", "sponsor": "RevMed"},
        "assay": {"plate_id": "PL-001", "readout_type": "luminescence"},
    }).encode()
    print(json.dumps(extract_json(meta, "metadata.json"), indent=2)[:1200])
