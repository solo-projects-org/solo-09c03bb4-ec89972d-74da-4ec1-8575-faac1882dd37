"""请求/响应 Pydantic 模型与 Swagger 示例。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

# 版本号统一类型：非空白、长度 1..128 的字符串。
VersionStr = Annotated[
    str,
    StringConstraints(strip_whitespace=False, min_length=1, max_length=128),
]

# 示例数据（行列序号均从 0 开始，与请求中的矩阵下标一致）---------------

# 示例 1：3x3，4 个隐藏格构成自由块，取值为区间，不可还原。
EX_RANGE: dict = {
    "matrix": [
        [5, None, None],
        [None, 3, None],
        [None, None, 2],
    ],
    "row_totals": [12, 10, 8],
    "col_totals": [9, 9, 12],
}

# 示例 2：3x3，隐藏格被合计完全锁定，全部可还原。
EX_RECOVERABLE: dict = {
    "matrix": [
        [2, None, 3],
        [None, 4, None],
        [5, 1, None],
    ],
    "row_totals": [10, 10, 9],
    "col_totals": [9, 10, 10],
}

# 示例 3：行合计之和与列合计之和不相等，直接冲突。
EX_TOTAL_CONFLICT: dict = {
    "matrix": [
        [1, None],
        [None, 2],
    ],
    "row_totals": [5, 5],
    "col_totals": [6, 6],
}

# 示例 4：总量相等但运输结构无解（第 0 行只能把量送进第 0 列，容量不够）。
EX_HALL_CONFLICT: dict = {
    "matrix": [
        [None, 4],
        [3, None],
    ],
    "row_totals": [10, 7],
    "col_totals": [5, 12],
}


class SuppressionRequest(BaseModel):
    """核查请求：计数矩阵（null 表示遮盖格）与公开行/列合计。"""

    matrix: list[list[StrictInt | None]] = Field(
        ...,
        description=(
            "最多 12x12 的非负整数计数矩阵；null 表示被遮盖的格子，"
            "其余数值视为已公开的固定格"
        ),
    )
    row_totals: list[StrictInt] = Field(
        ..., description="每一行的公开合计，长度须等于矩阵行数"
    )
    col_totals: list[StrictInt] = Field(
        ..., description="每一列的公开合计，长度须等于矩阵列数"
    )

    @field_validator("matrix")
    @classmethod
    def _validate_matrix_cells(cls, value):
        # StrictInt 在联合类型里仍可能放行 bool（bool 是 int 子类），显式拒绝；
        # 同时统一拦截负数
        for i, row in enumerate(value):
            for j, cell in enumerate(row):
                if isinstance(cell, bool):
                    raise ValueError(
                        f"matrix[{i}][{j}] 不能是布尔值，须为整数或 null"
                    )
                if cell is not None and cell < 0:
                    raise ValueError(f"matrix[{i}][{j}] 不能为负数：{cell}")
        return value

    @field_validator("row_totals", "col_totals")
    @classmethod
    def _validate_totals(cls, value):
        for i, cell in enumerate(value):
            if isinstance(cell, bool):
                raise ValueError(f"合计值不能是布尔值：位置 {i}")
            if cell < 0:
                raise ValueError(f"合计值不能为负数：位置 {i} = {cell}")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> "SuppressionRequest":
        if not self.matrix:
            raise ValueError("matrix 至少要包含一行")
        if len(self.matrix) > 12:
            raise ValueError("matrix 行数不能超过 12")
        width = len(self.matrix[0])
        if width == 0 or width > 12:
            raise ValueError("matrix 列数须在 1..12 之间")
        for i, row in enumerate(self.matrix):
            if len(row) != width:
                raise ValueError(f"第 {i} 行长度与首行不一致（应为 {width}）")
        if len(self.row_totals) != len(self.matrix):
            raise ValueError("row_totals 长度须等于矩阵行数")
        if len(self.col_totals) != width:
            raise ValueError("col_totals 长度须等于矩阵列数")
        if any(v < 0 for v in self.row_totals):
            raise ValueError("row_totals 不能包含负数")
        if any(v < 0 for v in self.col_totals):
            raise ValueError("col_totals 不能包含负数")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"summary": "可行：隐藏格存在取值区间（不可还原）", "value": EX_RANGE},
                {"summary": "可行：隐藏格被合计唯一锁定（可还原）", "value": EX_RECOVERABLE},
                {"summary": "冲突：行列总合计不相等", "value": EX_TOTAL_CONFLICT},
                {"summary": "冲突：总量相等但结构无解（Hall 条件）", "value": EX_HALL_CONFLICT},
            ]
        }
    }


class Dimensions(BaseModel):
    rows: int
    cols: int


class CellRange(BaseModel):
    row: int = Field(..., description="隐藏格行下标（从 0 开始）")
    col: int = Field(..., description="隐藏格列下标（从 0 开始）")
    min: int = Field(..., description="在全部合计同时成立前提下该格的最小取值")
    max: int = Field(..., description="在全部合计同时成立前提下该格的最大取值")
    recoverable: bool = Field(
        ..., description="min == max 时为 true，表示可被公开合计精确反推"
    )


class FeasibleResponse(BaseModel):
    status: Literal["feasible"]
    dimensions: Dimensions
    grand_total: int = Field(..., description="行合计之和（等于列合计之和）")
    hidden_count: int
    recoverable_count: int = Field(..., description="可还原隐藏格数量")
    cells: list[CellRange] = Field(
        ..., description="每个隐藏格的取值区间与可还原标记，按行优先顺序"
    )
    minimizer_matrix: list[list[int]] = Field(
        ..., description="达到各隐藏格下界的完整可行矩阵（对首个隐藏格取 min）"
    )
    maximizer_matrix: list[list[int]] = Field(
        ..., description="达到各隐藏格上界的完整可行矩阵（对首个隐藏格取 max）"
    )


class ConflictWitness(BaseModel):
    max_flow: int
    required_flow: int
    row_subset: list[int] = Field(
        ..., description="最小割源侧的行下标集合 R（0 起）"
    )
    neighbor_cols: list[int] = Field(
        ..., description="与 R 中隐藏格相邻的列下标集合 N(R)"
    )
    subset_demand: int = Field(..., description="行子集 R 的隐藏格总需求")
    neighbor_supply: int = Field(
        ..., description="邻接列 N(R) 能承接的总供给，小于需求即无解"
    )


class ConflictResponse(BaseModel):
    status: Literal["conflict"]
    reasons: list[str] = Field(
        ..., description="冲突的文字说明；无解时不返回任何取值区间"
    )
    witness: ConflictWitness | None = Field(
        default=None, description="最大流最小割给出的 Hall 型冲突见证（若适用）"
    )


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


# ---------------------------------------------------------------------------
# 版本登记 / 撤回：端到端 Swagger 样例
# 版本号在各端点之间保持一致，可在 Swagger UI 中按顺序试跑完整流程：
# 登记底表 -> 登记月报 -> 运行核查 -> 撤回底表 -> 确认级联停用。
# ---------------------------------------------------------------------------

EX_BASE_VERSION = "BASE-2026-11-A"
EX_REPORT_VERSION = "RPT-2026-11-001"
EX_CONFLICT_REPORT_VERSION = "RPT-2026-11-002"
EX_BASE_NOTE = "2026 年 11 月院区 A 病例底表快照"

EX_BASE_REGISTER: dict = {
    "version": EX_BASE_VERSION,
    "note": EX_BASE_NOTE,
}

EX_REPORT_REGISTER: dict = {
    "version": EX_REPORT_VERSION,
    "base_version": EX_BASE_VERSION,
    **EX_RANGE,
}

# 使用无解矩阵的月报，核查时会得到 409（约束无解，不登记核查结果）。
EX_REPORT_REGISTER_CONFLICT: dict = {
    "version": EX_CONFLICT_REPORT_VERSION,
    "base_version": EX_BASE_VERSION,
    **EX_HALL_CONFLICT,
}

EX_WITHDRAW_BODY: dict = {
    "reason": "院区 A 底表核对发现漏数，停用全部待发布产物",
}


# ---------------------------------------------------------------------------
# 底表版本
# ---------------------------------------------------------------------------


class BaseRegisterRequest(BaseModel):
    """登记底表版本请求。"""

    version: VersionStr = Field(..., description="底表版本号，全局唯一，登记后不得覆盖")
    note: str | None = Field(default=None, description="备注（如院区、快照日期、来源）")

    @field_validator("version")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("版本号不能为空白字符串")
        return value


class BaseVersionRecord(BaseModel):
    version: str
    note: str | None = None
    valid: bool = Field(..., description="true=有效；false=已撤回失效")
    created_at: str
    invalidated_at: str | None = None
    invalidate_reason: str | None = None


# ---------------------------------------------------------------------------
# 月报版本
# ---------------------------------------------------------------------------


class ReportRegisterRequest(SuppressionRequest):
    """登记月报请求：遮盖矩阵 + 公开合计 + 实际使用的底表版本。"""

    version: VersionStr = Field(..., description="月报版本号，全局唯一，登记后不得覆盖")
    base_version: VersionStr = Field(
        ..., description="本月报实际使用的底表版本，必须已登记且有效"
    )

    @field_validator("version", "base_version")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("版本号不能为空白字符串")
        return value

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"summary": "可行月报（3x3，6 个遮盖格）", "value": EX_REPORT_REGISTER},
                {
                    "summary": "无解月报（Hall 条件破坏，核查将返回 409）",
                    "value": EX_REPORT_REGISTER_CONFLICT,
                },
            ]
        }
    }


class ReportVersionRecord(BaseModel):
    version: str
    base_version: str
    matrix: list[list[StrictInt | None]]
    row_totals: list[StrictInt]
    col_totals: list[StrictInt]
    valid: bool = Field(..., description="true=有效；false=因底表撤回而级联失效")
    created_at: str
    invalidated_at: str | None = None
    invalidate_reason: str | None = None


# ---------------------------------------------------------------------------
# 核查结果
# ---------------------------------------------------------------------------


class CheckResultContent(FeasibleResponse):
    """登记入库的核查结果内容（现有 solver.analyze 的有解输出）。"""


class CheckRecord(BaseModel):
    version: str = Field(..., description="核查结果版本号，全局递增，如 CHK-000001")
    report_version: str
    base_version: str
    result: CheckResultContent
    valid: bool = Field(..., description="true=有效；false=随底表撤回而失效")
    created_at: str
    invalidated_at: str | None = None
    invalidate_reason: str | None = None


class CheckCreatedResponse(BaseModel):
    """有解核查的登记响应：核查版本及其完整结果。"""

    check_version: str
    report_version: str
    base_version: str
    valid: Literal[True]
    created_at: str
    result: CheckResultContent


# ---------------------------------------------------------------------------
# 撤回
# ---------------------------------------------------------------------------


class WithdrawRequest(BaseModel):
    reason: str | None = Field(
        default=None, description="撤回原因；留空时记录为“人工撤回”"
    )


class WithdrawResponse(BaseModel):
    base_version: str
    already_withdrawn: bool = Field(
        ..., description="true=该底表此前已撤回（幂等返回，停用列表为空）"
    )
    decommissioned_reports: list[str] = Field(
        ..., description="本次必须停用的月报版本（撤回前仍有效的依赖月报）"
    )
    decommissioned_checks: list[str] = Field(
        ..., description="本次必须停用的核查结果版本（撤回前仍有效的依赖核查）"
    )
    invalidated_at: str | None = None


# ---------------------------------------------------------------------------
# 统一错误信封
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    error: "ErrorDetail"


class ErrorDetail(BaseModel):
    code: str = Field(..., description="稳定错误码，如 BASE_VERSION_NOT_FOUND")
    message: str
    detail: dict | None = None


ErrorResponse.model_rebuild()


class CheckConflictResponse(BaseModel):
    """月报核查无解（409）：沿用 solver 的冲突结构，不登记任何核查结果。"""

    status: Literal["conflict"]
    reasons: list[str]
    witness: ConflictWitness | None = None
