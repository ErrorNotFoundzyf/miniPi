# 原生 Agent 回归评测

这套工具解决的是「已经出现的问题能否稳定复现，以及下一版是否修好」，不替代 Terminal-Bench，也不在普通用户任务上自动触发。所有操作从仓库根目录执行。

| 模式 | 执行内容 | 能证明什么 |
| --- | --- | --- |
| `offline` | 给原生 `CodingAgent` 固定模型动作，或直接探测原生工具的取消边界 | 不依赖模型，检查运行时行为是否符合契约 |
| `live` | Harbor 给真实模型一个全新任务容器，模型自行决定工具与步骤 | 指定模型、任务和环境下的实际完成情况；与离线分数分开 |

## 跑仓库自带用例

```powershell
uv run python my-pi-eval/scripts/run_regression.py run --output .my_agent_core/regression/offline-01
```

自带的 `fresh-verification` 是**合成用例**，不是已确认的线上事故。两个固定路径分别验证：绿灯后写入变成 `stale`；编辑后重新验证才恢复为 `passed`。另一个硬检查把最终文件的 SHA-256 与最后一次成功验证时打印的 SHA-256 对照，避免只看 `passed` 就认为当前产物得到验证。

`bash-pipefail` 来自[会话日志排查案例 3](../docs/coding/09-session-log-debugging-cases-会话日志排查案例.md)记录的真实失败：`pytest … | tail …` 的前一段失败，Bash 工具却报告成功。用例只保留这个故障，不复制原会话，也不覆盖同一案例里的 Esc 问题。最小 fixture 让上游输出标记、退出 `42`，下游 `cat` 正常退出。评分器检查 Bash 事件里的真实失败与退出码、最终诊断文件的字节，并确认 fixture 没被改写。这里**命令按预期失败，才算用例通过**。

真实模型路径需要独立的 **Harbor 0.26.0** 环境、可用的 Linux Docker 容器、联网安装依赖和专用评测 API Key。见 [`NATIVE_EVALUATION.md`](NATIVE_EVALUATION.md) 的安装说明。将对应的 `*_API_KEY`（可选 `*_BASE_URL`）配置在进程环境中，不要写进 Case 或命令行。

```powershell
uv run python my-pi-eval/scripts/run_regression.py run --mode live `
  --case my-pi-eval/cases/fresh-verification --model deepseek/deepseek-chat `
  --harbor .venv-harbor/Scripts/harbor.exe --trials 3 --output .my_agent_core/regression/live-01
```

Linux/macOS 用 `.venv-harbor/bin/harbor`，并按 Shell 语法换行。每次尝试都是新 Harbor trial，关闭 Harbor 的自动重试，判定脚本不进 Agent 镜像，而是由 Harbor 在执行后注入。`live` 不使用离线脚本动作，也不使用宿主个人会话配置。默认离线每场景跑一次，真实模型每题跑三次；每次运行必须使用一个不存在的输出目录。

## 把真实故障加入用例库

1. 找到已有的 `session.jsonl`，在本地审阅原始消息和事件，选出要复现的 user 消息。先手工写出脱敏、能独立执行的最小题面。
2. 建草稿。下面的 `<...>` 是占位符；`--user-message` 是按 JSONL 中 user 消息出现顺序的 1 起始序号，不是行号。

   ```powershell
   uv run python my-pi-eval/scripts/run_regression.py capture `
     --session <session.jsonl> --user-message 1 --instruction-file <reviewed.md> `
     --output .my_agent_core/regression/drafts/my-failure
   ```

3. 补最小初始文件到 `workspace/`；在 `case.json` 写最终字节断言 `expected_files`，需要守住的输入文件放到 `preserve_files`。可选的 `verify_script` 必须放在 `preserve_files` 中。若检查器输出 `REGRESSION_SHA256 <文件> <哈希>`，可将文件列入 `checkpoint_files`，要求最终字节与验证时一致。离线场景在 `scripts` 中写 `read`、`write`、`edit` 或无参数 `verify` 的动作序列与预期状态；允许真实模型重做时设置 `live: true`。
4. 审查并把 `draft` 改成 `false`，以 `run --case <该用例目录>` 重跑。确认坏版本失败、修复版本通过后，再归档到 `my-pi-eval/cases/<id>/`。默认运行会选用该目录中的相应模式用例。

