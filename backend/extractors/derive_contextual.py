"""
Contextual derivation dispatcher for Probe.

Classifies uploaded data by scoring column-level SDTM mappings against
known variable groups, then inspects actual DataFrame structure to detect
more specific scientific data types when no domain-level match is found.

Contexts
--------
clinical_trial      -> ADSL, ADAE, ADTTE          (delegates to derive_adam)
lab_assay           -> LB_SUMMARY, LB_FLAGS, LB_SHIFTS
plate_assay         -> PLATE_ASSAY, PLATE_QC, DOSE_RESPONSE
time_series         -> TIME_INDEX, TREND_SUMMARY
expression_matrix   -> FEATURE_VARIANCE, SAMPLE_STATS, TOP_VARIABLE
grouped_comparison  -> GROUP_STATS, EFFECT_SIZES, ANOVA_SUMMARY
generic             -> PROFILE, NUMERIC_SUMMARY   (true fallback)
"""
import numpy as np
import pandas as pd

# ── Variable group membership ─────────────────────────────────────────────────

CLINICAL_VARS = {
    "USUBJID", "AGE", "SEX", "RACE", "ARMCD", "KRASMUT", "ECOGBL",
    "EXSTDTC", "EXENDTC", "EXTRT", "EXDOSE",
    "AETERM", "AEDECOD", "AEBODSYS", "AETOXGR", "AESER",
    "RSORRES", "RSEVAL", "DSDECOD", "DSDTC",
}
LAB_VARS = {"LBTEST", "LBORRES", "LBORRESU", "LBSTNRLO", "LBSTNRHI"}
PLATE_VARS = {"WELLID", "PLATEID", "SAMPLEID", "CONCENTRATION", "READOUT"}

# ── Context catalogue ─────────────────────────────────────────────────────────

CONTEXTS = {
    "clinical_trial": {
        "label": "Clinical trial — ADaM",
        "description": (
            "ADSL, ADAE, and ADTTE derive in dependency order from the mapped "
            "SDTM domains — pure pandas, no model involved."
        ),
        "steps": [
            {"key": "adsl",  "label": "ADSL — subject-level dataset"},
            {"key": "adae",  "label": "ADAE — adverse events, treatment-emergent flag"},
            {"key": "adtte", "label": "ADTTE — time-to-event dataset"},
        ],
    },
    "lab_assay": {
        "label": "Lab / assay data",
        "description": (
            "LB_SUMMARY, LB_FLAGS, and LB_SHIFTS derive from the uploaded lab "
            "data — analyte-level statistics, out-of-range flags, and baseline shifts."
        ),
        "steps": [
            {"key": "lb_summary", "label": "LB_SUMMARY — per-analyte descriptive statistics"},
            {"key": "lb_flags",   "label": "LB_FLAGS — observations outside reference range"},
            {"key": "lb_shifts",  "label": "LB_SHIFTS — baseline-to-on-treatment shifts"},
        ],
    },
    "plate_assay": {
        "label": "Plate-based assay",
        "description": (
            "Signal and compound layout sheets are joined on WELLID, cell lines "
            "assigned from metadata, viability normalised to DMSO controls, "
            "then QC metrics and dose-response summaries are derived."
        ),
        "steps": [
            {"key": "plate_assay",   "label": "PLATE_ASSAY — merged, normalised well-level data"},
            {"key": "plate_qc",      "label": "PLATE_QC — per-plate quality metrics (CV%, z-prime)"},
            {"key": "dose_response", "label": "DOSE_RESPONSE — viability by concentration and cell line"},
        ],
    },
    "time_series": {
        "label": "Longitudinal / time-series data",
        "description": (
            "A time or date column was detected. Derives a time-ordered index with "
            "inter-observation delta and a per-variable linear trend summary (slope, R²)."
        ),
        "steps": [
            {"key": "time_index",    "label": "TIME_INDEX — observations sorted and indexed by time"},
            {"key": "trend_summary", "label": "TREND_SUMMARY — slope and R² for each numeric variable over time"},
        ],
    },
    "expression_matrix": {
        "label": "Expression / feature matrix",
        "description": (
            "A wide numeric matrix was detected (features × samples). Derives per-feature "
            "variance and CV%, per-sample detection statistics, and a top-variable feature ranking."
        ),
        "steps": [
            {"key": "feature_variance", "label": "FEATURE_VARIANCE — variance and CV% per feature, ranked"},
            {"key": "sample_stats",     "label": "SAMPLE_STATS — total signal, mean, and detection rate per sample"},
            {"key": "top_variable",     "label": "TOP_VARIABLE — top 50 highest-variance features"},
        ],
    },
    "grouped_comparison": {
        "label": "Grouped measurement data",
        "description": (
            "A categorical group column with numeric measurements was detected. Derives "
            "group-level statistics, Cohen's d effect sizes, and one-way ANOVA p-values per measurement."
        ),
        "steps": [
            {"key": "group_stats",   "label": "GROUP_STATS — mean, SD, median, and N per group per measurement"},
            {"key": "effect_sizes",  "label": "EFFECT_SIZES — Cohen's d between all group pairs per measurement"},
            {"key": "anova_summary", "label": "ANOVA_SUMMARY — one-way ANOVA F-statistic and p-value per measurement"},
        ],
    },
    "generic": {
        "label": "General tabular data",
        "description": (
            "PROFILE and NUMERIC_SUMMARY provide a column-level audit and "
            "descriptive statistics across all uploaded tables."
        ),
        "steps": [
            {"key": "profile",         "label": "PROFILE — column-level data profile"},
            {"key": "numeric_summary", "label": "NUMERIC_SUMMARY — descriptive statistics"},
        ],
    },
}

