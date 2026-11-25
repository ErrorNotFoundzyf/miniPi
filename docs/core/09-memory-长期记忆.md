# Memory 长期记忆系统设计规范 (`my_agent_core.memory`)

- **定位**：跨会话受控持久化记忆存储与快照注入引擎 (`src/my_agent_core/memory.py`)
- **核心类**：`MemoryStore`, `make_memory_tool`
- **主要实现**：`memory.py`

---

## 一、架构设计与定位

与记录对话流水的 `session` 不同，`memory` 解决的是**跨会话的长期事实、项目约定与用户偏好沉淀**。
系统对标 Hermes Agent 精简版，采用 **“双 Markdown 存储 + Frozen Snapshot 冻结注入 + 受控条目化工具”** 设计：

```text
               启动装配期 (Agent.__init__)
                         │
                         ▼
        MemoryStore(mem_dir) ➔ load_from_disk()
          ├── 读 MEMORY.md (2200 字符限制) ➔ 捕获冻结快照 _snapshot
          └── 读 USER.md   (1375 字符限制) ➔ 捕获冻结快照 _snapshot
                         │
                         ▼
         _init_messages() 注入 <MEMORY_CONTEXT> (冻结快照，本会话全程静止!)
         _register_tools() 自动挂载 make_memory_tool(store)
                         │
                         ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ 运行交互期: 大模型调用 memory(target, action, content, ...) │
   ├─────────────────────────────────────────────────────────────┤
   │  1. store.add / replace / remove 校验与更新 live 内存列表    │
   │  2. tempfile + fsync + os.replace 原子写入磁盘              │
   │  3. 【关键不变式】_snapshot 绝对不动！(保护大模型前缀缓存)   │
   └─────────────────────────────────────────────────────────────┘
                         │
                         ▼
               下次启动新会话或 reset() ➔ 重新读取磁盘，新记忆生效！
```

---

## 二、核心类与数据结构

### 1. `MemoryStore` 存储引擎

- **双 Store 字符预算硬限制**：
  - `MEMORY.md`（上限 2200 字符）：存放 Agent 自己的客观笔记、环境事实、踩坑记录；
  - `USER.md`（上限 1375 字符）：存放用户画像、偏好风格、沟通习惯。
- **条目分隔符**：`ENTRY_DELIMITER = "\n§\n"`，条目内部支持多行 Markdown；
- **BOM 容错**：采用 `utf-8-sig` 读写，兼容 Windows 记事本特殊 BOM 头。

### 2. 受控维护方法

- **`add(target, content)`**：精确去重，超限时拒绝写入并输出当前全部条目，引导大模型先合并精简旧记忆（Consolidate）；
- **`replace(target, old_text, new_content)`**：唯一子串匹配；若匹配到多条不同条目（歧义），**立即拒绝修改并打印冲突项**，防止误改误删；
- **`remove(target, old_text)`**：唯一子串定位删除并原子落盘。
- **`patch(target, old_text, new_content)`** 只替换唯一匹配的片段。多条条目命中，或同一条目内多处命中时，都会报告歧义。
- **`batch`** 接收同一文件的 `operations`。所有操作先应用到候选内容，按最终字符数校验，通过后一次保存。

每次修改都在目标文件锁内重读磁盘状态。锁超时、读取失败、校验失败或保存失败时，工具返回失败及错误类别。直接编辑记忆文件的外部程序需要使用同一把锁才能参与并发协调。

### 3. `make_memory_tool` 工具工厂

生成符合 OpenAI 标准的单工具多 Action 接口：

```python
@tool(
    name="memory",
    description="Manage long-term memory across sessions. Target 'memory' or 'user'..."
)
def memory(
    target: Literal["memory", "user"],
    action: Literal["add", "replace", "remove", "patch", "batch"],
    content: str | None = None,
    old_text: str | None = None,
    new_content: str | None = None,
    operations: list[MemoryOperation] | None = None,
) -> ToolResult:
    ...
```

---

## 三、核心设计不变式

1. **Frozen Snapshot（冻结快照）与 Prefix Cache 保护**：
   - 写入的记忆由下一次显式加载或新会话采用，当前会话沿用启动时读取的记忆快照；
   - 稳定的记忆前缀有利于复用 Prompt Cache，实际命中由模型供应商与整段请求前缀决定。
2. **子代理沙箱隔离**：
   - 派发子代理时强制设置 `memory_dir=False` 并在工具列表中剔除 `memory` 工具，防止子代理发生工具冲突或意外修改主 Agent 记忆。
