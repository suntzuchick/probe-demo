"""Stage 4 — ADaM derivation. Pure pandas. Explicit, auditable, no model."""
import pandas as pd

DATA_CUTOFF = pd.Timestamp("2024-06-01")

# ── Provenance: one entry per derived column, co-located with the code ────────
# These are read by _derive_clinical() in app.py. If you change any derivation
# logic above, update the matching entry here so the provenance panel stays true.
PIPELINE_PROVENANCE = {
    # ── ADSL ──────────────────────────────────────────────────────────────────
    "adsl.USUBJID": {
        "source": "dm.USUBJID",
        "transform": "direct copy",
        "confidence": 1.00,
    },
    "adsl.ARMCD": {
        "source": "dm.ARMCD",
        "transform": "direct copy from Demographics domain — not derived from EX",
        "confidence": 1.00,
    },
    "adsl.TRTSDT": {
        "source": "ex.EXSTDTC",
        "transform": "min(EXSTDTC) per subject — first dose date",
        "confidence": 1.00,
    },
    "adsl.TRTEDTM": {
        "source": "ex.EXENDTC",
        "transform": "max(EXENDTC) per subject — last dose date",
        "confidence": 1.00,
    },
    "adsl.DTHDT": {
        "source": "ds[DSDECOD == 'DEATH'].DSDTC",
        "transform": "death date from DS rows where DSDECOD = 'DEATH'",
        "confidence": 1.00,
    },
    "adsl.LSTALVDT": {
        "source": "ds[DSDECOD == 'ALIVE'].DSDTC",
        "transform": "last known alive date from DS rows where DSDECOD = 'ALIVE'",
        "confidence": 1.00,
    },
    "adsl.SAFFL": {
        "source": "adsl.TRTSDT",
        "transform": "'Y' if TRTSDT is not null (subject received at least one dose), else 'N'",
        "confidence": 1.00,
    },
    # ── ADAE ──────────────────────────────────────────────────────────────────
    "adae.TRTEMFL": {
        "source": "ae.AESTDTC vs adsl.TRTSDT and adsl.TRTEDTM",
        "transform": (
            "'Y' if TRTSDT ≤ AESTDTC ≤ TRTEDTM + 30 days; "
            "'N' otherwise; null if AESTDTC is missing"
        ),
        "confidence": 1.00,
    },
    "adae.AETOXGR": {
        "source": "ae.AETOXGR",
        "transform": "direct copy — NCI-CTCAE toxicity grade (numeric string)",
        "confidence": 1.00,
    },
    # ── ADTTE ─────────────────────────────────────────────────────────────────
    "adtte.CNSR": {
        "source": "adsl.DTHDT",
        "transform": "0 if DTHDT is not null (death observed = event), 1 if null (censored)",
        "confidence": 1.00,
    },
    "adtte.EVENT_DT": {
        "source": "adsl.DTHDT → adsl.LSTALVDT → DATA_CUTOFF",
        "transform": (
            "DTHDT if known; else LSTALVDT (last known alive date); "
            f"else data cutoff {DATA_CUTOFF.date()} — three-step fallback"
        ),
        "confidence": 1.00,
    },
    "adtte.AVAL": {
        "source": "adtte.EVENT_DT − adsl.TRTSDT",
        "transform": "(EVENT_DT − TRTSDT).dt.days — overall survival duration in days",
        "confidence": 1.00,
    },
}


def derive_adsl(dm: pd.DataFrame, ex: pd.DataFrame, ds: pd.DataFrame) -> pd.DataFrame:
    adsl = dm[["USUBJID", "STUDYID", "AGE", "SEX", "ARMCD", "ARM", "KRASMUT", "ECOGBL"]].copy()

    first_dose = (
        ex.groupby("USUBJID")["EXSTDTC"].min().reset_index().rename(columns={"EXSTDTC": "TRTSDT"})
    )
    adsl = adsl.merge(first_dose, on="USUBJID", how="left")

    last_dose = (
        ex.groupby("USUBJID")["EXENDTC"].max().reset_index().rename(columns={"EXENDTC": "TRTEDTM"})
    )
    adsl = adsl.merge(last_dose, on="USUBJID", how="left")

    death = ds[ds["DSDECOD"] == "DEATH"][["USUBJID", "DSDTC"]].rename(columns={"DSDTC": "DTHDT"})
    adsl = adsl.merge(death, on="USUBJID", how="left")

    # last known alive / contact date for censoring (ALIVE disposition rows)
    alive = ds[ds["DSDECOD"] == "ALIVE"][["USUBJID", "DSDTC"]].rename(columns={"DSDTC": "LSTALVDT"})
    adsl = adsl.merge(alive, on="USUBJID", how="left")

    adsl["ITTFL"] = "Y"
    adsl["SAFFL"] = adsl["TRTSDT"].notna().map({True: "Y", False: "N"})

    return adsl