# ── DataFrame-content detection ───────────────────────────────────────────────

_TIME_KEYWORDS = {
    "date", "time", "day", "week", "month", "year", "hour",
    "timestamp", "datetime", "visit", "timepoint", "period", "cycle",
}


def _detect_sub_context(dfs: dict) -> str:
    """Inspect actual DataFrame content to refine a generic classification."""
    for df in dfs.values():
        cols_lower = [c.lower() for c in df.columns]
        num_cols = df.select_dtypes(include="number").columns.tolist()

        # Time series: has a column whose name contains a temporal keyword
        time_col = next(
            (df.columns[i] for i, cl in enumerate(cols_lower)
             if any(kw in cl for kw in _TIME_KEYWORDS)),
            None,
        )
        if time_col is not None and len(num_cols) >= 1:
            return "time_series"

        # Expression matrix: ≥8 columns, ≥70% numeric
        if len(df.columns) >= 8 and len(num_cols) / len(df.columns) >= 0.70:
            return "expression_matrix"

        # Grouped comparison: ≥1 low-cardinality categorical + ≥2 numeric
        cat_cols = [
            c for c in df.columns
            if df[c].dtype == object and 1 < df[c].nunique() <= 12
        ]
        if cat_cols and len(num_cols) >= 2:
            return "grouped_comparison"

    return "generic"


