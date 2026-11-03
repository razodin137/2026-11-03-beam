(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ESC_MAP[ch]);
  const safeHref = (value) => {
    const text = String(value || "");
    return /^(https?:\/\/|\/)/i.test(text) ? text : "#";
  };
  const plural = (count, singular, pluralForm) => `${count} ${count === 1 ? singular : (pluralForm || singular + "s")}`;
  const extractUrls = (text) => {
    const matches = String(text || "").match(/https?:\/\/[^\s<>"'`)\]]+/gi) || [];
    const seen = new Set();
    const urls = [];
    for (let value of matches) {
      value = value.replace(/[.,;:!?]+$/, "");
      if (value && !seen.has(value)) {
        seen.add(value);
        urls.push(value);
      }
    }
    return urls;
  };
  const formatSize = (bytes) => {
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };
  const formatWhen = (iso) => {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const now = Date.now();
    const seconds = Math.max(0, Math.round((now - date.getTime()) / 1000));
    if (seconds < 45) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`;
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };

  const MAX_FILE_BYTES = 40 * 1024 * 1024;
  const MAX_TOTAL_UPLOAD = 50 * 1024 * 1024;

  const state = {
    files: [],
    statuses: new Map(),
    statusReady: false,
    maxCollection: 12,
    polling: false,
  };

  let toastTimer = 0;

  function toast(message, kind = "ok") {
    const element = $("toast");
    if (!element) return;
    element.textContent = message;
    element.className = "toast is-visible" + (kind === "error" ? " toast--error" : "");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      element.classList.remove("is-visible");
    }, 4600);
  }

  async function postJson(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let data = {};
    try {
      data = await response.json();
    } catch (_) {
      data = {};
    }
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    return data;
  }

  // ---------- files ----------
  function renderFiles() {
    const list = $("file-list");
    if (!list) return;
    list.innerHTML = "";
    state.files.forEach((file, index) => {
      const chip = document.createElement("span");
      chip.className = "file-chip";

      const name = document.createElement("span");
      name.className = "file-chip__name";
      name.textContent = file.name;
      name.title = file.name;

      const size = document.createElement("span");
      size.className = "file-chip__size";
      size.textContent = formatSize(file.size);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "file-chip__remove";
      remove.textContent = "\u00d7";
      remove.setAttribute("aria-label", `Remove ${file.name}`);
      remove.addEventListener("click", () => {
        state.files.splice(index, 1);
        renderFiles();
        updateDropState();
      });

      chip.append(name, size, remove);
      list.appendChild(chip);
    });
  }

  function readFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error || new Error(`Could not read ${file.name}`));
      reader.onload = () => {
        const result = String(reader.result || "");
        const comma = result.indexOf(",");
        resolve({
          name: file.name,
          size: file.size,
          data: comma >= 0 ? result.slice(comma + 1) : result,
        });
      };
      reader.readAsDataURL(file);
    });
  }

  async function addFiles(fileList) {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;
    const existing = state.files.reduce((sum, file) => sum + file.size, 0);
    let total = existing;
    const accepted = [];
    for (const file of incoming) {
      if (file.size > MAX_FILE_BYTES) {
        toast(`${file.name} is larger than the 40 MB limit.`, "error");
        continue;
      }
      if (total + file.size > MAX_TOTAL_UPLOAD) {
        toast("Those files would exceed the 50 MB upload limit for one batch.", "error");
        break;
      }
      total += file.size;
      accepted.push(file);
    }
    if (!accepted.length) return;
    try {
      const read = await Promise.all(accepted.map(readFile));
      state.files.push(...read);
      renderFiles();
      updateDropState();
    } catch (error) {
      toast(error.message || "Could not read those files.", "error");
    }
  }

  function updateDropState() {
    const links = extractUrls($("drop-links").value);
    const hasFiles = state.files.length > 0;
    const button = $("queue-drops");
    button.disabled = links.length === 0 && !hasFiles;
    const parts = [];
    if (links.length) parts.push(plural(links.length, "link"));
    if (hasFiles) {
      const total = state.files.reduce((sum, file) => sum + file.size, 0);
      parts.push(`${plural(state.files.length, "file")} \u00b7 ${formatSize(total)}`);
    }
    $("drop-hint").textContent = parts.length ? `${parts.join(" \u00b7 ")} staged` : "Nothing staged yet.";
  }

  // ---------- tabs / request ----------
  function updateTabsState() {
    const urls = extractUrls($("tabs-text").value);
    $("queue-tabs").disabled = urls.length === 0;
    $("tabs-hint").textContent = urls.length
      ? `${plural(urls.length, "URL")} detected \u00b7 each gets sorted on its own`
      : "No tabs detected.";
  }

  function updateRequestState() {
    const urls = extractUrls($("request-links").value);
    const tooMany = urls.length > state.maxCollection;
    const hint = $("request-hint");
    $("queue-collection").disabled = urls.length === 0 || tooMany;
    hint.classList.toggle("hint--warn", tooMany);
    hint.textContent = tooMany
      ? `${urls.length} sources \u00b7 max ${state.maxCollection}`
      : `${plural(urls.length, "source")} \u00b7 max ${state.maxCollection}`;
  }

  // ---------- status ----------
  function renderJob(job) {
    const title = job.title || "Untitled";
    const titleHtml = job.status === "done" && job.href
      ? `<a href="${esc(safeHref(job.href))}">${esc(title)}</a>`
      : esc(title);
    const meta = job.message || job.category_label || (job.kind === "collection" ? "Collection" : "Brief");
    return (
      `<li class="job job--${esc(job.status)}">` +
      '<span class="job__dot" aria-hidden="true"></span>' +
      `<div class="job__body"><div class="job__title">${titleHtml}</div>` +
      `<div class="job__meta">${esc(meta)}</div></div>` +
      `<span class="job__status">${esc(job.status)}</span></li>`
    );
  }

  function renderRecent(item) {
    const cover = item.cover
      ? `<img src="${esc(safeHref(item.cover))}" alt="" loading="lazy">`
      : esc((item.label || "?").slice(0, 1));
    const meta = [item.label, item.source_label, `${item.reading_time || 0} min`]
      .filter(Boolean)
      .join(" \u00b7 ");
    return (
      '<li><a class="recent" href="' + esc(safeHref(item.href)) + '">' +
      `<span class="recent__thumb">${cover}</span>` +
      '<span class="recent__body">' +
      `<span class="recent__title">${esc(item.title)}</span>` +
      `<span class="recent__meta">${esc(meta)}</span>` +
      "</span></a></li>"
    );
  }

  function renderStatus(data) {
    const health = data.health || {};
    const healthElement = $("health");
    const healthLabel = $("health-label");
    healthElement.classList.remove("health--ok", "health--bad", "health--checking");
    if (health.ok) {
      healthElement.classList.add("health--ok");
      healthLabel.textContent = `Ollama ${health.version || "ready"}`;
      healthElement.title = "The local Ollama endpoint is responding.";
    } else {
      healthElement.classList.add("health--bad");
      healthLabel.textContent = "Ollama offline";
      healthElement.title = health.error || "Could not reach the local Ollama endpoint.";
    }

    const counts = data.counts || {};
    const categories = counts.categories || {};
    const briefs = Object.values(categories).reduce((sum, value) => sum + Number(value || 0), 0);
    $("count-briefs").textContent = String(briefs);
    $("count-collections").textContent = String(counts.collections || 0);
    $("count-queued").textContent = String((data.queue && data.queue.active) || 0);
    $("active-count").textContent = String((data.queue && data.queue.active) || 0);

    state.maxCollection = Number((data.limits && data.limits.max_collection_sources) || state.maxCollection);
    updateRequestState();

    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    const active = jobs.filter((job) => ["queued", "running", "failed"].includes(job.status)).slice(0, 8);
    $("job-list").innerHTML = active.length
      ? active.map(renderJob).join("")
      : '<li class="empty-row">Nothing queued. Drop something above.</li>';

    const recent = Array.isArray(data.recent) ? data.recent : [];
    $("recent-list").innerHTML = recent.length
      ? recent.map(renderRecent).join("")
      : '<li class="empty-row">Nothing sorted yet.</li>';

    if (state.statusReady) {
      for (const job of jobs) {
        const previous = state.statuses.get(job.id);
        if (previous && previous !== job.status) {
          if (job.status === "done") {
            toast(`Ready: ${job.title}`);
          } else if (job.status === "failed") {
            toast(`Failed: ${job.title}`, "error");
          }
        }
      }
    }
    state.statuses = new Map(jobs.map((job) => [job.id, job.status]));
    state.statusReady = true;
  }

  async function refreshStatus() {
    if (state.polling) return;
    state.polling = true;
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`Status request failed (${response.status})`);
      const data = await response.json();
      renderStatus(data);
    } catch (error) {
      const healthElement = $("health");
      const healthLabel = $("health-label");
      healthElement.classList.remove("health--ok", "health--checking");
      healthElement.classList.add("health--bad");
      healthLabel.textContent = "App server offline";
      healthElement.title = error.message;
    } finally {
      state.polling = false;
    }
  }

  // ---------- drag & drop ----------
  function bindDropzone() {
    const dropzone = $("dropzone");
    const fileInput = $("file-input");
    const browse = $("browse-files");
    let dragDepth = 0;

    dropzone.addEventListener("click", (event) => {
      if (event.target.closest("#browse-files")) return;
      fileInput.click();
    });
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fileInput.click();
      }
    });
    browse.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      fileInput.click();
    });
    fileInput.addEventListener("change", async () => {
      await addFiles(fileInput.files);
      fileInput.value = "";
    });

    dropzone.addEventListener("dragenter", (event) => {
      event.preventDefault();
      event.stopPropagation();
      dragDepth += 1;
      dropzone.classList.add("is-over");
    });
    dropzone.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    });
    dropzone.addEventListener("dragleave", (event) => {
      event.preventDefault();
      event.stopPropagation();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) dropzone.classList.remove("is-over");
    });
    dropzone.addEventListener("drop", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      dragDepth = 0;
      dropzone.classList.remove("is-over");
      const transfer = event.dataTransfer;
      if (!transfer) return;

      const text = transfer.getData("text/uri-list") || transfer.getData("text/plain");
      const urls = extractUrls(text);
      if (urls.length) {
        const current = $("drop-links").value.trim();
        $("drop-links").value = [current, ...urls].filter(Boolean).join("\n");
        updateDropState();
      }
      if (transfer.files && transfer.files.length) {
        await addFiles(transfer.files);
      }
    });

    window.addEventListener("dragover", (event) => event.preventDefault());
    window.addEventListener("drop", (event) => event.preventDefault());
  }

  // ---------- submissions ----------
  async function queueDrops() {
    const links = extractUrls($("drop-links").value);
    if (!links.length && !state.files.length) return;
    const button = $("queue-drops");
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Queueing\u2026";
    try {
      const result = await postJson("/api/submit", {
        mode: "mixed",
        links,
        files: state.files.map((file) => ({ name: file.name, data: file.data })),
      });
      toast(`Queued ${plural(result.queued, "item")}. Beam is sorting them now.`);
      $("drop-links").value = "";
      state.files = [];
      renderFiles();
      updateDropState();
      refreshStatus();
    } catch (error) {
      toast(error.message || "Could not queue those items.", "error");
      button.disabled = false;
    } finally {
      button.textContent = label;
      updateDropState();
    }
  }

  async function queueTabs() {
    const text = $("tabs-text").value;
    const urls = extractUrls(text);
    if (!urls.length) {
      toast("No URLs found in that tab dump.", "error");
      return;
    }
    const button = $("queue-tabs");
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Queueing\u2026";
    try {
      const result = await postJson("/api/submit", { mode: "tabs", text });
      toast(`Queued ${plural(result.queued, "tab")}. Each will be sorted into a category.`);
      $("tabs-text").value = "";
      updateTabsState();
      refreshStatus();
    } catch (error) {
      toast(error.message || "Could not queue those tabs.", "error");
    } finally {
      button.textContent = label;
      updateTabsState();
    }
  }

  async function queueCollection() {
    const links = extractUrls($("request-links").value);
    if (!links.length) {
      toast("Add at least one source link.", "error");
      return;
    }
    if (links.length > state.maxCollection) {
      toast(`Collections are limited to ${state.maxCollection} sources.`, "error");
      return;
    }
    const button = $("queue-collection");
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Building\u2026";
    try {
      await postJson("/api/submit", {
        mode: "collection",
        links,
        prompt: $("request-prompt").value.trim(),
      });
      toast("Summary site queued. It will land in the library when it is ready.");
      $("request-links").value = "";
      updateRequestState();
      refreshStatus();
    } catch (error) {
      toast(error.message || "Could not queue that collection.", "error");
    } finally {
      button.textContent = label;
      updateRequestState();
    }
  }

  // ---------- init ----------
  document.addEventListener("DOMContentLoaded", () => {
    $("footer-host").textContent = window.location.host;
    bindDropzone();
    renderFiles();

    $("drop-links").addEventListener("input", updateDropState);
    $("tabs-text").addEventListener("input", updateTabsState);
    $("request-links").addEventListener("input", updateRequestState);
    $("queue-drops").addEventListener("click", queueDrops);
    $("queue-tabs").addEventListener("click", queueTabs);
    $("queue-collection").addEventListener("click", queueCollection);

    updateDropState();
    updateTabsState();
    updateRequestState();
    refreshStatus();
    window.setInterval(refreshStatus, 2000);
  });
})();
