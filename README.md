# 医院计数矩阵遮盖核查与底表版本登记 API

发布分年龄组（列）、乡镇（行）的病例计数前，用本服务检查 `null` 遮盖格
能否被**公开的行/列合计精确反推**。对每个隐藏格，在所有合计同时成立的
前提下返回：

- 可行最小值 `min`、最大值 `max`（隐藏值可取任意非负整数）；
- 分别达到两界的完整可行矩阵 `minimizer_matrix` / `maximizer_matrix`；
- `min == max` 时标记 `recoverable: true`，表示该格可被唯一反推，
  工程师应调整遮盖位置后重新核查；
- 约束无解时返回 HTTP 409 与结构化冲突说明，**不输出任何区间**。

此外，服务用 **SQLite（WAL 模式）登记底表版本、月报版本与核查结果**：

- 登记底表版本后，月报只能引用**已登记且有效**的底表，已登记版本不得覆盖；
- 按有效月报版本运行核查，**有解才登记**并返回核查结果及其版本；
- 撤回某个底表版本时，底表本身与依赖它的月报、核查结果一起标为失效，
  并返回必须停用的月报/核查版本清单；撤回是短事务，**不等待求解结束**；
- 若月报在核查结果登记前已失效（例如求解期间底表被撤回），接口报错，
  **不登记也不返回核查结果**。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 应用与路由（含 Swagger 端到端示例）
│   ├── schemas.py     # Pydantic 请求/响应模型与内置示例
│   ├── solver.py      # Dinic 最大流可行性判定 + HiGHS 线性规划求界
│   └── db.py          # SQLite 登记中心：底表/月报/核查版本与级联撤回
├── tests/             # pytest 测试（含暴力枚举交叉验证与并发撤回测试）
├── Dockerfile
├── docker-compose.yml # 命名卷持久化 /data/suppression.db
├── requirements.txt
└── requirements-dev.txt
```

## 启动方式（Docker Compose）

```bash
docker compose up --build
```

启动后：

- Swagger UI：<http://localhost:8000/docs> （可直接选用内置示例提交）
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`。

## 请求与响应

`POST /api/v1/suppression-check`

请求体（矩阵最多 12×12；`null` 为遮盖格，其余为固定非负整数）：

```json
{
  "matrix": [
    [5, null, null],
    [null, 3, null],
    [null, null, 2]
  ],
  "row_totals": [12, 10, 8],
  "col_totals": [9, 9, 12]
}
```

可行响应（HTTP 200，节选）：

```json
{
  "status": "feasible",
  "grand_total": 30,
  "hidden_count": 6,
  "recoverable_count": 0,
  "cells": [
    {"row": 0, "col": 1, "min": 0, "max": 4, "recoverable": false}
  ],
  "minimizer_matrix": [[5, 0, 7], [4, 3, 3], [0, 6, 2]],
  "maximizer_matrix": [[5, 4, 3], [0, 3, 7], [4, 2, 2]]
}
```

无解响应（HTTP 409，不含任何区间信息）：

```json
{
  "status": "conflict",
  "reasons": [
    "不存在满足全部公开合计的非负整数矩阵：最大流可行性检验未通过",
    "行子集 [0] 的隐藏格需求合计为 6，但这些行的隐藏格只能进入列 [0]，其可承接供给仅为 2（Hall 条件被破坏：需求 > 邻接供给）",
    "最大流 6 < 隐藏格所需总流量 10"
  ],
  "witness": {
    "max_flow": 6,
    "required_flow": 10,
    "row_subset": [0],
    "neighbor_cols": [0],
    "subset_demand": 6,
    "neighbor_supply": 2
  }
}
```

状态码约定：

| 场景 | 状态码 |
| --- | --- |
| 存在可行矩阵 | 200，返回区间与两张完整矩阵 |
| 合计约束无解（总量不符 / 固定格超限 / Hall 条件破坏） | 409，返回冲突原因与见证 |
| 请求结构或类型错误（超 12×12、负数、布尔、长度不符） | 422，Pydantic 校验信息 |

## 底表版本登记、核查与撤回

SQLite 登记库默认位于容器内 `/data/suppression.db`（命名卷
`suppression-data` 持久化），可用环境变量 `SUPPRESSION_DB_PATH` 覆盖。

