// HTTP layer: static assets, the import/search API, and process lifecycle.
import { createServer } from "node:http";
import { createReadStream, existsSync, statSync } from "node:fs";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { Store, EMBED_DIM } from "./db.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const PORT = Number(process.env.PORT || 5173);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".wasm": "application/wasm",
  ".onnx": "application/octet-stream",
  ".txt": "text/plain; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

const ROUTES = [
  ["/", join(ROOT, "public")],
  ["/vendor", join(ROOT, "node_modules", "@huggingface", "transformers", "dist")],
  ["/models", join(ROOT, "models")],
  ["/samples", join(ROOT, "samples")],
];

function resolveStatic(pathname) {
  for (const [prefix, dir] of ROUTES) {
    const within = prefix === "/" ? pathname.startsWith("/") : pathname === prefix || pathname.startsWith(prefix + "/");
    if (within) {
      const rel = normalize(decodeURIComponent(pathname.slice(prefix.length)))
        .replace(/^([/\\])+/, "")
        .replace(/^\.$/, "");
      if (rel.includes("..")) return null;
      const full = join(dir, rel || "index.html");
      if (!full.startsWith(dir + sep) && full !== dir) return null;
      if (existsSync(full) && statSync(full).isFile()) return full;
    }
  }
  return null;
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
  });
  res.end(payload);
}

async function readBody(req, limit = 64 * 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error("payload too large");
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

const store = new Store(ROOT);

const api = {
  "GET /api/state": async () => ({
    ready: store.ready,
    active: store.active,
    collections: await store.listCollections(),
    dimension: EMBED_DIM,
    dataDir: store.dataDir,
    dataDirSource: store.dataDirWhy,
  }),
  "POST /api/collections": async (body) => {
    const created = await store.createCollection(body.name);
    return { ...created, active: store.active, collections: await store.listCollections() };
  },
  "POST /api/active": async (body) => {
    const res = await store.setActive(body.name);
    return { ...res, collections: await store.listCollections() };
  },
  "POST /api/collections/delete": async (body) => {
    const res = await store.dropCollection(body.name);
    return { ...res, collections: await store.listCollections() };
  },
  "POST /api/chunks": async (body) => store.addChunks(body.rows),
  "POST /api/clear": async () => ({ ...(await store.clear()), collections: await store.listCollections() }),
  "POST /api/search": async (body) => {
    if (!Array.isArray(body.embedding) || body.embedding.length !== EMBED_DIM) {
      throw new Error(`query embedding must have ${EMBED_DIM} dimensions`);
    }
    return store.search({
      text: body.text,
      embedding: body.embedding,
      limit: body.limit,
      rrfK: body.rrfK,
      ftsPool: body.ftsPool,
    });
  },
};

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const pathname = url.pathname;

  if (pathname.startsWith("/api/")) {
    const key = `${req.method} ${pathname}`;
    const handler = api[key];
    if (!handler) return sendJson(res, 404, { error: `no route ${key}` });
    try {
      const body = req.method === "GET" ? {} : await readBody(req);
      sendJson(res, 200, await handler(body));
    } catch (err) {
      console.error(`[api] ${key} failed:`, err.message);
      sendJson(res, 500, { error: err.message });
    }
    return;
  }

  const file = resolveStatic(pathname);
  if (!file) {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    res.end("not found");
    return;
  }
  const ext = extname(file).toLowerCase();
  const immutable = pathname.startsWith("/vendor/") || pathname.startsWith("/models/");
  res.writeHead(200, {
    "content-type": MIME[ext] || "application/octet-stream",
    "content-length": statSync(file).size,
    "cache-control": immutable ? "public, max-age=31536000, immutable" : "no-cache",
    // Cross-origin isolation is what unlocks SharedArrayBuffer, which the WASM
    // backend needs for multi-threaded inference.
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-embedder-policy": "require-corp",
  });
  createReadStream(file).pipe(res);
});

store
  .open()
  .then(() => {
    server.listen(PORT, "127.0.0.1", () => {
      console.log(`seekdb data dir : ${store.dataDir} (${store.dataDirWhy})`);
      console.log(`collection      : ${store.active || "(none yet)"}`);
      console.log(`open            : http://127.0.0.1:${PORT}`);
    });
  })
  .catch((err) => {
    console.error("failed to start:", err.message);
    process.exit(1);
  });

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, async () => {
    await store.close();
    process.exit(0);
  });
}