`capture` 只保存原会话 SHA-256、选中的 user 序号和你提供的新题面，不复制原用户内容、历史工具调用、原始轨迹或本地路径。不会自动脱敏原文，也不会自动执行历史操作。多轮故障请把必要上下文显式重构进脱敏题面和 fixture。

从先前的本地回归尝试建草稿时，也可以把 `--session <session.jsonl>` 换成 `--evidence <attempt>/evidence.json`。CLI 会校验索引引用的会话文件，并把索引的 SHA-256 一起写入草稿。仍需手工提供已脱敏的 `--instruction-file`，审核 fixture 和断言后才能重跑。取消探针没有模型会话，不能用此方式建草稿。

## 复跑 Bash 故障的修复前后对照

本地需要 Bash（Windows 可用 Git Bash）。`--baseline-bash pipefail-pre-fix` 加载并校验修复提交 `9a06e9d` 的父提交 `601067d` 留存的 Bash 工具快照，不要求克隆原 Git 历史。其他运行时代码、评测逻辑和用例保持一致，不切分支、不改工作区，也不等同于运行整个历史版本。

```powershell
$case = 'my-pi-eval/cases/bash-pipefail'
$out = Join-Path '.my_agent_core/regression' ('pipefail-ab-' + [guid]::NewGuid().ToString('N').Substring(0,8))
uv run python my-pi-eval/scripts/run_regression.py run --case $case --trials 3 `
  --baseline-bash pipefail-pre-fix --output (Join-Path $out before)
uv run python my-pi-eval/scripts/run_regression.py run --case $case --trials 3 --output (Join-Path $out after)
uv run python my-pi-eval/scripts/run_regression.py compare `
  (Join-Path $out before/scorecard.json) (Join-Path $out after/scorecard.json)
```

修复前实测 **0/3**（退出码 `1` 是预期结果），当前 Bash **3/3**，对照报告 `+3 passes`。两边都写对了诊断文件，但旧 Bash 的工具事件显示 `isError: false`，且没有报告退出码 `42`，仍然判失败。这是离线运行时证据，不代表真实模型通过了用例；后者需要用 `--mode live` 单独运行。

### 用同一个真实模型跑对照

在有 Docker Linux 容器和 Harbor 0.26 环境的机器上配置专用评测凭据，并把 `MY_PI_EVAL_MODEL` 设为同一个明确的 `provider/model` 路由。建议首次每组跑一次，确认环境和真实模型均可用，再用新的输出目录把每组试验数改为 `3`。两次运行之间不要编辑工作区。

```powershell
$out = Join-Path '.my_agent_core/regression' ('pipefail-live-' + [guid]::NewGuid().ToString('N').Substring(0,8))
$common = @('--mode', 'live', '--case', 'my-pi-eval/cases/bash-pipefail',
            '--model', $env:MY_PI_EVAL_MODEL, '--harbor', '.venv-harbor/Scripts/harbor.exe', '--trials', '1')
uv run python my-pi-eval/scripts/run_regression.py run @common `
  --baseline-bash pipefail-pre-fix --output (Join-Path $out before)
uv run python my-pi-eval/scripts/run_regression.py run @common --output (Join-Path $out after)
uv run python my-pi-eval/scripts/run_regression.py compare `
  (Join-Path $out before/scorecard.json) (Join-Path $out after/scorecard.json)
```

Linux/macOS 使用 `.venv-harbor/bin/harbor`，以同样的参数各执行一次 `run`。旧组只在 `inputs/runtime/` 的冻结快照里换成历史 `bash.py`，Harbor 从该快照安装；新组用当前实现。两组都给模型同一份题面，不播放任何录制的工具调用，由容器外定义的验证器检查真实工具结果和最终文件。退出码 `1` 表示用例未通过，仍可对照；`2` 或没有 `scorecard.json` 表示评测未得到有效结论，需先看预检错误或对应尝试的 `harbor.log`。以实跑结果为准，不把离线的通过次数当作 live 结果。

