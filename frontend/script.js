/* PII Redaction Tool — frontend
 *
 * Two states: a password gate, and the upload tool. The session token is
 * kept in sessionStorage (not localStorage) so it dies with the browser
 * tab -- appropriate for a tool that handles confidential documents on
 * what may be a shared machine.
 */

const MAX_FILE_SIZE = 10 * 1024 * 1024;
const SUPPORTED_EXT = [".docx", ".pdf", ".txt"];
const TOKEN_KEY = "pii_token";
/* A large filing takes ~30s. Five minutes is far beyond any legitimate
   run, so past that the request is treated as lost rather than leaving
   the user watching a spinner with no idea whether it is still alive. */
const REQUEST_TIMEOUT_MS = 5 * 60 * 1000;

const TYPE_LABELS = {
  PERSON: "names", EMAIL: "emails", PHONE: "phones", ORG: "companies",
  ADDRESS: "addresses", SSN: "SSNs", CREDIT_CARD: "cards",
  DATE_OF_BIRTH: "birth dates", IP_ADDRESS: "IPs",
  PAN_NUMBER: "PAN", AADHAAR_NUMBER: "Aadhaar",
  IFSC_CODE: "IFSC codes", GSTIN: "GSTINs", UPI_ID: "UPI IDs",
  PASSPORT_NUMBER: "passports", VOTER_ID: "voter IDs",
  DRIVING_LICENSE: "driving licences", BANK_ACCOUNT_NUMBER: "bank accounts",
};

const $ = (id) => document.getElementById(id);
const gate = $("gate");
const app = $("app");
let objectUrl = null;

// ---------------------------------------------------------------- auth

const getToken = () => sessionStorage.getItem(TOKEN_KEY) || "";
const setToken = (t) => sessionStorage.setItem(TOKEN_KEY, t);
const clearToken = () => sessionStorage.removeItem(TOKEN_KEY);

function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function showGate() {
  gate.hidden = false;
  app.hidden = true;
  $("passwordInput").focus();
}

function showApp(authRequired) {
  gate.hidden = true;
  app.hidden = false;
  $("logoutBtn").hidden = !authRequired;
}

async function init() {
  let authRequired = false;
  try {
    const res = await fetch("/api/auth/status");
    authRequired = (await res.json()).auth_required;
  } catch {
    // Status endpoint unreachable: fall through to the tool and let the
    // upload request surface the real error rather than blocking here.
  }
  if (authRequired && !getToken()) showGate();
  else showApp(authRequired);
}

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("loginBtn");
  const err = $("gateError");
  err.hidden = true;
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("passwordInput").value }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || "Incorrect password.");
    }
    setToken((await res.json()).token);
    $("passwordInput").value = "";
    showApp(true);
  } catch (e2) {
    err.textContent = e2.message;
    err.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Unlock";
  }
});

$("logoutBtn").addEventListener("click", () => {
  clearToken();
  showGate();
});

// ---------------------------------------------------------------- file input

function setFile(file) {
  if (!file) return;
  const ext = "." + file.name.split(".").pop().toLowerCase();
  if (!SUPPORTED_EXT.includes(ext)) {
    return showError(`Unsupported file type "${ext}". Upload a .docx, .pdf or .txt.`);
  }
  if (file.size > MAX_FILE_SIZE) {
    return showError(`File is ${(file.size / 1_000_000).toFixed(1)} MB — the limit is 10 MB.`);
  }
  const dt = new DataTransfer();
  dt.items.add(file);
  $("fileInput").files = dt.files;

  $("dropInner").hidden = true;
  $("fileChosen").hidden = false;
  $("fileChosenName").textContent = file.name;
  $("submitBtn").disabled = false;
  $("status").hidden = true;
}

$("fileInput").addEventListener("change", (e) => setFile(e.target.files[0]));

$("fileClear").addEventListener("click", (e) => {
  e.preventDefault();
  resetForm();
});

function resetForm() {
  $("fileInput").value = "";
  $("dropInner").hidden = false;
  $("fileChosen").hidden = true;
  $("submitBtn").disabled = true;
  $("status").hidden = true;
}

const dropZone = $("dropZone");
["dragenter", "dragover"].forEach((evt) =>
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((evt) =>
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
  })
);
dropZone.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));

// ---------------------------------------------------------------- status

function showError(message) {
  $("status").hidden = false;
  $("working").hidden = true;
  $("result").hidden = true;
  $("error").hidden = false;
  $("errorText").textContent = message;
}

/* Redaction gives no incremental progress signal, so rather than fake a
 * percentage the label just reflects how long it has been running. */
function startWorkingTimer() {
  const started = Date.now();
  return setInterval(() => {
    const secs = Math.round((Date.now() - started) / 1000);
    $("workingText").textContent = `Analysing document… ${secs}s`;
  }, 1000);
}

$("againBtn").addEventListener("click", resetForm);

// ---------------------------------------------------------------- results panels

const CONFIDENCE_ORDER = ["high", "medium", "needs_review"];
const CONFIDENCE_LABEL = { high: "High", medium: "Medium", needs_review: "Needs review" };

