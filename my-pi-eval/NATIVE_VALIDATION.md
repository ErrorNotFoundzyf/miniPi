# 原生评估接入验证记录

日期：2026-11-18。

## 已通过

- Python 3.12.12、原项目锁定依赖：tests/eval/test_native_headless.py 共 3 项通过。
- 独立 Harbor 0.26.0 环境：tests/eval/test_installed_agent.py 与 test_cli_runner.py 共 9 项通过。
- 原生执行链实际运行了 write → read → edit → bash；最终文件内容、会话持久化、事件日志和工具模块来源均已检查。
- 原生工具来源为 my_coding_agent.tools.*；LLM 使用离线脚本替身，未请求真实模型。
- Harbor 的真实 TaskConfig 模型接受 native-smoke/task.toml。
- uv lock --check --offline 通过；uv.lock 仅新增 Pillow 的两项直接依赖声明，原锁定版本和包来源保留。
- 新增和调整的 Python 文件通过 Ruff。
- Windows 预检可正常输出；缺少 Docker 时以非零状态退出，不继续启动评估。

## 尚未验证

- 本机未找到 Docker，尚未构建题目容器，安装适配器只经过命令与上传流程的离线测试。
- 未找到原报告使用的外部题集目录；题集版本与运行机器待指定。
- 没有评估专用 .env，模型路由与凭据配置位置待指定；未读取个人 auth.json。
- 未调用真实模型，没有生成新的 Terminal-Bench reward、通过率或费用。

## 修改范围

新增 headless.py、installed.py、native-smoke 接线题及回归测试；CLI 增加 --mode native，使用 Harbor 0.26 的 --agent 参数。
Pillow 是原生读取工具已经导入但此前漏声明的运行依赖，本次补入主项目依赖。
原有宿主侧桥接适配器保留；本次未重写原生工具实现，也未合并旧报告中的成功题目。

运行方式见 [NATIVE_EVALUATION.md](NATIVE_EVALUATION.md)。
