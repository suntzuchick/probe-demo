"""
Bioinformatics-aware code generation for the Probe notebook.

The model receives a richly interpreted context that includes:
  - Detected scientific context (clinical trial, plate assay, lab assay, generic)
  - Schema with value intelligence: unique values for categorical columns,
    numeric ranges, embedded-structure hints (e.g. 'RVM-1102_10.0uM')
  - Biological interpretation: known cell lines, mutation nomenclature,
    concentration series patterns, control well classifications, SDTM semantics
  - Extraction provenance: column → standard variable mapping decisions + confidence
  - Joinable variable relationships
  - Recommended statistical models for the detected data type

Synthetic data generation is explicitly supported: the model may create new
DataFrames in memory (e.g. modified copies for sensitivity analysis, permutation
tests, edge-case testing). File I/O and network calls remain forbidden.

Requires ANTHROPIC_API_KEY in the environment.
"""
import os
import re

import pandas as pd

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a bioinformatics and clinical data scientist writing Python for a scientific notebook.
You receive rich context about the data: schema, biological interpretation of every column, recommended analyses, and which variables are derived vs raw.

─── Core rules ───────────────────────────────────────────────────────────────────────────────────
1. Respond with ONLY a single Python code block (```python ... ```). No prose, no headers, no comments.
2. Use only variables and columns in the schema. Use derived tables over raw sheets.
3. Never read or write files, never make network calls, never use os / sys / subprocess / open() / exec() / eval().
4. No plt.show() — figures are captured automatically.
5. End with a bare expression (DataFrame or scalar) OR a chart — not both unless asked.
6. If a request cannot be answered, write a single print() line explaining what's missing.

─── Plate / cell-viability assays ───────────────────────────────────────────────────────────────
- Use VIABILITY_PCT, not raw SIGNAL. Use real CONCENTRATION values, never invent doses.
- Dose-response curves: fit with a 4-parameter logistic (4PL) using scipy.optimize.curve_fit.
  Hill equation: f(x) = bottom + (top-bottom) / (1 + (IC50/x)^HillSlope)
  Report IC50 in µM. Plot on log-scale x-axis.
- Control wells: DMSO = untreated (100% viability reference), blank = background signal.
- Group by CELL_LINE, never by plate row letters.
- Z-factor = 1 - 3*(σ_pos + σ_neg) / |μ_pos - μ_neg|. Z' > 0.5 = excellent assay quality.
- S:B ratio = μ_DMSO / μ_blank. CV% = σ/μ × 100 per control group.

─── Clinical trial / SDTM / ADaM ────────────────────────────────────────────────────────────────
- TRTEMFL='Y' filters treatment-emergent AEs. AETOXGR gives NCI-CTCAE toxicity grade (1–5).
- ADTTE: AVAL = time-to-event in days, CNSR = censoring flag (1=censored, 0=event).
- For KM curves: use lifelines.KaplanMeierFitter. For HR: CoxPHFitter.
- Biomarker subgroup analysis: stratify by mutation status columns (KRAS, BRAF, TP53, etc.).
- OS = overall survival, PFS = progression-free survival, DOR = duration of response.
- CR/PR/SD/PD are standard RECIST response categories.

─── Genomics / biomarker columns ────────────────────────────────────────────────────────────────
- Mutation notation like G12D, V600E, R175H = specific amino-acid substitutions in oncogenes.
- KRAS G12D/G12C/G12V are common KRAS driver mutations in pancreatic and lung cancer.
- BRAF V600E drives ~50% of melanoma. TP53 is a tumour suppressor.
- When a column contains mutation labels, stratify analyses by mutation subtype.

─── Synthetic / modified data ───────────────────────────────────────────────────────────────────
- You MAY generate new DataFrames in memory (modified copies, simulated records, permutations).
- Assign to a new variable name (e.g. ae_corrected, plate_simulated). Do NOT write to files.
- Realistic synthetic plate data: use np.random.normal around known DMSO/blank means ± realistic CV.
- Realistic synthetic clinical data: sample from existing distributions (use df.sample() or np.random).
- Always end with the new DataFrame as the final expression so it gets displayed.

─── scipy is available for curve fitting ────────────────────────────────────────────────────────
  from scipy.optimize import curve_fit
  from scipy.stats import ttest_ind, mannwhitneyu, fisher_exact
