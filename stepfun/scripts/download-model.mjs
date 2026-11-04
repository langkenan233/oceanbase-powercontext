// Fetch the embedding model files a browser build needs. The files land under
// models/<id>/ so the app can serve them from its own origin and the browser
// never has to reach a model hub at runtime.
import { createWriteStream } from "node:fs";
import { mkdir, stat } from "node:fs/promises";
import { dirname, join } from "node:path";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";

const MODEL_ID = process.env.MODEL_ID || "Xenova/bge-small-zh-v1.5";
const MIRROR = process.env.MODEL_MIRROR || "https://hf-mirror.com";
const OUT_ROOT = process.env.MODEL_DIR || "models";
const LOCAL_NAME = process.env.MODEL_NAME || MODEL_ID.split("/").pop();

// The app needs two variants: FP16 for the GPU path and INT8 for the CPU path.
// FP32 is only the reference point for the size and quality comparison, so it
// is fetched on request (FETCH_FP32=1) rather than by default.
const FILES = [
  "config.json",
  "tokenizer.json",
  "tokenizer_config.json",
  "onnx/model_fp16.onnx",
  "onnx/model_quantized.onnx",
  ...(process.env.FETCH_FP32 === "1" ? ["onnx/model.onnx"] : []),
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function head(url) {
  const res = await fetch(url, { method: "HEAD", redirect: "follow" });
  return { ok: res.ok, status: res.status, size: Number(res.headers.get("content-length") || 0) };
}

async function download(url, dest) {
  let lastError = "";
  for (let attempt = 1; attempt <= 8; attempt++) {
    try {
      const res = await fetch(url, { redirect: "follow" });
      if (res.status === 429) {
        const wait = 15000 * attempt;
        console.log(`  rate limited, waiting ${wait / 1000}s (attempt ${attempt})`);
        await sleep(wait);
        continue;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await mkdir(dirname(dest), { recursive: true });
      await pipeline(Readable.fromWeb(res.body), createWriteStream(dest));
      const { size } = await stat(dest);
      // Small config files are legitimate; only an empty or near-empty body
      // means the transfer went wrong.
      if (size < 64) throw new Error(`truncated: ${size} bytes`);
      return size;
    } catch (err) {
      lastError = err.message;
      console.log(`  failed (${err.message}), retrying in 10s`);
      await sleep(10000);
    }
  }
  throw new Error(`cannot download ${url}: ${lastError}`);
}

const target = join(OUT_ROOT, LOCAL_NAME);
console.log(`model ${MODEL_ID} -> ${target}`);

for (const file of FILES) {
  const dest = join(target, file);
  const remote = `${MIRROR}/${MODEL_ID}/resolve/main/${file}`;
  const existing = await stat(dest).catch(() => null);
  if (existing && existing.size > 1024) {
    console.log(`${file} already present (${(existing.size / 1048576).toFixed(1)} MB)`);
    continue;
  }
  const size = await download(remote, dest);
  console.log(`${file} ${(size / 1048576).toFixed(1)} MB`);
}

console.log("done");
