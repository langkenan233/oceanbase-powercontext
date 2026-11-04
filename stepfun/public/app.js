// Browser side of the app: split text, run the embedding model locally, hand
// the vectors to the server, and render hybrid search results.
import { pipeline, env } from "/vendor/transformers.min.js";

const MODEL_ID = "bge-small-zh-v1.5";
const DIM = 512;

// Everything is served by our own process, so the model never leaves the
// machine and the page works offline.
env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = "/models/";
env.backends.onnx.wasm.wasmPaths = "/vendor/";

const state = {
  backend: null,
  dtype: "q8",
  loadMs: null,
  extractor: null,
  active: null,
  collections: [],
  busy: false,
};

const el = (id) => document.getElementById(id);

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ---------- model ---------- */

async function detectBackend() {
  if (!("gpu" in navigator)) return "wasm";
  try {
    const adapter = await navigator.gpu.requestAdapter();
    return adapter ? "webgpu" : "wasm";
  } catch {
    return "wasm";
  }
}

// An INT8 graph on the WebGPU backend returns vectors that no longer match the
// model: encoding the same sentence twice gives a cosine of 0.67, and an
// unrelated sentence scores higher than the sentence itself. FP16 on WebGPU is
// correct and faster than every other pairing, so each backend gets the
// precision it can actually execute.
function precisionFor(device, requested) {
  if (requested === "auto") return device === "webgpu" ? "fp16" : "q8";
  if (requested === "q8" && device === "webgpu") return "q8-on-cpu";
  return requested;
}

async function loadModel(requested = "auto") {
  setBadge("loading");
  // ?device=wasm exercises the fallback path in a browser that has WebGPU, so
  // the degraded route can be demonstrated without a second machine.
  const forced = new URLSearchParams(location.search).get("device");
  let device = forced === "wasm" ? "wasm" : await detectBackend();
  state.forced = forced === "wasm";
  let dtype = precisionFor(device, requested);
  if (dtype === "q8-on-cpu") {
    device = "wasm";
    dtype = "q8";
    state.pinned = true;
  } else {
    state.pinned = false;
  }
  const started = performance.now();
  const attempts = device === "webgpu" ? ["webgpu", "wasm"] : ["wasm"];
  let lastError = null;
  for (const attempt of attempts) {
    const attemptDtype = attempt === device ? dtype : "fp16";
    try {
      const extractor = await pipeline("feature-extraction", MODEL_ID, {
        device: attempt,
        dtype: attemptDtype,
      });
      state.extractor = extractor;
      state.backend = attempt;
      state.dtype = attemptDtype;
      state.loadMs = Math.round(performance.now() - started);
      // Warm the graph so the first user action does not absorb the cost of
      // compiling kernels.
      await extractor(["预热"], { pooling: "cls", normalize: true });
      renderBadges();
      return;
    } catch (err) {
      lastError = err;
      console.warn(`[embed] ${attempt}/${attemptDtype} unavailable:`, err.message);
    }
  }
  throw lastError || new Error("no inference backend available");
}

function setBadge(mode) {
  const badge = el("backendBadge");
  if (mode === "loading") {
    badge.className = "badge ghost";
    badge.textContent = "加载模型中…";
  }
}

const DTYPE_LABEL = { fp16: "FP16", q8: "INT8", fp32: "FP32" };

function renderBadges() {
  const badge = el("backendBadge");
  if (state.backend === "webgpu") {
    badge.className = "badge ok";
    badge.textContent = "WebGPU 推理";
  } else if (state.pinned) {
    badge.className = "badge warn";
    badge.textContent = "WASM 推理（INT8 固定在 CPU）";
  } else if (state.forced) {
    badge.className = "badge warn";
    badge.textContent = "WASM 推理（手动指定）";
  } else {
    badge.className = "badge warn";
    badge.textContent = "WASM 推理（已降级）";
  }
  el("modelBadge").textContent = `${MODEL_ID} · ${DTYPE_LABEL[state.dtype] || state.dtype}`;
  el("kvBackend").textContent = state.backend === "webgpu" ? "WebGPU" : "WASM";
  el("kvDtype").textContent = `${DTYPE_LABEL[state.dtype] || state.dtype}${state.pinned ? "（CPU）" : ""}`;
  el("kvLoad").textContent = state.loadMs == null ? "—" : `${(state.loadMs / 1000).toFixed(2)} s`;
  el("kvDim").textContent = String(DIM);
}

async function embed(texts, batchSize, onProgress) {
  const vectors = [];
  const started = performance.now();
  for (let i = 0; i < texts.length; i += batchSize) {
    const slice = texts.slice(i, i + batchSize);
    const out = await state.extractor(slice, { pooling: "cls", normalize: true });
    const [rows, cols] = out.dims;
    const data = out.data;
    for (let r = 0; r < rows; r++) {
      vectors.push(Array.from(data.slice(r * cols, (r + 1) * cols)));
    }
    onProgress?.(Math.min(i + slice.length, texts.length), texts.length);
  }
  const ms = performance.now() - started;
  return { vectors, ms };
}