"""


# ── Biological knowledge bases ─────────────────────────────────────────────────────────────────

# Known cell lines → cancer type annotation
_CELL_LINE_DB = {
    "panc-1": "pancreatic ductal adenocarcinoma (KRAS G12D)",
    "mia paca-2": "pancreatic ductal adenocarcinoma (KRAS G12C)",
    "miapaca-2": "pancreatic ductal adenocarcinoma (KRAS G12C)",
    "bxpc-3": "pancreatic adenocarcinoma (KRAS wild-type)",
    "aspc-1": "pancreatic adenocarcinoma (KRAS G12D)",
    "hct116": "colorectal carcinoma (KRAS G13D)",
    "sw480": "colorectal adenocarcinoma (KRAS G12V)",
    "ls174t": "colorectal adenocarcinoma (KRAS G12D)",
    "a549": "non-small cell lung carcinoma (KRAS G12S)",
    "h358": "non-small cell lung carcinoma (KRAS G12C)",
    "h23": "non-small cell lung carcinoma (KRAS G12C)",
    "sk-mel-28": "melanoma (BRAF V600E)",
    "a375": "melanoma (BRAF V600E)",
    "mcf7": "breast adenocarcinoma (ER+)",
    "mda-mb-231": "triple-negative breast cancer",
    "hela": "cervical carcinoma (HPV18+)",
    "u2os": "osteosarcoma",
    "jurkat": "T-cell leukaemia",
}

# Mutation patterns → biological meaning
_MUTATION_PATTERN = re.compile(
    r'\b([A-Z][0-9]+[A-Z])\b'   # e.g. G12D, V600E, R175H
)

# Concentration unit patterns
_CONC_UNIT_PATTERN = re.compile(
    r'(\d+\.?\d*)\s*(um|µm|nm|mm|pm)', re.IGNORECASE
)

# Viability range heuristic: if numeric column has values 0–200 and name suggests readout
_VIABILITY_NAMES = re.compile(
    r'(viab|inhibit|growth|response|signal|readout|luminesc|fluores|absorb)',
    re.IGNORECASE
)

# SDTM variable meanings
_SDTM_GLOSSARY = {
    "USUBJID": "unique subject identifier",
    "ARMCD":   "treatment arm code",
    "AVAL":    "analysis value (time-to-event in days for ADTTE)",
    "CNSR":    "censoring flag: 1=censored, 0=event occurred",
    "TRTEMFL": "treatment-emergent flag: Y=yes",
    "AETOXGR": "NCI-CTCAE toxicity grade (1=mild … 5=fatal)",
    "AEBODSYS": "MedDRA system organ class",
    "AEDECOD": "MedDRA preferred term",
    "AGE":     "subject age in years at baseline",
    "SEX":     "sex: M=male, F=female",
    "RACE":    "race/ethnicity",
    "LBTEST":  "laboratory test name",
    "LBORRES": "laboratory result as collected",
    "LBORNRLO":"lower limit of normal reference range",
    "LBORNRHI":"upper limit of normal reference range",
}

_RECOMMENDED_ANALYSES = {
    "plate_assay": (
        "Recommended analyses: 4PL curve fitting for IC50, Z-factor for QC, "
        "S:B ratio, CV% per control group, viability heatmap by row/column, "
        "cell-line sensitivity comparison."
    ),
    "clinical_trial": (
        "Recommended analyses: Kaplan-Meier OS/PFS curves, Cox PH hazard ratio, "
        "logrank test, TEAE incidence table, grade 3+ AE breakdown by SOC, "
        "biomarker subgroup forest plot, waterfall plot of best response."
    ),
    "lab_assay": (
        "Recommended analyses: shift table (normal→high/low), flag rate by parameter, "
        "lab value distribution by visit, grade ≥3 toxicity flag."
    ),
}

# Columns whose values are always opaque identifiers — show count, not samples
_ID_PATTERNS = re.compile(
    r"(subj|patient|mrn|nhs|dob|birth|ssn|email|phone)\b",
    re.IGNORECASE,
)

_CATEGORICAL_THRESHOLD = 20


# ── Column value intelligence ──────────────────────────────────────────────────────────────────

def _col_intelligence(df, col: str) -> str:
    """Compact description of a column's values including biological interpretation."""
    series = df[col]
    n_unique = series.nunique(dropna=True)
    dtype = series.dtype
    parts = []

    # ── Numeric ───────────────────────────────────────────────────────────────
    if pd.api.types.is_numeric_dtype(dtype):
        try:
            mn, mx, mu = float(series.min()), float(series.max()), float(series.mean())
            parts.append(f"range {mn:.4g}–{mx:.4g}, mean {mu:.4g}")
            # Viability heuristic
            if _VIABILITY_NAMES.search(col) and 0 <= mu <= 200:
                parts.append("likely viability % — 100=DMSO baseline, 0=complete inhibition")
            # Log-spaced concentration heuristic
            if "conc" in col.lower() or "dose" in col.lower():
                log_spread = mx / max(mn, 1e-9)
                if log_spread > 100:
                    parts.append("log-spaced dose series → use log-scale x-axis")
        except Exception:
            pass
        return "; ".join(parts)

    # ── Datetime ──────────────────────────────────────────────────────────────
    if pd.api.types.is_datetime64_any_dtype(dtype):
        try:
            return f"dates {series.min().date()} → {series.max().date()}"
        except Exception:
            return ""

    # ── String / object ───────────────────────────────────────────────────────
    if n_unique == 0:
        return ""

    if _ID_PATTERNS.search(col) and n_unique > 20:
        return f"{n_unique} unique subject IDs"

    if n_unique <= _CATEGORICAL_THRESHOLD:
        vals = sorted(series.dropna().astype(str).unique())
        desc = "values: [" + ", ".join(repr(v) for v in vals) + "]"

        # Annotate known cell lines
        cl_annots = []
        for v in vals:
            key = v.lower().strip()
            if key in _CELL_LINE_DB:
                cl_annots.append(f"{v}={_CELL_LINE_DB[key]}")
        if cl_annots:
            desc += f" — cell lines: {'; '.join(cl_annots)}"

        # Annotate SDTM variable
        if col.upper() in _SDTM_GLOSSARY:
            desc += f" — SDTM: {_SDTM_GLOSSARY[col.upper()]}"

        # Detect mutation notation
        all_text = " ".join(str(v) for v in vals)
        mutations = _MUTATION_PATTERN.findall(all_text)
        if mutations:
            desc += f" — oncogenic mutation notation detected ({', '.join(set(mutations))})"

        # Detect control well labels
        lower_vals = {v.lower() for v in vals}
        if lower_vals & {"dmso", "vehicle", "untreated"}:
            desc += " — includes DMSO/vehicle control"
        if lower_vals & {"blank", "media", "background", "pbs"}:
            desc += " — includes blank/background control"

        return desc

    # High cardinality
    samples = series.dropna().astype(str).head(50).drop_duplicates().tolist()[:3]
    desc = "samples: [" + ", ".join(repr(s) for s in samples) + f"] ({n_unique} unique)"

    # Detect embedded concentration format
    sample_str = " ".join(samples)
    if _CONC_UNIT_PATTERN.search(sample_str):
        desc += " — encodes compound+concentration (e.g. 'NAME_10.0uM'); parse with str.extract()"

    return desc


