(() => {
  "use strict";

  const BATCH_SIZE = 10;
  const POLL_MS = 1500;
  const VISIBLE_INVOICE_ROWS = 10;
  const VISIBLE_DOCUMENT_ROWS = 10;
  const MAX_PROBLEM_ROWS = 500;
  const strings = JSON.parse(document.getElementById("i18n-data").textContent);
  const t = (key, params = {}) =>
    (strings[key] ?? key).replace(/\{(\w+)\}/g, (_, name) => params[name] ?? "");

  const dropzone = document.getElementById("dropzone");
  const input = document.getElementById("file-input");
  const pickButton = document.getElementById("pick-button");
  const stopButton = document.getElementById("stop-button");
  const uploadStatus = document.getElementById("upload-status");
  const uploadWarning = document.getElementById("upload-warning");
  const docsSummary = document.getElementById("docs-summary");
  const docsTable = document.getElementById("docs-table");
  const docsBody = docsTable.querySelector("tbody");
  const docsWrap = document.getElementById("docs-wrap");
  const invSummary = document.getElementById("inv-summary");
  const invNotice = document.getElementById("inv-notice");
  const deleteButton = document.getElementById("delete-button");
  const invTable = document.getElementById("inv-table");
  const invWrap = document.getElementById("inv-wrap");
  const invBody = invTable.querySelector("tbody");
  const locale = document.documentElement.lang || undefined;
  const chartTitle = document.getElementById("chart-title");
  const chartWrap = document.getElementById("chart-wrap");
  const chartEmpty = document.getElementById("chart-empty");
  const chartCanvas = document.getElementById("cashflow-chart");
  const partnersTitle = document.getElementById("partners-title");
  const partnersEmpty = document.getElementById("partners-empty");
  const partnersTable = document.getElementById("partners-table");
  const partnersBody = partnersTable.querySelector("tbody");
  const partnersHead = partnersTable.querySelector("thead");
  const amountHeader = document.getElementById("amount-header");

  const ACTIVE = new Set(["pending", "processing"]);
  let pollTimer = null;
  let uploading = false;
  let stopRequested = false;
  let deleting = false;
  let processingActive = false;
  let invoiceCount = 0;

  // Törölni csak akkor lehet, ha van mit, és nincs folyamatban feltöltés vagy feldolgozás.
  function updateDeleteButton() {
    deleteButton.disabled = deleting || uploading || processingActive || invoiceCount === 0;
  }

  function showNotice(text) {
    invNotice.textContent = text;
    invNotice.hidden = !text;
  }
  // A szerver UTC-ben adja az időt; a felhasználó helyi idejében mutatjuk.
  const formatTime = (iso) =>
    iso ? new Date(iso).toLocaleString(locale, { dateStyle: "short", timeStyle: "medium" }) : "";

  const errorMessage = (error) => {
    if (!error) return "";
    const base = strings[`error.${error.code}`] ?? error.message;
    const fields = (error.fields ?? []).map((name) => strings[`ui.field.${name}`] ?? name);
    // Az LLM-hibáknál a szolgáltató eredeti üzenete is látszik, hogy a hiba oka azonosítható legyen.
    const detail = /^(llm_|duplicate_invoice$)/.test(error.code) && error.message ? ` – ${error.message}` : "";
    return (fields.length ? `${base}: ${fields.join(", ")}` : base) + detail;
  };

  let lastDocuments = { documents: [], counts: { pending: 0, processing: 0, failed: 0 } };
  let lastReport = null;

  function renderDocuments(data) {
    lastDocuments = data;
    renderProblems();
    processingActive = data.counts.pending + data.counts.processing > 0;
    updateDeleteButton();
    return processingActive;
  }

  // A hibás fájlok: a sikertelen feldolgozások, valamint a kinyert, de az összesítésből (részben) kimaradó
  // számlák (pl. nem elérhető MNB-árfolyam) – mindegyik az okával. A folyamatban lévők nem jelennek meg.
  function renderProblems() {
    const { documents, counts } = lastDocuments;
    const excluded = (lastReport?.invoices ?? []).filter((item) => item.problem);
    const rows = documents
      .filter((doc) => doc.status === "failed")
      .map((doc) => ({
        name: doc.filename,
        status: t(`status.${doc.status}`),
        statusClass: `status-${doc.status}`,
        uploaded: formatTime(doc.uploaded_at),
        reason: errorMessage(doc.error),
      }));
    excluded.forEach((item) =>
      rows.push({
        name: [item.invoice_number, item.partner?.name].filter(Boolean).join(" – ") || t("ui.inv.missing"),
        status: t("ui.docs.excluded"),
        statusClass: "status-excluded",
        uploaded: "",
        reason: strings[`ui.problem.${item.problem}`] ?? item.problem,
      }),
    );
    const hidden = Math.max(0, rows.length - MAX_PROBLEM_ROWS);
    rows.length = Math.min(rows.length, MAX_PROBLEM_ROWS);
    docsTable.hidden = rows.length === 0;
    docsBody.replaceChildren(
      ...rows.map((entry) => {
        const row = document.createElement("tr");
        [entry.name, entry.status, entry.uploaded, entry.reason].forEach((value, index) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          if (index === 1) cell.className = `status ${entry.statusClass}`;
          row.appendChild(cell);
        });
        return row;
      }),
    );
    docsSummary.textContent = rows.length
      ? [
          t("ui.docs.summary", {
            failed: counts.failed,
            excluded: excluded.length,
          }),
          hidden ? t("ui.docs.limited", { limit: MAX_PROBLEM_ROWS }) : "",
        ]
          .filter(Boolean)
          .join(" ")
      : t("ui.docs.empty");
    limitHeight(docsWrap, docsBody, VISIBLE_DOCUMENT_ROWS);
  }

  // Az összegek szövegként érkeznek (pl. "1234.50"); a pontosságot megtartva csoportosítjuk.
  function formatAmount(value) {
    if (value === null || value === undefined) return null;
    const [whole, fraction] = value.split(".");
    const negative = whole.startsWith("-");
    const grouped = BigInt(whole.replace("-", "")).toLocaleString(locale);
    const decimal = (1.1).toLocaleString(locale).charAt(1);
    return `${negative ? "-" : ""}${grouped}${fraction ? decimal + fraction : ""}`;
  }

  function issueText(issue) {
    const field = issue.field ? strings[`ui.field.${issue.field}`] ?? issue.field : "";
    return (strings[`ui.issue.${issue.code}`] ?? issue.code).replace("{field}", field);
  }

  function cell(row, content, className = "") {
    const td = document.createElement("td");
    if (className) td.className = className;
    if (content instanceof Node) {
      td.appendChild(content);
    } else if (content === null || content === undefined || content === "") {
      td.textContent = t("ui.inv.missing");
      td.classList.add("missing");
    } else {
      td.textContent = content;
    }
    row.appendChild(td);
  }

  function party(entry) {
    if (!entry.name && !entry.tax_id) return null;
    const box = document.createElement("span");
    box.textContent = entry.name ?? t("ui.inv.missing");
    if (entry.tax_id) {
      const sub = document.createElement("small");
      sub.className = "sub";
      sub.textContent = entry.tax_id;
      box.appendChild(sub);
    }
    return box;
  }

  function statusCell(invoice) {
    const box = document.createElement("span");
    box.className = `inv-${invoice.status}`;
    box.textContent = t(`ui.inv.status.${invoice.status}`);
    if (invoice.issues.length) {
      const list = document.createElement("ul");
      list.className = "notes";
      invoice.issues.forEach((issue) => {
        const item = document.createElement("li");
        item.textContent = issueText(issue);
        list.appendChild(item);
      });
      box.appendChild(list);
    }
    return box;
  }

  function renderInvoices(data, report) {
    const { invoices, counts } = data;
    invoiceCount = invoices.length;
    updateDeleteButton();
    const converted = new Map((report?.invoices ?? []).map((item) => [item.id, item]));
    invTable.hidden = invoices.length === 0;
    invBody.replaceChildren(
      ...invoices.map((invoice) => {
        const row = document.createElement("tr");
        cell(row, invoice.invoice_number);
        cell(row, party(invoice.seller));
        cell(row, party(invoice.buyer));
        cell(row, invoice.issue_date);
        cell(row, invoice.performance_date);
        cell(row, invoice.due_date);
        cell(row, invoice.currency);
        cell(row, formatAmount(invoice.net_total), "num");
        cell(row, formatAmount(invoice.vat_total), "num");
        cell(row, formatAmount(invoice.gross_total), "num");
        const money = converted.get(invoice.id);
        cell(row, rateText(money));
        cell(row, money?.rate_date ?? null);
        cell(row, amountCell(money), "num");
        cell(row, invoice.direction ? t(`ui.inv.direction.${invoice.direction}`) : t("ui.inv.direction.unknown"));
        cell(row, party(invoice.partner));
        cell(row, statusCell(invoice));
        return row;
      }),
    );
    limitHeight(invWrap, invBody, VISIBLE_INVOICE_ROWS);
    invSummary.textContent = invoices.length
      ? t("ui.inv.summary", {
          total: invoices.length,
          ok: counts.ok,
          review: counts.needs_review,
        })
      : t("ui.inv.empty");
  }

  // A sorok magassága változó (megjegyzések, adószám), ezért az utolsó látható sor aljáig mért magasságra korlátozunk;
  // alacsony ablakban a képernyő 85%-a a határ, hogy a vízszintes görgetősáv is elérhető maradjon.
  function limitHeight(wrap, body, maxRows) {
    wrap.style.maxHeight = "";
    const rows = body.rows;
    if (rows.length <= maxRows) return;
    const last = rows[maxRows - 1].getBoundingClientRect().bottom;
    const height = last - wrap.getBoundingClientRect().top + wrap.scrollTop;
    const limit = Math.min(Math.ceil(height) + 1, Math.round(window.innerHeight * 0.85));
    wrap.style.maxHeight = `${limit}px`;
  }

  // A forint árfolyama egységenként érkezik (pl. JPY: 100 egység ára).
  function rateText(money) {
    if (!money?.rate) return null;
    const unit = money.rate_unit > 1 ? ` / ${money.rate_unit} ${money.currency}` : "";
    return `${formatAmount(money.rate)}${unit}`;
  }

  function amountCell(money) {
    if (!money) return null;
    const box = document.createElement("span");
    box.textContent = money.amount === null ? t("ui.inv.missing") : formatAmount(String(money.amount));
    if (money.problem) {
      const note = document.createElement("small");
      note.className = "problem";
      note.textContent = strings[`ui.problem.${money.problem}`] ?? money.problem;
      box.appendChild(note);
    }
    return box;
  }

  // Oszlopok a partnercégek, sorok az Össz bejövő és az Össz kimenő; az utolsó oszlop az összes partner összege.
  function renderPartners(report) {
    const { partners } = report;
    partnersTitle.textContent = t("ui.report.partners_title", { currency: report.currency });
    amountHeader.textContent = t("ui.inv.col.amount", { currency: report.currency });
    partnersTable.hidden = partners.length === 0;
    partnersEmpty.hidden = partners.length > 0;

    const head = document.createElement("tr");
    head.appendChild(document.createElement("th"));
    partners.forEach((partner) => {
      const th = document.createElement("th");
      th.scope = "col";
      th.className = "num";
      th.appendChild(party({ name: partner.name, tax_id: partner.tax_id }) ?? document.createTextNode(t("ui.inv.missing")));
      head.appendChild(th);
    });
    const totalHead = document.createElement("th");
    totalHead.scope = "col";
    totalHead.className = "num total";
    totalHead.textContent = t("ui.report.total");
    head.appendChild(totalHead);
    partnersHead.replaceChildren(head);

    const line = (label, key, totalKey) => {
      const row = document.createElement("tr");
      const th = document.createElement("th");
      th.scope = "row";
      th.textContent = label;
      row.appendChild(th);
      partners.forEach((partner) => cell(row, formatAmount(String(partner[key])), "num"));
      cell(row, formatAmount(String(report[totalKey])), "num total");
      return row;
    };
    partnersBody.replaceChildren(
      line(t("ui.report.col.incoming"), "incoming", "total_incoming"),
      line(t("ui.report.col.outgoing"), "outgoing", "total_outgoing"),
    );
  }

  let chart = null;

  function renderChart(report) {
    const { months } = report;
    chartTitle.textContent = t("ui.report.chart_title", { currency: report.currency });
    chartWrap.hidden = months.length === 0;
    chartEmpty.hidden = months.length > 0;
    if (!months.length || typeof Chart === "undefined") return;
    const labels = months.map((entry) => entry.month);
    const number = (value) => value.toLocaleString(locale);
    const datasets = [
      { label: t("ui.report.incoming"), data: months.map((entry) => entry.incoming), borderColor: "#067647", backgroundColor: "#067647" },
      { label: t("ui.report.outgoing"), data: months.map((entry) => entry.outgoing), borderColor: "#b42318", backgroundColor: "#b42318" },
    ].map((dataset) => ({ ...dataset, tension: 0.2, pointRadius: 4 }));
    if (chart) {
      chart.data.labels = labels;
      chart.data.datasets = datasets;
      chart.update();
      return;
    }
    chart = new Chart(chartCanvas, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { title: { display: true, text: t("ui.report.axis_month") } },
          y: {
            beginAtZero: true,
            title: { display: true, text: report.currency },
            ticks: { callback: (value) => number(value) },
          },
        },
        plugins: {
          legend: { position: "bottom" },
          tooltip: { enabled: false },
        },
      },
    });
  }

  async function refreshInvoices() {
    try {
      const [invoicesResponse, reportResponse] = await Promise.all([fetch("/api/invoices"), fetch("/api/report")]);
      const report = reportResponse.ok ? await reportResponse.json() : null;
      if (report) {
        lastReport = report;
        renderProblems();
      }
      if (invoicesResponse.ok) renderInvoices(await invoicesResponse.json(), report);
      if (report) {
        renderChart(report);
        renderPartners(report);
      }
    } catch (error) {
      // A következő frissítés újra próbálkozik.
    }
  }

  async function refresh() {
    clearTimeout(pollTimer);
    let active = false;
    try {
      const response = await fetch("/api/documents");
      if (!response.ok) throw new Error(String(response.status));
      active = renderDocuments(await response.json());
    } catch (error) {
      docsSummary.textContent = t("ui.docs.refresh_failed");
      active = true;
    }
    await refreshInvoices();
    if (active || uploading) pollTimer = setTimeout(refresh, POLL_MS);
  }

  async function sendBatch(files, uploadId) {
    const body = new FormData();
    body.append("upload_id", uploadId);
    files.forEach((file) => body.append("files", file, file.name));
    const response = await fetch("/api/documents", { method: "POST", body });
    if (!response.ok) throw new Error(String(response.status));
    return (await response.json()).results;
  }

  // A még sorban álló fájlok megszakítása; ha a hívás nem sikerül, azok feldolgozódnak.
  async function cancelPending(uploadId) {
    try {
      const response = await fetch(`/api/documents/uploads/${encodeURIComponent(uploadId)}/cancel`, { method: "POST" });
      return response.ok ? (await response.json()).cancelled : 0;
    } catch (error) {
      return 0;
    }
  }

  // Új feltöltésnél a korábbi hibás fájlok listája kiürül; ha a hívás nem sikerül, a lista megmarad.
  async function clearFailed() {
    try {
      await fetch("/api/documents/failed", { method: "DELETE" });
    } catch (error) {
      // a feltöltés ettől még indulhat
    }
  }

  const newUploadId = () =>
    crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

  async function deleteInvoices() {
    if (deleteButton.disabled || !window.confirm(t("ui.inv.delete_confirm", { total: invoiceCount }))) return;
    deleting = true;
    updateDeleteButton();
    showNotice("");
    try {
      const response = await fetch("/api/invoices", { method: "DELETE" });
      if (response.ok) {
        showNotice(t("ui.inv.deleted", { deleted: (await response.json()).deleted }));
      } else {
        const code = (await response.json().catch(() => null))?.error?.code;
        showNotice(strings[`error.${code}`] ?? t("ui.inv.delete_failed"));
      }
    } catch (error) {
      showNotice(t("ui.inv.delete_failed"));
    }
    deleting = false;
    await refresh();
    updateDeleteButton();
  }

  async function upload(fileList) {
    const files = Array.from(fileList);
    if (!files.length || uploading) return;
    uploading = true;
    stopRequested = false;
    pickButton.disabled = true;
    updateDeleteButton();
    showNotice("");
    stopButton.disabled = false;
    stopButton.hidden = false;
    uploadWarning.hidden = false;
    const uploadId = newUploadId();
    const tally = { accepted: 0, duplicate: 0, rejected: 0 };
    let done = 0;
    let failed = false;
    await clearFailed();
    refresh();

    for (let start = 0; start < files.length && !stopRequested; start += BATCH_SIZE) {
      const batch = files.slice(start, start + BATCH_SIZE);
      uploadStatus.textContent = t("ui.upload.progress", { done, total: files.length });
      try {
        for (const result of await sendBatch(batch, uploadId)) tally[result.outcome] += 1;
      } catch (error) {
        tally.rejected += batch.length;
        failed = true;
      }
      done += batch.length;
    }

    // A futó kérés befejezése után kérjük a megszakítást, így nem csúszhat be újabb fájl a sorba.
    let stopped = 0;
    if (stopRequested) {
      const cancelled = await cancelPending(uploadId);
      tally.accepted = Math.max(0, tally.accepted - cancelled);
      stopped = files.length - done + cancelled;
    }

    uploading = false;
    pickButton.disabled = false;
    updateDeleteButton();
    stopButton.hidden = true;
    uploadWarning.hidden = true;
    uploadStatus.textContent = [
      t("ui.upload.summary", tally),
      stopped ? t("ui.upload.stopped", { stopped }) : "",
      failed ? t("ui.upload.failed") : "",
    ]
      .filter(Boolean)
      .join(" ");
    input.value = "";
    refresh();
  }

  // A böngésző saját, általános szöveget mutat; a részletes figyelmeztetés az oldalon látszik.
  window.addEventListener("beforeunload", (event) => {
    if (!uploading) return;
    event.preventDefault();
    event.returnValue = "";
  });

  pickButton.addEventListener("click", () => input.click());
  deleteButton.addEventListener("click", deleteInvoices);
  stopButton.addEventListener("click", () => {
    stopRequested = true;
    stopButton.disabled = true;
    uploadStatus.textContent = t("ui.upload.stopping");
  });
  input.addEventListener("change", () => upload(input.files));
  ["dragenter", "dragover"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragging");
    }),
  );
  ["dragleave", "drop"].forEach((name) =>
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragging");
    }),
  );
  dropzone.addEventListener("drop", (event) => upload(event.dataTransfer.files));
  window.addEventListener("resize", () => {
    limitHeight(invWrap, invBody, VISIBLE_INVOICE_ROWS);
    limitHeight(docsWrap, docsBody, VISIBLE_DOCUMENT_ROWS);
  });

  refresh();
})();