/* ---------- text ---------- */

function splitSentences(text) {
  return text
    .replace(/\r\n/g, "\n")
    .split(/(?<=[。！？；\n!?;])/)
    .map((s) => s.trim())
    .filter(Boolean);
}

// Chunks follow sentence boundaries and keep one sentence of overlap so a
// fact split across two chunks is still reachable from either side.
function chunkText(text, max = 320, minOverlap = 60) {
  const out = [];
  let current = [];
  let length = 0;
  const push = () => {
    if (current.length) out.push(current.join(""));
    const keep = [];
    let keepLength = 0;
    for (let i = current.length - 1; i >= 0 && keepLength < minOverlap; i--) {
      keep.unshift(current[i]);
      keepLength += current[i].length;
    }
    current = keep;
    length = keepLength;
  };
  for (const sentence of splitSentences(text)) {
    if (sentence.length > max) {
      if (current.length) push();
      for (let i = 0; i < sentence.length; i += max) out.push(sentence.slice(i, i + max));
      current = [];
      length = 0;
      continue;
    }
    if (length + sentence.length > max && current.length) push();
    current.push(sentence);
    length += sentence.length;
  }
  if (current.length) out.push(current.join(""));
  return out.map((s) => s.trim()).filter(Boolean);
}

function hash(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(36);
}

/* ---------- import ---------- */

function showProgress(text, ratio) {
  const wrap = el("progress");
  wrap.hidden = false;
  el("progressFill").style.width = `${Math.round(ratio * 100)}%`;
  el("progressText").textContent = text;
}

function hideProgress() {
  el("progress").hidden = true;
}

async function importDocuments(docs) {
  if (state.busy) return;
  if (!state.extractor) {
    el("importHint").textContent = "模型尚未就绪，请稍候";
    return;
  }
  if (!state.active) {
    await createCollection("默认知识库");
  }
  state.busy = true;
  el("loadSample").disabled = true;
  const batchSize = Number(el("batchSize").value);

  try {
    const rows = [];
    for (const doc of docs) {
      // Keyed on the source name so importing an edited file replaces its
      // previous chunks instead of leaving the old version behind.
      const docId = hash(doc.source);
      const parts = chunkText(doc.text);
      parts.forEach((part, i) => {
        rows.push({
          id: `${docId}-${i}`,
          document: part,
          metadata: { docId, title: doc.title, source: doc.source, chunk: i, chunks: parts.length },
        });
      });
    }
    if (!rows.length) throw new Error("没有可导入的文本");

    showProgress(`向量化 0/${rows.length} 片段`, 0);
    const embedStart = performance.now();
    const { vectors, ms } = await embed(
      rows.map((r) => r.document),
      batchSize,
      (done, total) => {
        const rate = done / Math.max((performance.now() - embedStart) / 1000, 0.001);
        showProgress(`向量化 ${done}/${total} 片段 · ${rate.toFixed(1)} 条/秒`, (done / total) * 0.85);
      }
    );
    rows.forEach((row, i) => (row.embedding = vectors[i]));

    showProgress("写入 seekdb…", 0.9);
    const written = await api("/api/chunks", { rows });

    const rate = (rows.length / (ms / 1000)).toFixed(1);
    showProgress(
      `完成：${rows.length} 片段 · 向量化 ${(ms / 1000).toFixed(2)} s（${rate} 条/秒）· 写入 ${written.writeMs} ms · 索引刷新 ${written.refreshMs} ms`,
      1
    );
    el("importHint").textContent = `${docs.length} 篇文档切分为 ${rows.length} 个片段，已写入 ${state.active}`;
    await refreshState();
  } catch (err) {
    showProgress(`导入失败：${err.message}`, 0);
  } finally {
    state.busy = false;
    el("loadSample").disabled = false;
  }
}

/* ---------- search ---------- */

let highlighting = [];