def _customize_plan(context: str, dfs: dict) -> dict:
    """Return a copy of CONTEXTS[context] with descriptions and step labels
    personalised to the actual column names and dimensions of the data."""
    meta = {**CONTEXTS[context], "steps": [dict(s) for s in CONTEXTS[context]["steps"]]}

    for df in dfs.values():
        cols_lower = [c.lower() for c in df.columns]
        num_cols = df.select_dtypes(include="number").columns.tolist()

        if context == "time_series":
            time_col = next(
                (df.columns[i] for i, cl in enumerate(cols_lower)
                 if any(kw in cl for kw in _TIME_KEYWORDS)),
                None,
            )
            if time_col:
                n_obs = len(df)
                n_vars = len([c for c in num_cols if c != time_col])
                meta["description"] = (
                    f"Time column '{time_col}' detected across {n_obs} observations "
                    f"and {n_vars} numeric variable(s). Derives a time-ordered index "
                    "with inter-observation delta and a per-variable linear trend analysis."
                )
                meta["steps"][0]["label"] = f"TIME_INDEX — {n_obs} observations sorted by '{time_col}'"
                meta["steps"][1]["label"] = (
                    f"TREND_SUMMARY — slope and R² for {n_vars} variable(s) over '{time_col}'"
                )
            break

        elif context == "expression_matrix":
            id_col = df.columns[0] if df.columns[0] not in num_cols else None
            n_features = len(df)
            n_samples = len(num_cols)
            meta["description"] = (
                f"Wide numeric matrix detected: {n_features} feature rows × {n_samples} sample columns. "
                "Derives per-feature variance ranking and CV%, per-sample detection statistics, "
                "and a top-50 most variable feature table."
            )
            meta["steps"][0]["label"] = (
                f"FEATURE_VARIANCE — variance and CV% across {n_samples} samples, ranked by variability"
            )
            meta["steps"][1]["label"] = (
                f"SAMPLE_STATS — total signal, mean, and detection rate for each of {n_samples} samples"
            )
            meta["steps"][2]["label"] = (
                f"TOP_VARIABLE — top 50 highest-variance features from {n_features} total"
            )
            break

        elif context == "grouped_comparison":
            cat_cols = [
                c for c in df.columns
                if df[c].dtype == object and 1 < df[c].nunique() <= 12
            ]
            if cat_cols and num_cols:
                group_col = cat_cols[0]
                n_groups = df[group_col].nunique()
                n_meas = len(num_cols)
                meta["description"] = (
                    f"Group column '{group_col}' detected ({n_groups} groups) with "
                    f"{n_meas} numeric measurement(s). Derives group-level statistics, "
                    "Cohen's d effect sizes, and one-way ANOVA p-values per measurement."
                )
                meta["steps"][0]["label"] = (
                    f"GROUP_STATS — mean, SD, N per '{group_col}' group × {n_meas} measurement(s)"
                )
                meta["steps"][1]["label"] = (
                    f"EFFECT_SIZES — Cohen's d between {n_groups} groups × {n_meas} measurement(s)"
                )
                meta["steps"][2]["label"] = (
                    f"ANOVA_SUMMARY — one-way ANOVA across '{group_col}' for each measurement"
                )
            break

    return meta

# ── Context classification ────────────────────────────────────────────────────

def classify_context(sess: dict, dfs: dict = None) -> str:
    """
    Infer data context from confirmed domains first, then from auto-mapped
    variable scores, then (if dfs are provided) from actual DataFrame structure.
    """
    confirmed = set(sess.get("domain_data", {}).keys())

    if confirmed & {"DM", "EX", "AE", "RS", "DS"}:
        return "clinical_trial"
    if "LB" in confirmed:
        return "lab_assay"
    if "PLATE" in confirmed:
        return "plate_assay"

    scores = {"clinical_trial": 0, "lab_assay": 0, "plate_assay": 0}
    for file_result in sess.get("files", {}).values():
        if file_result.get("status") != "ok":
            continue
        if file_result.get("detected_layout") == "plate_map":
            scores["plate_assay"] += 5
        for m in file_result.get("mapping", []):
            if m.get("action") != "AUTO_MAP":
                continue
            var = m.get("top_match", "")
            if var in CLINICAL_VARS:
                scores["clinical_trial"] += 1
            elif var in LAB_VARS:
                scores["lab_assay"] += 1
            elif var in PLATE_VARS:
                scores["plate_assay"] += 1

    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best

    # No domain-level signal — inspect the actual loaded data
    if dfs:
        return _detect_sub_context(dfs)
    return "generic"


def get_plan(sess: dict, dfs: dict = None) -> dict:
    context = classify_context(sess, dfs=dfs)
    if dfs and context in ("time_series", "expression_matrix", "grouped_comparison"):
        meta = _customize_plan(context, dfs)
    else:
        meta = CONTEXTS[context]
    return {
        "context": context,
        "context_label": meta["label"],
        "description": meta["description"],
        "steps": meta["steps"],
    }


