# AgentPermit

AgentPermit 是一个运行在本机、面向单用户的 AI Agent 工具治理网关。它位于标准 MCP 客户端与 Agent 想要访问的文件或命令之间，提供策略判断、人工审批、有界执行、快照和可审计 Dashboard。

它面向开发者工作站，不是容器、操作系统沙箱、多用户服务或托管控制平面。

## 功能

- 为 Claude Code、Codex 及兼容客户端提供标准 MCP stdio 集成。
- 每次运行复制独立 workspace，不直接修改源代码目录。
- 结构化文件和命令工具，支持 argv 前缀策略和有界输出。
- 带稳定请求指纹、原子状态更新和一次性消费的审批记录。
- SQLite 持久化运行、决策、审批、工具结果和快照证据。
- Dashboard 展示创建、修改、删除、二进制和超大文件的前后快照差异。
- 仅监听回环地址的 HTML Dashboard，审批表单带审核人、理由和 CSRF 防护。
- 保留确定性的 scripted agent 用于演示和评测；公开集成路径是标准 MCP。

![AgentPermit 已完成运行，展示创建、修改和删除文件的快照证据](https://raw.githubusercontent.com/spacesky-cell/agentpermit/main/docs/assets/dashboard-completed-run.png)

## 安装

AgentPermit 目前处于公开采用准备阶段，尚未发布到 npm。可以在仓库目录构建并安装准确的 npm 产物：

```powershell
npm pack
npm install --ignore-scripts .\agentpermit-0.2.0.tgz
npx --no-install agentpermit --help
```

启动器需要 Node.js 18+ 和 Python 3.10+。在发布任务完成前，包版本保持 `0.2.0`。

## 三分钟快速开始

在仓库目录通过已安装的 npm 启动器运行确定性示例：

```powershell
npx --no-install agentpermit --home .demo run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --auto-approve
npx --no-install agentpermit --home .demo runs
npx --no-install agentpermit --home .demo serve --port 8765
```

打开 <http://127.0.0.1:8765>，即可查看运行状态、策略轨迹、审批决定和快照证据。原始 `examples\sample_repo` 不会被修改。

使用标准 MCP：

```powershell
npx --no-install agentpermit --home .demo mcp `
  --source examples\sample_repo `
  --task "检查仓库"
```

第一次 `tools/call` 会创建受治理运行。被策略拦截的调用会返回稳定的待审批 id；在 Dashboard 或命令行批准后，重试完全相同的 MCP 调用。

## 标准 MCP 配置

公开集成方式是标准 MCP stdio。请使用客户端实际支持的命令格式。

Claude Code（项目级）：

```powershell
claude mcp add --scope project agentpermit -- npx --no-install agentpermit --home . mcp --source . --task "治理此工作区"
```

Codex 项目级 `.codex/config.toml` 配置：

```toml
[mcp_servers.agentpermit]
command = "npx"
args = ["--no-install", "agentpermit", "--home", ".", "mcp", "--source", ".", "--task", "治理此工作区"]
```

如果客户端找不到 `npx`，可以改用 npm 可执行文件的绝对路径。不要把 `--auto-approve` 放进客户端配置；它只适用于明确可信的本地演示服务进程。

协议顺序为 `initialize`、`notifications/initialized`、`tools/list`、`tools/call`。详见 [docs/MCP_STDIO.md](docs/MCP_STDIO.md)。

## 审批与 Dashboard

网关在工具执行前做策略判断。默认情况下写入和 patch 需要审批。`http://127.0.0.1:8765` Dashboard 提供：

1. 运行状态和任务元数据。
2. 审批请求、经过脱敏的参数、审核人和理由。
3. 策略、审批、工具执行事件筛选。
4. 创建、修改、删除文件的快照计数和有界差异。

命令行等价操作必须使用与运行相同的 `.demo` home：

```powershell
npx --no-install agentpermit --home .demo approvals --run-id <run_id>
npx --no-install agentpermit --home .demo approve <approval_id> --approver reviewer --reason "已审核准确请求"
npx --no-install agentpermit --home .demo reject <approval_id> --approver reviewer --reason "已拒绝准确请求"
npx --no-install agentpermit --home .demo show <run_id>
npx --no-install agentpermit --home .demo export <run_id> --format html --out report.html
```

## 架构

```text
Claude Code / Codex / MCP 客户端
              |
          标准 MCP stdio
              v
          RuntimeGateway
        /      |       \
     Policy  Approval  AuditStore
       |        |          |
   ToolExecutor ---- WorkspaceManager
              |
         快照 + Dashboard
```

网关拥有治理语义；MCP 服务和 scripted agent 只是适配器。边界和生命周期见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 安全边界

AgentPermit 的 Dashboard 只绑定回环地址，面向本地单用户。复制的 workspace 是组织边界，不是容器或操作系统沙箱。同一用户的进程仍可篡改本地状态；允许执行的命令仍可按操作系统权限访问主机文件系统和网络。脱敏和 protected globs 只是纵深防御，不是 DLP。使用敏感仓库前请阅读 [SECURITY.md](SECURITY.md)。

## 开发

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m build
npm test
npm pack --dry-run
```

变更、测试和漏洞披露流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。项目使用 MIT 许可证，详见 [LICENSE](LICENSE)。

## 文档

- [架构](docs/ARCHITECTURE.md)
- [标准 MCP stdio](docs/MCP_STDIO.md)
- [确定性演示](docs/DEMO_SCRIPT.md)
- [安全政策](SECURITY.md)
- [贡献指南](CONTRIBUTING.md)
