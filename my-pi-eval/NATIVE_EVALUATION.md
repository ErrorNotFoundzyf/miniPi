# 原生 CodingAgent 容器内评估

## 评估对象

旧入口 `my_pi_eval.agent:MyPiAgent` 在宿主机运行核心 Agent，使用 HarborToolRegistry 的另一套工具。
新入口 `my_pi_eval.installed:MyPiInstalledAgent` 把当前工作树的 Python 包和 uv.lock 上传进每道题的容器，在容器内调用 `my_coding_agent.headless`。
后者直接创建 CodingAgent，复用默认提示词、七个原生工具、文件修改队列、Context、Session 和动态资源加载；不启动交互式 TUI。
权限模式固定为 autonomous，以便无人值守执行。项目资源仍按原生默认规则发现；未复制宿主机的个人记忆、密钥或扩展配置。
这测量的是无界面、自主权限配置下的原生后端，不覆盖 TUI 交互体验。

## 环境准备

- 宿主机：Python 3.12+、uv、Docker Engine（能够运行 Linux 容器）。
- 适配接口固定为 Harbor 0.26.0。
- 容器：POSIX shell、tar、curl；需要联网安装隔离的 Python 3.12 运行时与锁定依赖。
- 准备明确版本的 Terminal-Bench 题集；旧脚本硬编码的外部目录不会自动迁移。
- 在进程环境配置 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY` 或 `ANTHROPIC_API_KEY`，可选相应 `*_BASE_URL`。
  模型路由必须显式传入 `provider/model`。不要把密钥写入命令参数或提交到 Git。

从仓库根目录安装：

```sh
uv venv --python 3.12 .venv-harbor
uv pip install --python .venv-harbor/bin/python -e ./my-pi-eval
```

Windows 将上述 Python 路径换成 `.venv-harbor/Scripts/python.exe`。Harbor 与原生项目的 MCP 依赖范围不同，必须使用这个独立宿主环境；容器内只按原项目 uv.lock 安装 Agent。

CLI 的 `--mode native` 选择新适配器；默认 `host` 保持旧入口。

## 先验证单题

```sh
.venv-harbor/bin/my-pi-eval --check
.venv-harbor/bin/my-pi-eval --mode native -p my-pi-eval/tasks/native-smoke -m deepseek/deepseek-chat -n 1
```

Windows 使用 `.venv-harbor/Scripts/my-pi-eval.exe`。

native-smoke 是本仓库提供的接线测试，只检查容器内的 Agent 能否创建正确文件，不计入 Terminal-Bench 分数。
正式题集试跑时，把 -p 换成已下载题集中的一道题目录。确认安装、模型访问、原生工具、日志和 verifier 全部可用后再运行整套。
新评估需要新的 Harbor job；不要调用旧的“跳过已通过题目”脚本来累计成绩。

## 实际运行链

```text
Harbor → MyPiInstalledAgent.install
       → 上传源码快照与 uv.lock
       → 容器 /installed-agent/my-pi-native/source/.venv 安装原生框架
Harbor → MyPiInstalledAgent.run
       → 容器内 python -m my_coding_agent.headless
       → CodingAgent → 原生 read/write/edit/bash/grep/find/ls
       → Harbor 独立运行 verifier → reward
```

Agent 用绝对路径启动自己的虚拟环境解释器，不把这个虚拟环境添加到题目的 PATH；因此 Bash 调用 python 时仍使用题目环境中的 Python。
运行目录由 Harbor 环境的默认工作目录决定，未硬编码为 /app。
题面通过文件上传，避免 shell 转义改变题意。每次尝试要求新的 session.jsonl，防止意外续用旧会话。

## 结果与复现

- 宿主侧 source-manifest.json：上传源码内容的 SHA256 和 uv 版本。
- /logs/agent/python-version.txt、dependencies.txt：实际隔离运行时版本。
- /logs/agent/session.jsonl、events.jsonl、debug.log：会话与事件。
- /logs/agent/answer.txt：正常完成时的最终文本。
- /logs/agent/metrics.json：停止原因、耗时、迭代数、工具调用及其结果、工具实现模块、已完成助手消息的原始 usage，以及 `model_requests`。

`model_requests` 分别记录正常对话（`conversation`）、Agent 层重试（`retry`）和摘要（`summary`）。每类包含逻辑调用数、支持观测的异步 HTTP 尝试数、其中带 SDK 重试标记的次数，以及服务商返回的 usage 汇总。缺少 HTTP 观测或 usage 的调用分别计数。HTTP 尝试在发送前的传输回调中统计，计费以服务商数据为准。
- Harbor result.json：独立验证结果；Agent 正常退出不代表 reward 为 1。

usage 只覆盖会话中完成的助手消息，不能当成包含摘要调用和失败请求的完整账单。
直接运行 headless 时可用 `--verify-command "python -m pytest -q"` 追踪 Agent 提交给前台 Bash 的完全匹配的命令。`verification.attempts` 按顺序记录匹配结果。验证通过后，原生 `edit` 或 `write` 改变文件字节时，`verification.status` 变为 `stale`，重跑验证后更新。未运行时为 `not_run`。此参数只记录结果，验证命令仍由题面指示 Agent 执行。

交互模式可输入 `/verify-command python -m pytest -q` 设置当前会话要跟踪的命令，然后让 Agent 执行这条命令。输入 `/last-run` 查看最近一轮报告。交互报告存在本次内核运行的当前会话内，跟踪范围是 Agent 的前台原生 `bash`、`edit` 和 `write`。
固定题集版本、模型路由、尝试次数、超时和资源限制，记录完整 job 配置。失败后重跑必须记作新尝试，不得混成单次通过率。
本适配器不删除旧结果、不清理其他 Docker 资源，也不会自动调整题目测试。

## 本地功能对照

在仓库根目录运行。需要 Bash 和本项目的 Python 环境；历史 Bash 工具快照随仓库分发。每次用一个新的输出目录。

```powershell
uv run python my-pi-eval/scripts/run_feature_regression.py --output .my_agent_core/feature-regression/offline
uv run python my-pi-eval/scripts/run_feature_regression.py --model deepseek/deepseek-flash --trials 3 --output .my_agent_core/feature-regression/deepseek
```

第二条命令从当前进程读取 `DEEPSEEK_API_KEY`。脚本在两组分别注入相同的触发步骤，随后由同一个模型完成日志查找和编辑恢复。每次运行最多使用 6 次真实模型流请求，达到评测上限记为 `request_limit`，并保留此前已产生的工具记录。审批三种结果始终用固定的回调和文件字节检查。

- 长日志场景用固定提交 `a867a98` 中的旧 Bash 工具作为基线，检查超时前的报错是否能通过保存的日志找回。
- 编辑场景在基线组关闭当前工具的重复失败跟踪，检查第四次错误编辑的结果以及最后的文件内容。两组共享同一版 headless 运行器。
- `summary.json` 汇总结果并记录基线和源码哈希。每个子目录保留原始工具事件、运行指标、最终输出、工作区和实际落盘的 Bash 日志。

这些合成场景测量指定机制的行为。通过次数仅适用于这组本地回归。

需要以统一 Case 复跑历史问题、生成 scorecard 或做版本对照，请使用 [原生 Agent 回归评测](REGRESSION.md)。
