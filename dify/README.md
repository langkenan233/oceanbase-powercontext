# 恶作剧记忆 NPC

这是一个最小的 Dify + PowerContext 互动作品。玩家向「月亮失物招领处」的 NPC 提议一件恶作剧，NPC 会接梗、给出一条荒诞的后果，并在下一轮提起之前发生的事。Dify 负责输入、记忆读取、模型生成、记忆写入和输出；PowerContext 保存并召回跨运行的互动记录。

## 交付文件

- `prank_npc.yml`：Dify Workflow DSL，唯一需要导入的文件。
- `powercontext_check.py`：创建 Scope，并验证 PowerContext 的记忆读写。
- `验证样例.md`：按步骤体验玩法及检查技术作用。

## 运行环境

建议使用 Dify 1.9 或更新的自托管版本、Python 3.11+、PowerContext `master`（以本文验证时的版本为准），以及 Dify 中可用的 Chat 模型。DSL 默认使用 OpenAI `gpt-4o-mini`；导入后可在「NPC 回应」节点换成自己已配置的 Chat 模型。未使用 Embedding、Rerank 或插件。PowerContext 本地默认 SQLite 持久化。

## 启动与配置

1. 按 [PowerContext 官方安装说明](https://powercontext.oceanbase.io/zh/docs/develop/api-quickstart/) 安装 Server：

   ```bash
   uv tool install --force "powercontext[cli,server] @ git+https://github.com/oceanbase/powercontext.git@master"
   ```

   若 Dify 与 PowerContext 在同一个主机进程网络中，直接执行 `powercontext server run`，并把 DSL 的服务地址改为 `http://127.0.0.1:8000`。若 Dify 在 Docker 容器中、PowerContext 在主机上，须按[部署说明](https://powercontext.oceanbase.io/zh/docs/operate/deploy-server/)配置可信网络和 Bearer 鉴权，例如在只对可信容器开放的环境文件中设置 `POWERCONTEXT_SERVER_HTTP_HOST=0.0.0.0`、`POWERCONTEXT_SERVER_ACCESS_MODE=enforced`、`POWERCONTEXT_SERVER_AUTH_TOKEN=<自行生成的长随机值>`，再执行 `powercontext server run --env-file <环境文件路径>`。不要把无鉴权的 Server 直接监听到外部网络。

2. 确认 `http://127.0.0.1:8000/health/live` 可访问。在同一台机器上运行 `python dify/powercontext_check.py init`，复制输出的 `scope_id`。若启用鉴权，先为脚本进程设置 `POWERCONTEXT_TOKEN`。脚本不要求模型或额外 Python 包。
3. 将 `dify/prank_npc.yml` 中的 `REPLACE_SCOPE_ID` 全部替换为这个 ID。将两处 `http://host.docker.internal:8000` 改成 **Dify 的 API/worker 容器能够访问的** PowerContext 地址。Windows/macOS Docker Desktop 通常可使用示例地址；Linux 或远程 Dify 应使用实际可达地址。PowerContext Server 也必须监听相应网卡，且网络访问应限制在可信环境。
4. 在 Dify「工作室 → 导入 DSL 文件」导入 `prank_npc.yml`。检查两个 HTTP 节点的地址，在「NPC 回应」节点选用已配置的 Chat 模型，然后发布。使用 Dify 的运行页或 Workflow API 作为入口。
5. 如 PowerContext Server 启用了 Bearer 鉴权，在两个 HTTP 节点的 Authorization 中配置密钥。不要把真实密钥写入 DSL 或提交仓库。Dify 需要允许访问该服务地址；若其 SSRF 策略阻止私网地址，应按部署环境的安全配置开放专用地址。
6. 运行 `python dify/powercontext_check.py verify <scope_id>` 可单独验证 PowerContext 记忆链路。运行 Dify 工作流后，用 `python dify/powercontext_check.py recall <scope_id>` 检查玩家记录已保存。

PowerContext 的 Scope 是数据分区键，并非访问控制。这个基础版本使用一个演示 Scope，适合单人或可信评审环境。不同玩家应各用独立 Scope，并由可信后端负责选择 Scope 和鉴权。为重复验证，可再次执行 `init` 创建新的空 Scope，然后更新 DSL 中的 ID。

## 已知限制

需要可用的 Dify 实例、模型凭据和可由 Dify 访问的 PowerContext Server；仓库不包含这些运行资源。DSL 的 HTTP 节点若请求失败，工作流会报错，不会假称已经记住。PowerContext 的默认全文检索使用固定标记 `npc_prank`，因此本作品只检索该 NPC 的历史。
