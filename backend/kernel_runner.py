"""
Persistent kernel subprocess for Probe.

Spawned once per session by notebook_engine. All scientific libraries are
imported at startup so individual cell executions never pay import cost.

Protocol:
  stdin  ← one JSON line per cell: {"session_dir": "...", "code": "..."}
  stdout → ___PROBE_OUTPUT_START___ / JSON / ___PROBE_OUTPUT_END___
"""
import sys
import json
import io
import pickle
import os
import base64
import warnings
import traceback

warnings.filterwarnings("ignore")

# ── Core data stack ────────────────────────────────────────────────────────────
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
sns.set_theme(style="whitegrid", palette="muted", font_scale=1.1)

# ── Survival analysis ─────────────────────────────────────────────────────────
from lifelines import KaplanMeierFitter, CoxPHFitter
from lifelines.statistics import logrank_test

# ── Scientific computing / curve fitting ──────────────────────────────────────
import scipy.optimize as opt
import scipy.stats as stats
from scipy.optimize import curve_fit
from scipy.stats import (
    ttest_ind, ttest_rel, mannwhitneyu, wilcoxon,
    fisher_exact, chi2_contingency,
    pearsonr, spearmanr, kendalltau,
    shapiro, normaltest, kstest,
    kruskal, f_oneway, sem, iqr,
)

# ── Statistical modelling ─────────────────────────────────────────────────────
import statsmodels.api as sm
import statsmodels.formula.api as smf
from statsmodels.stats.multitest import multipletests
from statsmodels.graphics.mosaicplot import mosaic

# ── Machine learning / dimensionality reduction ───────────────────────────────
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.cluster import KMeans, AgglomerativeClustering
from sklearn.metrics import silhouette_score
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

# ── Biostats with effect sizes ────────────────────────────────────────────────
try:
    import pingouin as pg
except ImportError:
    pg = None

# ── UMAP dimensionality reduction ─────────────────────────────────────────────
try:
    import umap
    UMAP = umap.UMAP
except ImportError:
    UMAP = None

# ── Bioinformatics ────────────────────────────────────────────────────────────
try:
    from Bio import SeqIO, Entrez
    from Bio.SeqUtils import gc_fraction
except ImportError:
    SeqIO = Entrez = gc_fraction = None

# ── 4PL dose-response model (pre-defined so cells can call it directly) ───────
def hill4(x, bottom, top, ic50, slope):
    """4-parameter logistic (Hill) equation for dose-response curves."""
    return bottom + (top - bottom) / (1.0 + (ic50 / np.where(x == 0, 1e-12, x)) ** slope)

def fit_4pl(concentrations, viabilities):
    """
    Fit a 4PL curve and return (ic50_uM, hill_slope, r_squared, popt).
    concentrations and viabilities must be array-like, controls excluded.
    """
    x = np.asarray(concentrations, dtype=float)
    y = np.asarray(viabilities, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y) & (x > 0)
    x, y = x[mask], y[mask]
    if len(x) < 4:
        return None, None, None, None
    try:
        p0     = [0, 100, float(np.median(x)), 1.5]
        bounds = ([  -20,   50,  x.min()*0.01, 0.1],
                  [   50,  150,  x.max()*100,  10 ])
        popt, _ = curve_fit(hill4, x, y, p0=p0, bounds=bounds, maxfev=8000)
        y_pred  = hill4(x, *popt)
        ss_res  = np.sum((y - y_pred) ** 2)
        ss_tot  = np.sum((y - y.mean()) ** 2)
        r2      = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return float(popt[2]), float(popt[3]), float(r2), popt
    except Exception:
        return None, None, None, None

# ── Z-factor and plate QC helpers ─────────────────────────────────────────────
def zfactor(pos_vals, neg_vals):
    """Z-factor for plate assay quality. >0.5 = excellent."""
    mp, mn = np.mean(pos_vals), np.mean(neg_vals)
    sp, sn = np.std(pos_vals), np.std(neg_vals)
    denom  = abs(mp - mn)
    return 1 - 3*(sp + sn)/denom if denom > 0 else np.nan

def plate_cv(vals):
    """Coefficient of variation (%) for a set of replicate wells."""
    return float(np.std(vals) / np.mean(vals) * 100) if np.mean(vals) != 0 else np.nan

_real_stdout = sys.__stdout__
_real_stdout.write("___PROBE_READY___\n")
_real_stdout.flush()

