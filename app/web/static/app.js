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
  const uploadIssues = document.getElementById("upload-issues");
  const docsSummary = document.getElementById("docs-summary");
  const docsTable = document.getElementById("docs-table");
  const docsBody = docsTable.querySelector("tbody");

  const ACTIVE = new Set(["pending", "processing"]);
  let pollTimer = null;
  let uploading = false;

  const formatSize = (bytes) =>
    bytes >= 1048576
      ? `${(bytes / 1048576).toFixed(1)} MB`
      : `${Math.max(1, Math.round(bytes / 1024))} KB`;

  const errorMessage = (error) =>
    error ? strings[`error.${error.code}`] ?? error.message : "";

  function renderDocuments(data) {
    const { documents, counts } = data;
    docsTable.hidden = documents.length === 0;
    docsBody.replaceChildren(
      ...documents.map((doc) => {
        const row = document.createElement("tr");
        const cells = [
          doc.filename,
          t(`status.${doc.status}`),
          doc.page_count ?? "",
          formatSize(doc.size_bytes),
          errorMessage(doc.error),
        ];
        cells.forEach((value, index) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          if (index === 1) cell.className = `status status-${doc.status}`;
          if (index === 2 || index === 3) cell.className = "num";
          row.appendChild(cell);
        });
        return row;
      }),
    );
    docsSummary.textContent = documents.length
      ? t("ui.docs.summary", {
          total: documents.length,
          done: counts.done,
          active: counts.pending + counts.processing,
          failed: counts.failed,
        })
      : t("ui.docs.empty");
    return counts.pending + counts.processing > 0;
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
    uploadStatus.textContent = t("ui.upload.summary", tally);
    input.value = "";
    refresh();
  }

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
