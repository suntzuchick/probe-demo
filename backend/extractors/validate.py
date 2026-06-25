"""
Stage 3 — Data quality rules for clinical SDTM domains.

Three tiers of checks:
  1. Controlled-terminology (CT)  — value not in allowed codelist
  2. Structural / intra-domain    — required fields, impossible values,
                                    intra-record date logic, unique-key violations
  3. Referential integrity        — cross-domain FK checks (USUBJID in AE ∉ DM, etc.)

All checks are deterministic. No model involved.
"""
import pandas as pd
from datetime import datetime

# ── Controlled-terminology codelists ─────────────────────────────────────────

CT = {
    # AE
    "AETOXGR": {"0", "1", "2", "3", "4", "5"},
    "AESER":   {"Y", "N"},
    "AESEV":   {"MILD", "MODERATE", "SEVERE"},
    # Demographics
    "SEX":     {"M", "F", "U", "UNDIFFERENTIATED"},
    # Tumor response
    "RSORRES":  {"CR", "PR", "SD", "PD", "NE"},
    "RSSTRESC": {"CR", "PR", "SD", "PD", "NE"},
    # Disposition — common CDISC completion codes
    "DSDECOD": {
        "COMPLETED",
        "ADVERSE EVENT",
        "DEATH",
        "LACK OF EFFICACY",
        "LOST TO FOLLOW-UP",
        "PHYSICIAN DECISION",
        "PROTOCOL DEVIATION",
        "PROTOCOL VIOLATION",
        "STUDY TERMINATED BY SPONSOR",
        "WITHDRAWAL BY SUBJECT",
        "OTHER",
    },
}

# Required SDTM variables per domain — flagged as ERROR when entirely absent
REQUIRED = {
    "DM": ["USUBJID", "AGE", "SEX"],
    "AE": ["USUBJID", "AETERM", "AETOXGR"],
    "EX": ["USUBJID", "EXTRT", "EXSTDTC"],
    "RS": ["USUBJID", "RSORRES"],
    "DS": ["USUBJID", "DSDECOD", "DSDTC"],
}

# Columns that must be unique per domain (e.g. one row per subject in DM)
UNIQUE_KEY = {
    "DM": "USUBJID",
}

# Date-pair rules: end must be >= start  (column names, either may be absent)
DATE_ORDER = {
    "AE": ("AESTDTC", "AEENDTC"),
    "EX": ("EXSTDTC", "EXENDTC"),
}


