(() => {
  "use strict";

  const BATCH_SIZE = 10;
  const POLL_MS = 1500;
  const strings = JSON.parse(document.getElementById("i18n-data").textContent);
  const t = (key, params = {}) =>
    (strings[key] ?? key).replace(/\{(\w+)\}/g, (_, name) => params[name] ?? "");

  const dropzone = document.getElementById("dropzone");
  const input = document.getElementById("file-input");
  const pickButton = document.getElementById("pick-button");
  const uploadStatus = document.getElementById("upload-status");
  const uploadWarning = document.getElementById("upload-warning");
  const uploadIssues = document.getElementById("upload-issues");
  const docsSummary = document.getElementById("docs-summary");
  const docsTable = document.getElementById("docs-table");
  const docsBody = docsTable.querySelector("tbody");
  const invSummary = document.getElementById("inv-summary");
  const invTable = document.getElementById("inv-table");
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
  const partnersFoot = partnersTable.querySelector("tfoot");
  const amountHeader = document.getElementById("amount-header");

  const ACTIVE = new Set(["pending", "processing"]);
  let pollTimer = null;
  let uploading = false;

  const formatSize = (bytes) =>
    bytes >= 1048576
      ? `${(bytes / 1048576).toFixed(1)} MB`
      : `${Math.max(1, Math.round(bytes / 1024))} KB`;

  const errorMessage = (error) => {
    if (!error) return "";
    const base = strings[`error.${error.code}`] ?? error.message;
    const fields = (error.fields ?? []).map((name) => strings[`ui.field.${name}`] ?? name);
    // Az LLM-hibáknál a szolgáltató eredeti üzenete is látszik, hogy a hiba oka azonosítható legyen.
    const detail = /^(llm_|duplicate_invoice$)/.test(error.code) && error.message ? ` – ${error.message}` : "";
    return (fields.length ? `${base}: ${fields.join(", ")}` : base) + detail;
  };

  function renderDocuments(data) {
    const { documents, counts } = data;
    docsTable.hidden = documents.length === 0;
    docsBody.replaceChildren(
      ...documents.map((doc) => {
        const row = document.createElement("tr");
        const cells = [
          doc.filename,
          t(`status.${doc.status}`),
          formatSize(doc.size_bytes),
          errorMessage(doc.error),
        ];
        cells.forEach((value, index) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          if (index === 1) cell.className = `status status-${doc.status}`;
          if (index === 2) cell.className = "num";
          row.appendChild(cell);
        });
        return row;
      }),
    );
    docsSummary.textContent = documents.length
      ? t("ui.docs.summary", {
          active: counts.pending + counts.processing,
          failed: counts.failed,
        })
      : t("ui.docs.empty");
    return counts.pending + counts.processing > 0;
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
    invSummary.textContent = invoices.length
      ? t("ui.inv.summary", {
          total: invoices.length,
          ok: counts.ok,
          review: counts.needs_review,
        })
      : t("ui.inv.empty");
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

  function renderPartners(report) {
    const { partners } = report;
    partnersTitle.textContent = t("ui.report.partners_title", { currency: report.currency });
    amountHeader.textContent = t("ui.inv.col.amount", { currency: report.currency });
    partnersTable.hidden = partners.length === 0;
    partnersEmpty.hidden = partners.length > 0;
    partnersBody.replaceChildren(
      ...partners.map((partner) => {
        const row = document.createElement("tr");
        cell(row, partner.name);
        cell(row, partner.tax_id);
        cell(row, String(partner.count), "num");
        cell(row, formatAmount(String(partner.incoming)), "num");
        cell(row, formatAmount(String(partner.outgoing)), "num");
        return row;
      }),
    );
    const total = document.createElement("tr");
    cell(total, t("ui.report.total"));
    cell(total, "");
    cell(total, String(partners.reduce((sum, partner) => sum + partner.count, 0)), "num");
    cell(total, formatAmount(String(report.total_incoming)), "num");
    cell(total, formatAmount(String(report.total_outgoing)), "num");
    partnersFoot.replaceChildren(partners.length ? total : "");
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
          tooltip: { callbacks: { label: (item) => `${item.dataset.label}: ${number(item.parsed.y)} ${report.currency}` } },
        },
      },
    });
  }

  async function refreshInvoices() {
    try {
      const [invoicesResponse, reportResponse] = await Promise.all([fetch("/api/invoices"), fetch("/api/report")]);
      const report = reportResponse.ok ? await reportResponse.json() : null;
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

  function addIssue(text) {
    const item = document.createElement("li");
    item.textContent = text;
    uploadIssues.appendChild(item);
  }

  async function sendBatch(files) {
    const body = new FormData();
    files.forEach((file) => body.append("files", file, file.name));
    const response = await fetch("/api/documents", { method: "POST", body });
    if (!response.ok) throw new Error(String(response.status));
    return (await response.json()).results;
  }

  async function upload(fileList) {
    const files = Array.from(fileList);
    if (!files.length || uploading) return;
    uploading = true;
    uploadWarning.hidden = false;
    uploadIssues.replaceChildren();
    const tally = { accepted: 0, duplicate: 0, rejected: 0 };
    let done = 0;
    refresh();

    for (let start = 0; start < files.length; start += BATCH_SIZE) {
      const batch = files.slice(start, start + BATCH_SIZE);
      uploadStatus.textContent = t("ui.upload.progress", { done, total: files.length });
      try {
        for (const result of await sendBatch(batch)) {
          tally[result.outcome] += 1;
          if (result.outcome !== "accepted") {
            const reason = result.error ? errorMessage(result.error) : t(`outcome.${result.outcome}`);
            addIssue(`${result.filename}: ${reason}`);
          }
        }
      } catch (error) {
        tally.rejected += batch.length;
        addIssue(t("ui.upload.failed"));
      }
      done += batch.length;
    }

    uploading = false;
    uploadWarning.hidden = true;
    uploadStatus.textContent = t("ui.upload.summary", tally);
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

  refresh();
})();