Windows 运行前确认 `docker`（以及 Docker 的凭据助手）在当前终端的 `PATH` 中。如果 Harbor 的日志出现控制台编码错误，可以在当前 PowerShell 设置 `$env:PYTHONUTF8 = '1'`。原生适配器自己连接模型供应商，Harbor 原样传递模型路由，不负责重新推断供应商或映射凭据。

### 三次试验的实际结果

`deepseek/deepseek-flash` 在独立容器中自主完成同一题面：修复前 **0/3**，修复后 **2/3**，无 `blocked`。旧组的三次尝试都没有在指定 Bash 工具调用中报告退出码 `42`；其中一次虽然写对了诊断文件，仍未通过独立验证。

修复后未通过的那次也写对了诊断文件，但模型在指定管道后追加了 `echo`，使整条命令成功退出。验证器没有看到题面要求的原样命令失败，因此拒绝通过。这个结果区分了 `pipefail` 的运行时修复与模型的指令遵循；三次样本不足以估计长期通过率。

## Windows Bash 取消延迟

`bash-cancellation` 来自同一份案例报告中的另一个真实故障：用户按下 Esc 后，运行中的命令等了近 30 秒才退出。它只验证原生 Bash 工具的 `CancellationToken` 边界，不验证 TUI 按键，也不调用模型：驱动等子进程创建 `ready.txt` 后触发取消，从发信号开始计时；验证器另查工具是否报告中断，以及子进程是否仍写出自然完成标记。未观察到就绪属于 `blocked`，不是通过或取消失败。

```powershell
uv run python my-pi-eval/scripts/run_regression.py run `
  --case my-pi-eval/cases/bash-cancellation --trials 2 --output .my_agent_core/regression/cancel-current