def auto_detect_domain(extraction_result: dict) -> str | None:
    """
    Guess the domain key for a single extracted file based on its
    high-confidence column mappings. Returns None if nothing scores well.
    For XLSX workbooks, aggregates mappings across all sheets.
    """
    if extraction_result.get("status") != "ok":
        return None

    if extraction_result.get("detected_layout") == "plate_map":
        return "PLATE"

    # XLSX: gather all sheet-level mappings and check for plate sheets
    sheets = extraction_result.get("sheets", [])
    if sheets:
        for sheet in sheets:
            if sheet.get("detected_layout") == "plate_map":
                return "PLATE"
        all_mappings = [m for sheet in sheets for m in sheet.get("mapping", [])]
    else:
        all_mappings = extraction_result.get("mapping", [])

    auto_vars = {m["top_match"] for m in all_mappings if m.get("action") == "AUTO_MAP"}

    if not auto_vars:
        return "DATA"  # file extracted OK but no column mapped confidently

    lab_score      = len(auto_vars & LAB_VARS)
    plate_score    = len(auto_vars & PLATE_VARS)
    clinical_score = len(auto_vars & CLINICAL_VARS)

    if lab_score >= 2 and lab_score >= clinical_score:
        return "LB"
    if plate_score >= 2 and plate_score >= clinical_score:
        return "PLATE"

    # Clinical: try to identify the specific SDTM domain
    if {"USUBJID", "AGE", "SEX"} & auto_vars and "ARMCD" in auto_vars:
        return "DM"
    if {"EXSTDTC", "EXTRT"} & auto_vars:
        return "EX"
    if {"AETERM", "AETOXGR"} & auto_vars:
        return "AE"
    if "RSORRES" in auto_vars:
        return "RS"
    if "DSDECOD" in auto_vars:
        return "DS"

    # Generic fallback if at least something mapped
    return "DATA"


# ── Shared helper ─────────────────────────────────────────────────────────────

def _col(df: pd.DataFrame, candidates: list) -> str | None:
    """Return the first matching column name (case-insensitive)."""
    upper = {c.upper(): c for c in df.columns}
    for name in candidates:
        if name.upper() in upper:
            return upper[name.upper()]
    return None


# ── Lab assay pipelines ───────────────────────────────────────────────────────

def derive_lb_summary(lb: pd.DataFrame) -> pd.DataFrame:
    result_col = _col(lb, ["LBORRES", "RESULT", "VALUE", "MEASURED_VALUE"])
    test_col   = _col(lb, ["LBTEST", "TEST_NAME", "ANALYTE", "ASSAY_NAME"])
    unit_col   = _col(lb, ["LBORRESU", "UNIT", "UNITS"])
    lo_col     = _col(lb, ["LBSTNRLO", "REF_RANGE_LOW", "LOWER_LIMIT", "NORMAL_LOW"])
    hi_col     = _col(lb, ["LBSTNRHI", "REF_RANGE_HIGH", "UPPER_LIMIT", "NORMAL_HIGH"])

    if result_col is None:
        raise ValueError("No result/value column found in lab data.")

    lb = lb.copy()
    lb["_num"] = pd.to_numeric(lb[result_col], errors="coerce")

    group_cols = ([test_col] if test_col else []) + ([unit_col] if unit_col else [])

    if group_cols:
        agg = (
            lb.groupby(group_cols)["_num"]
            .agg(N="count", mean="mean", sd="std", min="min", median="median", max="max")
            .round(3)
            .reset_index()
        )
    else:
        s = lb["_num"]
        agg = pd.DataFrame([{
            "N": s.count(), "mean": round(s.mean(), 3), "sd": round(s.std(), 3),
            "min": round(s.min(), 3), "median": round(s.median(), 3), "max": round(s.max(), 3),
        }])

    if lo_col and hi_col:
        lb["_lo"] = pd.to_numeric(lb[lo_col], errors="coerce")
        lb["_hi"] = pd.to_numeric(lb[hi_col], errors="coerce")
        lb["_flagged"] = (lb["_num"] < lb["_lo"]) | (lb["_num"] > lb["_hi"])
        if group_cols:
            flag_rate = (
                lb.groupby(group_cols)["_flagged"].mean()
                .reset_index()
                .rename(columns={"_flagged": "pct_flagged"})
            )
            flag_rate["pct_flagged"] = (flag_rate["pct_flagged"] * 100).round(1)
            agg = agg.merge(flag_rate, on=group_cols, how="left")
        else:
            agg["pct_flagged"] = round(lb["_flagged"].mean() * 100, 1)

    return agg