def derive_adae(ae: pd.DataFrame, adsl: pd.DataFrame) -> pd.DataFrame:
    adae = ae.merge(
        adsl[["USUBJID", "TRTSDT", "TRTEDTM", "SAFFL", "ARMCD"]],
        on="USUBJID", how="left",
    )

    adae["AESTDTC_DT"] = pd.to_datetime(adae["AESTDTC"], errors="coerce")
    adae["TRTSDT_DT"] = pd.to_datetime(adae["TRTSDT"], errors="coerce")
    adae["TRTEDTM_DT"] = pd.to_datetime(adae["TRTEDTM"], errors="coerce")

    adae["TRTEMFL"] = (
        (adae["AESTDTC_DT"] >= adae["TRTSDT_DT"])
        & (adae["AESTDTC_DT"] <= adae["TRTEDTM_DT"] + pd.Timedelta(days=30))
    ).map({True: "Y", False: "N"})

    adae.loc[adae["AESTDTC_DT"].isna(), "TRTEMFL"] = None

    return adae


def derive_adtte(adsl: pd.DataFrame) -> pd.DataFrame:
    adtte = adsl[["USUBJID", "ARMCD", "ECOGBL", "ITTFL", "TRTSDT", "DTHDT", "LSTALVDT"]].copy()
    adtte["TRTSDT"] = pd.to_datetime(adtte["TRTSDT"])
    adtte["DTHDT"] = pd.to_datetime(adtte["DTHDT"])
    adtte["LSTALVDT"] = pd.to_datetime(adtte["LSTALVDT"])

    # CNSR: 0 = event (death observed), 1 = censored
    adtte["CNSR"] = adtte["DTHDT"].isna().astype(int)

    # event/censor date: death date if known, else last known alive date, else cutoff
    adtte["EVENT_DT"] = adtte["DTHDT"]
    adtte["EVENT_DT"] = adtte["EVENT_DT"].fillna(adtte["LSTALVDT"])
    adtte["EVENT_DT"] = adtte["EVENT_DT"].fillna(DATA_CUTOFF)

    adtte["AVAL"] = (adtte["EVENT_DT"] - adtte["TRTSDT"]).dt.days

    neg = adtte[adtte["AVAL"] < 0]
    if len(neg) > 0:
        raise ValueError(f"Negative AVAL for subjects: {neg['USUBJID'].tolist()}")

    adtte["PARAM"] = "Overall Survival"
    adtte["PARAMCD"] = "OS"

    return adtte


def run_pipeline(dm, ex, ae, rs, ds):
    adsl = derive_adsl(dm, ex, ds)
    adae = derive_adae(ae, adsl)
    adtte = derive_adtte(adsl)
    return adsl, adae, adtte


if __name__ == "__main__":
    base = "/home/claude/revmed-demo/data"
    dm = pd.read_csv(f"{base}/DM.csv")
    ex = pd.read_csv(f"{base}/EX.csv")
    ae = pd.read_csv(f"{base}/AE.csv")
    rs = pd.read_csv(f"{base}/RS.csv")
    ds = pd.read_csv(f"{base}/DS.csv")

    adsl, adae, adtte = run_pipeline(dm, ex, ae, rs, ds)

    print("ADSL:", len(adsl), "rows |", adsl["SAFFL"].value_counts().to_dict())
    print("ADAE:", len(adae), "rows |", adae["TRTEMFL"].value_counts(dropna=False).to_dict())
    print("ADTTE:", len(adtte), "rows |", adtte["CNSR"].value_counts().to_dict())
    print("\nADTTE sample:\n", adtte[["USUBJID", "ARMCD", "AVAL", "CNSR"]].head(8))

    out_dir = "/home/claude/revmed-demo/output"
    import os
    os.makedirs(out_dir, exist_ok=True)
    adsl.to_csv(f"{out_dir}/ADSL.csv", index=False)
    adae.to_csv(f"{out_dir}/ADAE.csv", index=False)
    adtte.to_csv(f"{out_dir}/ADTTE.csv", index=False)
