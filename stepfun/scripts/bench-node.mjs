// Evaluation harness: builds a throwaway collection from the sample corpus and
// compares retrieval strategies against hand-labelled queries. Queries are
// split into a semantic group and a keyword group, because the two halves of a
// hybrid query are good at different things.
import { pipeline, env } from "@huggingface/transformers";
import { readFile } from "node:fs/promises";

const BASE = process.env.BASE || "http://127.0.0.1:5173";
const COLLECTION = process.env.BENCH_COLLECTION || "bench";
const DTYPE = process.env.BENCH_DTYPE || "q8";

env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = "models/";

const SEMANTIC = [
  ["浏览器不支持显卡时怎么降级", ["推理后端如何选择与降级"]],
  ["怎么让模型变小", ["INT8 量化的原理", "FP16 半精度推理"]],
  ["两个文本相似度怎么算", ["余弦距离与内积的关系"]],
  ["为什么要除以模长", ["为什么要对向量做归一化"]],
  ["长文档切太碎怎么办", ["重叠窗口的作用"]],
  ["导入后搜不到新内容", ["索引刷新与检索可见性"]],
  ["重复导入同一个文件会不会重复", ["增量写入与幂等性"]],
  ["embedding 放在浏览器算安全吗", ["端侧推理与隐私"]],
  ["十万条数据要多少内存", ["向量数量增长后的容量规划"]],
  ["怎么评估检索准不准", ["近似检索的召回率评估"]],
  ["多个知识库怎么隔离", ["多知识库与集合隔离"]],
  ["图片能不能搜", ["图片内容的扩展方向"]],
  ["为什么两路检索比一路好", ["混合检索为什么常优于单路"]],
  ["数据库怎么部署才不用装服务", ["嵌入式数据库与客户端服务器模式的区别"]],
  ["向量多了内存怎么算", ["向量数量增长后的容量规划"]],
];

const KEYWORD = [
  ["RRF 的 rank_constant 怎么取", ["倒数排名融合 RRF"]],
  ["SharedArrayBuffer 需要什么响应头", ["WASM 多线程与 SharedArrayBuffer"]],
  ["HNSW 的 ef_construction 参数", ["HNSW 索引的基本结构"]],
  ["BM25 的文档长度归一化", ["BM25 打分的基本思路"]],
  ["bge-small-zh-v1.5 的向量维度", ["bge 系列中文嵌入模型"]],
  ["COOP 和 COEP 是什么", ["WASM 多线程与 SharedArrayBuffer"]],
  ["INT8 量化需要校准集吗", ["INT8 量化的原理"]],
  ["ngram_token_size 参数", ["ngram 分词与适用场景"]],
  ["ik_mode 取 smart 还是 max_word", ["中文分词器 IK 的特点"],
  ],
  ["nDCG 和 MRR 的区别", ["排序质量的常用指标"]],
];

const LABELS = [
  ...SEMANTIC.map(([q, t]) => ({ q, t, group: "semantic" })),
  ...KEYWORD.map(([q, t]) => ({ q, t, group: "keyword" })),
];

async function post(path, body) {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const corpus = JSON.parse(await readFile("samples/corpus.json", "utf8"));
console.log(`corpus ${corpus.length} docs · ${LABELS.length} queries · dtype=${DTYPE}`);

const extractor = await pipeline("feature-extraction", "bge-small-zh-v1.5", { device: "cpu", dtype: DTYPE });
const encode = async (texts) => {
  const out = await extractor(texts, { pooling: "cls", normalize: true });
  const [rows, cols] = out.dims;
  return Array.from({ length: rows }, (_, r) => Array.from(out.data.slice(r * cols, (r + 1) * cols)));
};

const t0 = Date.now();
const docVecs = await encode(corpus.map((d) => d.text));
const encodeMs = Date.now() - t0;
console.log(`embedded ${corpus.length} docs in ${encodeMs} ms (${(corpus.length / (encodeMs / 1000)).toFixed(1)} docs/s)`);

try {
  await post("/api/collections/delete", { name: COLLECTION });
} catch {}
await post("/api/collections", { name: COLLECTION });const rows = corpus.map((doc, i) => ({
  id: `bench-${i}`,
  document: doc.text,
  embedding: docVecs[i],
  metadata: { title: doc.title, source: doc.source, chunk: 0, chunks: 1 },
}));
const written = await post("/api/chunks", { rows });
console.log(`wrote ${written.written} rows in ${written.writeMs} ms, refresh ${written.refreshMs} ms`);

const queryVecs = await encode(LABELS.map((l) => l.q));

async function run(mode, extra = {}) {
  const stats = { semantic: [], keyword: [] };
  let mrrTotal = 0;
  const misses = [];
  for (let i = 0; i < LABELS.length; i++) {
    const label = LABELS[i];
    const res = await post("/api/search", {
      text: mode === "hybrid" ? label.q : "",
      embedding: queryVecs[i],
      limit: 10,
      ...extra,
    });
    const titles = res.hits.map((h) => h.metadata?.title || "");
    const hitRank = titles.findIndex((t) => label.t.includes(t));
    const top5 = hitRank >= 0 && hitRank < 5;
    stats[label.group].push(top5 ? 1 : 0);
    if (hitRank >= 0) mrrTotal += 1 / (hitRank + 1);
    if (!top5) misses.push(`[${label.group}] ${label.q} -> ${titles.slice(0, 2).join(" / ")}`);
  }
  const rate = (arr) => (arr.length ? (arr.reduce((a, b) => a + b, 0) / arr.length) * 100 : 0);
  return {
    semantic: rate(stats.semantic),
    keyword: rate(stats.keyword),
    all: rate([...stats.semantic, ...stats.keyword]),
    mrr: mrrTotal / LABELS.length,
    misses,
  };
}

const combos = [
  { label: "vector only", mode: "vector" },
  { label: "hybrid rrf k=2 pool=10", mode: "hybrid", rrfK: 2, ftsPool: 10 },
];

console.log("");
const results = [];
for (const combo of combos) {
  const { label, mode, ...extra } = combo;
  const r = await run(mode, extra);
  results.push({ label, r });
  console.log(
    `${label.padEnd(20)} semantic=${r.semantic.toFixed(0).padStart(3)}%  keyword=${r.keyword
      .toFixed(0)
      .padStart(3)}%  all=${r.all.toFixed(0).padStart(3)}%  MRR=${r.mrr.toFixed(3)}`
  );
}

for (const { label, r } of results) {
  console.log(`\n${label} misses:`);
  for (const m of r.misses) console.log("  " + m);
}

