// Owns the seekdb instance: startup, collection lifecycle and every statement
// that touches the database. The browser never talks to seekdb directly, and
// this process never computes an embedding.
import { seekdb } from "@oceanbase/seekdb-node";
import { SeekdbClient } from "seekdb";
import { mkdirSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

export const EMBED_DIM = Number(process.env.EMBED_DIM || 512);
const COLLECTION_PREFIX = "kb_";

const isAscii = (p) => /^[\x20-\x7e]+$/.test(p);

// The bundled runtime cannot open a data directory whose path contains
// non-ASCII characters, and it does not create the directory itself. Both are
// handled here so the app starts wherever the project happens to be cloned.
function pickDataDir(projectRoot) {
  const candidates = [];
  if (process.env.SEEKDB_DATA_DIR) candidates.push({ dir: process.env.SEEKDB_DATA_DIR, why: "SEEKDB_DATA_DIR" });
  candidates.push({ dir: join(projectRoot, "data", "seekdb"), why: "project" });
  candidates.push({ dir: join(homedir(), ".seekdb-local-kb", "data"), why: "home" });
  candidates.push({ dir: join(tmpdir(), "seekdb-local-kb", "data"), why: "temp" });
  for (const c of candidates) {
    if (!isAscii(c.dir)) continue;
    try {
      mkdirSync(c.dir, { recursive: true });
      return c;
    } catch {}
  }
  throw new Error(
    "could not find a writable ASCII path for the database; set SEEKDB_DATA_DIR to one"
  );
}

export const nameOf = (raw) => {
  const slug = String(raw || "default")
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 40);
  if (slug.startsWith(COLLECTION_PREFIX)) return slug;
  return COLLECTION_PREFIX + (slug || "default");
};

export class Store {
  constructor(projectRoot) {
    this.projectRoot = projectRoot;
    this.db = null;
    this.client = null;
    this.active = null;
    this.dataDir = null;
    this.ready = false;
  }

  async open() {
    const pick = pickDataDir(this.projectRoot);
    this.dataDir = pick.dir;
    this.dataDirWhy = pick.why;
    this.db = await seekdb.open(pick.dir);
    const conn = this.db.connectionOptions();
    this.client = new SeekdbClient({
      host: "127.0.0.1",
      port: conn.port,
      user: conn.user,
      password: "",
    });
    await this.client.execute("select 1");
    this.ready = true;
    const names = await this.listCollections();
    if (names.length) {
      this.active = names[0].name;
      await this.collection(this.active);
    }
    return this;
  }

  async close() {
    try {
      if (this.client) await this.client.close();
    } catch {}
    try {
      if (this.db) this.db.close();
    } catch {}
  }

  async listCollections() {
    const cols = await this.client.listCollections({ withEmbeddingFunction: false });
    const out = [];
    for (const col of cols) {
      out.push({ name: col.name, count: await col.count().catch(() => 0) });
    }
    out.sort((a, b) => a.name.localeCompare(b.name));
    return out;
  }

  async createCollection(raw) {
    const name = nameOf(raw);
    const col = await this.client.getOrCreateCollection({
      name,
      configuration: {
        hnsw: { dimension: EMBED_DIM, distance: "cosine" },
        fulltextConfig: { analyzer: "ik", properties: { ik_mode: "smart" } },
      },
      embeddingFunction: null,
    });
    this.active = name;
    return { name, count: await col.count() };
  }

  async dropCollection(raw) {
    const name = nameOf(raw);
    await this.client.deleteCollection(name);
    if (this.active === name) this.active = null;
    const rest = await this.listCollections();
    if (!this.active && rest.length) this.active = rest[0].name;
    return { active: this.active };
  }

  async collection(name) {
    const col = await this.client.getCollection({ name: name || this.active, embeddingFunction: null });
    if (!col) throw new Error(`collection ${name} not found`);
    return col;
  }

  async setActive(raw) {
    const name = nameOf(raw);
    await this.collection(name);
    this.active = name;
    return { active: name };
  }

  // Writes rows produced by the browser. Embeddings arrive ready-made; this
  // process never computes one.
  async addChunks(rows) {
    if (!Array.isArray(rows) || rows.length === 0) throw new Error("no rows");
    for (const r of rows) {
      if (!Array.isArray(r.embedding) || r.embedding.length !== EMBED_DIM) {
        throw new Error(`row ${r.id} has no ${EMBED_DIM}-dim embedding`);
      }
    }
    const col = await this.collection();
    const started = Date.now();
    const batch = Number(process.env.WRITE_BATCH || 32);
    let written = 0;
    for (let i = 0; i < rows.length; i += batch) {
      const slice = rows.slice(i, i + batch);
      await col.add({
        ids: slice.map((r) => r.id),
        documents: slice.map((r) => r.document),
        embeddings: slice.map((r) => r.embedding),
        metadatas: slice.map((r) => r.metadata || {}),
      });
      written += slice.length;
    }
    const writeMs = Date.now() - started;
    const refreshStart = Date.now();
    await col.refresh_index();
    const refreshMs = Date.now() - refreshStart;
    return { written, writeMs, refreshMs, count: await col.count() };
  }

  async clear() {
    const name = this.active;
    const info = await this.collection(name);
    const count = await info.count();
    await this.client.deleteCollection(name);
    await this.createCollection(name);
    return { removed: count, active: name };
  }

  // Hybrid search runs entirely inside seekdb: DBMS_HYBRID_SEARCH receives one
  // search parameter document holding both the full-text query and the vector,
  // then fuses them with RRF.
  async search({ text, embedding, limit = 10, rrfK, ftsPool }) {
    const col = await this.collection();
    const k = Math.max(1, Math.min(50, Number(limit) || 10));
    const pool = Math.max(k, Math.min(100, Number(ftsPool) || 10));
    // The keyword half is only reliable at its head, so the constant is kept
    // small: a rank-1 hit dominates instead of the whole list contributing
    // almost equally, which is what a textbook value of 60 would do.
    const rankK = Number(rrfK) > 0 ? Number(rrfK) : 2;
    const options = { nResults: k, include: ["documents", "metadatas", "distances"] };
    const clean = sanitizeFulltext(text);
    if (clean) {
      options.query = { whereDocument: { $contains: clean } };
      options.knn = { queryEmbeddings: [embedding], nResults: pool };
      options.rank = { rrf: { rankConstant: rankK, rankWindowSize: pool } };
    } else {
      options.knn = { queryEmbeddings: [embedding], nResults: k };
    }
    const started = Date.now();
    let res;
    try {
      res = await col.hybridSearch(options);
    } catch (err) {
      // A full-text query openK can reject (for example a stray operator);
      // fall back to the vector half so a search still returns something.
      if (!options.query) throw err;
      delete options.query;
      delete options.rank;
      res = await col.hybridSearch(options);
    }
    const dbMs = Date.now() - started;
    const ids = res.ids?.[0] || [];
    const docs = res.documents?.[0] || [];
    const metas = res.metadatas?.[0] || [];
    const dists = res.distances?.[0] || [];
    const hits = ids.map((id, i) => ({
      id,
      document: docs[i] ?? null,
      metadata: metas[i] ?? null,
      score: dists[i] ?? null,
      rank: i + 1,
    }));
    return { hits, dbMs, fused: Boolean(options.query) };
  }
}

// query_string has its own operators; keep only characters a user query can
// carry safely and let the analyzer see the rest as plain text.
export function sanitizeFulltext(text) {
  if (!text) return "";
  return String(text)
    .replace(/[+\-!(){}\[\]^"~*?:\\\/|&]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 200);
}