def _parse_date(s: str):
    """Try common date/datetime formats; return datetime or None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    # Partial ISO: "2024-03-10T14:30" or "2024-03"
    for prefix_len, fmt in ((10, "%Y-%m-%d"), (7, "%Y-%m")):
        if len(s) >= prefix_len:
            try:
                return datetime.strptime(s[:prefix_len], fmt)
            except Exception:
                continue
    return None


def validate_domain(domain: str, df: pd.DataFrame) -> list:
    """
    Run CT + structural checks on a single SDTM domain DataFrame.
    Returns a list of issue dicts compatible with _detect_quality_issues().
    """
    domain = domain.upper()
    issues = []

    # ── 1. Required fields ────────────────────────────────────────────────────
    for req_col in REQUIRED.get(domain, []):
        if req_col not in df.columns:
            issues.append({
                "type": "missing_required_field",
                "domain": domain,
                "col": req_col,
                "description": f"Required column '{req_col}' is missing from {domain}",
                "severity": "high",
                "fix": None,
                "fix_label": None,
            })
        else:
            n_null = int(df[req_col].isna().sum()) + int((df[req_col].astype(str).str.strip() == "").sum())
            if n_null:
                issues.append({
                    "type": "missing_required_value",
                    "domain": domain,
                    "col": req_col,
                    "description": f"{n_null} row{'s' if n_null > 1 else ''} with blank/null '{req_col}' — required in {domain}",
                    "severity": "high",
                    "fix": "fill_unknown",
                    "fix_label": "Fill with 'UNKNOWN'",
                })

    # ── 2. Unique-key violations ──────────────────────────────────────────────
    uk_col = UNIQUE_KEY.get(domain)
    if uk_col and uk_col in df.columns:
        dupes = int(df[uk_col].duplicated().sum())
        if dupes:
            dup_vals = df.loc[df[uk_col].duplicated(keep=False), uk_col].unique().tolist()[:5]
            issues.append({
                "type": "duplicate_key",
                "domain": domain,
                "col": uk_col,
                "description": (
                    f"{dupes} duplicate '{uk_col}' value{'s' if dupes > 1 else ''} in {domain} "
                    f"— e.g. {dup_vals}. Each subject should appear once."
                ),
                "severity": "high",
                "fix": "drop_duplicates",
                "fix_label": "Keep first occurrence of each duplicate",
            })

    # ── 3. Controlled-terminology ─────────────────────────────────────────────
    for col, codelist in CT.items():
        if col not in df.columns:
            continue
        vals = df[col].dropna().astype(str).str.strip().str.upper()
        bad = vals[vals != ""][~vals[vals != ""].isin({v.upper() for v in codelist})]
        if bad.empty:
            continue
        examples = bad.value_counts().head(3).index.tolist()
        issues.append({
            "type": "ct_violation",
            "domain": domain,
            "col": col,
            "description": (
                f"{len(bad)} invalid '{col}' value{'s' if len(bad) > 1 else ''} in {domain}: "
                f"{examples}. Allowed: {sorted(codelist)}"
            ),
            "severity": "high",
            "fix": "normalize_upper",
            "fix_label": "Normalize to uppercase (may still need manual correction)",
        })

    # ── 4. Implausible numeric values ─────────────────────────────────────────
    if domain == "DM" and "AGE" in df.columns:
        ages = pd.to_numeric(df["AGE"], errors="coerce")
        bad_age = int(((ages < 0) | (ages > 120)).sum())
        if bad_age:
            examples = df.loc[(ages < 0) | (ages > 120), "AGE"].dropna().tolist()[:3]
            issues.append({
                "type": "implausible_value",
                "domain": domain,
                "col": "AGE",
                "description": (
                    f"{bad_age} AGE value{'s' if bad_age > 1 else ''} outside 0–120: {examples}"
                ),
                "severity": "high",
                "fix": None,
                "fix_label": None,
            })

    # ── 5. Date-ordering violations ───────────────────────────────────────────
    start_col, end_col = DATE_ORDER.get(domain, (None, None))
    if start_col and end_col and start_col in df.columns and end_col in df.columns:
        n_inverted = 0
        examples = []
        for _, row in df.iterrows():
            start = _parse_date(str(row.get(start_col, "") or ""))
            end   = _parse_date(str(row.get(end_col,   "") or ""))
            if start and end and end < start:
                n_inverted += 1
                if len(examples) < 3:
                    subj = row.get("USUBJID", "?")
                    examples.append(f"USUBJID={subj}: {row[start_col]} → {row[end_col]}")
        if n_inverted:
            issues.append({
                "type": "date_order_violation",
                "domain": domain,
                "col": end_col,
                "description": (
                    f"{n_inverted} record{'s' if n_inverted > 1 else ''} where {end_col} < {start_col} "
                    f"(end before start) — e.g. {examples[0] if examples else ''}"
                ),
                "severity": "high",
                "fix": None,
                "fix_label": None,
            })

    return issues


def validate_referential_integrity(domain_dfs: dict) -> list:
    """
    Cross-domain checks. domain_dfs: {domain_upper: DataFrame}.
    Returns list of issue dicts.
    """
    issues = []
    dm_df = domain_dfs.get("DM")

    if dm_df is None or "USUBJID" not in dm_df.columns:
        return issues

    dm_subjects = set(dm_df["USUBJID"].dropna().astype(str).str.strip())

    for domain, df in domain_dfs.items():
        if domain == "DM" or "USUBJID" not in df.columns:
            continue
        domain_subjects = set(df["USUBJID"].dropna().astype(str).str.strip())
        orphans = domain_subjects - dm_subjects
        if orphans:
            examples = sorted(orphans)[:5]
            issues.append({
                "type": "orphaned_subject",
                "domain": domain,
                "col": "USUBJID",
                "description": (
                    f"{len(orphans)} subject ID{'s' if len(orphans) > 1 else ''} in {domain} "
                    f"not present in DM: {examples}"
                ),
                "severity": "high",
                "fix": None,
                "fix_label": None,
            })

    return issues


def run_clinical_quality(sdir, available_vars_fn, load_fn) -> list:
    """
    Convenience entry point called from app.py quality_check().
    Loads all known SDTM domains from the session and runs all checks.
    Returns combined list of issues, each with a 'var' key for the UI.
    """
    domain_map = {}  # domain_upper -> df
    SDTM_DOMAINS = {"DM", "AE", "EX", "RS", "DS", "LB"}

    for v in available_vars_fn(sdir):
        df = load_fn(sdir, v)
        if df is not None and v.upper() in SDTM_DOMAINS:
            domain_map[v.upper()] = (v, df)

    if not domain_map:
        return []

    all_issues = []

    # Per-domain checks
    for domain_upper, (var_name, df) in domain_map.items():
        for issue in validate_domain(domain_upper, df):
            issue["var"] = var_name  # needed by UI
            all_issues.append(issue)

    # Cross-domain referential integrity
    dfs_by_domain = {d: df for d, (_, df) in domain_map.items()}
    for issue in validate_referential_integrity(dfs_by_domain):
        # Attach var = the non-DM domain's var name
        domain = issue["domain"]
        if domain in domain_map:
            issue["var"] = domain_map[domain][0]
        else:
            issue["var"] = domain.lower()
        all_issues.append(issue)

    return all_issues