def derive_lb_flags(lb: pd.DataFrame) -> pd.DataFrame:
    result_col = _col(lb, ["LBORRES", "RESULT", "VALUE", "MEASURED_VALUE"])
    lo_col     = _col(lb, ["LBSTNRLO", "REF_RANGE_LOW", "LOWER_LIMIT", "NORMAL_LOW"])
    hi_col     = _col(lb, ["LBSTNRHI", "REF_RANGE_HIGH", "UPPER_LIMIT", "NORMAL_HIGH"])

    if not result_col:
        raise ValueError("No result column found.")

    lb = lb.copy()
    lb["_num"] = pd.to_numeric(lb[result_col], errors="coerce")

    if not lo_col or not hi_col:
        mean, sd = lb["_num"].mean(), lb["_num"].std()
        out = lb[abs(lb["_num"] - mean) > 3 * sd].copy()
        out["FLAG_REASON"] = "statistical_outlier (>3 SD)"
        return out.drop(columns=["_num"]).reset_index(drop=True)

    lb["_lo"] = pd.to_numeric(lb[lo_col], errors="coerce")
    lb["_hi"] = pd.to_numeric(lb[hi_col], errors="coerce")
    low  = lb[lb["_num"] < lb["_lo"]].copy()
    low["FLAG_REASON"]  = "below_lower_limit"
    high = lb[lb["_num"] > lb["_hi"]].copy()
    high["FLAG_REASON"] = "above_upper_limit"
    return (
        pd.concat([low, high])
        .drop(columns=["_num", "_lo", "_hi"])
        .reset_index(drop=True)
    )


def derive_lb_shifts(lb: pd.DataFrame) -> pd.DataFrame:
    result_col = _col(lb, ["LBORRES", "RESULT", "VALUE"])
    test_col   = _col(lb, ["LBTEST", "TEST_NAME", "ANALYTE"])
    subj_col   = _col(lb, ["USUBJID", "SUBJECT_ID", "PATIENT_ID", "SAMPLEID", "SAMPLE_ID"])
    time_col   = _col(lb, ["VISITNUM", "VISIT", "TIMEPOINT", "DAY", "LBDY"])

    if not result_col or not subj_col or not time_col:
        return pd.DataFrame({"message": [
            "Shift table requires subject ID, timepoint, and result columns — not all were found."
        ]})

    lb = lb.copy()
    lb["_num"]  = pd.to_numeric(lb[result_col], errors="coerce")
    lb["_time"] = pd.to_numeric(lb[time_col], errors="coerce")

    group_cols = [subj_col] + ([test_col] if test_col else [])
    baseline_time = lb.groupby(subj_col)["_time"].min()
    lb["_is_bl"] = lb.apply(lambda r: r["_time"] == baseline_time.get(r[subj_col]), axis=1)
    bl = (
        lb[lb["_is_bl"]].groupby(group_cols)["_num"]
        .mean().reset_index().rename(columns={"_num": "BASELINE"})
    )
    shifts = lb.merge(bl, on=group_cols, how="left")
    shifts["SHIFT"] = (shifts["_num"] - shifts["BASELINE"]).round(3)
    return shifts.drop(columns=["_num", "_time", "_is_bl"]).reset_index(drop=True)


# ── Plate assay pipelines ─────────────────────────────────────────────────────

