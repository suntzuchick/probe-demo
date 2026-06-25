(function () {
  "use strict";

  // API base: empty string = same origin (works on Render); fallback for file:// dev
  var API = window.location.protocol === "file:" ? "http://localhost:5050" : "";
  var sessionId = null;
  var REQUIRED_DOMAINS = ["DM", "EX", "AE", "RS", "DS"];
  var pendingTraces = [];
  var cellCounter = 0;
  var _provenanceMeta = null;
  var _notebookVars = [];

  // ── Auth token management ──────────────────────────────────────────────────
  var _probeToken = null;

  // If arriving via magic link redirect: ?probe_token=XXX
  (function () {
    var params = new URLSearchParams(window.location.search);
    var urlToken = params.get("probe_token");
    if (urlToken) {
      localStorage.setItem("probe_token", urlToken);
      // Remove the token from the visible URL immediately
      var clean = window.location.pathname + window.location.hash;
      window.history.replaceState({}, document.title, clean);
    }
    _probeToken = localStorage.getItem("probe_token");
  })();

  // Intercept all API fetches: inject X-Probe-Token and handle 401 globally
  (function () {
    var _origFetch = window.fetch.bind(window);
    window.fetch = function (url, opts) {
      var isApi = typeof url === "string" && url.includes("/api/") && !url.includes("/api/auth/");
      if (isApi && _probeToken) {
        opts = opts ? Object.assign({}, opts) : {};
        opts.headers = Object.assign({}, opts.headers || {}, { "X-Probe-Token": _probeToken });
      }
      return _origFetch(url, opts).then(function (res) {
        if (res.status === 401 && isApi) {
          _probeToken = null;
          localStorage.removeItem("probe_token");
          showAuthOverlay();
        }
        return res;
      });
    };
  })();

  // ── Auth overlay ───────────────────────────────────────────────────────────

  function showAuthOverlay() {
    document.getElementById("auth-overlay").style.display = "flex";
    document.querySelector(".masthead").style.display = "none";
    document.querySelector(".shell").style.display = "none";
  }

  function hideAuthOverlay() {
    document.getElementById("auth-overlay").style.display = "none";
    document.querySelector(".masthead").style.display = "";
    document.querySelector(".shell").style.display = "";
  }

  async function checkAuth() {
    try {
      var res = await fetch(API + "/api/auth/status", {
        headers: _probeToken ? { "X-Probe-Token": _probeToken } : {},
      });
      var data = await res.json();
      if (!data.authenticated && data.email === "") {
        // Auth is enabled but we're not authenticated
        showAuthOverlay();
        return false;
      }
      if (data.authenticated && data.email) {
        // Show the logged-in user in the masthead
        var mu = document.getElementById("masthead-user");
        if (mu && !mu.textContent) mu.textContent = data.email;
      }
      return true;
    } catch (e) {
      // Can't reach server or auth not configured — proceed
      return true;
    }
  }

  // Auth form: request magic link
  var authForm = document.getElementById("auth-form");
  if (authForm) {
    authForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      var email = (document.getElementById("auth-email").value || "").trim();
      var errEl = document.getElementById("auth-error");
      errEl.textContent = "";
      if (!email || !email.includes("@")) {
        errEl.textContent = "Enter a valid email address.";
        return;
      }
      var btn = authForm.querySelector("button[type=submit]");
      btn.disabled = true;
      btn.textContent = "Sending…";
      try {
        var res = await fetch(API + "/api/auth/request", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: email }),
        });
        var data = await res.json();
        if (data.error) {
          errEl.textContent = data.error;
          btn.disabled = false;
          btn.textContent = "Send access link →";
        } else {
          document.getElementById("auth-sent-email").textContent = email;
          document.getElementById("auth-request-section").style.display = "none";
          document.getElementById("auth-sent-section").style.display = "block";
        }
      } catch (err) {
        errEl.textContent = "Could not reach the server. Try again in a moment.";
        btn.disabled = false;
        btn.textContent = "Send access link →";
      }
    });
  }

  var authTryAgain = document.getElementById("auth-try-again");
  if (authTryAgain) {
    authTryAgain.addEventListener("click", function () {
      document.getElementById("auth-request-section").style.display = "block";
      document.getElementById("auth-sent-section").style.display = "none";
      document.getElementById("auth-error").textContent = "";
      document.querySelector(".auth-email-input").value = "";
      authForm.querySelector("button[type=submit]").disabled = false;
      authForm.querySelector("button[type=submit]").textContent = "Send access link →";
    });
  }

  // ------------- screen nav -------------
  var screens = ["screen-0", "screen-1", "screen-2", "screen-3", "screen-4", "screen-5"];
  var railStages = document.querySelectorAll(".rail-stage");

  function goTo(screenId, stageNum) {
    screens.forEach(function (id) {
      document.getElementById(id).classList.toggle("active", id === screenId);
    });
    railStages.forEach(function (el) {
      var n = parseInt(el.dataset.stage, 10);
      el.classList.remove("active", "done");
      if (n < stageNum) el.classList.add("done");
      if (n === stageNum) el.classList.add("active");
    });
    document.body.classList.toggle("is-info",     screenId === "screen-0");
    document.body.classList.toggle("is-notebook", screenId === "screen-5");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function setRailMeta(stage, text) {
    var el = document.getElementById("rail-meta-" + stage);
    if (el) el.textContent = text;
  }

  // Logout button — only shown when auth is active
  var logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", async function () {
      await fetch(API + "/api/auth/logout", { method: "POST" });
      _probeToken = null;
      localStorage.removeItem("probe_token");
      showAuthOverlay();
    });
  }

  // ── Bootstrap: check auth before anything else ─────────────────────────────
  checkAuth().then(function (ok) {
    if (ok) {
      // Show logout button only when auth is actually enabled
      fetch(API + "/api/auth/status").then(function (r) {
        return r.json();
      }).then(function (d) {
        if (d.authenticated && logoutBtn) logoutBtn.style.display = "inline-block";
      }).catch(function () {});
    }
  });

  // ------------- screen 0: info gate -------------
  document.getElementById("info-form").addEventListener("submit", async function (e) {
    e.preventDefault();
    var name    = document.getElementById("info-name").value.trim();
    var org     = document.getElementById("info-org").value.trim();
    var email   = document.getElementById("info-email").value.trim();
    var project = document.getElementById("info-project").value.trim();
    var errorEl = document.getElementById("info-error");

    errorEl.textContent = "";
    if (!name || !org || !email) {
      errorEl.textContent = "Please fill in your name, organization, and email.";
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      errorEl.textContent = "Please enter a valid email address.";
      return;
    }

    var submitBtn = this.querySelector(".info-submit");
    submitBtn.disabled = true;
    submitBtn.textContent = "Setting up…";

    try {
      await ensureSession();
      await fetch(API + "/api/session/info", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, name: name, organization: org, email: email, project: project }),
      });

      var userEl = document.getElementById("masthead-user");
      if (userEl) userEl.textContent = name + (org ? " · " + org : "") + " — ";

      goTo("screen-1", 1);
    } catch (err) {
      errorEl.textContent = "Could not connect — make sure the backend is running.";
      submitBtn.disabled = false;
      submitBtn.textContent = "Get started →";
    }
  });

  // ------------- session -------------
  async function ensureSession() {
    if (sessionId) return sessionId;
    var res = await fetch(API + "/api/session", { method: "POST" });
    var data = await res.json();
    sessionId = data.session_id;
    document.getElementById("session-id").textContent = sessionId;
    return sessionId;
  }

  // ------------- screen 1: ingest -------------
  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("file-input");

  document.getElementById("browse-btn").addEventListener("click", function (e) {
    e.stopPropagation();
    fileInput.click();
  });
  dropzone.addEventListener("click", function () { fileInput.click(); });
  ["dragenter", "dragover"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.add("dragover"); });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.classList.remove("dragover"); });
  });
  dropzone.addEventListener("drop", function (e) { handleFiles(e.dataTransfer.files); });
  fileInput.addEventListener("change", function () { handleFiles(fileInput.files); });

  async function handleFiles(fileList) {
    await ensureSession();
    for (var i = 0; i < fileList.length; i++) {
      await uploadFile(fileList[i]);
    }
  }

  async function uploadFile(file) {
    var formData = new FormData();
    formData.append("session_id", sessionId);
    formData.append("file", file);
    try {
      var res = await fetch(API + "/api/upload", { method: "POST", body: formData });
      var data = await res.json();
      pendingTraces.push({ filename: file.name, domain: null, trace: data.trace, extraction: data.extraction });
      document.getElementById("btn-to-trace").disabled = false;
      setRailMeta(1, pendingTraces.length + " file(s) read");
    } catch (err) {
      pendingTraces.push({ filename: file.name, domain: null, trace: [{ level: "error", text: "Could not reach Probe backend at " + API }] });
      document.getElementById("btn-to-trace").disabled = false;
    }
  }

  document.getElementById("load-sample-btn").addEventListener("click", async function () {
    await ensureSession();
    var btn = document.getElementById("load-sample-btn");
    btn.disabled = true;
    btn.textContent = "Loading\u2026";
    var res = await fetch(API + "/api/load-sample", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });
    var data = await res.json();
    btn.textContent = "RASolute 302 (simulated) \u2014 loaded \u2713";

    pendingTraces = pendingTraces.concat(data.traces);

    Object.keys(data.loaded || {}).forEach(function (domain) {
      var info = data.loaded[domain];
      var cell = document.querySelector('.domain-card[data-domain="' + domain + '"]');
      cell.classList.add("received");
      cell.querySelector(".stat").textContent = info.n_rows + " rows";
    });

    setRailMeta(1, "5 domains loaded");
    document.getElementById("btn-to-trace").disabled = false;
  });

  document.getElementById("btn-to-trace").addEventListener("click", function () {
    goTo("screen-2", 2);
    runTerminalScreen();
  });

  // ------------- screen 2: extraction terminal -------------
  var terminalStarted = false;

  function runTerminalScreen() {
    if (terminalStarted) return;
    terminalStarted = true;

    var terminal = document.getElementById("terminal");
    var allLines = [];
    pendingTraces.forEach(function (t) {
      allLines.push({ type: "header", text: t.filename });
      t.trace.forEach(function (l) { allLines.push({ type: "line", level: l.level, text: l.text }); });
    });

    var i = 0;
    function emitNext() {
      if (i >= allLines.length) {
        setRailMeta(2, allLines.length + " trace lines");
        document.getElementById("btn-to-quality").disabled = false;
        return;
      }
      var item = allLines[i];
      var el = document.createElement("div");
      if (item.type === "header") {
        el.className = "term-line term-file-header";
        el.textContent = item.text;
      } else {
        el.className = "term-line " + item.level;
        el.textContent = item.text;
      }
      terminal.appendChild(el);
      terminal.scrollTop = terminal.scrollHeight;
      i++;
      setTimeout(emitNext, item.type === "header" ? 280 : 90);
    }
    emitNext();
  }

  document.getElementById("btn-back-1").addEventListener("click", function () { goTo("screen-1", 1); });
  document.getElementById("btn-to-quality").addEventListener("click", function () {
    goTo("screen-3", 3);
    runQualityScreen();
  });

  // ------------- screen 3: data quality -------------
  var qualityRan = false;
  var _qualityIssues = [];

  var SEVERITY_LABEL = { high: "High", medium: "Medium", low: "Low" };
  var ISSUE_TYPE_LABEL = {
    missing:                 "Missing values",
    outlier:                 "Outliers",
    mixed_case:              "Mixed case",
    duplicates:              "Duplicate rows",
    type_mismatch:           "Type mismatch",
    missing_required_field:  "Missing required column",
    missing_required_value:  "Missing required value",
    duplicate_key:           "Duplicate subject ID",
    ct_violation:            "Controlled terminology violation",
    implausible_value:       "Implausible value",
    date_order_violation:    "Date ordering error",
    orphaned_subject:        "Orphaned subject (referential integrity)",
  };

  async function runQualityScreen() {
    if (qualityRan) return;
    qualityRan = true;

    var listEl = document.getElementById("quality-issue-list");
    var descEl = document.getElementById("quality-desc");
    listEl.innerHTML = '<div class="quality-scanning">Scanning variables…</div>';

    try {
      var res = await fetch(API + "/api/quality/check?session_id=" + encodeURIComponent(sessionId));
      var data = await res.json();
      _qualityIssues = data.issues || [];

      if (_qualityIssues.length === 0) {
        listEl.innerHTML = '<div class="quality-clean"><span class="quality-clean-icon">✓</span> No issues detected — all variables passed quality checks.</div>';
        descEl.textContent = "All ingested variables passed quality checks. Proceed to derivation.";
        setRailMeta(3, "0 issues");
      } else {
        descEl.textContent = _qualityIssues.length + " issue" + (_qualityIssues.length > 1 ? "s" : "") + " detected across " +
          [...new Set(_qualityIssues.map(function(i){ return i.var; }))].length + " variable(s). Select fixes to apply before derivation.";
        setRailMeta(3, _qualityIssues.length + " issues");

        listEl.innerHTML = "";
        _qualityIssues.forEach(function (issue, idx) {
          var card = document.createElement("div");
          card.className = "quality-card severity-" + issue.severity;
          card.innerHTML =
            '<div class="quality-card-header">' +
              '<label class="quality-checkbox-label">' +
                '<input type="checkbox" class="quality-fix-cb" data-idx="' + idx + '" ' + (issue.severity === "high" && issue.fix_label ? "checked" : "") + (issue.fix_label ? "" : " disabled") + '>' +
                '<span class="quality-issue-type">' + escapeHtml(ISSUE_TYPE_LABEL[issue.type] || issue.type) + '</span>' +
              '</label>' +
              '<span class="quality-severity severity-' + issue.severity + '">' + escapeHtml(SEVERITY_LABEL[issue.severity] || issue.severity) + '</span>' +
            '</div>' +
            '<div class="quality-card-location"><code>' + escapeHtml(issue.var) + (issue.col ? '.' + escapeHtml(issue.col) : '') + '</code></div>' +
            '<div class="quality-card-desc">' + escapeHtml(issue.description) + '</div>' +
            (issue.fix_label
              ? '<div class="quality-fix-row"><span class="quality-fix-label">Proposed fix:</span> <span class="quality-fix-action">' + escapeHtml(issue.fix_label) + '</span></div>'
              : '<div class="quality-fix-row quality-fix-manual"><span class="quality-fix-label">Action required:</span> <span class="quality-fix-action quality-no-autofix">Manual correction needed in source data</span></div>'
            );
          listEl.appendChild(card);
        });
      }
    } catch (e) {
      listEl.innerHTML = '<div class="quality-scanning">Quality check unavailable — proceed to derivation.</div>';
      setRailMeta(3, "skipped");
    }
  }

  document.getElementById("btn-apply-quality").addEventListener("click", async function () {
    var selected = [];
    document.querySelectorAll(".quality-fix-cb:checked").forEach(function (cb) {
      var idx = parseInt(cb.dataset.idx, 10);
      if (_qualityIssues[idx]) selected.push(_qualityIssues[idx]);
    });

    if (selected.length > 0) {
      try {
        await fetch(API + "/api/quality/apply", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, fixes: selected }),
        });
      } catch (e) { /* non-fatal */ }
    }

    goTo("screen-4", 4);
    runDerivationScreen();
  });

  // ------------- screen 4: derivation -------------
  var derivationRan = false;
  var _derivePlan = null;

  async function runDerivationScreen() {
    if (derivationRan) return;
    derivationRan = true;

    // 1. Fetch the context-aware plan
    var planRes = await fetch(API + "/api/derive/plan?session_id=" + encodeURIComponent(sessionId));
    _derivePlan = await planRes.json();

    // 2. Update context badge + description
    document.getElementById("context-badge").textContent = _derivePlan.context_label;
    document.getElementById("derive-desc").textContent = _derivePlan.description;

    // 3. Render steps from the plan
    var track = document.getElementById("run-track");
    track.innerHTML = "";
    _derivePlan.steps.forEach(function (step) {
      var el = document.createElement("div");
      el.className = "run-step";
      el.dataset.step = step.key;
      el.innerHTML =
        '<div class="run-step-icon">\u25cb</div>' +
        '<div class="run-step-body">' +
          '<div class="run-step-name">' + escapeHtml(step.label) + '</div>' +
          '<div class="run-step-meta"></div>' +
        '</div>';
      track.appendChild(el);
    });

    // 4. Animate steps while derivation runs
    var stepKeys = _derivePlan.steps.map(function (s) { return s.key; });
    var idx = 0;
    function advance() {
      if (idx > 0) {
        var prev = document.querySelector('.run-step[data-step="' + stepKeys[idx - 1] + '"]');
        if (prev) { prev.classList.remove("active"); prev.classList.add("done"); prev.querySelector(".run-step-icon").textContent = "\u2713"; }
      }
      if (idx < stepKeys.length) {
        var cur = document.querySelector('.run-step[data-step="' + stepKeys[idx] + '"]');
        if (cur) { cur.classList.add("active"); cur.querySelector(".run-step-icon").textContent = "\u25cc"; }
        idx++;
        setTimeout(advance, 650);
      }
    }
    advance();

    // 5. Run derivation
    var res = await fetch(API + "/api/derive", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });
    var data = await res.json();

    setTimeout(function () {
      if (data.status === "ok") {
        var datasets = data.datasets || {};
        var n = Object.keys(datasets).length;
        stepKeys.forEach(function (key) {
          var el = document.querySelector('.run-step[data-step="' + key + '"]');
          if (!el) return;
          el.classList.remove("active"); el.classList.add("done");
          el.querySelector(".run-step-icon").textContent = "\u2713";
          if (datasets[key]) {
            el.querySelector(".run-step-meta").textContent =
              datasets[key].rows + " rows, " + datasets[key].columns.length + " columns";
          }
        });
        setRailMeta(4, n + "/" + n + " datasets derived");
        document.getElementById("btn-to-notebook").disabled = false;

      } else if (data.status === "incomplete") {
        var missing = data.missing_domains || [];
        window._missingDomains = missing;

        var DOMAIN_META = {
          DM: { label: "Demographics",  needed_by: ["ADSL","ADAE","ADTTE"] },
          EX: { label: "Exposure",       needed_by: ["ADSL"] },
          AE: { label: "Adverse Events", needed_by: ["ADAE"] },
          RS: { label: "Tumor Response", needed_by: ["ADTTE"] },
          DS: { label: "Disposition",    needed_by: ["ADSL","ADTTE"] },
        };
        var DATASET_NEEDS = { ADSL:["DM","EX","DS"], ADAE:["DM","AE"], ADTTE:["DM","DS","RS"] };

        var missingRows = missing.map(function (code) {
          var meta = DOMAIN_META[code] || { label: code, needed_by: [] };
          return '<tr><td><code>' + escapeHtml(code) + '</code></td><td>' + escapeHtml(meta.label) + '</td><td>' +
            (meta.needed_by.length ? meta.needed_by.map(function(d){ return '<span class="impact-tag">'+d+'</span>'; }).join(' ') : '\u2014') +
            '</td></tr>';
        }).join('');

        var datasetRows = stepKeys.map(function (key) {
          var DS = key.toUpperCase();
          var needed = DATASET_NEEDS[DS] || [];
          var blocked = needed.filter(function (d) { return missing.indexOf(d) !== -1; });
          return '<tr><td><code>' + DS + '</code></td><td>' +
            (blocked.length === 0 ? '<span class="status-ok">derived</span>'
              : '<span class="status-blocked">blocked \u2014 needs ' + blocked.join(', ') + '</span>') +
            '</td></tr>';
        }).join('');

        track.insertAdjacentHTML("beforeend",
          '<div class="derive-warning">' +
            '<div class="derive-warning-title">Derivation incomplete \u2014 missing source domains</div>' +
            '<table class="derive-table"><thead><tr><th>Domain</th><th>Name</th><th>Impacts</th></tr></thead><tbody>' + missingRows + '</tbody></table>' +
            '<div class="derive-warning-title" style="margin-top:14px;">Derived dataset status</div>' +
            '<table class="derive-table"><thead><tr><th>Dataset</th><th>Status</th></tr></thead><tbody>' + datasetRows + '</tbody></table>' +
            '<p style="margin:12px 0 0;font-size:13px;">You can continue to the notebook with whatever data was derived, or go back and upload the missing files.</p>' +
          '</div>');
        var btn = document.getElementById("btn-to-notebook");
        btn.disabled = false; btn.textContent = "Proceed with manual review \u2192"; btn.classList.add("btn-warn");

      } else {
        track.insertAdjacentHTML("beforeend",
          '<div class="derive-warning"><div class="derive-warning-title">Derivation error</div>' +
          '<p style="font-family:var(--mono);font-size:12.5px;color:var(--alert-amber-deep);">' + escapeHtml(data.error || "Unknown error") + '</p>' +
          '<p style="margin:10px 0 0;font-size:13px;">You can still open the notebook to work with raw uploaded data.</p></div>');
        var btn = document.getElementById("btn-to-notebook");
        btn.disabled = false; btn.textContent = "Proceed with manual review \u2192"; btn.classList.add("btn-warn");
      }
    }, stepKeys.length * 650 + 200);
  }

  document.getElementById("btn-back-2").addEventListener("click", function () { goTo("screen-2", 2); });
  document.getElementById("btn-back-3").addEventListener("click", function () { goTo("screen-3", 3); });
  // Context-specific templates for each data type
  var NOTEBOOK_TEMPLATES = {
    clinical_trial: [
      { label: "demographics by arm",
        code: "adsl.groupby('ARMCD').agg(n=('USUBJID','count'), mean_age=('AGE','mean'), pct_female=('SEX', lambda x: (x=='F').mean()*100)).round(1)" },
      { label: "KM curve by arm",
        code: "import matplotlib.pyplot as plt\nfig, ax = plt.subplots(figsize=(7,5))\nfor arm in adtte['ARMCD'].unique():\n    sub = adtte[adtte['ARMCD']==arm].copy()\n    sub['AVAL'] = sub['AVAL'].astype(float)\n    sub['CNSR'] = sub['CNSR'].astype(int)\n    kmf = KaplanMeierFitter()\n    kmf.fit(sub['AVAL'], event_observed=(sub['CNSR']==0), label=arm)\n    kmf.plot_survival_function(ax=ax)\nplt.title('Overall survival by arm')\nplt.xlabel('Days')" },
      { label: "Cox PH hazard ratio",
        code: "adtte2 = adtte.copy()\nadtte2['AVAL'] = adtte2['AVAL'].astype(float)\nadtte2['CNSR'] = adtte2['CNSR'].astype(int)\nadtte2['ARM_BIN'] = (adtte2['ARMCD']=='DARA').astype(int)\ncox = CoxPHFitter()\ncox.fit(adtte2[['AVAL','CNSR','ARM_BIN']], duration_col='AVAL', event_col='CNSR')\ncox.print_summary()" },
      { label: "grade 3+ AEs",
        code: "teae = adae[(adae['TRTEMFL']=='Y') & (adae['AETOXGR'].astype(float)>=3)]\nteae.groupby(['AEBODSYS','AEDECOD'])['USUBJID'].nunique().sort_values(ascending=False).head(20)" },
      { label: "AE rate by arm",
        code: "n_subj = adsl.groupby('ARMCD')['USUBJID'].nunique()\nae_subj = adae[adae['TRTEMFL']=='Y'].groupby('ARMCD')['USUBJID'].nunique()\n(ae_subj / n_subj * 100).round(1).rename('TEAE %').reset_index()" },
      { label: "exposure duration",
        code: "import matplotlib.pyplot as plt\nadsl['TRTDURD'] = adsl['TRTDURD'].astype(float)\nfig, ax = plt.subplots(figsize=(7,4))\nfor arm, grp in adsl.groupby('ARMCD'):\n    ax.hist(grp['TRTDURD'].dropna(), bins=20, alpha=0.6, label=arm)\nax.set_xlabel('Treatment duration (days)')\nax.set_ylabel('Subjects')\nax.legend()\nplt.title('Exposure duration by arm')" },
      { label: "response waterfall",
        code: "import matplotlib.pyplot as plt\nif 'RS' in dir() or 'rs' in dir():\n    pass\nrs2 = adsl[['USUBJID','ARMCD']].copy()\nfig, ax = plt.subplots(figsize=(9,4))\ncolors = {'CR':'#2a7a3b','PR':'#6cc87c','SD':'#f0c040','PD':'#d64a3b'}\nif 'RSORRES' in adsl.columns:\n    rs2['RESP'] = adsl['RSORRES']\n    rs2 = rs2.sort_values('RESP')\n    ax.bar(range(len(rs2)), rs2['RESP'].map({'CR':100,'PR':50,'SD':5,'PD':-30}).fillna(0),\n           color=rs2['RESP'].map(colors).fillna('#aaa'))\n    ax.axhline(0, color='black', linewidth=0.8)\n    ax.set_ylabel('Best response (%)')\n    ax.set_xlabel('Subjects')\n    plt.title('Waterfall plot')\nelse:\n    print('RSORRES not in adsl \u2014 check adrs or rs variable')" },
      { label: "logrank test",
        code: "arm_a = adtte[adtte['ARMCD']==adtte['ARMCD'].unique()[0]]\narm_b = adtte[adtte['ARMCD']==adtte['ARMCD'].unique()[1]]\nresult = logrank_test(arm_a['AVAL'].astype(float), arm_b['AVAL'].astype(float),\n                      event_observed_A=(arm_a['CNSR'].astype(int)==0),\n                      event_observed_B=(arm_b['CNSR'].astype(int)==0))\nresult.print_summary()" },
    ],
    plate_assay: [
      { label: "dose-response table",
        code: "dose_response" },
      { label: "viability curves",
        code: "import matplotlib.pyplot as plt\nfig, ax = plt.subplots(figsize=(7,5))\nfor cl, grp in plate_assay[plate_assay['CONTROL_TYPE']=='treated'].groupby('CELL_LINE'):\n    mn = grp.groupby('CONCENTRATION')['VIABILITY_PCT'].mean()\n    ax.plot(mn.index, mn.values, marker='o', label=cl)\nax.set_xscale('log')\nax.axhline(50, color='grey', linestyle='--', linewidth=0.8)\nax.set_xlabel('Concentration (\u00b5M)')\nax.set_ylabel('Viability (%)')\nax.legend()\nplt.title('Dose-response by cell line')" },
      { label: "plate QC summary",
        code: "plate_qc" },
      { label: "control well values",
        code: "plate_assay[plate_assay['CONTROL_TYPE'].isin(['blank','dmso'])].groupby(['CELL_LINE','CONTROL_TYPE'])['SIGNAL'].agg(['mean','std','count']).round(1)" },
    ],
    lab_assay: [
      { label: "lab summary",
        code: "lb_summary" },
      { label: "abnormal flags",
        code: "lb_flags[lb_flags['HIGH_FLAG'] | lb_flags['LOW_FLAG']].sort_values('PARAM')" },
      { label: "shift table",
        code: "lb_shifts.pivot_table(index='PARAM', columns='SHIFT', values='n', fill_value=0)" },
    ],
    generic: [
      { label: "profile",
        code: "profile" },
      { label: "numeric summary",
        code: "numeric_summary" },
    ],
  };

  var NOTEBOOK_SUGGESTIONS = {
    clinical_trial: [
      "Show overall survival by treatment arm as a KM curve",
      "What is the hazard ratio for OS comparing the two arms?",
      "Which adverse events occur more than 10% more often in the treatment arm?",
      "Show me grade 3 or higher AEs by system organ class",
      "What is the median treatment duration in each arm?",
      "Break down demographics by arm \u2014 age, sex, ECOG score",
      "How many subjects discontinued and what were the reasons?",
      "Show response rates by KRAS mutation status",
    ],
    plate_assay: [
      "What is the IC50 for each cell line?",
      "Plot dose-response curves on a log scale for both cell lines",
      "Which cell line is more sensitive to the compound?",
      "Show me the DMSO control signal distribution across the plate",
      "Are there any edge effects visible in the plate layout?",
      "Summarise viability at the highest and lowest tested doses",
    ],
    lab_assay: [
      "Which lab parameters have the highest abnormality rate?",
      "Show me the shift table \u2014 how many values moved from normal to high?",
      "Plot the distribution of ALT values across visits",
      "Flag any subjects with grade 3 or higher lab abnormalities",
    ],
    generic: [
      "Show me a summary of the numeric columns",
      "Which columns have missing values and how many?",
      "Plot a histogram of the most variable numeric column",
      "Show the top 10 rows sorted by the first numeric column descending",
    ],
  };

  var NOTEBOOK_PLACEHOLDERS = {
    clinical_trial: "e.g. \u201cshow OS by KRAS subtype\u201d or \u201cfit a Cox model for PFS\u201d",
    plate_assay: "e.g. \u201cIC50 for each cell line\u201d or \u201cplot viability curves on log scale\u201d",
    lab_assay: "e.g. \u201cshow abnormal lab values by visit\u201d",
    generic: "e.g. \u201cshow me a histogram of the numeric columns\u201d",
  };

  var NOTEBOOK_HINTS = {
    clinical_trial: "Describe the analysis in plain English. The system understands your SDTM structure \u2014 which columns map to which clinical variables and at what confidence \u2014 and writes code against the derived ADaM datasets.",
    plate_assay: "Describe the analysis in plain English. The system knows your plate layout: signal sheet, compound labels, WELLID join, and real concentration values parsed from treatment strings.",
    lab_assay: "Describe the analysis in plain English. The system writes code against your derived lab summary, flag, and shift tables.",
    generic: "Describe the analysis in plain English. The system works from column names and types \u2014 never raw data values.",
  };

  function renderNotebookTemplates(context) {
    var container = document.getElementById("cell-templates");
    if (!container) return;
    container.innerHTML = '<span class="template-label">templates:</span>';
    var chips = NOTEBOOK_TEMPLATES[context] || NOTEBOOK_TEMPLATES.generic;
    chips.forEach(function (t) {
      var btn = document.createElement("button");
      btn.className = "template-chip";
      btn.textContent = t.label;
      btn.addEventListener("click", function () { addCell(t.code); });
      container.appendChild(btn);
    });
  }

  function renderSuggestions(context) {
    var existing = document.getElementById("suggestions-panel");
    if (existing) existing.remove();

    var questions = NOTEBOOK_SUGGESTIONS[context] || NOTEBOOK_SUGGESTIONS.generic;
    if (!questions || !questions.length) return;

    var panel = document.createElement("div");
    panel.id = "suggestions-panel";
    panel.className = "suggestions-panel";
    panel.innerHTML = '<div class="suggestions-label">try asking</div>';

    questions.forEach(function (q) {
      var chip = document.createElement("button");
      chip.className = "suggestion-chip";
      chip.textContent = q;
      chip.addEventListener("click", function () {
        var inp = document.getElementById("generative-input");
        if (inp) {
          inp.value = q;
          inp.focus();
        }
      });
      panel.appendChild(chip);
    });

    var notebook = document.getElementById("notebook");
    var newCellRow = document.querySelector(".new-cell-row");
    if (newCellRow && newCellRow.parentNode) {
      newCellRow.parentNode.insertBefore(panel, newCellRow.nextSibling);
    } else if (notebook && notebook.parentNode) {
      notebook.parentNode.appendChild(panel);
    }
  }

  async function initNotebookScreen() {
    try {
      var res = await fetch(API + "/api/notebook/vars?session_id=" + sessionId);
      var data = await res.json();

      var vars = data.vars || [];
      var context = data.context || "generic";
      var contextLabel = data.context_label || "General tabular data";

      _notebookVars = vars;

      // Update var list
      var varEl = document.getElementById("var-list");
      if (varEl) varEl.textContent = vars.length ? vars.join(", ") : "none yet";

      // Update placeholder + hint based on context
      var inp = document.getElementById("generative-input");
      if (inp) inp.placeholder = NOTEBOOK_PLACEHOLDERS[context] || NOTEBOOK_PLACEHOLDERS.generic;
      var hint = document.getElementById("generative-hint");
      if (hint) hint.textContent = NOTEBOOK_HINTS[context] || NOTEBOOK_HINTS.generic;

      // Render context-appropriate template chips and suggestions
      renderNotebookTemplates(context);
      renderSuggestions(context);

      // Update rail meta
      setRailMeta(4, contextLabel + " \u00b7 " + vars.length + " vars");
    } catch (e) {
      // non-fatal; notebook still works without context refresh
    }

    // Fetch provenance metadata for notebook cell footers (non-fatal)
    try {
      var provRes = await fetch(API + "/api/session/" + encodeURIComponent(sessionId) + "/provenance");
      _provenanceMeta = await provRes.json();
    } catch (e) { /* non-fatal */ }
  }

  document.getElementById("btn-to-notebook").addEventListener("click", function () {
    goTo("screen-5", 5);

    if (window._missingDomains && window._missingDomains.length) {
      var existing = document.getElementById("partial-data-banner");
      if (!existing) {
        var banner = document.createElement("div");
        banner.id = "partial-data-banner";
        banner.className = "partial-data-banner";
        banner.innerHTML =
          '<strong>Partial derivation</strong> \u2014 missing domains: <code>' + window._missingDomains.join(", ") + '</code>. ' +
          'Some variables may be undefined or incomplete. Review and fill gaps manually below.';
        var desc = document.querySelector("#screen-5 .screen-desc");
        desc.parentNode.insertBefore(banner, desc.nextSibling);
      }
    }

    initNotebookScreen().then(function () {
      if (document.getElementById("notebook").children.length === 0) {
        var firstVar = (_derivePlan && _derivePlan.steps.length) ? _derivePlan.steps[0].key : "data";
        addCell(firstVar + ".head()");
      }
    });
  });

  // ------------- screen 5: notebook -------------
  document.getElementById("add-cell-btn").addEventListener("click", function () { addCell(""); });

  document.getElementById("export-btn").addEventListener("click", async function () {
    var btn = document.getElementById("export-btn");
    btn.disabled = true;
    btn.textContent = "Preparing…";
    try {
      var url = API + "/api/export?session_id=" + encodeURIComponent(sessionId);
      var res = await fetch(url);
      if (!res.ok) { btn.textContent = "Export failed"; setTimeout(function(){ btn.disabled=false; btn.textContent="Export dataset ↓"; }, 3000); return; }
      var blob = await res.blob();
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "probe_export_" + sessionId.slice(0,8) + ".xlsx";
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
    } catch (e) { /* non-fatal */ }
    btn.disabled = false;
    btn.textContent = "Export dataset ↓";
  });

  function addCell(initialCode) {
    cellCounter++;
    var notebook = document.getElementById("notebook");
    var cellId = "cell-" + cellCounter;

    var cellEl = document.createElement("div");
    cellEl.className = "nb-cell";
    cellEl.id = cellId;
    cellEl.innerHTML =
      '<div class="nb-cell-input">' +
        '<div class="nb-cell-marker">[' + cellCounter + ']</div>' +
        '<textarea class="nb-code-area" rows="3">' + escapeHtml(initialCode) + '</textarea>' +
      '</div>' +
      '<div class="nb-cell-controls">' +
        '<button class="btn-run">Run cell \u25b6</button>' +
        '<button class="btn-remove">Remove</button>' +
      '</div>' +
      '<div class="nb-cell-output empty"></div>';
    notebook.appendChild(cellEl);

    var textarea = cellEl.querySelector(".nb-code-area");
    autoGrow(textarea);
    textarea.addEventListener("input", function () { autoGrow(textarea); });
    textarea.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        runCellEl(cellEl);
      }
    });

    cellEl.querySelector(".btn-run").addEventListener("click", function () { runCellEl(cellEl); });
    cellEl.querySelector(".btn-remove").addEventListener("click", function () { cellEl.remove(); });

    if (typeof cellEl.scrollIntoView === "function") {
      cellEl.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    textarea.focus();
    return cellEl;
  }

  function _stripComments(code) {
    // Remove standalone comment lines (lines that are only a # comment, including blank comment lines).
    // Inline comments after real code are preserved so things like list comprehensions stay readable.
    return code
      .split("\n")
      .filter(function (line) { return !/^\s*#/.test(line); })
      .join("\n")
      .trim();
  }

  function showGenerativeError(message) {
    var row = document.querySelector(".generative-row");
    var existing = document.getElementById("generative-error");
    if (existing) existing.remove();
    var el = document.createElement("div");
    el.id = "generative-error";
    el.className = "generative-error";
    el.textContent = message;
    row.parentNode.insertBefore(el, row.nextSibling);
    setTimeout(function () { if (el.parentNode) el.remove(); }, 5000);
  }

  function _buildProvenanceFooter(cellEl, code, schemaCheck) {
    if (!_provenanceMeta) return;
    var dm = _provenanceMeta.derivation_meta || {};
    var origins = dm.variable_origins || {};
    var rawOrigins = _provenanceMeta.raw_origins || {};
    var heldForReview = _provenanceMeta.held_for_review || [];
    var lowConf = dm.low_confidence || [];
    var schemaVerified = (schemaCheck && schemaCheck.verified) || [];
    var schemaMissing  = (schemaCheck && schemaCheck.missing)  || [];

    var hasAnyContent = dm.recipe || Object.keys(origins).length || Object.keys(rawOrigins).length
                        || schemaVerified.length || schemaMissing.length;
    if (!hasAnyContent) return;

    // Detect which derived/raw variable names the code mentions
    var mentionedVars = [];
    _notebookVars.forEach(function (v) {
      var re = new RegExp("\\b" + v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b");
      if (re.test(code) && mentionedVars.indexOf(v) === -1) {
        mentionedVars.push(v);
      }
    });
    // Also check variable names implicit in origins keys (e.g. "plate_assay" from "plate_assay.SIGNAL")
    Object.keys(origins).forEach(function (key) {
      var v = key.split(".")[0];
      var re = new RegExp("\\b" + v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b");
      if (re.test(code) && mentionedVars.indexOf(v) === -1) {
        mentionedVars.push(v);
      }
    });

    // Relevant origin entries for mentioned vars
    var relevantOrigins = Object.keys(origins).filter(function (key) {
      return mentionedVars.indexOf(key.split(".")[0]) !== -1;
    });

    // Relevant flags
    var relevantLowConf = lowConf.filter(function (k) {
      return mentionedVars.indexOf(k.split(".")[0]) !== -1;
    });
    var relevantHeld = heldForReview.filter(function (h) {
      return mentionedVars.indexOf(h.var) !== -1 || code.indexOf(h.var) !== -1;
    });

    // Also look up raw extraction origins for mentioned vars that aren't in derivation origins
    var relevantRawOrigins = Object.keys(rawOrigins).filter(function (key) {
      return mentionedVars.indexOf(key.split(".")[0]) !== -1
          && !relevantOrigins.includes(key);
    });

    var hasAnything = relevantOrigins.length || relevantRawOrigins.length
                      || dm.recipe || schemaMissing.length || schemaVerified.length;
    if (!hasAnything) return;

    var footer = document.createElement("div");
    footer.className = "prov-footer";

    var hasFlags = relevantLowConf.length > 0 || relevantHeld.length > 0 || schemaMissing.length > 0;
    var toggleLabel = "↳ view provenance" + (hasFlags ? " ⚠" : "");
    var toggleBtn = document.createElement("button");
    toggleBtn.className = "prov-toggle";
    toggleBtn.textContent = toggleLabel;
    footer.appendChild(toggleBtn);

    var panels = document.createElement("div");
    panels.className = "prov-panels";
    panels.hidden = true;

    // Panel 1: Derivation origins (from derive_adam.PIPELINE_PROVENANCE or equivalent)
    if (relevantOrigins.length) {
      var origPanel = document.createElement("div");
      var origTitle = document.createElement("div");
      origTitle.className = "prov-panel-title";
      origTitle.textContent = "Derivation origins";
      origPanel.appendChild(origTitle);
      relevantOrigins.forEach(function (key) {
        var info = origins[key];
        var conf = Math.round(info.confidence * 100);
        var row = document.createElement("div");
        row.className = "prov-row";
        row.innerHTML =
          "<strong>" + escapeHtml(key) + "</strong>" +
          " ← <span class=\"prov-source\">" + escapeHtml(info.source) + "</span>" +
          " — " + escapeHtml(info.transform) +
          " <span class=\"prov-conf\">(" + conf + "%)</span>";
        origPanel.appendChild(row);
      });
      panels.appendChild(origPanel);
    }

    // Panel 2: Extraction origins (raw domain → source file mapping from sdtm_mapping.py)
    if (relevantRawOrigins.length) {
      var rawPanel = document.createElement("div");
      var rawTitle = document.createElement("div");
      rawTitle.className = "prov-panel-title";
      rawTitle.textContent = "Extraction origins";
      rawPanel.appendChild(rawTitle);
      relevantRawOrigins.forEach(function (key) {
        var info = rawOrigins[key];
        var conf = Math.round(info.confidence * 100);
        var row = document.createElement("div");
        row.className = "prov-row";
        row.innerHTML =
          "<strong>" + escapeHtml(key) + "</strong>" +
          " ← <span class=\"prov-source\">" + escapeHtml(info.source_file) + " col '" + escapeHtml(info.source_col) + "'</span>" +
          " <span class=\"prov-conf\">(" + conf + "% confidence, " + escapeHtml(info.action) + ")</span>";
        rawPanel.appendChild(row);
      });
      panels.appendChild(rawPanel);
    }

    // Panel 3: Schema verification — column references checked against actual loaded data
    if (schemaVerified.length || schemaMissing.length) {
      var schemaPanel = document.createElement("div");
      var schemaTitle = document.createElement("div");
      schemaTitle.className = "prov-panel-title" + (schemaMissing.length ? " prov-title-warn" : "");
      schemaTitle.textContent = "Schema verification";
      schemaPanel.appendChild(schemaTitle);

      schemaVerified.forEach(function (ref) {
        var row = document.createElement("div");
        row.className = "prov-row prov-row-ok";
        row.innerHTML = "<span class=\"prov-check\">✓</span> <strong>" + escapeHtml(ref) + "</strong> — column confirmed in session data";
        schemaPanel.appendChild(row);
      });
      schemaMissing.forEach(function (ref) {
        var row = document.createElement("div");
        row.className = "prov-row-warn";
        row.innerHTML = "<span class=\"prov-check-fail\">✗</span> <strong>" + escapeHtml(ref) + "</strong> — column NOT FOUND in session data";
        schemaPanel.appendChild(row);
      });
      panels.appendChild(schemaPanel);
    }

    // Panel 4: Flags (low confidence + held for review)
    if (hasFlags) {
      var flagPanel = document.createElement("div");
      var flagTitle = document.createElement("div");
      flagTitle.className = "prov-panel-title prov-title-warn";
      flagTitle.textContent = "Flags";
      flagPanel.appendChild(flagTitle);
      relevantLowConf.forEach(function (k) {
        var info = origins[k];
        var row = document.createElement("div");
        row.className = "prov-row-warn";
        row.textContent = k + " — confidence " + Math.round(info.confidence * 100) + "% (below threshold; verify normalisation)";
        flagPanel.appendChild(row);
      });
      relevantHeld.forEach(function (h) {
        var row = document.createElement("div");
        row.className = "prov-row-warn";
        row.textContent = h.var + "." + h.col + " ← source col '" + h.source_col + "' — held for review ("
          + Math.round(h.confidence * 100) + "% confidence; confirm mapping before analysis)";
        flagPanel.appendChild(row);
      });
      panels.appendChild(flagPanel);
    }

    // Panel 5: Derivation recipe
    if (dm.recipe) {
      var recipePanel = document.createElement("div");
      var recipeTitle = document.createElement("div");
      recipeTitle.className = "prov-panel-title";
      recipeTitle.textContent = "Derivation recipe";
      recipePanel.appendChild(recipeTitle);
      var recipeEl = document.createElement("div");
      recipeEl.className = "prov-recipe";
      recipeEl.innerHTML =
        "<strong>" + escapeHtml(dm.recipe) + "</strong> — " +
        escapeHtml(dm.fired_because || "");
      recipePanel.appendChild(recipeEl);
      panels.appendChild(recipePanel);
    }

    footer.appendChild(panels);

    toggleBtn.addEventListener("click", function () {
      panels.hidden = !panels.hidden;
      toggleBtn.classList.toggle("open", !panels.hidden);
      toggleBtn.textContent = panels.hidden ? toggleLabel : "↳ hide provenance";
    });

    cellEl.appendChild(footer);
  }

  function addGeneratedCell(requestText, code, result, schemaCheck) {
    cellCounter++;
    var notebook = document.getElementById("notebook");

    var cleanCode = _stripComments(code);

    var cellEl = document.createElement("div");
    cellEl.className = "nb-cell";
    cellEl.innerHTML =
      '<div class="nb-cell-input">' +
        '<div class="nb-cell-marker">[' + cellCounter + ']</div>' +
        '<textarea class="nb-code-area" rows="3">' + escapeHtml(cleanCode) + '</textarea>' +
      '</div>' +
      '<div class="nb-cell-controls">' +
        '<button class="btn-run">Run cell \u25b6</button>' +
        '<button class="btn-remove">Remove</button>' +
      '</div>' +
      '<div class="nb-cell-output empty"></div>';
    notebook.appendChild(cellEl);

    var textarea = cellEl.querySelector(".nb-code-area");
    autoGrow(textarea);
    textarea.addEventListener("input", function () { autoGrow(textarea); });
    cellEl.querySelector(".btn-run").addEventListener("click", function () { runCellEl(cellEl); });
    cellEl.querySelector(".btn-remove").addEventListener("click", function () { cellEl.remove(); });

    var outputEl = cellEl.querySelector(".nb-cell-output");
    renderCellOutput(outputEl, result);

    _buildProvenanceFooter(cellEl, cleanCode, schemaCheck);

    if (typeof cellEl.scrollIntoView === "function") {
      cellEl.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    return cellEl;
  }

  var generativeInput = document.getElementById("generative-input");
  var generativeBtn = document.getElementById("generative-submit-btn");

  generativeBtn.addEventListener("click", submitGenerative);
  generativeInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") submitGenerative();
  });

  async function submitGenerative() {
    var text = generativeInput.value.trim();
    if (!text) return;

    generativeBtn.disabled = true;
    generativeBtn.textContent = "Working\u2026";
    generativeInput.disabled = true;

    try {
      var res = await fetch(API + "/api/notebook/generate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, text: text }),
      });
      var cell = await res.json();

      var REASON_MESSAGES = {
        "cannot_write_files": "This notebook analyses existing data — it can't create files or generate new records.",
        "no_data":            "No derived data yet — complete the Derivation step first.",
        "api_key":            "API key not configured — set ANTHROPIC_API_KEY and restart the backend.",
        "general":            "Analysis unavailable — try rephrasing or narrowing your request.",
      };

      if (cell.status === "error") {
        showGenerativeError(REASON_MESSAGES[cell.reason] || REASON_MESSAGES["general"]);
      } else {
        var cleanCode = _stripComments(cell.code || "");
        if (!cleanCode) {
          showGenerativeError("That analysis isn't possible with this dataset — try a different question.");
        } else {
          addGeneratedCell(text, cell.code, cell.result, cell.schema_check);
        }
      }
    } catch (err) {
      showGenerativeError("Could not connect — make sure the backend is running and try again.");
    }

    generativeBtn.disabled = false;
    generativeBtn.textContent = "Analyse \u2192";
    generativeInput.disabled = false;
    generativeInput.value = "";
    generativeInput.focus();
  }



  function autoGrow(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.max(60, textarea.scrollHeight) + "px";
  }

  async function runCellEl(cellEl) {
    var textarea = cellEl.querySelector(".nb-code-area");
    var runBtn = cellEl.querySelector(".btn-run");
    var outputEl = cellEl.querySelector(".nb-cell-output");
    var code = textarea.value;

    runBtn.disabled = true;
    runBtn.textContent = "Running\u2026";
    outputEl.className = "nb-cell-output";
    outputEl.textContent = "";

    try {
      var res = await fetch(API + "/api/notebook/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, code: code }),
      });
      var cell = await res.json();
      renderCellOutput(outputEl, cell.result);
    } catch (err) {
      outputEl.className = "nb-cell-output error";
      outputEl.textContent = "Could not reach Probe backend at " + API + ".";
    }

    runBtn.disabled = false;
    runBtn.textContent = "Run cell \u25b6";
  }

  function renderCellOutput(outputEl, result) {
    if (!result || result.status === "error") {
      outputEl.className = "nb-cell-output error";
      outputEl.textContent = (result && result.error) ? result.error : "Execution failed.";
      return;
    }

    outputEl.className = "nb-cell-output";
    outputEl.innerHTML = "";

    if (result.stdout) {
      var stdoutEl = document.createElement("div");
      stdoutEl.className = "nb-stdout";
      stdoutEl.textContent = result.stdout;
      outputEl.appendChild(stdoutEl);
    }
    if (result.result_repr) {
      var reprEl = document.createElement("div");
      reprEl.className = "nb-result-repr";
      reprEl.textContent = result.result_repr;
      outputEl.appendChild(reprEl);
    }
    (result.figures || []).forEach(function (b64) {
      var img = document.createElement("img");
      img.src = "data:image/png;base64," + b64;
      outputEl.appendChild(img);
    });

    if (!result.stdout && !result.result_repr && (!result.figures || result.figures.length === 0)) {
      outputEl.className = "nb-cell-output empty";
    }
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }
})();