```

用前面的对照命令替换 `--case` 即可对比历史实现。`pipefail-pre-fix` 指向同时修复 Bash 管道与取消的提交之父，因此也适用于这条用例。Windows 早期实测：历史版 **0/2**（两次都自然结束），首次修复版 **1/2**（两次都取消，但一次用时 4.40 秒）。进一步计时发现同一 PID 被 `taskkill` 两次，且单次调用也可能超过 3 秒。

当前实现只请求一次终止，在 Windows 将 `taskkill` 放到工作线程，工具仍须等子进程退出、输出管道关闭才返回中断。Windows + Git Bash 复测 **5/5**，中断返回耗时约 **0.84–1.21 秒**，没有自然完成标记。3 秒门槛不变，五次结果不是对所有机器的延迟保证。证据保存于 `agent/cancellation.json`。

## Bash 超时输出回归

`bash-timeout-output` 是从本地真实会话提炼的公开最小用例，不含原会话指纹、文本或命令。固定脚本先打印诊断，再触发三秒超时。验证器要求同一次 Bash 调用既返回超时失败，也保留此前的诊断；仅写对回执不会通过。PR 检查验证历史基线 `timeout-output-pre-fix` 因缺诊断而失败，并在当前版离线回放中要求通过。它不检查 Windows 管道资源清理；本地对照中观察到的清理警告不属于本用例的通过条件。

## GitHub Actions

离线门禁还运行 TUI Escape 端到端测试：用确定性模型驱动真实 Python 内核启动 Bash 子进程，向真实 TUI 键处理器注入 Esc，经过 stdio JSON-RPC 检查中断结果；子进程不能写出完成标记，TUI 必须显示中断并能接受下一轮输入。这不需要 API 密钥，也不替代 Windows 手动任务的 3 秒时限检查。

| 工作流 | 触发方式 | 如何使用结果 |
| --- | --- | --- |
| [Agent regression](../.github/workflows/regression-ci.yml) | PR、推送到 `main` | 无 API 密钥，也不启动 Docker。跑评测测试、确定性的取消测试，以及 `fresh-verification` / `bash-pipefail` 离线回放。 |
| [Agent regression (manual)](../.github/workflows/regression-manual.yml) · `windows-cancellation` | 手动 | Windows + Git Bash 跑三次，保留 3 秒门槛；仅作为诊断，不阻塞 PR。 |
| 同一手动工作流 · `live-pipefail` | 手动，仅默认分支 | DeepSeek Flash 在修复前后各跑三次；报告用例变化，不把小样本通过率当作 PR 门槛。 |

首次合入时，不要只提交 `.github/workflows/`：离线工作流依赖用例、评测实现、`tests/eval/`、确定性 Bash 取消测试，以及原生无界面入口和它依赖的运行时接口（事件、流式输出、文件改动与取消）。至少在一个干净 checkout 中用锁定依赖跑通上述离线工作流的测试和回放命令，再启用门禁；不要靠本地未跟踪文件补齐依赖。

工作流进入默认分支后，在 GitHub 的 **Actions → Agent regression (manual) → Run workflow** 选择套件。真实模型套件额外需要在 **Settings → Environments** 创建 `regression-live`，将部署分支限制为默认分支，并在该环境添加专用的 `DEEPSEEK_API_KEY`。本机环境变量不会自动传到 GitHub。只有真实模型运行步骤接收密钥，PR 工作流不读密钥。参见 [手动运行工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow) 和 [GitHub 环境保护规则](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)。

运行页面的 **Artifacts** 只保留清单、评分卡和指定的单次判定 JSON（14 天）；不上传会话、原始轨迹、Harbor 日志或源代码快照。确认第一次 PR 检查成功后，再在默认分支的保护规则中将 `Offline regression` 设为必需检查；不要把手动工作流设为必需检查。

## 读报告与做对照

- `manifest.json`：模式、模型、重复数、用例与运行时代码指纹；`inputs/` 保存这次的用例快照，真实模型模式还冻结一份本次运行的源码。
- `attempts/.../result.json`：硬检查、耗时、结果和证据位置。原生 `agent/` 下有 `session.jsonl`、`events.jsonl` 和 `metrics.json`；真实模型尝试还保留 Harbor 的 result 和 verifier 输出。
- `attempts/.../evidence.json`：本地索引，记录会话、工具事件、指标、最终回答或取消探针文件的相对路径和哈希；汇总工具与自动重试事件次数，并索引验证器观察到的**用例声明产物**。它不复制原始内容、不盘点整个工作区，GitHub 工作流也不上传它。
- `scorecard.json`：按场景记录 `pass / fail / blocked` 次数。正常结束但断言不满足是 `fail`；没有可信判定（例如容器、模型、运行器错误）是 `blocked`，不能当成模型答错，也不能从分母里静默丢掉。退出码分别是 `0 / 1 / 2`；混合结果中 `blocked` 优先提示需排查。

```powershell
uv run python my-pi-eval/scripts/run_regression.py compare `
  .my_agent_core/regression/offline-01/scorecard.json `
  .my_agent_core/regression/offline-02/scorecard.json
```

对照要求两次的模式、模型、服务端配置指纹、用例、重复数、宿主 Python 与评测代码一致；运行时代码指纹可以不同。使用 `--baseline-bash` 时还要求两组的原始工作区运行时代码一致，避免误把其他改动算到 Bash 修复上。比较逐场景通过次数，出现未出分的尝试标记 `inconclusive`。真实模型小样本波动不能当成统计显著性；保留每次结果，重新比较时用同一模型版本、环境和样本数。

**安全边界**：只运行已审查的 fixture 和检查器。离线路径限制工具操作范围，但检查器是本机执行的受信任 Python 代码，**不是 OS 沙箱**；真实模型路径在 Docker 中执行，需要把专用评测凭据提供给容器。原始运行迹线可能包含提示、工具输出或敏感路径，输出留在忽略目录，不要提交真实会话、密钥或个人项目文件。此 verifier 检查功能行为，不是抗篡改安全评测。
