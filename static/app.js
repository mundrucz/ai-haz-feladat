const formatHuf = value => new Intl.NumberFormat("hu-HU").format(Number(value || 0));
const safe = value => String(value ?? "—");

function cell(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function renderChart(months) {
  const svg = document.querySelector("#cashflow-chart");
  svg.replaceChildren();
  const width = Math.max(640, months.length * 82);
  const height = 275;
  const left = 64, right = 18, top = 18, bottom = 48;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const maximum = Math.max(1, ...months.flatMap(item => [item.bejövő, item.kimenő]));
  const scaleMax = maximum * 1.15;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const make = (name, attrs = {}) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    svg.append(node);
    return node;
  };
  for (let tick = 0; tick <= 4; tick++) {
    const y = top + plotHeight - plotHeight * tick / 4;
    make("line", { x1: left, y1: y, x2: width - right, y2: y, stroke: "#e8edf0" });
    const label = make("text", { x: left - 8, y: y + 4, "text-anchor": "end", fill: "#718096", "font-size": 10 });
    label.textContent = formatHuf(scaleMax * tick / 4);
  }
  const x = index => months.length < 2 ? left + plotWidth / 2 : left + index * plotWidth / (months.length - 1);
  for (let index = 0; index < months.length; index++) {
    const label = make("text", { x: x(index), y: height - 19, "text-anchor": "middle", fill: "#718096", "font-size": 10 });
    label.textContent = months[index].month;
  }
  const points = key => months.map((item, index) => {
    const y = top + plotHeight - (item[key] / scaleMax) * plotHeight;
    return `${x(index)},${y}`;
  }).join(" ");
  for (const [key, color] of [["bejövő", "#137c72"], ["kimenő", "#e58952"]]) {
    if (!months.length) continue;
    make("polyline", { points: points(key), fill: "none", stroke: color, "stroke-width": 3, "stroke-linecap": "round", "stroke-linejoin": "round" });
    months.forEach((item, index) => {
      const y = top + plotHeight - (item[key] / scaleMax) * plotHeight;
      const circle = make("circle", { cx: x(index), cy: y, r: 3.5, fill: color });
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${item.month}: ${formatHuf(item[key])} HUF`;
      circle.append(title);
    });
  }
  const axis = make("text", { x: 14, y: 14, fill: "#718096", "font-size": 10 });
  axis.textContent = "HUF";
}

function renderPartners(partners) {
  const table = document.querySelector("#partner-table");
  table.replaceChildren();
  if (!partners.length) {
    const body = table.createTBody(), row = body.insertRow(), item = row.insertCell();
    item.colSpan = 4; item.className = "empty"; item.textContent = "Még nincs feldolgozott számla.";
    return;
  }
  const head = table.createTHead().insertRow();
  head.append(cell("th", "Irány"));
  partners.forEach(partner => {
    const title = partner.tax_id ? `${partner.name} (${partner.tax_id})` : partner.name;
    head.append(cell("th", title, "numeric"));
  });
  const body = table.createTBody();
  for (const [direction, label] of [["bejövő", "Össz bejövő"], ["kimenő", "Össz kimenő"]]) {
    const row = body.insertRow();
    row.append(cell("th", label));
    partners.forEach(partner => row.append(cell("td", formatHuf(partner[direction]), "numeric")));
  }
}

function renderInvoices(invoices) {
  const table = document.querySelector("#invoice-table");
  table.replaceChildren();
  document.querySelector("#invoice-count").textContent = `${invoices.length} számla`;
  const columns = [
    ["Számlaszám", "invoice_number"], ["Irány", "direction"], ["Partner", "partner_name"],
    ["Partner adószám", "partner_tax_id"], ["Kiállító", "seller_name"], ["Kiállító adószáma", "seller_tax_id"],
    ["Vevő", "buyer_name"], ["Vevő adószáma", "buyer_tax_id"], ["Kiállítás", "issue_date"],
    ["Teljesítés", "service_date"], ["Fizetési határidő", "due_date"], ["Eredeti összeg", "gross_amount"],
    ["Deviza", "currency"], ["MNB-árfolyam", "exchange_rate"], ["Árfolyam dátuma", "exchange_rate_date"],
    ["Összeg (HUF)", "huf_amount"], ["Fájl", "file_name"]
  ];
  const head = table.createTHead().insertRow();
  columns.forEach(([label]) => head.append(cell("th", label, label.includes("összeg") || label.includes("árfolyam") ? "numeric" : "")));
  const body = table.createTBody();
  invoices.forEach(invoice => {
    const row = body.insertRow();
    columns.forEach(([, key]) => {
      const value = key === "huf_amount" ? formatHuf(invoice[key]) :
        key === "gross_amount" ? `${invoice[key]} ${invoice.currency}` : safe(invoice[key]);
      row.append(cell("td", value, key === "gross_amount" || key === "huf_amount" || key === "exchange_rate" ? "numeric" : ""));
    });
  });
  if (!invoices.length) {
    const row = body.insertRow(), item = row.insertCell();
    item.colSpan = columns.length; item.className = "empty"; item.textContent = "Még nincs feldolgozott számla.";
  }
}

function renderErrors(errors) {
  const section = document.querySelector("#errors-panel");
  section.hidden = errors.length === 0;
  const table = document.querySelector("#error-table");
  table.replaceChildren();
  if (!errors.length) return;
  const head = table.createTHead().insertRow();
  ["Fájl", "Hiba"].forEach(label => head.append(cell("th", label)));
  const body = table.createTBody();
  errors.forEach(error => {
    const row = body.insertRow();
    row.append(cell("td", error.file_name));
    row.append(cell("td", error.error, "error-text"));
  });
}

function render(data) {
  renderChart(data.months);
  renderPartners(data.partners);
  renderInvoices(data.invoices);
  renderErrors(data.errors);
}

async function loadDashboard() {
  const response = await fetch("/api/dashboard");
  if (!response.ok) throw new Error("A kimutatás nem tölthető be.");
  render(await response.json());
}

document.querySelector("#upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  const files = document.querySelector("#files").files;
  const status = document.querySelector("#upload-status");
  const button = document.querySelector("#upload-button");
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  button.disabled = true;
  status.textContent = `${files.length} fájl feltöltése és feldolgozása…`;
  try {
    const response = await fetch("/api/upload", { method: "POST", body: form });
    if (!response.ok) throw new Error("A feltöltés sikertelen.");
    const result = await response.json();
    status.textContent = result.results.map(item => `${item.file}: ${item.message}`).join("\n");
    render(result.dashboard);
  } catch (error) {
    status.textContent = error.message || "Hálózati hiba történt.";
  } finally {
    button.disabled = false;
  }
});

loadDashboard().catch(error => {
  document.querySelector("#upload-status").textContent = error.message;
});
