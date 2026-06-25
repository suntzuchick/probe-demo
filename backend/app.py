"""
Probe backend v2. Flask app exposing:

  POST /api/session                    -> create a new session
  POST /api/upload                     -> real extraction; returns result + trace log
  POST /api/load-sample                -> load bundled RASolute sample (real extraction path)
  POST /api/confirm-mapping            -> human confirms a domain assignment
  GET  /api/session/<sid>/status        -> domain completeness
  GET  /api/derive/plan                 -> context-aware derivation plan for the session
  POST /api/derive                      -> contextual derivation, saved into notebook state
  POST /api/notebook/run                -> execute a real notebook cell against session state
  GET  /api/session/<sid>/notebook       -> list all cells run so far
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "extractors"))

from flask import Flask, request, jsonify
from flask_cors import CORS
import pandas as pd

from agent import extract
from extraction_trace import build_trace
import session_store as store
import derive_adam
import derive_contextual
import notebook_engine
import validate as _validate
import auth as _auth

from flask import redirect, send_from_directory

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = Flask(__name__)
CORS(app, expose_headers=["X-Probe-Token"])


# ── Auth middleware ────────────────────────────────────────────────────────────

def _session_token_from_request():
    return request.headers.get("X-Probe-Token") or request.args.get("probe_token")


@app.before_request
def enforce_auth():
    """Protect all /api/ routes when ALLOWED_EMAILS is configured."""
    if not _auth.auth_enabled():
        return  # dev mode — no auth required
    if not request.path.startswith("/api/"):
        return  # static files are public; auth happens client-side
    if request.path.startswith("/api/auth/"):
        return  # auth endpoints are always public
    token = _session_token_from_request()
    if not _auth.validate_session(token or ""):
        return jsonify({"error": "unauthorized", "code": "auth_required"}), 401


# ── Frontend static serving ────────────────────────────────────────────────────

@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(FRONTEND_DIR, filename)


# ── Auth endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/auth/request", methods=["POST"])
def auth_request_link():
    body  = request.get_json(force=True)
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "A valid email address is required."}), 400

    if not _auth.auth_enabled():
        return jsonify({"status": "sent"})  # dev mode — always succeed

    # Always return the same response whether email is on allowlist or not
    # (prevents allowlist enumeration)
    if _auth.is_allowed(email):
        token = _auth.create_magic_token(email)
        sent  = _auth.send_magic_link(email, token)
        if not sent:
            return jsonify({"error": "Could not send the email. Contact sera@thingblinglabs.io."}), 500

    return jsonify({"status": "sent"})


@app.route("/api/auth/verify", methods=["GET"])
def auth_verify():
    token = request.args.get("token", "")
    session_token = _auth.verify_magic_token(token)
    if not session_token:
        return (
            "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif;padding:48px;color:#333;max-width:480px;margin:auto'>"
            "<h2 style='color:#4E0C70'>Link expired or already used</h2>"
            "<p>Magic links expire after 15 minutes and can only be used once.</p>"
            "<p><a href='/' style='color:#2289FA'>← Request a new link</a></p>"
            "</body></html>"
        ), 400

    base = os.environ.get("APP_BASE_URL", "").rstrip("/")
    return redirect(f"{base}/?probe_token={session_token}", code=302)


@app.route("/api/auth/status", methods=["GET"])
def auth_status():
    token = _session_token_from_request()
    email = _auth.validate_session(token or "") if token else None
    return jsonify({"authenticated": bool(email), "email": email or ""})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = _session_token_from_request()
    if token:
        _auth.revoke_session(token)
    return jsonify({"status": "ok"})

SAMPLE_DATA_DIR = os.path.join(os.path.dirname(__file__), "sample_data")
NOTEBOOK_STATE_ROOT = os.path.join(os.path.dirname(__file__), "notebook_state")


def _session_dir(sid):
    return os.path.join(NOTEBOOK_STATE_ROOT, sid)


def _slug(name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "data"


def _auto_save_domain(sid: str, filename: str, content: bytes, extraction_result: dict):
    """
    Save uploaded data to notebook state without requiring confirm-mapping.
    XLSX workbooks: each sheet becomes its own named variable.
    CSV/TSV: domain is detected from column mappings; variable name is the
    domain code (e.g. 'lb') or a filename slug for unrecognised data.
    """
    fmt = extraction_result.get("detected_format", "")
    sdir = _session_dir(sid)
    os.makedirs(sdir, exist_ok=True)
    sess = store.get_session(sid)

    if fmt == "xlsx" and extraction_result.get("sheets"):
        _save_xlsx_sheets(filename, content, extraction_result, sdir, sess)
        return

    # Single-sheet path (CSV / TSV / other tabular)
    domain = derive_contextual.auto_detect_domain(extraction_result)
    if not domain:
        return

    try:
        sep = "\t" if fmt == "tsv" else ","
        df = pd.read_csv(io.BytesIO(content), sep=sep, dtype=str, keep_default_na=False)
    except Exception:
        return

    if df is None or df.empty:
        return

    var_name = _slug(filename.rsplit(".", 1)[0]) if domain == "DATA" else domain.lower()
    notebook_engine.save_state(sdir, var_name, df)

    mappings = {
        m["source_label"]: {"top_match": m["top_match"], "confidence": m["confidence"], "action": m["action"]}
        for m in extraction_result.get("mapping", [])
    }
    sess["domain_data"][domain] = []
    sess["domain_source"][domain] = {
        "filename": filename, "var_name": var_name,
        "n_rows": len(df), "auto_detected": True, "mappings": mappings,
    }


def _save_xlsx_sheets(filename, content, extraction_result, sdir, sess):
    """Save each sheet of an XLSX workbook as its own notebook variable,
    applying the same plate-reshape logic that the extractor already ran."""
    from sdtm_mapping import is_plate_layout
    from tabular import _reshape_plate

    try:
        xls = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    except Exception:
        return

    sheet_meta = {s["sheet_name"]: s for s in extraction_result.get("sheets", []) if s.get("status") == "ok"}

    for sheet_name in xls.sheet_names:
        meta = sheet_meta.get(sheet_name)
        if not meta:
            continue
        try:
            raw_df = xls.parse(sheet_name, dtype=str, keep_default_na=False)
            if raw_df.empty:
                continue

            cols = [str(c) for c in raw_df.columns]
            raw_df.columns = cols

            if meta.get("detected_layout") == "plate_map" and is_plate_layout(cols, raw_df[cols[0]].tolist()[:20]):
                df = _reshape_plate(raw_df)
            else:
                df = raw_df

            var_name = _slug(sheet_name)
            notebook_engine.save_state(sdir, var_name, df)

            mappings = {
                m["source_label"]: {"top_match": m["top_match"], "confidence": m["confidence"], "action": m["action"]}
                for m in meta.get("mapping", [])
            }
            domain_key = "SHEET_" + _slug(sheet_name).upper()
            sess["domain_data"][domain_key] = []
            sess["domain_source"][domain_key] = {
                "filename": filename, "sheet_name": sheet_name, "var_name": var_name,
                "n_rows": len(df), "layout": meta.get("detected_layout", "long_format"),
                "auto_detected": True, "mappings": mappings,
            }
        except Exception:
            continue


# -------------------- session --------------------

@app.route("/api/session", methods=["POST"])
def create_session():
    sid = store.new_session()
    os.makedirs(_session_dir(sid), exist_ok=True)
    return jsonify({"session_id": sid})


@app.route("/api/session/info", methods=["POST"])
def session_info():
    body = request.get_json(force=True)
    sid = body.get("session_id")
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    sess = store.get_session(sid)
    sess["user_info"] = {
        "name":         body.get("name", "").strip(),
        "organization": body.get("organization", "").strip(),
        "email":        body.get("email", "").strip(),
        "project":      body.get("project", "").strip(),
    }
    return jsonify({"status": "ok"})


@app.route("/api/session/<sid>/status", methods=["GET"])
def session_status(sid):
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    sess = store.get_session(sid)
    domain_rows = {d: len(sess["domain_data"][d]) for d in sess["domain_data"]}
    plan = derive_contextual.get_plan(sess)
    return jsonify({
        "session_id": sid,
        "files_uploaded": list(sess["files"].keys()),
        "domains_mapped": list(sess["domain_data"].keys()),
        "domain_row_counts": domain_rows,
        "context": plan["context"],
        "context_label": plan["context_label"],
        "notebook_vars": notebook_engine.available_vars(_session_dir(sid)),
    })


# -------------------- upload / extraction --------------------

@app.route("/api/upload", methods=["POST"])
def upload():
    sid = request.form.get("session_id")
    if not sid or not store.session_exists(sid):
        return jsonify({"error": "missing or unknown session_id"}), 400
    if "file" not in request.files:
        return jsonify({"error": "no file in request"}), 400

    f = request.files["file"]
    filename = f.filename
    content = f.read()

    result = extract(filename, content)
    trace = build_trace(result)

    sess = store.get_session(sid)
    sess["files"][filename] = result

    # Auto-detect domain and persist to notebook state without requiring confirm-mapping
    _auto_save_domain(sid, filename, content, result)

    return jsonify({"extraction": result, "trace": trace})


@app.route("/api/load-sample", methods=["POST"])
def load_sample():
    body = request.get_json(force=True)
    sid = body.get("session_id")
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404

    sess = store.get_session(sid)
    domain_files = {"DM": "DM.csv", "EX": "EX.csv", "AE": "AE.csv", "RS": "RS.csv", "DS": "DS.csv"}
    loaded = {}
    all_traces = []

    for domain, fname in domain_files.items():
        path = os.path.join(SAMPLE_DATA_DIR, fname)
        with open(path, "rb") as fh:
            content = fh.read()
        extraction = extract(fname, content)
        trace = build_trace(extraction)
        all_traces.append({"filename": fname, "domain": domain, "trace": trace})

        sess["files"][fname] = extraction
        df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        full_rows = df.to_dict(orient="records")
        sess["domain_data"][domain] = full_rows
        sess["domain_source"][domain] = {"filename": fname, "n_rows": len(full_rows)}

        notebook_engine.save_state(_session_dir(sid), domain.lower(), df)

        loaded[domain] = {"filename": fname, "n_rows": len(full_rows)}

    return jsonify({
        "status": "ok", "loaded": loaded, "traces": all_traces,
    })


@app.route("/api/confirm-mapping", methods=["POST"])
def confirm_mapping():
    body = request.get_json(force=True)
    sid = body.get("session_id")
    filename = body.get("filename")
    domain = body.get("domain")
    column_map = body.get("column_map", {})
    sheet_name = body.get("sheet_name")

    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404

    sess = store.get_session(sid)
    if filename not in sess["files"]:
        return jsonify({"error": f"file '{filename}' was not uploaded in this session"}), 400

    file_result = sess["files"][filename]

    if file_result.get("detected_format") == "xlsx":
        sheet = next((s for s in file_result["sheets"] if s["sheet_name"] == sheet_name), None)
        if sheet is None:
            return jsonify({"error": f"sheet '{sheet_name}' not found"}), 400
        preview_rows = sheet.get("preview", [])
    else:
        preview_rows = file_result.get("preview", [])

    if not preview_rows:
        return jsonify({"error": "no row data available to map"}), 400

    renamed_rows = [{column_map.get(k, k): v for k, v in row.items()} for row in preview_rows]

    sess["domain_data"][domain] = renamed_rows
    sess["domain_source"][domain] = {"filename": filename, "sheet_name": sheet_name, "n_rows": len(renamed_rows)}

    df = pd.DataFrame(renamed_rows)
    notebook_engine.save_state(_session_dir(sid), domain.lower(), df)

    return jsonify({
        "status": "ok", "domain": domain, "n_rows_mapped": len(renamed_rows),
        "missing_domains": store.missing_domains(sid),
    })


# -------------------- derivation plan + derivation --------------------

def _load_all_dfs(sdir: str) -> dict:
    """Load all persisted DataFrames from the session directory."""
    dfs = {}
    for v in notebook_engine.available_vars(sdir):
        df = notebook_engine.load_state(sdir, v)
        if df is not None:
            dfs[v] = df
    return dfs


@app.route("/api/derive/plan", methods=["GET"])
def derive_plan():
    sid = request.args.get("session_id")
    if not sid or not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    sess = store.get_session(sid)
    sdir = _session_dir(sid)
    dfs = _load_all_dfs(sdir)
    return jsonify(derive_contextual.get_plan(sess, dfs=dfs))


@app.route("/api/derive", methods=["POST"])
def derive():
    body = request.get_json(force=True)
    sid = body.get("session_id")
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404

    sess = store.get_session(sid)
    sdir = _session_dir(sid)
    dfs = _load_all_dfs(sdir)
    plan = derive_contextual.get_plan(sess, dfs=dfs)
    context = plan["context"]

    try:
        if context == "clinical_trial":
            result = _derive_clinical(sess, sdir)
        elif context == "lab_assay":
            result = _derive_lab(sdir)
        elif context == "plate_assay":
            result = _derive_plate(sdir)
        elif context == "time_series":
            result = _derive_time_series(sdir, dfs)
        elif context == "expression_matrix":
            result = _derive_expression(sdir, dfs)
        elif context == "grouped_comparison":
            result = _derive_grouped(sdir, dfs)
        else:
            result = _derive_generic(sdir)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 200

    if result["status"] != "ok":
        return jsonify(result), 200

    # Store provenance into session before persisting datasets
    if "provenance" in result:
        sess["derivation_meta"] = result["provenance"]

    # Persist all derived datasets and build the response payload
    datasets = {}
    for key, df in result["derived"].items():
        notebook_engine.save_state(sdir, key, df)
        datasets[key] = {"rows": len(df), "columns": list(df.columns)}

    return jsonify({
        "status": "ok",
        "context": context,
        "context_label": plan["context_label"],
        "datasets": datasets,
    })


def _derive_clinical(sess, sdir):
    missing = [d for d in ["DM", "EX", "AE", "RS", "DS"] if d not in sess.get("domain_data", {})]
    if missing:
        return {"status": "incomplete", "missing_domains": missing}

    dm = notebook_engine.load_state(sdir, "dm")
    ex = notebook_engine.load_state(sdir, "ex")
    ae = notebook_engine.load_state(sdir, "ae")
    ds = notebook_engine.load_state(sdir, "ds")

    if "AGE" in dm.columns:
        dm["AGE"] = pd.to_numeric(dm["AGE"], errors="coerce")
    if "ECOGBL" in dm.columns:
        dm["ECOGBL"] = pd.to_numeric(dm["ECOGBL"], errors="coerce")

    adsl  = derive_adam.derive_adsl(dm, ex, ds)
    adae  = derive_adam.derive_adae(ae, adsl)
    adtte = derive_adam.derive_adtte(adsl)

    provenance = {
        "recipe": "clinical_trial",
        "fired_because": (
            "All 5 SDTM domains present (DM, EX, AE, RS, DS). "
            "ADaM pipeline: ADSL from DM+EX+DS, ADAE from AE+ADSL, ADTTE (OS endpoint) from ADSL."
        ),
        # Read directly from derive_adam.py — lives next to the code that produces each column.
        "variable_origins": derive_adam.PIPELINE_PROVENANCE,
        "low_confidence": [],
    }
    return {"status": "ok", "derived": {"adsl": adsl, "adae": adae, "adtte": adtte}, "provenance": provenance}


def _derive_lab(sdir):
    lb = notebook_engine.load_state(sdir, "lb")
    lb_var = "lb"
    if lb is None:
        for v in notebook_engine.available_vars(sdir):
            df = notebook_engine.load_state(sdir, v)
            if df is not None and derive_contextual._col(df, ["LBORRES", "RESULT", "VALUE"]):
                lb = df
                lb_var = v
                break
    if lb is None:
        return {"status": "error", "error": "No lab data found in session."}

    lb_summary = derive_contextual.derive_lb_summary(lb)
    lb_flags   = derive_contextual.derive_lb_flags(lb)
    lb_shifts  = derive_contextual.derive_lb_shifts(lb)

    provenance = {
        "recipe": "lab_assay",
        "fired_because": (
            f"Lab data detected in '{lb_var}' (contains result/value column). "
            "Derived summary statistics, abnormality flags against reference range, and shift table."
        ),
        "variable_origins": {
            f"lb_summary.mean":    {"source": f"{lb_var}.LBORRES",                    "transform": "grouped mean by test + visit",                   "confidence": 1.00},
            f"lb_flags.HIGH_FLAG": {"source": f"{lb_var}.LBORRES vs LBORNRHI",        "transform": "value > upper limit of normal reference range",  "confidence": 0.95},
            f"lb_flags.LOW_FLAG":  {"source": f"{lb_var}.LBORRES vs LBORNRLO",        "transform": "value < lower limit of normal reference range",  "confidence": 0.95},
            f"lb_shifts.SHIFT":    {"source": f"{lb_var}.LBORRES + reference range",  "transform": "normal/high/low classification per visit",       "confidence": 0.90},
        },
        "low_confidence": [],
    }
    return {"status": "ok", "derived": {"lb_summary": lb_summary, "lb_flags": lb_flags, "lb_shifts": lb_shifts}, "provenance": provenance}


def _derive_plate(sdir):
    """
    Build a proper plate-assay derivation:
    1. Identify signal sheet (numeric READOUT) and compound sheet (string READOUT).
    2. Join on WELLID; parse COMPOUND@CONCENTRATION format.
    3. Assign cell lines from a metadata sheet's PLATE_ROWS column if present.
    4. Normalise to viability %: (signal - blank_mean) / (dmso_mean - blank_mean) * 100.
    5. Derive PLATE_QC and DOSE_RESPONSE from the normalised table.
    """
    signal_df = compound_df = metadata_df = None
    signal_var = compound_var = metadata_var = None

    for v in notebook_engine.available_vars(sdir):
        df = notebook_engine.load_state(sdir, v)
        if df is None:
            continue
        cols_up = {c.upper() for c in df.columns}
        if "WELLID" in cols_up and "READOUT" in cols_up:
            numeric_frac = pd.to_numeric(df["READOUT"], errors="coerce").notna().mean()
            if numeric_frac > 0.7:
                if signal_df is None:
                    signal_df = df
                    signal_var = v
            else:
                if compound_df is None:
                    compound_df = df
                    compound_var = v
        elif "WELLID" not in cols_up and df.shape[0] < 50:
            metadata_df = df

    if signal_df is None:
        return {"status": "error", "error": "No plate signal data found — need a sheet with WELLID + numeric READOUT."}

    # ── 1. Start with signal ─────────────────────────────────────────────────
    plate = signal_df.copy()
    plate["SIGNAL"] = pd.to_numeric(plate["READOUT"], errors="coerce")
    plate = plate.drop(columns=["READOUT"])

    # ── 2. Join compound layout ──────────────────────────────────────────────
    if compound_df is not None:
        comp = compound_df[["WELLID", "READOUT"]].copy()

        def _parse_treatment(val):
            """Return (compound_name, concentration_float_or_None).

            Handles: 'COMPOUND@10.0uM', 'COMPOUND_10.0uM', 'COMPOUND_10.0',
                     'COMPOUND 10.0uM', plain control labels ('BLANK', 'DMSO').
            """
            if pd.isna(val):
                return str(val), None
            s = str(val).strip()
            for sep in ("@", "_", " "):
                m = re.match(
                    r'^(.+?)' + re.escape(sep) + r'([\d.]+)\s*(?:um|µm|nm|mm|pm)?$',
                    s, re.IGNORECASE
                )
                if m:
                    return m.group(1).strip(), float(m.group(2))
            return s, None

        parsed = comp["READOUT"].apply(_parse_treatment)
        comp["COMPOUND"] = parsed.apply(lambda t: t[0])
        comp["CONCENTRATION"] = pd.to_numeric(parsed.apply(lambda t: t[1]), errors="coerce")

        _blank_names  = {"blank", "media", "background", "pbs"}
        _dmso_names   = {"dmso", "vehicle", "control", "neg", "untreated"}
        def _control_type(name):
            n = str(name).lower()
            if n in _blank_names:  return "blank"
            if n in _dmso_names:   return "dmso"
            return "treated"
        comp["CONTROL_TYPE"] = comp["COMPOUND"].apply(_control_type)

        comp = comp.drop(columns=["READOUT"])
        plate = plate.merge(comp, on="WELLID", how="left")

    # ── 3. Assign cell lines from metadata PLATE_ROWS ───────────────────────
    if metadata_df is not None:
        rows_col = derive_contextual._col(metadata_df, ["PLATE_ROWS", "ROWS", "ROW_RANGE"])
        name_col = (derive_contextual._col(metadata_df, ["CELL_LINE_NAME", "CELL_LINE_ID", "SAMPLEID"]) or
                    next((c for c in metadata_df.columns if "name" in c.lower() or "id" in c.lower()), None))
        if rows_col and name_col and "ROW" in plate.columns:
            for _, row in metadata_df.iterrows():
                spec = str(row[rows_col]).strip()
                label = str(row[name_col])
                if "-" in spec:
                    a, b = spec.split("-", 1)
                    letters = [chr(r) for r in range(ord(a.strip().upper()), ord(b.strip().upper()) + 1)]
                else:
                    letters = [r.strip().upper() for r in spec.split(",")]
                plate.loc[plate["ROW"].isin(letters), "CELL_LINE"] = label

    # ── 4. Normalise to viability % ──────────────────────────────────────────
    if "CONTROL_TYPE" in plate.columns:
        blank_mask = plate["CONTROL_TYPE"] == "blank"
        dmso_mask  = plate["CONTROL_TYPE"] == "dmso"
        blank_mean = plate.loc[blank_mask, "SIGNAL"].mean() if blank_mask.any() else 0.0
        if pd.isna(blank_mean):
            blank_mean = 0.0

        group_col = "CELL_LINE" if "CELL_LINE" in plate.columns else None
        groups = plate.groupby(group_col) if group_col else [(None, plate)]
        normed = []
        for _, grp in groups:
            grp = grp.copy()
            dmso_mean = grp.loc[dmso_mask.loc[grp.index], "SIGNAL"].mean()
            if pd.isna(dmso_mean) or dmso_mean == blank_mean:
                dmso_mean = grp["SIGNAL"].max()
            denom = dmso_mean - blank_mean
            grp["VIABILITY_PCT"] = ((grp["SIGNAL"] - blank_mean) / denom * 100).round(2) if denom else 0.0
            normed.append(grp)
        plate = pd.concat(normed).sort_index()

    # ── 5. Derive QC + dose-response ─────────────────────────────────────────
    plate_qc = derive_contextual.derive_plate_qc(plate.rename(columns={"SIGNAL": "READOUT"}, errors="ignore"))

    viability_col = "VIABILITY_PCT" if "VIABILITY_PCT" in plate.columns else "SIGNAL"
    treat_mask = plate["CONTROL_TYPE"] == "treated" if "CONTROL_TYPE" in plate.columns else pd.Series(True, index=plate.index)
    treat = plate[treat_mask]
    if "CONCENTRATION" in plate.columns and treat_mask.any():
        group_cols = ["CELL_LINE", "CONCENTRATION"] if "CELL_LINE" in treat.columns else ["CONCENTRATION"]
        dose_response = (
            treat.groupby(group_cols)[viability_col]
            .agg(N="count", mean_viability="mean", sd="std")
            .round(2).reset_index()
        )
    else:
        dose_response = derive_contextual.derive_dose_response(plate)

    # ── Build provenance ─────────────────────────────────────────────────────
    origins = {}
    origins["plate_assay.SIGNAL"] = {
        "source": f"{signal_var}.READOUT",
        "transform": "coerced to numeric via pd.to_numeric",
        "confidence": 1.00,
    }
    if compound_df is not None:
        origins["plate_assay.COMPOUND"] = {
            "source": f"{compound_var}.READOUT",
            "transform": "treatment string parsed: name before separator (@, _, space)",
            "confidence": 0.90,
        }
        origins["plate_assay.CONCENTRATION"] = {
            "source": f"{compound_var}.READOUT",
            "transform": "treatment string parsed: numeric suffix, unit stripped (µM)",
            "confidence": 0.90,
        }
        origins["plate_assay.CONTROL_TYPE"] = {
            "source": f"{compound_var}.COMPOUND",
            "transform": "rule-based classification: blank / dmso / treated",
            "confidence": 0.95,
        }
    if "VIABILITY_PCT" in plate.columns:
        origins["plate_assay.VIABILITY_PCT"] = {
            "source": "plate_assay.SIGNAL",
            "transform": "(signal − blank_mean) / (dmso_mean − blank_mean) × 100",
            "confidence": 0.85,
        }
    origins["dose_response.CONCENTRATION"] = {
        "source": "plate_assay.CONCENTRATION",
        "transform": "groupby key (treated wells only, per CELL_LINE)",
        "confidence": 0.90,
    }
    origins["dose_response.mean_viability"] = {
        "source": "plate_assay.VIABILITY_PCT",
        "transform": "mean per concentration per cell line",
        "confidence": 0.85,
    }

    sheet_parts = [f"{signal_var} (numeric signal)"]
    if compound_df is not None:
        sheet_parts.append(f"{compound_var} (treatment strings → compound + concentration)")
    provenance = {
        "recipe": "plate_assay",
        "fired_because": (
            f"Detected {len(sheet_parts)} plate-format sheet(s): {' + '.join(sheet_parts)}. "
            "Sheets joined on WELLID. Treatment strings parsed into COMPOUND + CONCENTRATION columns."
        ),
        "variable_origins": origins,
        "low_confidence": [k for k, v in origins.items() if v["confidence"] < 0.90],
    }

    return {
        "status": "ok",
        "derived": {"plate_assay": plate, "plate_qc": plate_qc, "dose_response": dose_response},
        "provenance": provenance,
    }


def _derive_time_series(sdir, dfs):
    if not dfs:
        return {"status": "error", "error": "No data found in session."}
    primary_var, primary_df = next(iter(dfs.items()))

    time_col = next(
        (c for c in primary_df.columns
         if any(kw in c.lower() for kw in derive_contextual._TIME_KEYWORDS)),
        None,
    )
    if time_col is None:
        return _derive_generic(sdir)

    time_index    = derive_contextual.derive_time_index(primary_df, time_col)
    trend_summary = derive_contextual.derive_trend_summary(primary_df, time_col)

    n_vars = len(primary_df.select_dtypes(include="number").columns)
    provenance = {
        "recipe": "time_series",
        "fired_because": (
            f"Column '{time_col}' in '{primary_var}' identified as a temporal index "
            f"({len(primary_df)} observations, {n_vars} numeric variable(s)). "
            "Derived time-ordered index with inter-observation delta and per-variable linear trend analysis."
        ),
        "variable_origins": {
            "time_index.TIME_DELTA": {
                "source": f"{primary_var}.{time_col}",
                "transform": "consecutive difference after chronological sort",
                "confidence": 0.95,
            },
            "trend_summary.slope": {
                "source": f"{primary_var} numeric columns vs '{time_col}'",
                "transform": "numpy.polyfit(deg=1) — slope and R² per variable",
                "confidence": 0.90,
            },
        },
        "low_confidence": [],
    }
    return {
        "status": "ok",
        "derived": {"time_index": time_index, "trend_summary": trend_summary},
        "provenance": provenance,
    }


def _derive_expression(sdir, dfs):
    if not dfs:
        return {"status": "error", "error": "No data found in session."}
    primary_var, primary_df = next(iter(dfs.items()))

    num_cols = primary_df.select_dtypes(include="number").columns.tolist()
    n_features = len(primary_df)
    n_samples = len(num_cols)

    feature_variance = derive_contextual.derive_feature_variance(primary_df)
    sample_stats     = derive_contextual.derive_sample_stats(primary_df)
    top_variable     = derive_contextual.derive_top_variable(primary_df, n=50)

    provenance = {
        "recipe": "expression_matrix",
        "fired_because": (
            f"Wide numeric matrix detected in '{primary_var}': {n_features} feature rows × "
            f"{n_samples} sample columns ({round(n_samples / len(primary_df.columns) * 100)}% numeric). "
            "Derived per-feature variance ranking, per-sample detection statistics, and top-50 variable features."
        ),
        "variable_origins": {
            "feature_variance.variance": {
                "source": f"{primary_var} (all {n_samples} sample columns)",
                "transform": "pandas DataFrame.var(axis=1) per feature row",
                "confidence": 1.00,
            },
            "sample_stats.pct_detected": {
                "source": f"{primary_var} (all {n_features} feature rows)",
                "transform": "fraction of values > 0 per sample column",
                "confidence": 1.00,
            },
            "top_variable": {
                "source": "feature_variance",
                "transform": f"head(50) by descending variance from {n_features} features",
                "confidence": 1.00,
            },
        },
        "low_confidence": [],
    }
    return {
        "status": "ok",
        "derived": {"feature_variance": feature_variance, "sample_stats": sample_stats, "top_variable": top_variable},
        "provenance": provenance,
    }


def _derive_grouped(sdir, dfs):
    if not dfs:
        return {"status": "error", "error": "No data found in session."}
    primary_var, primary_df = next(iter(dfs.items()))

    group_col, num_cols = derive_contextual._pick_group_cols(primary_df)
    if not group_col:
        return _derive_generic(sdir)

    n_groups = primary_df[group_col].nunique()

    group_stats   = derive_contextual.derive_group_stats(primary_df)
    effect_sizes  = derive_contextual.derive_effect_sizes(primary_df)
    anova_summary = derive_contextual.derive_anova(primary_df)

    provenance = {
        "recipe": "grouped_comparison",
        "fired_because": (
            f"Group column '{group_col}' detected in '{primary_var}' ({n_groups} groups, "
            f"{len(num_cols)} numeric measurement(s)). Derived group statistics, Cohen's d effect sizes, "
            "and one-way ANOVA significance tests per measurement."
        ),
        "variable_origins": {
            "group_stats.mean": {
                "source": f"{primary_var}.{group_col} + numeric columns",
                "transform": "groupby mean / std / median / N",
                "confidence": 1.00,
            },
            "effect_sizes.cohens_d": {
                "source": f"{primary_var} — pairwise group combinations",
                "transform": "pooled-SD Cohen's d for each group pair × measurement",
                "confidence": 0.90,
            },
            "anova_summary.p_value": {
                "source": f"{primary_var} — all {n_groups} groups per measurement",
                "transform": "scipy.stats.f_oneway — one-way ANOVA",
                "confidence": 0.90,
            },
        },
        "low_confidence": [],
    }
    return {
        "status": "ok",
        "derived": {"group_stats": group_stats, "effect_sizes": effect_sizes, "anova_summary": anova_summary},
        "provenance": provenance,
    }


def _derive_generic(sdir):
    all_vars = notebook_engine.available_vars(sdir)
    dfs = {v: notebook_engine.load_state(sdir, v) for v in all_vars}
    dfs = {v: df for v, df in dfs.items() if df is not None}
    if not dfs:
        return {"status": "error", "error": "No data found in session to derive from."}

    profile         = derive_contextual.derive_profile(dfs)
    numeric_summary = derive_contextual.derive_numeric_summary(dfs)

    src_list = ", ".join(dfs.keys())
    provenance = {
        "recipe": "generic",
        "fired_because": (
            f"No specific scientific domain detected. Generated statistical profile and "
            f"numeric summary from all available variables ({src_list})."
        ),
        "variable_origins": {
            "profile":         {"source": src_list, "transform": "shape, dtypes, missing count, unique count per variable", "confidence": 1.00},
            "numeric_summary": {"source": src_list, "transform": "min / max / mean / std / quartiles for all numeric columns",  "confidence": 1.00},
        },
        "low_confidence": [],
    }
    return {"status": "ok", "derived": {"profile": profile, "numeric_summary": numeric_summary}, "provenance": provenance}


# -------------------- provenance --------------------

@app.route("/api/session/<sid>/provenance", methods=["GET"])
def session_provenance(sid):
    """Return derivation provenance for the notebook footer."""
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    sess = store.get_session(sid)

    domain_source   = sess.get("domain_source", {})
    derivation_meta = sess.get("derivation_meta", {})
    files           = sess.get("files", {})

    # Build raw_origins and held_for_review from the actual extraction mapping records
    # stored in sess["files"], keyed by the variable name each domain was saved as.
    raw_origins:    dict = {}   # "var.col" -> {source_file, source_col, confidence, action}
    held_for_review: list = []

    for domain, ds_meta in domain_source.items():
        var_name = domain.lower()
        filename = ds_meta.get("filename", "")
        sheet    = ds_meta.get("sheet_name")

        file_result = files.get(filename, {})
        if file_result.get("detected_format") == "xlsx" and sheet:
            mapping = next(
                (s.get("mapping", []) for s in file_result.get("sheets", [])
                 if s.get("sheet_name") == sheet),
                [],
            )
        else:
            mapping = file_result.get("mapping", [])

        for entry in mapping:
            action     = entry.get("action", "")
            source_col = entry.get("col", "")
            sdtm_col   = entry.get("top_match", source_col)
            confidence = entry.get("confidence", 0.0)

            if action not in ("AUTO_MAP", "SURFACE_TO_USER"):
                continue

            ref = f"{var_name}.{sdtm_col}"
            raw_origins[ref] = {
                "source_file": filename + (f":{sheet}" if sheet else ""),
                "source_col":  source_col,
                "confidence":  round(confidence, 2),
                "action":      action,
            }

            if action == "SURFACE_TO_USER":
                held_for_review.append({
                    "var":        var_name,
                    "col":        sdtm_col,
                    "source_col": source_col,
                    "mapped_to":  sdtm_col,
                    "confidence": round(confidence, 2),
                })

    return jsonify({
        "domain_source":   domain_source,
        "derivation_meta": derivation_meta,
        "raw_origins":     raw_origins,
        "held_for_review": held_for_review,
    })


# -------------------- notebook --------------------

@app.route("/api/notebook/run", methods=["POST"])
def notebook_run():
    body = request.get_json(force=True)
    sid = body.get("session_id")
    code = body.get("code", "")

    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    if not code.strip():
        return jsonify({"status": "error", "error": "empty cell"}), 200

    sdir = _session_dir(sid)
    result = notebook_engine.run_cell(sdir, code)

    sess = store.get_session(sid)
    cell_record = {"id": len(sess["canvas_cells"]) + 1, "code": code, "result": result}
    sess["canvas_cells"].append(cell_record)

    return jsonify(cell_record)


@app.route("/api/session/<sid>/notebook", methods=["GET"])
def notebook_list(sid):
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    sess = store.get_session(sid)
    return jsonify({"cells": sess["canvas_cells"]})


def _detect_quality_issues(df, var_name: str) -> list:
    """Return a list of quality issue dicts for a single DataFrame."""
    issues = []

    # ── Duplicate rows ────────────────────────────────────────────────────────
    dup = int(df.duplicated().sum())
    if dup:
        issues.append({"var": var_name, "col": None, "type": "duplicates",
                        "description": f"{dup} duplicate row{'s' if dup>1 else ''}",
                        "fix": "drop_duplicates", "fix_label": "Drop duplicate rows",
                        "severity": "medium"})

    for col in df.columns:
        s = df[col]
        null_count = int(s.isna().sum())
        pct = null_count / max(len(df), 1) * 100

        # ── Missing values ────────────────────────────────────────────────────
        if null_count:
            is_numeric = pd.api.types.is_numeric_dtype(s)
            fill_label = (f"Fill with mean ({s.mean():.3g})" if is_numeric else "Fill with 'UNKNOWN'")
            issues.append({"var": var_name, "col": col, "type": "missing",
                            "description": f"{null_count} null values ({pct:.1f}%)",
                            "fix": "fill_mean" if is_numeric else "fill_unknown",
                            "fix_label": fill_label,
                            "severity": "high" if pct > 20 else "medium" if pct > 5 else "low"})

        # ── Outliers (numeric) ────────────────────────────────────────────────
        if pd.api.types.is_numeric_dtype(s) and s.notna().sum() > 4:
            mu, sigma = float(s.mean()), float(s.std())
            if sigma > 0:
                n_out = int(((s - mu).abs() > 3 * sigma).sum())
                if n_out:
                    issues.append({"var": var_name, "col": col, "type": "outlier",
                                    "description": f"{n_out} value{'s' if n_out>1 else ''} beyond 3σ (mean={mu:.3g}, σ={sigma:.3g})",
                                    "fix": "cap_3sigma", "fix_label": "Cap at ±3σ",
                                    "severity": "medium"})

        # ── Mixed case (low-cardinality strings) ──────────────────────────────
        if s.dtype == object and 1 < s.nunique() <= 30:
            vals = s.dropna().astype(str)
            if vals.str.lower().nunique() < vals.nunique():
                sample = sorted(vals.unique())[:3]
                issues.append({"var": var_name, "col": col, "type": "mixed_case",
                                "description": f"Mixed case — e.g. {sample}",
                                "fix": "normalize_upper", "fix_label": "Normalise to uppercase",
                                "severity": "low"})

    return issues


def _apply_fix(df: pd.DataFrame, fix: dict) -> pd.DataFrame:
    col = fix.get("col")
    fix_type = fix.get("fix")
    if fix_type == "drop_duplicates":
        return df.drop_duplicates()
    if col not in df.columns:
        return df
    df = df.copy()
    if fix_type == "fill_mean":
        df[col] = df[col].fillna(df[col].mean())
    elif fix_type == "fill_unknown":
        df[col] = df[col].fillna("UNKNOWN")
    elif fix_type == "cap_3sigma":
        mu, sigma = df[col].mean(), df[col].std()
        df[col] = df[col].clip(mu - 3*sigma, mu + 3*sigma)
    elif fix_type == "normalize_upper":
        df[col] = df[col].str.upper()
    return df


@app.route("/api/quality/check", methods=["GET"])
def quality_check():
    sid = request.args.get("session_id")
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    sdir = _session_dir(sid)
    all_issues = []

    # Generic per-column checks (missing values, outliers, mixed case, duplicates)
    for v in notebook_engine.available_vars(sdir):
        df = notebook_engine.load_state(sdir, v)
        if df is not None:
            all_issues.extend(_detect_quality_issues(df, v))

    # Clinical SDTM checks: CT violations, structural rules, referential integrity
    clinical_issues = _validate.run_clinical_quality(
        sdir,
        notebook_engine.available_vars,
        notebook_engine.load_state,
    )

    # Suppress generic "missing values" noise for columns the clinical rules already flag
    clinical_covered = {(i.get("var"), i.get("col")) for i in clinical_issues if i.get("col")}
    filtered_generic = [
        i for i in all_issues
        if not (i["type"] == "missing" and (i["var"], i["col"]) in clinical_covered)
    ]
    all_issues = filtered_generic + clinical_issues

    return jsonify({"issues": all_issues})


@app.route("/api/quality/apply", methods=["POST"])
def quality_apply():
    body = request.get_json(force=True)
    sid = body.get("session_id")
    fixes = body.get("fixes", [])
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    sdir = _session_dir(sid)
    # Group fixes by variable name
    by_var: dict[str, list] = {}
    for fix in fixes:
        by_var.setdefault(fix["var"], []).append(fix)
    for var_name, var_fixes in by_var.items():
        df = notebook_engine.load_state(sdir, var_name)
        if df is None:
            continue
        for fix in var_fixes:
            df = _apply_fix(df, fix)
        notebook_engine.save_state(sdir, var_name, df)
    return jsonify({"status": "ok", "applied": len(fixes)})


@app.route("/api/export", methods=["GET"])
def export_xlsx():
    """Export all derived variables as a formatted XLSX workbook."""
    import io as _io
    from datetime import datetime
    sid = request.args.get("session_id")
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({"error": "openpyxl not installed"}), 500

    sdir = _session_dir(sid)
    sess = store.get_session(sid)
    available = notebook_engine.available_vars(sdir)

    # Colours — restrained, scientific
    HEADER_FILL  = PatternFill("solid", fgColor="1A3A5C")
    HEADER_FONT  = Font(bold=True, color="FFFFFF", size=10, name="Calibri")
    ALT_FILL     = PatternFill("solid", fgColor="EEF3F8")
    NORMAL_FONT  = Font(size=10, name="Calibri")
    TITLE_FONT   = Font(bold=True, size=13, name="Calibri")
    META_FONT    = Font(size=10, name="Calibri", color="555555")
    thin         = Side(style="thin", color="CCCCCC")
    BORDER       = Border(bottom=thin)

    wb = Workbook()

    # ── Cover sheet ───────────────────────────────────────────────────────────
    ws0 = wb.active
    ws0.title = "Summary"
    ws0.column_dimensions["A"].width = 24
    ws0.column_dimensions["B"].width = 48

    try:
        plan = derive_contextual.get_plan(sess)
        ctx_label = plan.get("context_label", "General tabular data")
    except Exception:
        ctx_label = "General tabular data"

    user_info = sess.get("user_info", {})
    meta_rows = [
        ("PROBE EXPORT", None),
        (None, None),
        ("Name",         user_info.get("name", "")),
        ("Organisation", user_info.get("organization", "")),
        ("Email",        user_info.get("email", "")),
        ("Project",      user_info.get("project", "")),
        (None, None),
        ("Session",      sid),
        ("Exported",     datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")),
        ("Data context", ctx_label),
        ("Variables",    ", ".join(available)),
    ]
    for r, (label, value) in enumerate(meta_rows, 1):
        if label == "PROBE EXPORT":
            cell = ws0.cell(row=r, column=1, value=label)
            cell.font = TITLE_FONT
        elif label:
            ws0.cell(row=r, column=1, value=label).font = Font(bold=True, size=10, name="Calibri", color="1A3A5C")
            ws0.cell(row=r, column=2, value=value).font = META_FONT

    # ── One sheet per variable ────────────────────────────────────────────────
    PREFERRED_ORDER = ["adsl","adae","adtte","plate_assay","dose_response","plate_qc",
                       "lb_summary","lb_flags","lb_shifts","profile","numeric_summary"]
    ordered = [v for v in PREFERRED_ORDER if v in available] + [v for v in available if v not in PREFERRED_ORDER]

    for var in ordered:
        df = notebook_engine.load_state(sdir, var)
        if df is None or df.empty:
            continue

        ws = wb.create_sheet(title=var[:31])
        ws.freeze_panes = "A2"

        # Header row
        for ci, col_name in enumerate(df.columns, 1):
            cell = ws.cell(row=1, column=ci, value=str(col_name))
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = BORDER

        # Data rows
        for ri, (_, row) in enumerate(df.iterrows(), 2):
            fill = ALT_FILL if ri % 2 == 0 else None
            for ci, val in enumerate(row, 1):
                v = None if pd.isna(val) else val
                cell = ws.cell(row=ri, column=ci, value=v)
                cell.font = NORMAL_FONT
                cell.border = BORDER
                if fill:
                    cell.fill = fill
                if pd.api.types.is_float_dtype(df.iloc[:, ci-1]):
                    cell.number_format = "0.0000"

        # Auto column widths (capped)
        for ci, col_name in enumerate(df.columns, 1):
            sample_vals = df.iloc[:, ci-1].astype(str).head(50)
            max_len = max(len(str(col_name)), sample_vals.str.len().max() if not sample_vals.empty else 8)
            ws.column_dimensions[get_column_letter(ci)].width = min(max(max_len + 2, 10), 40)

        # Row height
        ws.row_dimensions[1].height = 18

    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    from flask import send_file
    return send_file(buf, as_attachment=True,
                     download_name=f"probe_export_{sid[:8]}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/notebook/vars", methods=["GET"])
def notebook_vars():
    """Return available variable names + context label for the notebook header."""
    sid = request.args.get("session_id")
    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    sdir = _session_dir(sid)
    sess = store.get_session(sid)
    available = notebook_engine.available_vars(sdir)
    try:
        plan = derive_contextual.get_plan(sess)
        context = plan.get("context", "generic")
        context_label = plan.get("context_label", "General tabular data")
    except Exception:
        context = "generic"
        context_label = "General tabular data"
    return jsonify({"vars": available, "context": context, "context_label": context_label})


@app.route("/api/notebook/generate", methods=["POST"])
def notebook_generate():
    """
    Natural-language -> code via Claude API. The model sees only column
    names + dtypes (schema), never patient-level values. Generated code
    runs through the same notebook_engine.run_cell() subprocess as
    manually-typed cells.
    """
    import codegen

    body = request.get_json(force=True)
    sid = body.get("session_id")
    request_text = body.get("text", "").strip()

    if not store.session_exists(sid):
        return jsonify({"error": "unknown session"}), 404
    if not request_text:
        return jsonify({"status": "error", "error": "empty request"}), 200

    sdir = _session_dir(sid)
    available = notebook_engine.available_vars(sdir)
    if not available:
        return jsonify({"status": "error", "error": "No derived data yet — run derivation first."}), 200

    sess = store.get_session(sid)
    gen_result = codegen.generate_code(sdir, available, request_text, sess=sess)
    if gen_result["status"] != "ok":
        # Map internal error details to a safe reason code — never expose raw messages
        err = gen_result.get("error", "")
        if "disallowed pattern" in err and any(p in err for p in ["open(", "import os", "import sys"]):
            reason = "cannot_write_files"
        elif "No derived data" in err or "No dataframes" in err:
            reason = "no_data"
        elif "ANTHROPIC_API_KEY" in err:
            reason = "api_key"
        else:
            reason = "general"
        return jsonify({"status": "error", "reason": reason}), 200

    code = gen_result["code"]

    # Verify every column reference in the generated code against actual session schema
    dfs = _load_all_dfs(sdir)
    schema_check = codegen.verify_against_schema(code, dfs)

    exec_result = notebook_engine.run_cell(sdir, code)

    sess = store.get_session(sid)
    cell_record = {
        "id": len(sess["canvas_cells"]) + 1,
        "code": code,
        "generated_from": request_text,
        "schema_check": schema_check,
        "result": exec_result,
    }
    sess["canvas_cells"].append(cell_record)

    return jsonify(cell_record)


if __name__ == "__main__":
    os.makedirs(NOTEBOOK_STATE_ROOT, exist_ok=True)
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)
