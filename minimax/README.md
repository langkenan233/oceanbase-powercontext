# 规有所依 —— 面向软件持续演进的 SDD 上下文管理系统

MiniMax × OceanBase 赛题作品。

上下文模块根据一个开发任务，找出适用的规格、相关的代码和验证用的测试，把它们组装成
一份开发上下文，交给使用 MiniMax 的开发助手完成代码修改，并把任务进展保存在
PowerContext 中，供下一次运行继续使用。

## 目录

```
minimax/
├── README.md                 本文件：运行说明
├── 验证样例.md                逐步验证样例
├── 技术使用记录.md            MiniMax、seekdb、PowerContext 三项技术的使用记录
├── 技术说明.md                一页技术说明
├── requirements.txt          第三方依赖清单
├── sddctx/                   上下文模块源码
├── scripts/                  启动、测试、验证与记录脚本
├── vendor/                   随作品交付的 PowerContext 运行期源码
├── workload/                 业务工程、规格、修改前后快照
├── env/                      环境文件模板
└── logs/                     测试日志与技术使用记录
```

## 一、技术选型与版本

| 技术 | 版本 | 接入方式 | 在本次任务中承担的作用 |
| --- | --- | --- | --- |
| MiniMax | `MiniMax-M3.1-Flash`（产出补丁的模型）；`MiniMax-M2`（模块默认模型） | MiniMax 官方 OpenAI 兼容接口 `https://api.minimax.chat/v1`，由 `sddctx/minimax_client.py` 调用 | 依据上下文产出代码修改 |
| seekdb | `seekdb-v1.4.0.0`，官方镜像 `oceanbase/seekdb:latest` | MySQL 协议，`pymysql` 直连 `127.0.0.1:2881` | 存储规格、代码、测试三类工程资料，检索结果用于构造上下文 |
| PowerContext | API 版本 `1.2.0`；源码取自 [oceanbase/powercontext](https://github.com/oceanbase/powercontext) 的 `2026-competition` 分支，commit `b3106895761a964056edded6964f4ecf9032b133` | HTTP API，`httpx` 调用 `http://127.0.0.1:8000/v1` | 保存任务进展与验证结果，重启后读取并用于后续上下文 |

辅助依赖：Python 3.11 以上（开发环境 3.13.12）。
`seekdb` 与 PowerContext Server 都通过 Docker 或本地进程提供。

PowerContext 的运行期源码随作品一起提交在 `minimax/vendor/`，与上游逐文件一致、
未做修改，来源与理由见 `minimax/vendor/README.md`。赛题指定的
`2026-competition-ecosystem` 分支只有各参赛方目录、没有 `src/`，题面要求「能够
运行」这一项不依赖未交付的本地文件或状态，所以这里把 Server 需要的源码一并提交。

## 二、准备环境

### 1. 依赖

```bash
python -m pip install -r minimax/requirements.txt
```

清单里分两部分：`pymysql`、`httpx`、`pytest` 供上下文模块使用，其余是随作品交付的
PowerContext Server 需要的三方库。每个包的版本区间取自上游 PowerContext 的
`pyproject.toml`，注释里是本次实测通过的版本。

### 2. seekdb

```bash
docker run -d --name seekdb-ctx -p 2881:2881 -e TZ=Asia/Shanghai oceanbase/seekdb:latest
```

等待日志出现 `SQL ready` 后，容器监听 `2881`，账号 `root`，空密码。

### 3. PowerContext Server

Server 源码随作品提交在 `minimax/vendor/powercontext/`，直接用仓库内的启动脚本：

```bash
python minimax/scripts/start_server.py run --env-file minimax/powercontext.env
```

脚本会在 `minimax/vendor/` 里找 PowerContext 源码（仓库根目录有 `src/` 时也认），
并把 `POWERCONTEXT_HOME` 指向 `minimax/data`，因此任务状态落在
`minimax/data/powercontext.db`，随仓库目录一起走。Server 起来后：

```bash
curl http://127.0.0.1:8000/health/ready
```

`access.mode` 设为 `disabled`，适合本地单评审环境；对外暴露时需要改成 `enforced`
并设置 `POWERCONTEXT_SERVER_AUTH_TOKEN`，同时在 `minimax/sddctx.env` 里填
`POWERCONTEXT_TOKEN`。

> PowerContext 自带的 `oceanbase` 存储后端要求 `ob_compatibility_mode = mysql`，
> seekdb 不提供这个系统变量，所以本作品让 PowerContext 用它默认的 SQLite 存任务
> 状态，seekdb 专职存工程资料。两者职责不同，互不影响。

### 4. 配置文件

模块的配置在 `minimax/sddctx.env`，模板在 `minimax/env/sddctx.env.example`。

```bash
cp minimax/env/sddctx.env.example minimax/sddctx.env
```

需要填写的项只有 `MINIMAX_API_KEY`，其余项按本机默认值即可。这个文件带凭据，
已在 `.gitignore` 里，不会进版本库；不创建它也能用，模块会退回内置默认值
（`127.0.0.1:2881`、`http://127.0.0.1:8000`、工作负载目录 `minimax/workload`）。

PowerContext Server 的环境文件是 `minimax/powercontext.env`，里面没有凭据，
模板在 `minimax/env/powercontext.env.example`。

## 三、初始化数据

```bash
PYTHONPATH=minimax python -m sddctx init --reset --env-file minimax/sddctx.env
```

命令会在 seekdb 中建库 `sdd_context`，建三张表（`sdd_spec` / `sdd_code` /
`sdd_test`）和一张导入日志表，并把 `minimax/workload` 下的规格、代码、测试导入
进去。输出：

```json
{
  "imported": {"code": 4, "specifications": 1, "tests": 3},
  "stored": {"code": 4, "spec": 1, "test": 3}
}
```

## 四、调用入口

模块的入口是命令行 `python -m sddctx`，输入输出如下。

| 子命令 | 输入 | 输出 |
| --- | --- | --- |
| `init` | 工作负载目录（默认 `minimax/workload`） | JSON：各类资料的导入条数与库中存量 |
| `context` | 任务编号、任务目标、关键词、条数上限 | JSON：命中的规格 / 代码 / 测试（含来源路径与版本）、已保存的任务状态、上下文摘要；加 `--prompt` 另附渲染后的提示词全文 |
| `state save` | 任务编号、目标、已完成项、待办项、验证结果 | JSON：写入 PowerContext 后的 Scope 与返回体。`--verification` 传 JSON 对象（`{"name":"target_tests_before","result":"6 failed"}`）或纯文本都行，纯文本会存成 `{"result": "..."}` |
| `state show` | 任务编号 | JSON：PowerContext 中保存的任务状态 |
| `patch` | 同 `context`，外加 `--output` | JSON：模型、base_url、usage、耗时、上下文摘要；补丁写入 `--output`，提示词与调用记录写入 `--trace-dir` |
| `apply` | 补丁文件路径 | JSON：补丁是否应用成功、触及的文件 |
| `verify` | 任意命令 | JSON：命令、退出码、标准输出与错误输出尾部 |

常用调用：

```bash
# 组装上下文
PYTHONPATH=minimax python -m sddctx context --env-file minimax/sddctx.env \
  --task-id sdd-task-001 \
  --objective "Implement SPEC-001: report a clamped line range when the requested end exceeds the content length" \
  --keyword read_range --keyword source_range_clamped --keyword clamp --keyword limitations \
  --limit 5 --prompt

# 让 MiniMax 产出补丁（需要 MINIMAX_API_KEY）
PYTHONPATH=minimax python -m sddctx patch --env-file minimax/sddctx.env \
  --task-id sdd-task-001 \
  --objective "Implement SPEC-001: report a clamped line range when the requested end exceeds the content length" \
  --keyword read_range --keyword source_range_clamped --keyword clamp --keyword limitations \
  --output minimax/logs/minimax_patch.diff

# 应用补丁
PYTHONPATH=minimax python -m sddctx apply --env-file minimax/sddctx.env \
  --patch minimax/logs/minimax_patch.diff
```

## 五、测试命令

业务工程的测试在 `minimax/workload/app` 下，`PYTHONPATH` 指向该目录。

```bash
# 目标功能测试（改动前失败，改动后通过）
PYTHONPATH=minimax/workload/app python -m pytest \
  minimax/workload/app/tests/test_reader_range_clamp.py -v

# 回归测试（改动前后都通过）
PYTHONPATH=minimax/workload/app python -m pytest \
  minimax/workload/app/tests/test_reader.py \
  minimax/workload/app/tests/test_indexer.py -v

# 模块自身的测试
PYTHONPATH=minimax python -m pytest minimax/tests -v
```

带日志的封装脚本会把命令、退出码和代码版本写进 `minimax/logs/`：

```bash
python minimax/scripts/run_tests.py target-before   # 日志 minimax/logs/target-before.log
python minimax/scripts/run_tests.py target-after    # 日志 minimax/logs/target-after.log
python minimax/scripts/run_tests.py regression      # 日志 minimax/logs/regression.log
```

一条命令跑完整验证序列（退回改动前 → 记录失败 → 应用补丁 → 记录通过 → 回归）：

```bash
python minimax/scripts/run_verification.py
```

结果写入 `minimax/logs/verification.log`。

## 六、复现本次开发

```bash
# 1. 退回改动前的状态
python minimax/scripts/reset_workload.py --state before

# 2. 目标测试失败（退出码 1）
python minimax/scripts/run_tests.py target-before

# 3. 应用 MiniMax 产出的补丁
PYTHONPATH=minimax python -m sddctx apply --env-file minimax/sddctx.env \
  --patch minimax/logs/minimax_patch.diff

# 4. 目标测试与回归测试通过（退出码 0）
python minimax/scripts/run_tests.py target-after
python minimax/scripts/run_tests.py regression
```

## 七、技术使用记录的生成脚本

```bash
python minimax/scripts/record_seekdb.py         # minimax/logs/seekdb_queries.log
python minimax/scripts/record_powercontext.py   # minimax/logs/powercontext_state.log
python minimax/scripts/record_context.py        # minimax/logs/development_context.json、development_prompt.md
python minimax/scripts/record_minimax.py        # minimax/logs/minimax_call.json
python minimax/scripts/verify_spec_change.py    # minimax/logs/spec_change.log
```

交给 MiniMax 的上下文是在业务工程处于**改动前**状态时组装的，检索到的 `reader.py`
是静默截断的那一版，提示词里给出的是待实现的行为和待改的代码，不是答案本身。
按同样的步骤可以复现这份上下文与提示词：

```bash
python minimax/scripts/reset_workload.py --state before
PYTHONPATH=minimax python -m sddctx init --reset --env-file minimax/sddctx.env
python minimax/scripts/record_context.py
```

摘要为 `207b405b4f8f5227`。直接在交付出来的树上跑 `context`（业务工程处于改动后状态）
得到的摘要是 `e5fabcf1dfbc6931`，两个摘要对应的是两份不同的资料内容。

## 八、已知限制

1. seekdb 的检索用 `LIKE` 关键词匹配，没有向量检索。工程资料规模大时需要换用
   PowerContext 自带的代码索引能力。
2. PowerContext Server 需要单独启动，模块不会替它拉起进程。模块在 Server 不可达
   时降级为「无状态」，`context` 输出里 `state` 为 `null`，其余功能不受影响。
3. `patch` 子命令需要 `MINIMAX_API_KEY`。没有配置时该命令报
   `MINIMAX_API_KEY is not configured`，其余子命令不受影响。
4. 任务状态以 JSON 存入 PowerContext 的 Memory 条目，单条上限 8192 字节，超出时
   `state save` 会明确报错，而不是静默截断。
5. `apply` 子命令用 `git apply` 落补丁，要求补丁以 `diff --git` 开头、路径相对于
   仓库根目录。
6. 模块自身的测试里，依赖 seekdb 与 PowerContext 的两项在服务不可达时自动跳过，
   其余用例不依赖任何外部服务。
7. `minimax/vendor/powercontext/` 是上游源码的完整副本（约 7 MB、427 个文件）。
   它保证评审在干净环境里能直接启动 Server，代价是提交体积变大。
