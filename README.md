# Collaborative Spreadsheet Service (Python)

支持多人并发编辑、离线重放、公式传播、因果序增量同步、选择性撤销/重做、
崩溃恢复以及表/单元格级权限的协同表格服务。纯 Python 实现，FastAPI HTTP 层，
文件 WAL 持久化，无外部服务依赖。

## 运行

```bash
uv sync
uv run uvicorn main:app --reload          # http://127.0.0.1:8000/docs
uv run pytest                              # 48 个自动化测试
```

## 快速示例

```bash
curl -s -X POST localhost:8000/tables -d '{"owner_id":"alice","sheet_id":"t1"}' -H 'content-type: application/json'
curl -s -X POST localhost:8000/tables/t1/cells \
  -H 'content-type: application/json' \
  -d '{"client_id":"alice","row":0,"col":0,"value":"41"}'
curl -s -X POST localhost:8000/tables/t1/cells \
  -H 'content-type: application/json' \
  -d '{"client_id":"alice","row":0,"col":1,"value":"=A1+1"}'
curl -s localhost:8000/tables/t1/snapshot
```

## 数据模型

所有变更都表示为一条不可变的 **Op**（`collab_sheet/models.py`）：

| 字段 | 含义 |
| --- | --- |
| `op_id` | 全局唯一幂等键（重复投递只生效一次） |
| `client_id` | 作者；PERMISSION op 中是授权人 |
| `grantee` | 仅 PERMISSION：被授权/被撤销的用户 |
| `seq` | 该客户端单调递增序号 |
| `ts_ms` | 客户端在操作发生时的墙钟（因果时间戳，离线期间也保留） |
| `kind` | `SET_CELL` / `PERMISSION` / `UNDO` / `REDO` |
| `row,col,value` | SET_CELL 负载；`value` 以 `=` 开头即为公式 |
| `scope,target,role` | PERMISSION 负载 |
| `undone_id/redone_id` | UNDO/REDO 指向的本客户端 SET_CELL op |

## 一致性语义与合并规则

### 1. 单元格：带稳定决胜的 Last-Writer-Wins（可交换、可复现）

对每个单元格，取该坐标所有"存活"的 SET_CELL op（未被作者撤销），
按以下全序键比较，**最大者胜**：

```
(ts_ms, client_id, seq, op_id)
```

- 键只依赖 op 自身携带的字段，与服务端到达顺序无关。同一组 op，无论按
  什么顺序到达、由谁先重放，最终状态完全一致（测试见
  `test_concurrent_edit.py::test_concurrent_same_cell_all_arrival_permutations_converge`）。
- 后写天然覆盖先写；同时间戳按 `client_id` 字典序，再按 `seq`、`op_id` 决胜，
  保证无并列、无随机。
- 每个单元格都返回 `provenance = {op_id, client_id, seq, ts_ms}`，最终内容可追溯。
- 离线重放使用操作发生时的 `ts_ms`：离线写如果因果时间戳更旧，会进入历史但
  **不会覆盖**在线期间他人已提交的新值（只按规则决胜）。

> 这是一个文档化的确定性冲突合并策略，而非"静默覆盖"：冲突双方都保留在日志里，
> 落后方仍可通过同步与撤销历史追溯。

### 2. 离线重放与幂等

- 客户端可在 `POST /tables/{sid}/ops` 中批量提交完全成型的 op（携带原始
  `ts_ms/seq/op_id`），按发生顺序提交，整批写入同一个 WAL 帧（原子）。
- 服务端以 `op_id` 去重：相同 op 重放只返回已接受，状态与版本号不变；
  同一 `op_id` 携带不同 body 会被判为冲突错误（`DuplicateOp`）。
- 批量中任何一条非法（越权/循环公式/格式错误）→ **整批拒绝**，不留下部分写入，
  也不会进入同步历史。

### 3. 公式与依赖传播

- 语法：`=A1+2*B2-(C3)`，支持 `+ - * /`、括号、一元负号，`[A-Z]+行号`
  形式的引用（A1、AB12、ZZZ999999）。
- 每次提交后按依赖图的**拓扑序**重算所有受影响公式（支持长链、菱形依赖），
  结果单元格的 provenance 附带 `computed_from`。
- 空单元格在算术中按 0 处理；文本参与算术 → `#VALUE!`；除零 → `#DIV/0!`；
  越界地址（如 `AAAA1`）在提交期以 `#REF!` 拒绝。
- 错误沿依赖边**原样传播**，错误单元格永远不会显示为普通数值。
- **循环引用稳定拒绝**：提交时在"当前有效依赖图 + 候选边"上做 DFS，
  检测到环则拒绝该 op，返回 `#CYCLE!` 与形如 `A1 -> B1 -> A1` 的可诊断路径，
  被拒绝的 op 不入库、旧值不变。评估期保留了第二层 `#CYCLE!` 防护（运行时
  错误单元格），错误同样不会被当成数字。

### 4. 因果序增量同步

- 同步请求携带客户端**已知的 op_id 集合**（`known_ids`，即水位），服务端返回
  规范序（同一合并键顺序）下该集合之外的所有 op，支持 `limit` 分页和
  `next_known` 新水位。