def derive_plate_qc(df: pd.DataFrame) -> pd.DataFrame:
    plate_col   = _col(df, ["PLATEID", "PLATE_ID", "PLATE_BARCODE"])
    readout_col = _col(df, ["READOUT", "SIGNAL", "OD", "RLU", "FLUORESCENCE", "ABSORBANCE"])

    if not readout_col:
        raise ValueError("No readout/signal column found.")

    df = df.copy()
    df["_val"] = pd.to_numeric(df[readout_col], errors="coerce")

    def _stats(x):
        mean = x.mean(); sd = x.std()
        return pd.Series({
            "N_wells": len(x), "mean_signal": round(mean, 3), "sd": round(sd, 3),
            "cv_pct": round(sd / mean * 100, 1) if mean else None,
            "min": round(x.min(), 3), "max": round(x.max(), 3),
        })

    if plate_col:
        return df.groupby(plate_col)["_val"].apply(_stats).reset_index()
    return pd.DataFrame([_stats(df["_val"]).to_dict()])


def derive_signal_norm(df: pd.DataFrame) -> pd.DataFrame:
    readout_col = _col(df, ["READOUT", "SIGNAL", "OD", "RLU", "FLUORESCENCE", "ABSORBANCE"])
    plate_col   = _col(df, ["PLATEID", "PLATE_ID"])

    if not readout_col:
        raise ValueError("No readout column found.")

    df = df.copy()
    df["_val"] = pd.to_numeric(df[readout_col], errors="coerce")
    if plate_col:
        df["SIGNAL_NORM"] = df.groupby(plate_col)["_val"].transform(
            lambda x: (x / x.mean() * 100).round(2)
        )
    else:
        df["SIGNAL_NORM"] = (df["_val"] / df["_val"].mean() * 100).round(2)
    return df.drop(columns=["_val"])


def derive_dose_response(df: pd.DataFrame) -> pd.DataFrame:
    conc_col    = _col(df, ["CONCENTRATION", "CONC", "DOSE", "DOSE_UM"])
    readout_col = _col(df, ["SIGNAL_NORM", "READOUT", "SIGNAL", "OD", "RLU", "FLUORESCENCE"])

    if not conc_col or not readout_col:
        return pd.DataFrame({"message": [
            "Dose-response requires concentration and readout columns."
        ]})

    df = df.copy()
    df["_conc"] = pd.to_numeric(df[conc_col], errors="coerce")
    df["_val"]  = pd.to_numeric(df[readout_col], errors="coerce")
    dr = (
        df.groupby("_conc")["_val"]
        .agg(N="count", mean_signal="mean", sd="std")
        .round(3).reset_index()
        .rename(columns={"_conc": conc_col})
    )
    return dr.reset_index(drop=True)


# ── Generic pipelines ─────────────────────────────────────────────────────────

def derive_profile(dfs: dict) -> pd.DataFrame:
    rows = []
    for var, df in dfs.items():
        for col in df.columns:
            s = df[col]
            n_miss = s.isna().sum() + (s == "").sum()
            rows.append({
                "variable": var, "column": col, "dtype": str(s.dtype),
                "n_rows": len(s), "n_unique": s.nunique(),
                "pct_missing": round(n_miss / len(s) * 100, 1) if len(s) else 0,
            })
    return pd.DataFrame(rows)


def derive_numeric_summary(dfs: dict) -> pd.DataFrame:
    frames = []
    for var, df in dfs.items():
        num = df.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
        if num.empty:
            continue
        desc = num.describe().T.round(3)
        desc.index = [f"{var}.{c}" for c in desc.index]
        frames.append(desc)
    if not frames:
        return pd.DataFrame({"message": ["No numeric columns found."]})
    return pd.concat(frames)


# ── Time-series pipelines ─────────────────────────────────────────────────────

def derive_time_index(df: pd.DataFrame, time_col: str) -> pd.DataFrame:
    """Sort by time column and compute inter-observation delta."""
    df = df.copy()
    parsed = pd.to_datetime(df[time_col], errors="coerce")
    if parsed.notna().mean() > 0.5:
        df["_t"] = parsed
        df = df.sort_values("_t")
        df["TIME_DELTA_HR"] = df["_t"].diff().dt.total_seconds().div(3600).round(2)
    else:
        numeric = pd.to_numeric(df[time_col], errors="coerce")
        df["_t"] = numeric
        df = df.sort_values("_t")
        df["TIME_DELTA"] = df["_t"].diff().round(4)
    return df.drop(columns=["_t"]).reset_index(drop=True)


