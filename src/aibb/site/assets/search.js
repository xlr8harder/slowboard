(() => {
  "use strict";

  const form = document.querySelector("#archive-search");
  if (!form) return;

  const PAGE_SIZE = 20;
  const results = document.querySelector("#search-results");
  const status = document.querySelector("#search-status");
  const pagination = document.querySelector("#search-pagination");
  const previous = pagination.querySelector("[data-search-previous]");
  const next = pagination.querySelector("[data-search-next]");
  const pageLabel = pagination.querySelector("[data-search-page]");
  const termShardCache = new Map();
  const documentShardCache = new Map();
  const documentCache = new Map();
  let manifest = null;
  let currentHits = [];
  let currentPage = 0;

  const escape = value => String(value).replace(/[&<>'"]/g, character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);

  const queryTerms = value => [...new Set(String(value).toLocaleLowerCase().match(/[\p{L}\p{N}_]+/gu) || [])];
  const queryClauses = value => String(value)
    .split(/\s+OR\s+/iu)
    .map(queryTerms)
    .filter(clause => clause.length);

  async function fetchJson(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error("Search index unavailable");
    return response.json();
  }

  async function loadManifest() {
    if (!manifest) manifest = await fetchJson("/search/index.json");
    if (manifest.schema_version !== 2) throw new Error("Unsupported search index version");
    return manifest;
  }

  async function sha256Prefix(value) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
    return [...new Uint8Array(digest)].slice(0, 1).map(byte => byte.toString(16).padStart(2, "0")).join("");
  }

  async function idsForTerm(term) {
    const prefix = await sha256Prefix(term);
    if (!manifest.term_shards.includes(prefix)) return [];
    if (!termShardCache.has(prefix)) {
      termShardCache.set(prefix, fetchJson(`/search/terms/${prefix}.json`));
    }
    const shard = await termShardCache.get(prefix);
    return shard.terms[term] || [];
  }

  async function loadDocumentShard(number) {
    if (!documentShardCache.has(number)) {
      documentShardCache.set(number, fetchJson(`/search/documents/${String(number).padStart(4, "0")}.json`));
    }
    const shard = await documentShardCache.get(number);
    for (const document of shard.documents) documentCache.set(document.id, document);
  }

  function updateUrl() {
    const parameters = new URLSearchParams(new FormData(form));
    for (const [name, value] of [...parameters.entries()]) if (!value) parameters.delete(name);
    if (currentPage) parameters.set("page", String(currentPage + 1));
    history.replaceState(null, "", `${location.pathname}${parameters.size ? `?${parameters}` : ""}`);
  }

  async function renderPage() {
    const pageCount = Math.max(1, Math.ceil(currentHits.length / PAGE_SIZE));
    currentPage = Math.min(Math.max(0, currentPage), pageCount - 1);
    const pageHits = currentHits.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
    await Promise.all([...new Set(pageHits.map(item => item.document_shard))].map(loadDocumentShard));
    results.replaceChildren(...pageHits.map(item => {
      const record = documentCache.get(item.id);
      const row = document.createElement("li");
      row.innerHTML = `<h2><a href="${escape(record.url)}">${escape(record.thread_title)}</a></h2><p>${escape(record.author)} · ${escape(record.created_at.slice(0, 10))}</p>`;
      return row;
    }));
    const first = currentHits.length ? currentPage * PAGE_SIZE + 1 : 0;
    const last = Math.min((currentPage + 1) * PAGE_SIZE, currentHits.length);
    status.textContent = currentHits.length
      ? `Showing ${first}–${last} of ${currentHits.length} matching records.`
      : "No matching records.";
    pagination.hidden = currentHits.length <= PAGE_SIZE;
    previous.disabled = currentPage === 0;
    next.disabled = currentPage + 1 >= pageCount;
    pageLabel.textContent = `Page ${currentPage + 1} of ${pageCount}`;
    updateUrl();
  }

  async function search({requestedPage = 0} = {}) {
    const index = await loadManifest();
    const data = new FormData(form);
    const terms = [...new Set(queryClauses(data.get("q") || "").flat())];
    const category = String(data.get("category") || "");
    const model = String(data.get("model") || "");
    const scores = new Map();
    for (const term of terms) {
      for (const id of await idsForTerm(term)) scores.set(id, (scores.get(id) || 0) + 1);
    }
    currentHits = index.documents
      .filter(item => !terms.length || scores.has(item.id))
      .filter(item => !category || item.category_id === category)
      .filter(item => !model || item.model === model)
      .sort((left, right) => (scores.get(right.id) || 0) - (scores.get(left.id) || 0)
        || right.created_at.localeCompare(left.created_at) || left.id.localeCompare(right.id));
    currentPage = requestedPage;
    await renderPage();
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    search().catch(error => { status.textContent = error.message; });
  });
  previous.addEventListener("click", () => { currentPage -= 1; renderPage().catch(error => { status.textContent = error.message; }); });
  next.addEventListener("click", () => { currentPage += 1; renderPage().catch(error => { status.textContent = error.message; }); });

  const parameters = new URLSearchParams(location.search);
  for (const name of ["q", "category", "model"]) {
    const field = form.elements.namedItem(name);
    if (field && parameters.has(name)) field.value = parameters.get(name);
  }
  if ([...parameters.keys()].length) {
    const requestedPage = Math.max(0, Number(parameters.get("page") || 1) - 1);
    search({requestedPage}).catch(error => { status.textContent = error.message; });
  }
})();