- 因为没有"接收游标"状态，慢客户端/长期离线客户端**不阻塞**任何其他客户端；
  重连后拿到的增量流按全序排列，将其物化到空库得到的状态与全量快照逐格一致
  （`test_incremental_sync.py`）。

### 5. 撤销 / 重做（按作者隔离的线性模型）

- 每个客户端维护由历史派生的 undo/redo 栈；UNDO/REDO 只切换**自己的**
  SET_CELL op 的存活状态，服务端生成的动作为其分配"晚于该客户端所有已知
  op"的因果时间戳，保证规范序下动作不乱序，且重启后语义不变。
- 撤销一个参与过并发合并的 op 时，他人的写入保留：例如 A 写 1、B 并发写 2
  （B 胜），A 撤销自己的 1，单元格仍为 B 的 2。
- 重做把自己的 op 重新置为存活，再按同一 LWW 键参与决胜（重做后"预期结果"
  即确定性合并结果）。撤销后产生的新编辑会清空 redo 历史（标准线性语义）。
- 禁止撤销他人的 op（`forbidden`）。UNDO/REDO 自身也按 `op_id` 幂等。

### 6. 持久化与崩溃恢复

- 磁盘布局（`data_dir`，默认 `./data`）：
  - `meta.jsonl`：每建表一行；
  - `ops.log`：每个提交批次一行 JSON **帧**（含 `version` 与 `ops`），换行符结尾。
- 一帧通过单次 append + flush + 可选 fsync 落盘，**整批原子可见**。
- 恢复时逐行重放：末尾没有换行符的残缺帧（崩溃发生在写入中途）会被直接丢弃——
  未完成的修改重启后对任何人不可见；此前的所有提交完整保留（见
  `test_crash_recovery.py::test_torn_trailing_frame_is_discarded_on_recovery`）。
- 权限拒绝/校验失败的 op 从不写盘，因此崩溃恢复后也不可能"复活"。

### 7. 权限

- 角色（强到弱）：`owner > admin > writer > reader`。建表者自动成为 owner。
- 授权记录本身是 PERMISSION op，按键
  `(sheet, scope, target, grantee)` 做与单元格相同的 LWW 合并，
  排序键仍是 `(ts_ms, client_id, seq, op_id)`，所以"权限变化"与"并发编辑"
  有确定的作用顺序：编辑在提交时对照当前生效的权限记录校验。
- `table` 级记录作用于整表；`cell` 级记录（target 为 `row,col`）仅对该格生效
  并覆盖表级角色；`role=""` 为撤销。
- 只有 owner 可授予 owner/admin；admin 可授予 writer/reader；不得授予高于
  自身的角色。越权修改返回 `forbidden` 且**不改变同步历史**。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tables` | 建表 `{owner_id, sheet_id?}` |
| GET | `/tables/{sid}` | 表信息与当前版本号 |
| GET | `/tables/{sid}/snapshot` | 全量求值后快照（含 provenance/error） |
| GET | `/tables/{sid}/cells/{row}/{col}` | 单格 |
| POST | `/tables/{sid}/cells` | 提交单个单元格编辑 |
| POST | `/tables/{sid}/ops` | 批量提交原始 op（离线重放） |
| POST | `/tables/{sid}/permissions` | 授权/撤销 |
| POST | `/tables/{sid}/undo` / `/redo` | 调用方的撤销/重做 |
| POST | `/tables/{sid}/sync` | `{client_id, known_ids, limit?}` 增量同步 |

所有写接口都接受可选的 `op_id`（端到端幂等）与 `ts_ms`（离线因果时间戳）。

## 可配置项

`collab_sheet/config.py`，可用构造参数或环境变量覆盖：

| 配置 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `data_dir` | `COLLAB_SHEET_DATA_DIR` | `./data` | WAL 与元数据目录 |
| `wal_fsync` | `COLLAB_SHEET_WAL_FSYNC` | `1` | 每帧是否 fsync（测试可关） |
| `max_formula_depth` | `COLLAB_SHEET_MAX_FORMULA_DEPTH` | `64` | 公式解析保护深度 |
| `sync_page_size` | `COLLAB_SHEET_SYNC_PAGE_SIZE` | `1000` | 增量同步单页上限 |

## 代码结构

```
collab_sheet/
  config.py       配置
  models.py       Op / 快照 / 提交结果等数据模型与序列化
  formula.py      词法、递归下降解析、依赖分析、环检测、求值
  store.py        WAL：原子帧、fsync、去重、崩溃恢复
  permissions.py  角色与表/单元格级权限视图（LWW）
  engine.py       合并引擎：校验、确定性投影、公式拓扑重算、undo/redo、同步
  service.py      应用门面：id 生成、因果时钟、undo/redo 动作
  server.py       FastAPI 路由
tests/            48 个测试（7 大场景 + 压测一致性）
```

## 已知边界

- 单进程内通过一把锁串行化提交（确定性 + 简单）；水平扩展可在
  `Store`/`Engine` 边界替换为分片或共识实现，合并规则本身与传输层无关。
- 冲突策略为文档化的 LWW + 稳定 tie-break；op 集合相同时结果必然相同。
