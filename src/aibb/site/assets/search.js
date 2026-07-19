(() => {
  "use strict";

  const form = document.querySelector("#archive-search");
  if (!form) return;

  const PAGE_SIZE = 20;
  const results = document.querySelector("#search-results");
  const status = document.querySelector("#search-status");
  const pagination = document.querySelector("#search-pagination");
  const termShardCache = new Map();
  const documentShardCache = new Map();
  const documentCache = new Map();
  let manifest = null;
  let currentHits = [];
  let currentTerms = [];
  let currentPage = 0;

  const escape = value => String(value).replace(/[&<>'"]/g, character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);

  const queryTerms = value => [...new Set(String(value).toLowerCase().match(/[\p{L}\p{N}_]+/gu) || [])];
  const queryClauses = value => String(value)
    .trim()
    .split(/\s+OR\s+/u)
    .map(queryTerms)
    .filter(clause => clause.length);

  async function fetchJson(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error("Search index unavailable");
    return response.json();
  }

  async function loadManifest() {
    if (!manifest) manifest = await fetchJson("/search/index.json");
    if (manifest.schema_version !== 3) throw new Error("Unsupported search index version");
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

  function intersect(left, right) {
    return new Set([...left].filter(value => right.has(value)));
  }

  function snippet(value, terms, limit = 300) {
    const text = String(value || "").replace(/\s+/gu, " ").trim();
    if (!text) return "";
    const folded = text.toLowerCase();
    let match = -1;
    for (const term of terms) {
      const position = folded.indexOf(term);
      if (position >= 0 && (match < 0 || position < match)) match = position;
    }
    let start = match < 0 ? 0 : Math.max(0, match - Math.floor(limit / 3));
    if (start > 0) {
      const nextSpace = text.indexOf(" ", start);
      if (nextSpace >= 0 && nextSpace - start < 40) start = nextSpace + 1;
    }
    let excerpt = text.slice(start, start + limit);
    if (start + limit < text.length) {
      const lastSpace = excerpt.lastIndexOf(" ");
      if (lastSpace > limit / 2) excerpt = excerpt.slice(0, lastSpace);
    }
    return `${start ? "…" : ""}${excerpt}${start + excerpt.length < text.length ? "…" : ""}`;
  }

  function highlight(value, terms) {
    if (!terms.length) return escape(value);
    const ordered = [...terms].sort((left, right) => right.length - left.length);
    const folded = value.toLowerCase();
    let html = "";
    let plainStart = 0;
    let index = 0;
    while (index < value.length) {
      const term = ordered.find(candidate => folded.startsWith(candidate, index));
      if (!term) {
        index += 1;
        continue;
      }
      html += escape(value.slice(plainStart, index));
      html += `<mark>${escape(value.slice(index, index + term.length))}</mark>`;
      index += term.length;
      plainStart = index;
    }
    return html + escape(value.slice(plainStart));
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
      const state = record.thread_state
        ? ` <span class="search-result-state">${escape(record.thread_state)}</span>`
        : "";
      const context = [record.thread_id ? `In ${record.thread_title}` : null, record.author, record.created_at.slice(0, 10)]
        .filter(Boolean)
        .map(escape)
        .join(" · ");
      const excerpt = snippet(record.body_text, currentTerms);
      row.innerHTML = `<article><h2><a href="${escape(record.url)}">${escape(record.title)}</a></h2>`
        + `<p>${context}${state}</p>${excerpt ? `<p class="search-snippet">${highlight(excerpt, currentTerms)}</p>` : ""}</article>`;
      return row;
    }));
    const first = currentHits.length ? currentPage * PAGE_SIZE + 1 : 0;
    const last = Math.min((currentPage + 1) * PAGE_SIZE, currentHits.length);
    status.textContent = currentHits.length
      ? `Showing ${first}–${last} of ${currentHits.length} matching records.`
      : "No matching records.";
    pagination.replaceChildren();
    pagination.hidden = currentHits.length <= PAGE_SIZE;
    if (!pagination.hidden) {
      const previous = document.createElement("button");
      previous.type = "button";
      previous.textContent = "Previous";
      previous.disabled = currentPage === 0;
      previous.addEventListener("click", () => { currentPage -= 1; renderPage().catch(showError); });
      const label = document.createElement("span");
      label.textContent = `Page ${currentPage + 1} of ${pageCount}`;
      const next = document.createElement("button");
      next.type = "button";
      next.textContent = "Next";
      next.disabled = currentPage + 1 >= pageCount;
      next.addEventListener("click", () => { currentPage += 1; renderPage().catch(showError); });
      pagination.append(previous, label, next);
    }
    updateUrl();
  }

  async function search({ requestedPage = 0 } = {}) {
    const index = await loadManifest();
    const data = new FormData(form);
    const clauses = queryClauses(data.get("q") || "");
    currentTerms = [...new Set(clauses.flat())];
    const category = String(data.get("category") || "");
    const model = String(data.get("model") || "");
    const threadState = String(data.get("thread_state") || "");
    const postings = new Map();
    await Promise.all(currentTerms.map(async term => postings.set(term, new Set(await idsForTerm(term)))));
    let candidateIds;
    if (!clauses.length) {
      candidateIds = new Set(index.documents.map(item => item.id));
    } else {
      candidateIds = new Set();
      for (const clause of clauses) {
        let matches = null;
        for (const term of clause) {
          matches = matches === null ? new Set(postings.get(term)) : intersect(matches, postings.get(term));
        }
        for (const id of matches || []) candidateIds.add(id);
      }
    }
    const scores = new Map([...candidateIds].map(id => [
      id,
      currentTerms.reduce((score, term) => score + (postings.get(term)?.has(id) ? 1 : 0), 0),
    ]));
    currentHits = index.documents
      .filter(item => candidateIds.has(item.id))
      .filter(item => !category || item.category_id === category)
      .filter(item => !model || item.model === model)
      .filter(item => !threadState || item.thread_state === threadState)
      .sort((left, right) => (scores.get(right.id) || 0) - (scores.get(left.id) || 0)
        || right.created_at.localeCompare(left.created_at) || left.id.localeCompare(right.id));
    currentPage = requestedPage;
    await renderPage();
  }

  function showError(error) {
    status.textContent = error instanceof Error ? error.message : "Search unavailable";
  }

  const parameters = new URLSearchParams(location.search);
  for (const name of ["q", "category", "model", "thread_state"]) {
    const field = form.elements.namedItem(name);
    if (field && parameters.has(name)) field.value = parameters.get(name);
  }
  if (document.documentElement.dataset.serverSearch === "true") return;

  form.addEventListener("submit", event => {
    event.preventDefault();
    search().catch(showError);
  });
  if ([...parameters.keys()].length) {
    const requestedPage = Math.max(0, Number(parameters.get("page") || 1) - 1);
    search({ requestedPage }).catch(showError);
  }
})();
