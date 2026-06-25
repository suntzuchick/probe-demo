"""KM curve rendering. Self-contained -- no dependency on any sibling project."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker


def render_km_curve_png(result: dict, kmf_dara, kmf_chemo, output_path):
    plt.rcParams["font.family"] = "DejaVu Sans"
    fig, (ax_km, ax_nar) = plt.subplots(
        2, 1, figsize=(7.4, 5.6),
        gridspec_kw={"height_ratios": [5, 1]},
        facecolor="white",
    )
    kmf_dara.plot_survival_function(ax=ax_km, ci_show=True, color="#1D7A5F", linewidth=2,
                                     label=f"Daraxonrasib (n={result['n_dara']})")
    kmf_chemo.plot_survival_function(ax=ax_km, ci_show=True, color="#88877F", linewidth=2,
                                      label=f"Chemotherapy (n={result['n_chemo']})")
    hr, cil, ciu, p = result["hazard_ratio"], result["ci_lower"], result["ci_upper"], result["p_value"]
    med_d = round(result["median_os_dara_days"] / 30.4, 1) if result["median_os_dara_days"] != float("inf") else None
    med_c = round(result["median_os_chemo_days"] / 30.4, 1) if result["median_os_chemo_days"] != float("inf") else None
    p_str = "< 0.0001" if p < 0.0001 else f"= {p:.4f}"
    med_str = f"Median OS: {med_d if med_d is not None else 'NR'} mo vs {med_c if med_c is not None else 'NR'} mo"
    annotation = f"HR = {hr} (95% CI: {cil}\u2013{ciu})\nLog-rank p {p_str}\n{med_str}"
    ax_km.text(0.97, 0.95, annotation, transform=ax_km.transAxes, ha="right", va="top",
               fontsize=9.5, family="monospace",
               bbox=dict(boxstyle="round,pad=0.5", facecolor="white", edgecolor="#D3D1C7", linewidth=0.7))
    ax_km.set_xlabel("Time (months)", fontsize=10)
    ax_km.set_ylabel("Survival probability", fontsize=10)
    ax_km.set_title("Overall survival \u2014 ITT population", fontsize=11.5)
    ax_km.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x/30.4:.0f}"))
    ax_km.set_ylim(0, 1.05)
    ax_km.set_xlim(left=0)
    ax_km.legend(fontsize=9, loc="lower left")
    ax_km.spines[["top", "right"]].set_visible(False)
    ax_km.grid(axis="y", alpha=0.15)
    ax_nar.axis("off")
    max_time = max(
        kmf_dara.event_table.index.max() if len(kmf_dara.event_table) else 0,
        kmf_chemo.event_table.index.max() if len(kmf_chemo.event_table) else 0,
    )
    n_points = 7
    timepoints = [round(i * max_time / (n_points - 1)) for i in range(n_points)] if max_time > 0 else [0]
    nar_dara = [kmf_dara.event_table.at_risk.asof(t) if t <= kmf_dara.event_table.index.max() else 0 for t in timepoints]
    nar_chemo = [kmf_chemo.event_table.at_risk.asof(t) if t <= kmf_chemo.event_table.index.max() else 0 for t in timepoints]
    ax_nar.text(0.0, 0.75, "Number at risk", fontsize=8.5, fontweight="bold", transform=ax_nar.transAxes)
    ax_nar.text(-0.04, 0.45, "Daraxonrasib", fontsize=8, ha="right", transform=ax_nar.transAxes)
    ax_nar.text(-0.04, 0.10, "Chemotherapy", fontsize=8, ha="right", transform=ax_nar.transAxes)
    max_t = max(timepoints) * 1.05 if max(timepoints) > 0 else 1
    for i, t in enumerate(timepoints):
        x_pos = t / max_t
        ax_nar.text(x_pos, 0.45, str(int(nar_dara[i])), fontsize=8, ha="center", transform=ax_nar.transAxes)
        ax_nar.text(x_pos, 0.10, str(int(nar_chemo[i])), fontsize=8, ha="center", transform=ax_nar.transAxes)
    plt.tight_layout()
    plt.savefig(output_path, format="png", dpi=200, bbox_inches="tight")
    plt.close()
    return output_path
