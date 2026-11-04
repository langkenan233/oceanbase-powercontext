# 本地知识库 · 浏览器端向量化 + seekdb 混合检索

一个纯本地的迷你知识库：文本在浏览器里切分并向量化，向量与原文写入 seekdb，检索由 seekdb 一次调用完成向量与全文的融合排序。

- 推理位置：浏览器。文档向量化与查询向量化都在用户设备上完成，不调用任何云端推理接口。
- 存储与检索位置：seekdb。通过 `seekdb-js` 连接嵌入式实例，集合里保存向量、原文与元数据。
- 融合位置：数据库。`DBMS_HYBRID_SEARCH` 接收一份搜索参数，内部完成全文查询、向量查询与 RRF 融合，应用侧只取最终排序。

## 运行

要求 Node.js 20 或更高版本，以及支持 WebGPU 的最新版 Chrome / Edge。

```bash
npm install
npm start
```

浏览器打开 <http://127.0.0.1:5173>。

仓库内的 `.npmrc` 打开了 `legacy-peer-deps`。原因是 `seekdb` 传递依赖的 `@seekdb/default-embed` 在 `peerDependencies` 中保留了 monorepo 的 `workspace:*` 协议，npm 默认会直接报 `EUNSUPPORTEDPROTOCOL` 并中止安装。

模型文件随仓库提供，启动后从本地 `models/` 目录加载，不访问任何外部网络。若该目录缺失，执行：

```bash
npm run setup          # 下载 FP16 与 INT8 两份
FETCH_FP32=1 npm run setup   # 额外下载 FP32，用于体积与质量对照
```

Windows 下也可以直接双击 `start.bat`，macOS / Linux 用 `./start.sh`。

## 使用

1. 点击「载入示例语料」导入随附的 41 篇中文短文，或把 `.txt` / `.md` 文件拖进导入区。
2. 在检索框输入问题，结果按融合排序展示，命中的关键词会高亮。
3. 左侧可以新建、切换、删除知识库；切换后检索立即落在新集合上。

示例语料覆盖向量检索、浏览器推理、模型量化与数据库主题，导入后即可用下列查询验收：

| 查询 | 观察点 |
| --- | --- |
| `HNSW 的 ef_construction 调多少` | 关键词精确命中，命中词高亮 |
| `浏览器不支持显卡时怎么降级` | 语义命中「推理后端如何选择与降级」 |
| `导入后搜不到新内容` | 命中「索引刷新与检索可见性」 |

## 端侧运行时

| 场景 | 后端 | 精度 | 说明 |
| --- | --- | --- | --- |
| 浏览器支持 WebGPU | WebGPU | FP16 | 默认路径 |
| 不支持 WebGPU，或初始化失败 | WASM | INT8 | 自动降级，界面标签变为橙色 |
| 手动选择 INT8 且存在 WebGPU | WASM | INT8 | 见下方说明 |

界面右上角的标签始终显示当前实际生效的后端与精度；地址栏加 `?device=wasm` 可以在支持 WebGPU 的机器上强制走降级路径。

**INT8 模型在 WebGPU 后端上数值不正确。** 同一段文本编码两次的余弦相似度为 0.671，与无关文本的相似度为 0.751，即无关文本反而更接近。同一份 INT8 模型在 WASM 后端上同指标为 1.000 与 0.507，FP16 在 WebGPU 上为 1.000 与 0.498。因此推理后端与精度按上表配对，选择 INT8 时自动固定到 CPU。

## 数据目录

嵌入式实例需要一个可写的纯 ASCII 路径，且目录必须已存在。程序按以下顺序选择，并在启动时打印实际使用的目录：

1. 环境变量 `SEEKDB_DATA_DIR`
2. 项目内 `data/seekdb`
3. 用户目录 `~/.seekdb-local-kb/data`
4. 系统临时目录 `seekdb-local-kb/data`

项目位于中文路径时，第 2 项会被跳过，落到第 3 项。

## 配置项

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `PORT` | `5173` | HTTP 端口，只监听 127.0.0.1 |
| `SEEKDB_DATA_DIR` | 见上 | 数据目录 |
| `EMBED_DIM` | `512` | 向量维度，需与模型一致 |
| `WRITE_BATCH` | `32` | 单次写入 seekdb 的行数 |

## 评测与复现

```bash
npm run bench
```

`scripts/bench-node.mjs` 会在独立集合中重建索引，用 25 条人工标注的查询对比「仅向量」与「混合检索」的 recall@5 与 MRR。环境变量 `BENCH_DTYPE` 可切换 `fp32` / `fp16` / `q8`。

浏览器内的后端与精度诊断页：<http://127.0.0.1:5173/diag.html?device=webgpu&dtype=fp16>。页面直接给出自身相似度、无关文本相似度、批大小吞吐扫描，用于确认当前后端真的在正确计算。

## 目录结构

```
server/index.mjs   HTTP 与静态资源
server/db.mjs      seekdb 生命周期、集合管理、写入与检索
public/index.html  主界面
public/app.js     切分、端侧推理、导入与检索交互
public/diag.html   后端与精度诊断页
public/styles.css  样式
samples/corpus.json 示例语料
models/            本地模型文件
scripts/           模型下载、评测脚本
docs/技术说明.md    一页技术说明
```

## 隐私边界

推理在浏览器完成，原始文本不需要离开页面就能得到向量。但向量与原文要写入 seekdb 才能检索，因此数据不出机器成立的前提是 seekdb 以嵌入式方式运行在本机，本项目的默认配置即如此。