### 端点一览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/v1/base-versions` | 登记有效底表版本；版本号已存在（含已撤回）返回 409，不覆盖 |
| `GET /api/v1/base-versions` / `/{version}` | 查询底表版本，可按 `valid` 过滤 |
| `POST /api/v1/reports` | 登记月报（版本、遮盖矩阵、公开合计、底表版本）；底表未登记 404、已失效 409 |
| `GET /api/v1/reports` / `/{version}` | 查询月报，可按 `base_version`、`valid` 过滤 |
| `POST /api/v1/reports/{version}/checks` | 按有效月报运行现有遮盖核查；有解 201 登记并返回核查版本与结果，无解 409 且不落库 |
| `GET /api/v1/checks` / `/{version}` | 查询核查结果，可按 `report_version`、`valid` 过滤 |
| `POST /api/v1/base-versions/{version}/withdraw` | 撤回底表，级联失效依赖月报/核查，返回须停用版本清单；重复撤回幂等 |

核查结果版本由数据库自增主键生成，形如 `CHK-000001`，全局递增。
错误响应统一为信封：`{"error": {"code": "...", "message": "..."}}`。

### Swagger UI 端到端试跑

打开 <http://localhost:8000/docs>，各端点内置示例使用同一组版本号，
按以下顺序即可从登记底表试到撤回：

1. `POST /api/v1/base-versions` → 选用示例 **登记院区 A 的 11 月底表快照**
   （版本 `BASE-2026-11-A`）。
2. `POST /api/v1/reports` → 选用 **可行月报**（`RPT-2026-11-001`）与
   **无解月报**（`RPT-2026-11-002`，核查阶段会 409）各登记一次。
3. `POST /api/v1/reports/{version}/checks`（路径参数示例已填
   `RPT-2026-11-001`）→ 返回 `CHK-000001` 与完整核查结果；对无解月报
   调用则返回 409，且 `GET /api/v1/checks` 中没有对应记录。
4. `POST /api/v1/base-versions/{version}/withdraw`（路径参数示例已填
   `BASE-2026-11-A`）→ 返回 `decommissioned_reports` 与
   `decommissioned_checks`，即必须停用的月报和核查结果版本。
5. 再对 `RPT-2026-11-001` 调用核查 → 409（月报已失效，不登记结果）；
   再次撤回同一底表 → 200 且 `already_withdrawn: true`、停用列表为空。

### 撤回不等待求解

LP 求解在数据库事务之外执行（可能耗时较长），撤回只需要一个
`BEGIN IMMEDIATE` 短事务。若求解进行中底表被撤回，求解结束后的结果登记
会在写事务内重新校验月报有效性，发现失效即返回 409
（`REPORT_VERSION_INVALID`），不写入任何核查记录。该并发语义由
`tests/test_registry_api.py` 中挂起求解器的线程测试覆盖。

## 算法说明

1. **建模**：每个遮盖格对应一个非负整数变量，行/列合计构成等式约束，
   即整数运输问题。
2. **可行性**：运输约束矩阵是全幺模矩阵，右侧为整数，线性规划松弛的
   顶点天然为整数解。服务先用自实现的 Dinic 最大流判定可行性：
   source→行（剩余行需求）、隐藏格所在行列之间建容量无限边、列→sink
   （剩余列供给）。最大流达不到隐藏格总流量即无解，并由最小割给出
   Hall 型冲突见证（哪些行的需求超出其邻接列供给）。
3. **求界**：可行时对每个隐藏格分别以该变量系数 +1 / −1 为目标做
   `scipy.optimize.linprog(method="highs")`，得到 `min` / `max`，
   并输出取扫描序第一个隐藏格的两张极值完整矩阵。
4. **复核**：输出矩阵在返回前重新逐行逐列核对公开合计、非负性与整数性。
5. **可还原判定**：`min == max` 即该格取值唯一，可被公开合计反推。

复杂度：设隐藏格数为 h（h ≤ 144），共 2h+1 次小规模 LP 与一次
O(V²E) 最大流；12×12 实测在百毫秒内完成。

## 本地开发（无 Docker 时）

```bash
python3 -m venv --without-pip .venv
.venv/bin/python get-pip.py            # 若 ensurepip 不可用
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest tests -q
```

测试集包含 70 组随机小规模问题的**暴力枚举交叉验证**：LP 求出的每个
区间与枚举全部可行赋值的 min/max 完全一致，并覆盖无解、可还原、
零矩阵、1×1、12×12 与各类 422/409 场景。`test_registry_api.py` 另覆盖
版本登记防覆盖、底表有效性门禁、有解才登记、撤回级联与幂等、
求解期间撤回的并发复验等完整生命周期。本地运行时 SQLite 默认写入
`./data/suppression.db`（已在 `.gitignore` 中忽略）。
