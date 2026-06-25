"""Stage 5 — Statistical computation. lifelines only. No model output enters here."""
import json
import pandas as pd
from lifelines import KaplanMeierFitter, CoxPHFitter
from lifelines.statistics import logrank_test


def run_os_analysis(adtte: pd.DataFrame, dataset_hash: str) -> dict:
    itt = adtte[adtte["ITTFL"] == "Y"].copy()

    dara = itt[itt["ARMCD"] == "DARA"]
    chemo = itt[itt["ARMCD"] == "CHEMO"]

    kmf_dara = KaplanMeierFitter()
    kmf_chemo = KaplanMeierFitter()
    kmf_dara.fit(dara["AVAL"], event_observed=(dara["CNSR"] == 0), label="Daraxonrasib")
    kmf_chemo.fit(chemo["AVAL"], event_observed=(chemo["CNSR"] == 0), label="Chemotherapy")

    cox_df = itt.dropna(subset=["ECOGBL"]).copy()
    # ARM_BIN = 1 for daraxonrasib so the resulting HR reads as "daraxonrasib
    # vs chemotherapy" -- HR < 1 means daraxonrasib reduces the hazard of death,
    # matching how RevMed reports RASolute 302 in their public disclosures.
    cox_df["ARM_BIN"] = (cox_df["ARMCD"] == "DARA").astype(int)
    # lifelines convention: event_col = 1 means event observed, 0 means censored.
    # CDISC CNSR convention is the opposite (0 = event, 1 = censored), so invert here.
    cox_df["EVENT_OBSERVED"] = (cox_df["CNSR"] == 0).astype(int)
    cox = CoxPHFitter()
    cox.fit(
        cox_df[["AVAL", "EVENT_OBSERVED", "ARM_BIN", "ECOGBL"]],
        duration_col="AVAL",
        event_col="EVENT_OBSERVED",
        formula="ARM_BIN + ECOGBL",
    )

    hr = float(cox.hazard_ratios_["ARM_BIN"])
    # confidence_intervals_ is on the log-hazard (coefficient) scale -- exponentiate
    # to get the hazard-ratio-scale CI that gets reported alongside HR.
    import math
    ci_log = cox.confidence_intervals_.loc["ARM_BIN"]
    ci_lower = math.exp(ci_log.iloc[0])
    ci_upper = math.exp(ci_log.iloc[1])

    lr = logrank_test(
        dara["AVAL"], chemo["AVAL"],
        event_observed_A=(dara["CNSR"] == 0), event_observed_B=(chemo["CNSR"] == 0),
    )

    result = {
        "analysis_id": "OS_ITT_PRIMARY",
        "hazard_ratio": round(hr, 3),
        "ci_lower": round(ci_lower, 3),
        "ci_upper": round(ci_upper, 3),
        "p_value": float(lr.p_value),
        "median_os_dara_days": float(kmf_dara.median_survival_time_),
        "median_os_chemo_days": float(kmf_chemo.median_survival_time_),
        "n_events": int((itt["CNSR"] == 0).sum()),
        "n_total": len(itt),
        "n_dara": len(dara),
        "n_chemo": len(chemo),
        "dataset_hash": dataset_hash,
        "library": "lifelines==0.30.3",
        "cox_method": "efron",
        "ci_level": 0.95,
    }

    assert 0 < result["hazard_ratio"] < 10, "HR out of range"
    assert result["ci_lower"] < result["ci_upper"], "CI ordering violated"
    assert result["n_events"] <= result["n_total"], "Event count exceeds total"

    return result, kmf_dara, kmf_chemo


def run_safety_table(adae: pd.DataFrame, adsl: pd.DataFrame) -> pd.DataFrame:
    denom = adsl[adsl["SAFFL"] == "Y"].groupby("ARMCD")["USUBJID"].nunique()

    teae_g3 = adae[
        (adae["TRTEMFL"] == "Y") & (adae["AETOXGR"].astype(str).isin(["3", "4", "5"]))
    ]

    counts = (
        teae_g3.groupby(["AEBODSYS", "AEDECOD", "ARMCD"])["USUBJID"]
        .nunique()
        .reset_index()
        .rename(columns={"USUBJID": "n"})
    )

    table = counts.pivot_table(
        index=["AEBODSYS", "AEDECOD"], columns="ARMCD", values="n", fill_value=0
    ).reset_index()

    for arm in ["DARA", "CHEMO"]:
        if arm in table.columns:
            table[f"{arm}_pct"] = (table[arm] / denom.get(arm, 1) * 100).round(1)
        else:
            table[arm] = 0
            table[f"{arm}_pct"] = 0.0

    table["_sort"] = table["DARA"]
    table = table.sort_values("_sort", ascending=False).drop(columns="_sort")

    return table


if __name__ == "__main__":
    adsl = pd.read_csv("/home/claude/revmed-demo/output/ADSL.csv")
    adae = pd.read_csv("/home/claude/revmed-demo/output/ADAE.csv")
    adtte = pd.read_csv("/home/claude/revmed-demo/output/ADTTE.csv")

    result, kmf_dara, kmf_chemo = run_os_analysis(adtte, dataset_hash="sha256:demo-fixture")
    print(json.dumps(result, indent=2))

    safety = run_safety_table(adae, adsl)
    print("\nSafety table (top 10):")
    print(safety.head(10).to_string(index=False))

    safety.to_csv("/home/claude/revmed-demo/output/safety_table.csv", index=False)
    with open("/home/claude/revmed-demo/output/os_result.json", "w") as f:
        json.dump(result, f, indent=2)
