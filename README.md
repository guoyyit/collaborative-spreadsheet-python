# Collaborative Spreadsheet Service

一个支持多人并发编辑、离线重放、公式传播、增量同步、单客户端撤销/重做、
崩溃恢复与读写权限的协同表格服务。纯 Python 实现，FastAPI 提供 HTTP 接口，
持久化采用"每批一个文件 + 原子改名"的崩溃安全 WAL。

- 语言：Python 3.14（`requires-python >= 3.14`）
- 运行：`uv sync --group dev`
- 启动：`uv run uvicorn main:app --reload`（默认数据目录 `/tmp/collab-sheets`，
  可用环境变量 `SHEET_DATA_DIR` 覆盖）
- 测试：`uv run pytest`

---

## 1. 数据模型

- 表格 `sheet` 由一个 `owner` 创建；单元格使用 A1 风格地址（`A1`、`B12`、`AA3`）。
- 操作 `Operation` 是复制日志的最小条目，字段：
  - `id = (client_id, seq)`：操作身份，`seq` 为该客户端单调递增序号，**重投去重以此为准**；
  - `timestamp`：客户端 Lamport 逻辑时间戳（操作的内在属性，见 §2）；
  - `kind`：`set` / `clear` / `grant` / `revoke` / `sheet_readonly`；
  - `cell`、`content`（`=` 开头表示公式）、`target`、`readonly` 等负载；
  - 撤销/重做溯源字段 `origin` / `restore` / `is_redo` / `compensation`。

---

## 2. 合并规则（强一致收敛）

### 2.1 全序

所有已接受的操作构成一条规范日志，按确定性全序排序：

```
order(op) = (op.timestamp, op.client_id)
```

- 时间戳为**客户端生成的 Lamport 时钟**：每次本地编辑自增；观察到时间戳 `t`
  的状态后再编辑，必须先把本地时钟推进到 `max(local, t) + 1`。
- 时间戳相等时按 `client_id` 字典序，排在全序**后面**的同时间戳操作获胜。
  （决胜规则与 seq 无关：同一客户端的操作时间戳天然单调递增。）

### 2.2 单元格冲突：LWW

每个单元格的可见内容 = 全序中针对该单元格的最后一个 `set`（空内容 `set`
等价于 `clear`）。因此：

- **收敛性**：最终物化状态是"已接受操作集合"的纯函数，与操作到达顺序无关；
  相同操作集合无论按什么顺序到达，最终状态逐字节一致（测试
  `test_conflict_outcome_independent_of_arrival_order` 穷举批次排列验证）。
- **不覆盖并发成果**：离线客户端用旧时间戳重放同一单元格时，会输给在线方
  已经提交的、时间戳更大的写入；而它对"未被他人触碰单元格"的编辑正常合入。
- **可追溯**：快照中每个单元格带 `written_by`（当前生效用户操作的 `client:seq`）。

### 2.3 幂等与离线重放

- 以 `(client_id, seq)` 去重；同一逻辑操作重投只生效一次。
- 提交按 `batch_id` 幂等：重复批次返回 `409 Duplicate batch`，不重复落盘。
- 一个批次内的操作按"发生顺序"（`seq`）排序后赋序提交；批次是原子提交单位。

### 2.4 公式与依赖传播

- 支持：四则运算、括号、一元正负、单元格与区域引用；函数
  `SUM / AVERAGE(AVG) / MIN / MAX / COUNT / ABS`。
- 修改被引用单元格后，依赖公式按**拓扑序**重新计算（Kahn 算法）。
- **循环引用被稳定拒绝**：Tarjan SCC 找出环，环内成员及传递依赖者均得到
  `#CIRCULAR` 错误对象，错误信息包含涉及的单元格（可诊断）；打破环后自动恢复。
- **缺失/失效引用不传播为数值**：引用空单元格（含被 clear 的单元格）为
  `#REF`；除零为 `#DIV0`；无法解析为数字为 `#VALUE`；公式语法错误为 `#SYNTAX`。
  错误以 `CellError` 值沿依赖链传播，永不被当成普通数字参与运算。

### 2.5 因果增量同步

- 每个客户端维护**版本向量**（各来源客户端已见的最大 seq）。
- `delta_for(log, known)` 返回所有未被向量覆盖的操作，按全序（即与因果一致的
  一个线性化）投递。重复拉取幂等；多次增量追平的结果与一次全量同步完全一致。
- 同步是对不可变规范日志的只读计算，慢客户端的落后向量不会阻塞任何其他客户端。

### 2.6 撤销 / 重做（按客户端隔离）