# ── Full context builder ───────────────────────────────────────────────────────────────────────

def _build_context(session_dir: str, available_vars: list, sess: dict | None) -> str:
    import notebook_engine as ne

    lines = []

    # ── Detected context + recommended analyses ───────────────────────────────
    context_key = "generic"
    if sess:
        try:
            import sys, os as _os
            sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "extractors"))
            from derive_contextual import get_plan
            plan = get_plan(sess)
            context_key = plan.get("context", "generic")
            lines.append(f"Data context: {plan['context_label']}")
        except Exception:
            pass

    if context_key in _RECOMMENDED_ANALYSES:
        lines.append(_RECOMMENDED_ANALYSES[context_key])
    lines.append("")

    # ── Variable classification ───────────────────────────────────────────────
    _DERIVED_NAMES = {
        "plate_assay", "plate_qc", "dose_response",
        "adsl", "adae", "adtte",
        "lb_summary", "lb_flags", "lb_shifts",
        "profile", "numeric_summary",
    }

    dfs, raw_vars, derived_vars = {}, [], []
    for var in available_vars:
        df = ne.load_state(session_dir, var)
        if df is None:
            continue
        dfs[var] = df
        (derived_vars if var in _DERIVED_NAMES else raw_vars).append(var)

    def _render_var(var):
        df = dfs[var]
        lines.append(f"  {var}: {len(df)} rows × {len(df.columns)} cols")
        for col in df.columns:
            intel = _col_intelligence(df, col)
            suffix = f" — {intel}" if intel else ""
            lines.append(f"    {col} ({df[col].dtype}){suffix}")

    if derived_vars:
        lines.append("Derived analysis tables (use these first):")
        for v in derived_vars:
            _render_var(v)
        lines.append("")

    if raw_vars:
        lines.append("Raw source sheets (join inputs or fallback):")
        for v in raw_vars:
            _render_var(v)
        lines.append("")

    # ── Extraction provenance ─────────────────────────────────────────────────
    if sess:
        domain_source = sess.get("domain_source", {})
        prov = []
        for source in domain_source.values():
            var_name = source.get("var_name")
            mappings = source.get("mappings", {})
            if not var_name or not mappings:
                continue
            auto = [f"{s}→{i['top_match']} ({i['confidence']*100:.0f}%)"
                    for s, i in mappings.items() if i.get("action") == "AUTO_MAP"]
            held = [f"{s}~{i['top_match']} ({i['confidence']*100:.0f}%, unconfirmed)"
                    for s, i in mappings.items() if i.get("action") == "SURFACE_TO_USER"]
            sheet = source.get("sheet_name", source.get("filename", ""))
            parts = (["auto-mapped: " + "; ".join(auto)] if auto else []) + \
                    (["held for review: " + "; ".join(held)] if held else [])
            if parts:
                prov.append(f"  {var_name} (from {sheet!r}): {' | '.join(parts)}")
        if prov:
            lines.append("Extraction provenance:")
            lines.extend(prov)
            lines.append("")

    # ── Joinable keys ─────────────────────────────────────────────────────────
    shared: dict[str, list[str]] = {}
    for var, df in dfs.items():
        for col in df.columns:
            shared.setdefault(col.upper(), []).append(var)
    joinable = {k: vs for k, vs in shared.items() if len(vs) >= 2}
    if joinable:
        lines.append("Joinable on shared key column:")
        for key, vars_ in joinable.items():
            lines.append(f"  {', '.join(vars_)} — key: {key}")
        lines.append("")

    return "\n".join(lines)


