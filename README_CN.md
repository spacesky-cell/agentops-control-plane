# AgentOps Control Plane 中文文档

[English README](README.md)

AgentOps Control Plane 是一个面向 AI Agent 的轻量级运行治理层。它不试图替代 Codex、Claude Code、LangGraph 或 OpenAI Agents SDK，而是位于这些 Agent 后端之上，统一管理工具执行、权限策略、人工审批、审计日志、隔离工作区、快照和运行报告。

## 为什么需要它

很多 Agent Demo 展示的是“LLM 调用工具”。但真正进入生产环境时，企业更关心的是：

- Agent 可以执行哪些命令？
- Agent 可以读写哪些文件？
- 哪些动作必须经过人工审批？
- Agent 具体做了什么，能不能追踪和回放？
- 出问题后能不能审计、复盘、导出证据？
- 多个 Agent 后端能不能共用一套治理规则？

这个项目就是围绕这些问题做的作品级 MVP。

## 核心能力

- 每次运行都会创建隔离 workspace，不直接修改原始项目目录。
- 策略引擎可控制文件读取、文件写入、文本 patch 和 shell 命令。
- 写入和 patch 默认需要人工审批，也支持 demo 场景下自动批准。
- SQLite 审计库记录 run、事件、策略决策、工具调用和审批记录。
- 每次运行前后都会生成 workspace 快照。
- 内置 scripted agent adapter，方便做确定性 demo 和测试。
- 支持 HTML / JSON 运行报告导出。
- 提供本地 Web Dashboard 浏览 run 和 trace。
- 运行时只依赖 Python 标准库，测试环境需要 pytest。

## 快速开始

```powershell
git clone https://github.com/spacesky-cell/agentops-control-plane.git
cd agentops-control-plane
python -m agentops_control_plane run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --auto-approve
```

列出运行记录：

```powershell
python -m agentops_control_plane runs
```

查看某次运行的完整 trace：

```powershell
python -m agentops_control_plane show <run_id>
```

## 人工审批与恢复

不使用 `--auto-approve` 时，`patch_text` 这类中风险操作会暂停并进入审批队列：

```powershell
python -m agentops_control_plane run-script `
  --plan examples\scripted_fix_agent.json `
  --source examples\sample_repo `
  --task "Approval gate demo"
```

查看审批项：

```powershell
python -m agentops_control_plane approvals --run-id <run_id>
```

批准后继续运行：

```powershell
python -m agentops_control_plane approve <approval_id> --approver reviewer
python -m agentops_control_plane resume-script <run_id> `
  --plan examples\scripted_fix_agent.json `
  --approver reviewer
```

## 导出报告

导出 HTML 报告：

```powershell
python -m agentops_control_plane export <run_id> --format html --out report.html
```

导出 JSON 审计数据：

```powershell
python -m agentops_control_plane export <run_id> --format json --out report.json
```

启动本地 Dashboard：

```powershell
python -m agentops_control_plane serve --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

## Demo 场景

示例 Agent 会修复 `examples/sample_repo/math_utils.py` 中的一个 bug：

```python
def add(a, b):
    return a - b
```

Agent 会在隔离 workspace 中将其修复为：

```python
def add(a, b):
    return a + b
```

原始 `examples/sample_repo` 不会被修改。你可以通过 `.agentops/workspaces/<run_id>` 查看 Agent 实际修改后的隔离工作区。

## 公开仓库内容说明

这个仓库面向公开展示，刻意保留了源码、文档、示例和 `tests/` 测试源码。`tests/` 不是测试运行产物，它的作用是让招聘方或 reviewer 能验证项目质量。

不会提交运行生成物和本地隐私文件。`.agentops/`、`.pytest_cache/`、`__pycache__/`、导出的 demo 报告、本地环境文件和日志都已经写入 `.gitignore`。

## 项目结构

```text
Agent backend -> Gateway -> Policy -> Tools -> Isolated workspace
                          -> Audit store
                          -> Approval queue
                          -> Snapshots/reports
```

主要模块：

- `agentops_control_plane/gateway.py`：所有工具调用的统一入口。
- `agentops_control_plane/policy.py`：策略判断和风险分级。
- `agentops_control_plane/tools.py`：受控工具执行器。
- `agentops_control_plane/workspace.py`：隔离 workspace 和快照管理。
- `agentops_control_plane/audit.py`：SQLite 审计存储。
- `agentops_control_plane/agents.py`：deterministic scripted agent adapter。
- `agentops_control_plane/web.py`：本地 Dashboard。
- `agentops_control_plane/evaluator.py`：批量 eval 入口。

## 测试

```powershell
python -m pytest -q
```

端到端 eval：

```powershell
python -m agentops_control_plane eval --tasks examples\tasks.jsonl --auto-approve
```

## 求职简历描述

可以这样写：

> Built a vendor-neutral AgentOps control plane for AI agents with isolated workspaces, policy-based tool execution, approval gates, command/file audit logs, snapshots, trace export, and deterministic evaluation demos.

中文表达：

> 实现了一个面向 AI Agent 的运行治理平台，支持隔离工作区、策略化工具执行、人工审批、命令/文件审计日志、运行快照、trace 导出和确定性评测 demo。

## 后续可扩展方向

- 接入 OpenAI Agents SDK。
- 接入 Claude Code / Codex CLI。
- 增加 MCP tool adapter。
- 增加 Docker、E2B、Modal、Daytona 等 sandbox backend。
- 增加 OpenTelemetry / Phoenix trace export。
- 增加多 Agent 后端对比评测。
- 增加 GitHub PR / CI 修复工作流。