- 每个客户端有独立 undo/redo 栈，只记录自己的真实用户写入（补偿/重做不入栈）。
- **选择性撤销（selective undo）**：撤销时检查该单元格当前"生效的用户操作"
  是否仍是自己的目标操作；若已被其他客户端覆盖，则该撤销对该单元格为空操作，
  **绝不回退他人后续修改**。
- 撤销通过带 `compensation` 标记、含 `restore` 指针的逆操作实现，恢复到目标
  操作之前该单元格显示的用户内容（无则清空）；重做以新时间戳重新施加目标内容。
- 重做语义：重做得到"该客户端预期的结果"（以新 Lamport 时间戳参与 LWW）。
- 若撤销/重做时权限已被收回，抛出 `403`，且不改变任何历史。

### 2.7 权限的确定作用顺序

- owner 始终可写；`grant` / `revoke` 管理"某客户端对某单元格"的授权集合；
  `sheet_readonly=true` 对非 owner 全局只读。
- 准入判定按全序回放权限：一个写操作只受全序中位于它之前的权限操作影响。
  权限变更本身只有 owner 可执行。
- 权限的因果语义：时间戳小于授权的写，无论授权何时到达都被拒绝（它因果上
  发生在授权之前）；客户端在观察到授权后重投同一操作（相同 client/seq）即可
  收敛。被拒绝的操作**不写入日志、不进入同步历史**。

---

## 3. 持久化与崩溃恢复（§需求 6）

目录布局：

```
<SHEET_DATA_DIR>/<sheet>/meta.json
<SHEET_DATA_DIR>/<sheet>/batch-000000000001.json
<SHEET_DATA_DIR>/<sheet>/.tmp-*.json      # 崩溃残留，加载时忽略
```

- `meta.json` 与每个 `batch-N.json` 都采用"同目录临时文件写入 → fsync →
  `os.replace` 原子改名 → fsync 目录"。
- **批量提交全有或全无**：改名前崩溃只留下不会被读取的 `.tmp-*`，因此未完成
  批次对其他客户端不可见；改名后重启时整批可见。
- 批次文件内含 `batch_id`，重启后重复追加同一 `batch_id` 为空操作。
- 启动时重放全部元数据与批次，重建规范日志、权限与物化网格，之后可继续同步/编辑。

---

## 4. HTTP API

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /sheets/{sheet_id}` | body `{"client_id": ...}` 创建表格（owner） |
| `GET  /sheets/{sheet_id}` | 全量快照：`cells`（raw/value/written_by）、`version`、`readonly` |
| `POST /sheets/{sheet_id}/batches/{batch_id}` | 提交一批操作（离线重放），body `{"ops":[...]}` |
| `GET  /sheets/{sheet_id}/sync?vector={"c":2}` | 增量同步，返回 `ops` 与最新 `version` |
| `POST /sheets/{sheet_id}/undo` / `redo` | body `{"client_id": ...}` |

操作 JSON 示例：

```json
{"client_id":"c1","seq":3,"ts":21,"kind":"set","cell":"A1","content":"=SUM(B1:B3)"}
{"client_id":"owner","seq":7,"ts":9,"kind":"grant","cell":"A1","target":"c1"}
{"client_id":"owner","seq":8,"ts":10,"kind":"sheet_readonly","readonly":true}
```

错误单元格在快照中的 `value` 形如：

```json
{"error": "CIRCULAR", "message": "circular reference involving A1"}
```

---

## 5. 可配置项

| 配置 | 方式 | 默认 | 说明 |
| --- | --- | --- | --- |
| 数据目录 | 环境变量 `SHEET_DATA_DIR` / `create_app(data_dir=...)` | 系统临时目录下 `collab-sheets` | WAL 根目录；重启指向同一目录即恢复 |
| 服务端口 | `uvicorn --port` | 8000（直接 `python main.py` 时） | HTTP 监听端口 |
| 权限模型 | 代码内 `PermissionState` | owner + 单元格授权 + 表级只读 | 可在该类扩展更细粒度规则 |

---

## 6. 目录结构

```
spreadsheet/
  celladdr.py     # A1 地址解析/格式化
  formula.py      # 词法/语法分析、依赖提取、求值、错误值
  operations.py   # 操作类型、全序键、序列化
  permissions.py  # 权限状态与全序回放
  engine.py       # 规范日志合并、准入、拓扑重算与环检测
  storage.py      # 原子 WAL 持久化与崩溃恢复加载
  sync.py         # 版本向量与因果增量
  service.py      # 加锁门面：提交/快照/同步/撤销重做/恢复
  api.py          # FastAPI HTTP 层
tests/            # 8 类场景的自动化测试
```

测试覆盖：并发同单元格（含多线程）、离线重放与幂等、循环引用/缺失引用/
传播、增量与全量等价、撤销重做隔离、崩溃与部分写入、越权修改与权限作用序、
以及 HTTP 端到端（含重启）。