def derive_trend_summary(df: pd.DataFrame, time_col: str) -> pd.DataFrame:
    """Linear slope and R² per numeric variable vs time."""
    parsed = pd.to_datetime(df[time_col], errors="coerce")
    if parsed.notna().mean() > 0.5:
        time_vals = parsed.astype("int64") // 10 ** 9  # seconds since epoch
    else:
        time_vals = pd.to_numeric(df[time_col], errors="coerce")

    rows = []
    for c in df.select_dtypes(include="number").columns:
        if c == time_col:
            continue
        vals = pd.to_numeric(df[c], errors="coerce")
        mask = time_vals.notna() & vals.notna()
        if mask.sum() < 3:
            continue
        x = time_vals[mask].values.astype(float)
        y = vals[mask].values.astype(float)
        m, b = np.polyfit(x, y, 1)
        y_pred = m * x + b
        ss_res = float(np.sum((y - y_pred) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else 0.0
        rows.append({
            "variable": c,
            "slope": round(float(m), 6),
            "intercept": round(float(b), 4),
            "r_squared": r2,
            "direction": "increasing" if m > 0 else "decreasing",
            "n_obs": int(mask.sum()),
        })
    if not rows:
        return pd.DataFrame({"message": ["No numeric variables found for trend analysis."]})
    return pd.DataFrame(rows).sort_values("r_squared", ascending=False).reset_index(drop=True)


# ── Expression / feature-matrix pipelines ────────────────────────────────────

def derive_feature_variance(df: pd.DataFrame) -> pd.DataFrame:
    """Variance, mean, and CV% per feature. Handles wide (rows=features) format."""
    num_cols = df.select_dtypes(include="number").columns.tolist()
    if not num_cols:
        return pd.DataFrame({"message": ["No numeric columns found."]})

    id_col = next((c for c in df.columns if c not in num_cols), None)

    if id_col:
        # Wide format: each row is a feature; numeric columns are samples
        data = df[num_cols].apply(pd.to_numeric, errors="coerce")
        result = pd.DataFrame({"feature": df[id_col].astype(str)})
        result["variance"]  = data.var(axis=1).round(4)
        result["mean"]      = data.mean(axis=1).round(3)
        result["cv_pct"]    = (
            data.std(axis=1) / data.mean(axis=1).abs().replace(0, float("nan")) * 100
        ).round(1)
        result["n_samples"] = data.notna().sum(axis=1)
        return result.sort_values("variance", ascending=False).reset_index(drop=True)
    else:
        # Tall format: each column is a feature
        rows = []
        for c in num_cols:
            vals = pd.to_numeric(df[c], errors="coerce").dropna()
            mean = float(vals.mean())
            rows.append({
                "feature":  c,
                "variance": round(float(vals.var()), 4),
                "mean":     round(mean, 3),
                "cv_pct":   round(float(vals.std() / abs(mean) * 100), 1) if mean != 0 else None,
                "n_obs":    len(vals),
            })
        return pd.DataFrame(rows).sort_values("variance", ascending=False).reset_index(drop=True)


def derive_sample_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Per-sample (column) summary statistics for an expression/feature matrix."""
    num_cols = df.select_dtypes(include="number").columns.tolist()
    rows = []
    for c in num_cols:
        vals = pd.to_numeric(df[c], errors="coerce")
        rows.append({
            "sample":       c,
            "total":        round(float(vals.sum()), 2),
            "mean":         round(float(vals.mean()), 3),
            "median":       round(float(vals.median()), 3),
            "n_detected":   int((vals > 0).sum()),
            "pct_detected": round(float((vals > 0).mean() * 100), 1),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame({"message": ["No numeric columns."]})


def derive_top_variable(df: pd.DataFrame, n: int = 50) -> pd.DataFrame:
    """Top N features by variance from derive_feature_variance output."""
    return derive_feature_variance(df).head(n)


# ── Grouped-comparison pipelines ──────────────────────────────────────────────

def _pick_group_cols(df: pd.DataFrame):
    """Return (group_col, num_cols) from a DataFrame."""
    cat_cols = [c for c in df.columns if df[c].dtype == object and 1 < df[c].nunique() <= 12]
    num_cols = df.select_dtypes(include="number").columns.tolist()
    group_col = cat_cols[0] if cat_cols else None
    return group_col, num_cols


def derive_group_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Mean, SD, median, and N per group per numeric measurement."""
    group_col, num_cols = _pick_group_cols(df)
    if not group_col or not num_cols:
        return pd.DataFrame({"message": ["Group column or numeric measurements not detected."]})
    rows = []
    for g, grp in df.groupby(group_col):
        for c in num_cols:
            vals = pd.to_numeric(grp[c], errors="coerce").dropna()
            if not len(vals):
                continue
            rows.append({
                "group": g, "measurement": c,
                "N": len(vals),
                "mean": round(float(vals.mean()), 3),
                "sd": round(float(vals.std()), 3),
                "median": round(float(vals.median()), 3),
                "min": round(float(vals.min()), 3),
                "max": round(float(vals.max()), 3),
            })
    return pd.DataFrame(rows) if rows else pd.DataFrame({"message": ["No valid group data found."]})


def derive_effect_sizes(df: pd.DataFrame) -> pd.DataFrame:
    """Cohen's d between every pair of groups for each measurement."""
    group_col, num_cols = _pick_group_cols(df)
    if not group_col or not num_cols:
        return pd.DataFrame({"message": ["Group column or numeric measurements not detected."]})
    groups = df[group_col].dropna().unique()
    rows = []
    for col in num_cols:
        vals = pd.to_numeric(df[col], errors="coerce")
        for i, g1 in enumerate(groups):
            for g2 in groups[i + 1:]:
                x1 = vals[df[group_col] == g1].dropna()
                x2 = vals[df[group_col] == g2].dropna()
                if len(x1) < 2 or len(x2) < 2:
                    continue
                pooled = np.sqrt(
                    ((len(x1) - 1) * x1.var() + (len(x2) - 1) * x2.var())
                    / (len(x1) + len(x2) - 2)
                )
                d = float((x1.mean() - x2.mean()) / pooled) if pooled > 0 else 0.0
                rows.append({
                    "measurement": col,
                    "group_1": str(g1), "group_2": str(g2),
                    "cohens_d": round(d, 3),
                    "magnitude": "large" if abs(d) >= 0.8 else "medium" if abs(d) >= 0.5 else "small",
                })
    return pd.DataFrame(rows) if rows else pd.DataFrame({
        "message": ["Cohen's d requires ≥2 groups with ≥2 observations each."]
    })


def derive_anova(df: pd.DataFrame) -> pd.DataFrame:
    """One-way ANOVA F-statistic and p-value for each measurement."""
    from scipy import stats as _scipy_stats  # type: ignore
    group_col, num_cols = _pick_group_cols(df)
    if not group_col or not num_cols:
        return pd.DataFrame({"message": ["Group column or numeric measurements not detected."]})
    groups = df[group_col].dropna().unique()
    rows = []
    for col in num_cols:
        group_vals = [
            pd.to_numeric(df.loc[df[group_col] == g, col], errors="coerce").dropna().values
            for g in groups
        ]
        group_vals = [v for v in group_vals if len(v) >= 2]
        if len(group_vals) < 2:
            continue
        try:
            F, p = _scipy_stats.f_oneway(*group_vals)
            rows.append({
                "measurement": col,
                "F_statistic": round(float(F), 3),
                "p_value": round(float(p), 4),
                "significant_p05": bool(p < 0.05),
            })
        except Exception:
            pass
    return pd.DataFrame(rows) if rows else pd.DataFrame({
        "message": ["ANOVA requires ≥2 groups with ≥2 observations each."]
    })