function highlight(text) {
  let html = escapeHtml(text);
  const terms = [...new Set(highlighting)].filter((t) => t.length >= 2).sort((a, b) => b.length - a.length);
  for (const term of terms.slice(0, 8)) {
    const safe = escapeHtml(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    html = html.replace(new RegExp(safe, "g"), (m) => `<mark>${m}</mark>`);
  }
  return html;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function runSearch(event) {
  event?.preventDefault();
  const text = el("query").value.trim();
  if (!text) return;
  if (!state.extractor) return;
  if (!state.active) {
    el("searchMeta").textContent = "当前没有知识库，请先导入语料";
    return;
  }
  const btn = el("searchBtn");
  btn.disabled = true;
  try {
    const embedStart = performance.now();
    const { vectors } = await embed([text], 1);
    const embedMs = performance.now() - embedStart;

    const limit = Number(el("topK").value);
    const res = await api("/api/search", { text, embedding: vectors[0], limit });

    highlighting = [text, ...text.split(/[\s,，。、；]+/)];
    el("searchMeta").textContent =
      `${res.fused ? "全文 + 向量 RRF 融合" : "向量检索"} · 数据库 ${res.dbMs} ms · 查询向量化 ${embedMs.toFixed(0)} ms`;

    const list = el("results");
    if (!res.hits.length) {
      list.innerHTML = `<li class="empty">没有命中结果</li>`;
      return;
    }
    list.innerHTML = res.hits
      .map((hit) => {
        const meta = hit.metadata || {};
        const title = meta.title || meta.source || hit.id;
        const tags = [
          meta.source ? `<span class="tag">${escapeHtml(meta.source)}</span>` : "",
          meta.chunks ? `<span class="tag">片段 ${Number(meta.chunk) + 1}/${meta.chunks}</span>` : "",
        ].join("");
        const score = hit.score == null ? "" : Number(hit.score).toFixed(4);
        return `<li class="result">
          <div class="rank">${hit.rank}</div>
          <div class="body">
            <div class="title">${escapeHtml(title)}</div>
            <div class="snippet">${highlight(hit.document || "")}</div>
            <div class="tags">${tags}</div>
          </div>
          <div class="score">${score}</div>
        </li>`;
      })
      .join("");
  } catch (err) {
    el("searchMeta").textContent = `检索失败：${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

/* ---------- collections ---------- */

async function refreshState() {
  const s = await api("/api/state");
  state.collections = s.collections;
  state.active = s.active;
  el("kvDir").textContent = s.dataDir || "—";
  el("kvDir").title = s.dataDirSource || "";
  el("kvDim").textContent = String(s.dimension || DIM);

  const active = s.collections.find((c) => c.name === s.active);
  el("kvCount").textContent = active ? String(active.count) : "0";

  el("kbList").innerHTML =
    s.collections
      .map(
        (c) => `<li class="kb-item ${c.name === s.active ? "active" : ""}" data-name="${c.name}">
          <span class="name">${escapeHtml(displayName(c.name))}</span>
          <span class="count">${c.count}</span>
          <button class="kill" data-kill="${c.name}" title="删除">✕</button>
        </li>`
      )
      .join("") || `<li class="empty">尚未创建知识库</li>`;
}

const displayName = (name) => name.replace(/^kb_/, "").replace(/_/g, " ");

async function createCollection(rawName) {
  const name = (rawName || el("newKbName").value || "").trim() || `知识库 ${state.collections.length + 1}`;
  const res = await api("/api/collections", { name });
  el("newKbName").value = "";
  await refreshState();
  el("importHint").textContent = `已切换到 ${displayName(res.active)}`;
}

async function switchCollection(name) {
  await api("/api/active", { name });
  await refreshState();
  el("results").innerHTML = "";
  el("searchMeta").textContent = "";
  el("importHint").textContent = `当前知识库：${displayName(name)}`;
}

async function deleteCollection(name) {
  if (!confirm(`删除 ${displayName(name)} 及其全部内容？`)) return;
  await api("/api/collections/delete", { name });
  await refreshState();
}

/* ---------- wiring ---------- */

function wire() {
  const zone = el("dropzone");
  const input = el("fileInput");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", async (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    await handleFiles([...e.dataTransfer.files]);
  });
  input.addEventListener("change", async () => {
    await handleFiles([...input.files]);
    input.value = "";
  });

  el("loadSample").addEventListener("click", async () => {
    try {
      const corpus = await fetch("/samples/corpus.json").then((r) => r.json());
      await importDocuments(corpus);
    } catch (err) {
      el("importHint").textContent = `示例语料加载失败：${err.message}`;
    }
  });

  el("dtype").addEventListener("change", async (e) => {
    const dtype = e.target.value;
    el("importHint").textContent = "正在切换模型精度…";
    try {
      await loadModel(dtype);
      el("importHint").textContent =
        `已切换为 ${DTYPE_LABEL[state.dtype]} · ${state.backend === "webgpu" ? "WebGPU" : "WASM"} · 加载 ${(state.loadMs / 1000).toFixed(2)} s`;
    } catch (err) {
      el("importHint").textContent = `模型加载失败：${err.message}`;
    }
  });

  el("searchForm").addEventListener("submit", runSearch);
  el("createKb").addEventListener("click", () => createCollection().catch((e) => (el("importHint").textContent = e.message)));

  el("kbList").addEventListener("click", async (e) => {
    const kill = e.target.closest("[data-kill]");
    if (kill) {
      await deleteCollection(kill.dataset.kill);
      return;
    }
    const item = e.target.closest(".kb-item");
    if (item) await switchCollection(item.dataset.name);
  });
}

async function handleFiles(files) {
  const docs = [];
  for (const file of files) {
    const text = await file.text();
    docs.push({ title: file.name.replace(/\.[^.]+$/, ""), source: file.name, text });
  }
  await importDocuments(docs);
}

async function boot() {
  wire();
  await refreshState();
  try {
    await loadModel("auto");
    el("importHint").textContent = `模型就绪 · ${state.backend === "webgpu" ? "WebGPU" : "WASM"} · ${DTYPE_LABEL[state.dtype]} · 加载 ${(state.loadMs / 1000).toFixed(2)} s`;
  } catch (err) {
    el("importHint").textContent = `模型加载失败：${err.message}`;
  }
}

boot();