# ── Public API ─────────────────────────────────────────────────────────────────────────────────

def generate_code(session_dir: str, available_vars: list, request_text: str,
                  sess: dict | None = None) -> dict:
    """Returns {"status": "ok", "code": "..."} or {"status": "error", "error": "..."}. Never raises."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"status": "error",
                "error": "ANTHROPIC_API_KEY is not set. Set it before starting app.py."}

    try:
        import anthropic
    except ImportError:
        return {"status": "error", "error": "'anthropic' package not installed. Run: pip install anthropic"}

    context = _build_context(session_dir, available_vars, sess)
    if not context.strip():
        return {"status": "error", "error": "No dataframes available yet — run derivation first."}

    user_message = f"{context}\nRequest: {request_text}"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        return {"status": "error", "error": f"Anthropic API call failed: {e}"}

    text = "".join(block.text for block in response.content if block.type == "text")

    match = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        match = re.search(r"```(?:python)?\s*\n(.*)", text, re.DOTALL)
    if not match:
        return {"status": "error",
                "error": "Model did not return a code block. Raw response: " + text[:300]}

    code = match.group(1).strip()

    # Only block actual file I/O and shell access — not DataFrame construction
    forbidden = [
        "import os", "import sys", "import subprocess",
        "__import__", "open(", "exec(", "eval(",
    ]
    hits = [f for f in forbidden if f in code]
    if hits:
        return {"status": "error",
                "error": f"Generated code contained disallowed pattern(s): {hits}. Refusing to run it."}

    return {"status": "ok", "code": code}


def verify_against_schema(code: str, dfs: dict) -> dict:
    """
    Parse generated code for explicit column subscript accesses — var['COL'] and var["COL"] —
    and check every reference against the columns that actually exist in the loaded DataFrames.

    Returns:
      verified  — references confirmed present in session data
      missing   — references that look like DataFrame column accesses but the column
                  does NOT exist in that variable (hallucinated or stale column names)
      checked   — total references inspected
    """
    known = {var: set(df.columns) for var, df in dfs.items()}
    verified_set: set[str] = set()
    missing_set:  set[str] = set()

    # Match: identifier['anything'] or identifier["anything"]
    # Captures the variable name and the string key separately.
    for m in re.finditer(r'\b(\w+)\s*\[[\'"]([^\'"]+)[\'"]\]', code):
        var = m.group(1)
        col = m.group(2).strip()
        if var not in known:
            continue  # not a session DataFrame — ignore built-ins, dicts, etc.
        ref = f"{var}.{col}"
        if col in known[var]:
            verified_set.add(ref)
        else:
            missing_set.add(ref)

    return {
        "verified": sorted(verified_set),
        "missing":  sorted(missing_set),
        "checked":  len(verified_set) + len(missing_set),
    }