namespace = {
    # Core
    "pd": pd, "np": np, "plt": plt, "sns": sns,
    # Survival
    "KaplanMeierFitter": KaplanMeierFitter,
    "CoxPHFitter": CoxPHFitter,
    "logrank_test": logrank_test,
    # Scipy
    "opt": opt, "stats": stats, "curve_fit": curve_fit,
    "ttest_ind": ttest_ind, "ttest_rel": ttest_rel,
    "mannwhitneyu": mannwhitneyu, "wilcoxon": wilcoxon,
    "fisher_exact": fisher_exact, "chi2_contingency": chi2_contingency,
    "pearsonr": pearsonr, "spearmanr": spearmanr, "kendalltau": kendalltau,
    "shapiro": shapiro, "normaltest": normaltest, "kstest": kstest,
    "kruskal": kruskal, "f_oneway": f_oneway, "sem": sem, "iqr": iqr,
    "multipletests": multipletests, "mosaic": mosaic,
    # Statsmodels
    "sm": sm, "smf": smf,
    # Sklearn
    "PCA": PCA, "StandardScaler": StandardScaler, "MinMaxScaler": MinMaxScaler,
    "KMeans": KMeans, "AgglomerativeClustering": AgglomerativeClustering,
    "silhouette_score": silhouette_score,
    "LinearRegression": LinearRegression, "LogisticRegression": LogisticRegression,
    "RandomForestClassifier": RandomForestClassifier,
    "RandomForestRegressor": RandomForestRegressor,
    # Biostats
    "pg": pg,
    # UMAP
    "UMAP": UMAP,
    # Bioinformatics (None if biopython not installed)
    "SeqIO": SeqIO, "Entrez": Entrez, "gc_fraction": gc_fraction,
    # Domain helpers
    "hill4": hill4, "fit_4pl": fit_4pl,
    "zfactor": zfactor, "plate_cv": plate_cv,
}

while True:
    raw = sys.stdin.readline()
    if not raw:
        break
    raw = raw.strip()
    if not raw:
        continue

    try:
        request = json.loads(raw)
    except Exception:
        continue

    session_dir = request["session_dir"]
    code        = request["code"]

    # Load every persisted session variable
    if os.path.isdir(session_dir):
        for fname in os.listdir(session_dir):
            if not fname.endswith(".pkl"):
                continue
            v = fname[:-4]
            try:
                with open(os.path.join(session_dir, fname), "rb") as f:
                    namespace[v] = pickle.load(f)
            except Exception:
                pass

    capture    = io.StringIO()
    sys.stdout = capture
    error      = None
    result_repr = None

    try:
        exec(compile(code, "<cell>", "exec"), namespace)
        lines     = code.strip().split("\n")
        last_line = lines[-1].strip() if lines else ""
        if (last_line and
                not last_line.startswith(("#", "import ", "from ", "for ", "if ",
                                          "def ", "class ", "with ", "print(")) and
                "=" not in last_line.split("(")[0]):
            try:
                result_repr = repr(eval(last_line, namespace))
            except Exception:
                pass
    except Exception:
        error = traceback.format_exc()
    finally:
        sys.stdout = _real_stdout

    stdout_text = capture.getvalue()

    figures_b64 = []
    for fignum in plt.get_fignums():
        fig = plt.figure(fignum)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        buf.seek(0)
        figures_b64.append(base64.b64encode(buf.read()).decode("ascii"))
    plt.close("all")

    # Persist session variables — anything already on disk or any new DataFrame
    already_persisted = (
        {f[:-4] for f in os.listdir(session_dir) if f.endswith(".pkl")}
        if os.path.isdir(session_dir) else set()
    )
    for v, val in namespace.items():
        if v.startswith("_") or val is None:
            continue
        if v in already_persisted or isinstance(val, pd.DataFrame):
            try:
                with open(os.path.join(session_dir, v + ".pkl"), "wb") as fh:
                    pickle.dump(val, fh)
            except Exception:
                pass

    output = {
        "stdout":      stdout_text,
        "result_repr": result_repr,
        "error":       error,
        "figures":     figures_b64,
    }

    _real_stdout.write("___PROBE_OUTPUT_START___\n")
    _real_stdout.write(json.dumps(output) + "\n")
    _real_stdout.write("___PROBE_OUTPUT_END___\n")
    _real_stdout.flush()
