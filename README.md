# Probe — Extraction Agent &amp; Analysis Notebook

A real extraction agent followed by a real, free-form Python notebook —
not a fixed dashboard, not a chatbot. Four screens: ingest, watch the
agent's real reasoning trace, derive, then write and run actual code
against the real data.

## What changed from the previous build

- **Generative natural-language cells, backed by a real Claude API call.** Above the notebook, there's an input: type a request in plain English (e.g. "show me OS by KRAS subtype") and Claude writes the pandas/lifelines code, which then runs immediately through the exact same execution engine as a hand-typed cell. This is the one place in Probe that calls an external model API.
- **Visual style and flow reverted** to the original clinical-paper aesthetic (cream background, serif headers, monospace data, the linear staged flow) — restyled, but the same calm clinical-document feel.
- **New extraction screen**: a live terminal that streams the agent's actual reasoning line by line — every line is generated directly from real computed values (real column names, real confidence scores, real row counts), not scripted copy.
- **The results dashboard is gone.** In its place: a real notebook. After derivation, you get `adsl`, `adae`, `adtte` as real pandas DataFrames and write actual Python against them. Cells execute server-side, state persists between cells (so a cast or filter in one cell is visible in the next), and any matplotlib figure a cell produces comes back as a real image.

## Setting up the generative cells (optional)

The natural-language-to-code feature needs an Anthropic API key. Without one, every other part of Probe works exactly the same — extraction, mapping, derivation, the manually-typed notebook cells — only the "Generate & run" input will return a clear error telling you the key isn't set.

To enable it:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
cd backend
python3 app.py
```
(Set the environment variable in the same terminal session, before starting the server. On Windows, use `set ANTHROPIC_API_KEY=sk-ant-...` in Command Prompt or `$env:ANTHROPIC_API_KEY="sk-ant-..."` in PowerShell.)

### What the model can and can't see
The model is sent only the column names and dtypes of whatever DataFrames exist in your session — never the actual patient-level values. Its only allowed output is a single Python code block; anything else is treated as a failed generation. The generated code then runs through the identical subprocess used for cells you type by hand — same timeout, same lack of sandboxing against malicious code (see the warning below), same output capture. The model is an input method for the code box, not a separate execution path with different rules.

## Architecture

```
Browser (frontend/)
  index.html / style.css / app.js
        |
        | fetch() over HTTP
        v
Flask backend (backend/app.py), port 5050
  extractors/        — real format detection + per-format parsers
  extraction_trace.py — turns a real extraction result into log lines
  derive_adam.py       — real ADSL/ADAE/ADTTE derivation
  notebook_engine.py    — runs a cell's code in a subprocess, with the
                           session's dataframes pre-loaded, returns
                           stdout / last-expression repr / any figures
  session_store.py       — in-memory per-session state
  notebook_state/<sid>/   — pickled dataframes, one file per variable,
                             updated after every cell run
```

## Important: notebook execution is real, unsandboxed Python

`POST /api/notebook/run` executes the submitted code in a subprocess
with a 15-second timeout. This is genuine arbitrary code execution —
appropriate for a local, single-tenant demo where the only person
running code is the person using the demo. It is **not** isolated
against malicious code and should not be exposed multi-tenant or on
the open internet without adding a real sandbox (gVisor, Firecracker,
or a hosted code-execution service) in front of it. This tradeoff is
intentional for the demo and worth stating plainly rather than papering
over if asked.

## Running it

### 1. Install dependencies
```bash
cd backend
pip install flask flask-cors pandas openpyxl pdfplumber pymupdf pytesseract pillow lifelines reportlab --break-system-packages
```
Also install the Tesseract OCR binary:
```bash
sudo apt-get install tesseract-ocr      # Debian/Ubuntu
brew install tesseract                   # Mac
```

### 2. Start the backend
```bash
cd backend
python3 app.py
```
Runs on `http://localhost:5050`.

### 3. Start the frontend
```bash
cd frontend
python3 -m http.server 8911
```
Open `http://localhost:8911/index.html`.

### 4. Walk through it
1. **Ingest** — click "Load RASolute 302 (simulated)" or drop your own CSV/XLSX/PDF/image/JSON.
2. **Extraction trace** — watch the real reasoning stream in: format detected, layout classified, every column scored against the SDTM/assay vocabulary with a real confidence number.
3. **Derivation** — ADSL → ADAE → ADTTE derive for real, in dependency order.
4. **Notebook** — `adsl`, `adae`, `adtte` are now real variables. Click a template chip to pre-fill a cell, or write your own. `Cmd/Ctrl+Enter` runs the focused cell. Add as many cells as you want.

## Notebook quick reference

Available out of the box in every cell: `pd`, `np`, `plt`, `KaplanMeierFitter`, `CoxPHFitter`, `logrank_test`, and any of `dm`, `ex`, `ae`, `rs`, `ds`, `adsl`, `adae`, `adtte` that exist in the session.

```python
# subgroup breakdown
adsl.groupby('KRASMUT')['AGE'].describe()

# real KM curve
from lifelines import KaplanMeierFitter
import matplotlib.pyplot as plt
adtte['AVAL'] = adtte['AVAL'].astype(float)
adtte['CNSR'] = adtte['CNSR'].astype(int)
fig, ax = plt.subplots()
for arm in adtte['ARMCD'].unique():
    sub = adtte[adtte['ARMCD']==arm]
    KaplanMeierFitter().fit(sub['AVAL'], event_observed=(sub['CNSR']==0), label=arm).plot_survival_function(ax=ax)

# Cox proportional hazards
from lifelines import CoxPHFitter
adtte['ARM_BIN'] = (adtte['ARMCD']=='DARA').astype(int)
CoxPHFitter().fit(adtte[['AVAL','CNSR','ARM_BIN']], duration_col='AVAL', event_col='CNSR').print_summary()
```

## File structure
```
backend/
  app.py                 Flask app, all endpoints
  codegen.py               natural-language -> code via the Claude API
  notebook_engine.py       real cell execution + state persistence
  extraction_trace.py       extraction result -> log lines
  session_store.py           in-memory session state
  extractors/
    agent.py                 dispatcher: format-detect -> right parser
    format_detect.py          magic-byte format detection
    tabular.py                  CSV/TSV + plate-map reshaping
    excel.py                     multi-sheet XLSX handling
    pdf_extractor.py               PDF text/table/parameter extraction
    image_extractor.py               OCR
    json_extractor.py                  JSON parsing
    sdtm_mapping.py                      variable mapping vocabulary + scoring
    validate.py                           controlled-terminology checks
    derive_adam.py                         ADSL/ADAE/ADTTE derivation
    analyze.py                              KM/Cox/safety table helpers
    render.py                                 KM curve rendering helper
  sample_data/             the bundled RASolute 302 (simulated) CSVs
frontend/
  index.html
  style.css                clinical-paper aesthetic
  app.js                     screen flow + notebook cell logic
```