function renderConfidence(confidence) {
  const panel = $("confidencePanel");
  if (!confidence) {
    panel.hidden = true;
    return;
  }
  const total = CONFIDENCE_ORDER.reduce((sum, k) => sum + (confidence[k] || 0), 0);
  if (total === 0) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  const bar = $("confidenceBar");
  bar.innerHTML = "";
  CONFIDENCE_ORDER.forEach((key) => {
    const count = confidence[key] || 0;
    if (count === 0) return;
    const seg = document.createElement("span");
    seg.className = `seg-${key}`;
    seg.style.width = `${(count / total) * 100}%`;
    bar.appendChild(seg);
  });

  const legend = $("confidenceLegend");
  legend.innerHTML = "";
  CONFIDENCE_ORDER.forEach((key) => {
    const count = confidence[key] || 0;
    if (count === 0) return;
    const item = document.createElement("span");
    item.className = "item";
    item.innerHTML = `<span class="dot seg-${key}"></span>${CONFIDENCE_LABEL[key]} <span class="count">${count}</span>`;
    legend.appendChild(item);
  });
}

function renderResidualScan(residual) {
  const panel = $("residualPanel");
  if (!residual) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  const leaksRow = $("residualLeaks");
  leaksRow.className = "residual-row" + (residual.leaked_original_count > 0 ? " has-issue" : "");
  leaksRow.innerHTML = residual.leaked_original_count > 0
    ? `<span class="dot"></span>${residual.leaked_original_count} original value(s) still found in the output — review before sharing`
    : `<span class="dot"></span>No original values found in the output — everything the tool redacted is confirmed gone`;

  const unexpectedRow = $("residualUnexpected");
  const n = residual.unexpected_match_count || 0;
  unexpectedRow.className = "residual-row" + (n > 0 ? " has-note" : "");
  unexpectedRow.innerHTML = n > 0
    ? `<span class="dot"></span>${n} more PII-shaped item(s) found on a second pass — worth a skim, some may be false positives`
    : `<span class="dot"></span>Second pass found nothing further`;
}

function renderWarnings(warnings) {
  const list = $("resultWarnings");
  list.innerHTML = "";
  if (!warnings || warnings.length === 0) {
    list.hidden = true;
    return;
  }
  list.hidden = false;
  warnings.forEach((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    list.appendChild(li);
  });
}

// ---------------------------------------------------------------- upload

$("uploadForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = $("fileInput").files[0];
  if (!file) return;

  $("status").hidden = false;
  $("working").hidden = false;
  $("result").hidden = true;
  $("error").hidden = true;
  $("submitBtn").disabled = true;
  $("workingText").textContent = "Analysing document… 0s";
  const timer = startWorkingTimer();

  const disabled = Array.from(document.querySelectorAll(".types input"))
    .filter((cb) => !cb.checked)
    .map((cb) => cb.value)
    .join(",");

  const body = new FormData();
  body.append("file", file);
  body.append("issuer_names", $("issuerNames").value);
  body.append("redact_issuer", $("redactIssuer").checked ? "true" : "false");
  body.append("verify", $("verifyOutput").checked ? "true" : "false");
  body.append("disable_types", disabled);

  const controller = new AbortController();
  const abortTimer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const res = await fetch("/api/redact", {
      method: "POST",
      body,
      headers: authHeaders(),
      signal: controller.signal,
    });

    if (res.status === 401) {
      clearToken();
      showGate();
      return;
    }
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      throw new Error(errBody.detail || `Request failed (${res.status})`);
    }

    const header = res.headers.get("X-Redaction-Summary");
    const summary = header ? JSON.parse(header) : { total_redacted: 0, by_type: {} };
    const blob = await res.blob();

    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = URL.createObjectURL(blob);

    const disposition = res.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="?([^"]+)"?/);
    $("downloadBtn").href = objectUrl;
    $("downloadBtn").download = match ? match[1] : "redacted.docx";

    const n = summary.total_redacted;
    $("resultCount").textContent = n > 0
      ? `${n} PII instance${n === 1 ? "" : "s"} redacted`
      : "No PII detected";

    const list = $("breakdown");
    list.innerHTML = "";
    Object.entries(summary.by_type || {})
      .sort((a, b) => b[1] - a[1])
      .forEach(([type, count]) => {
        const li = document.createElement("li");
        li.innerHTML = `<b>${count}</b> ${TYPE_LABELS[type] || type}`;
        list.appendChild(li);
      });

    renderConfidence(summary.confidence);
    renderResidualScan(summary.residual_scan);
    renderWarnings(summary.warnings);

    $("working").hidden = true;
    $("result").hidden = false;
  } catch (err) {
    showError(
      err.name === "AbortError"
        ? "That took longer than five minutes, so the request was stopped. Try a smaller file, or check that the server is still running."
        : err.message || "Something went wrong while processing the file."
    );
  } finally {
    clearTimeout(abortTimer);
    clearInterval(timer);
    $("submitBtn").disabled = false;
  }
});

init();
