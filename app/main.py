"""FastAPI 应用：遮盖值反推核查与底表版本登记/撤回服务。"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Path, Query
from fastapi.responses import JSONResponse

from . import solver
from .db import Registry, RegistryError
from .schemas import (
    EX_BASE_REGISTER,
    EX_BASE_VERSION,
    EX_CONFLICT_REPORT_VERSION,
    EX_HALL_CONFLICT,
    EX_RANGE,
    EX_RECOVERABLE,
    EX_REPORT_REGISTER,
    EX_REPORT_REGISTER_CONFLICT,
    EX_REPORT_VERSION,
    EX_TOTAL_CONFLICT,
    EX_WITHDRAW_BODY,
    BaseRegisterRequest,
    BaseVersionRecord,
    CheckConflictResponse,
    CheckCreatedResponse,
    CheckRecord,
    ConflictResponse,
    ErrorResponse,
    FeasibleResponse,
    HealthResponse,
    ReportRegisterRequest,
    ReportVersionRecord,
    SuppressionRequest,
    VersionStr,
    WithdrawRequest,
    WithdrawResponse,
)

app = FastAPI(
    title="医院计数矩阵遮盖核查与底表版本登记 API",
    description=(
        "发布分年龄组、乡镇的病例计数前，检查 null 遮盖格能否被公开的"
        "行/列合计精确反推。另用 SQLite 登记底表版本、依赖它的月报版本与"
        "核查结果：底表版本撤回时，依赖它的月报和核查结果级联失效，"
        "并返回必须停用的版本清单。"
    ),
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# SQLite 登记中心：进程内单例，路径可由环境变量覆盖（测试通过依赖覆盖替换）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_registry() -> Registry:
    return Registry(os.environ.get("SUPPRESSION_DB_PATH", "./data/suppression.db"))


RegistryDep = Annotated[Registry, Depends(get_registry)]


@app.exception_handler(RegistryError)
def _registry_error_handler(_request, exc: RegistryError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# ---------------------------------------------------------------------------
# 运维 / 既有核查端点
# ---------------------------------------------------------------------------


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["运维"],
    summary="健康检查",
)
def health() -> dict:
    return {"status": "ok", "service": "suppression-check"}


@app.post(
    "/api/v1/suppression-check",
    response_model=FeasibleResponse,
    responses={409: {"model": ConflictResponse, "description": "约束无解"}},
    tags=["核查"],
    summary="提交计数矩阵与公开合计，核查隐藏格反推风险",
)
def suppression_check(
    payload: Annotated[
        SuppressionRequest,
        Body(
            openapi_examples={
                "feasible_range": {
                    "summary": "可行：隐藏格存在取值区间（不可还原）",
                    "description": "3x3 矩阵，6 个遮盖格只能确定到区间，工程师需调整遮盖位置",
                    "value": EX_RANGE,
                },
                "feasible_recoverable": {
                    "summary": "可行：隐藏格被合计唯一锁定（可还原）",
                    "description": "4 个遮盖格的 min == max，存在精确反推风险",
                    "value": EX_RECOVERABLE,
                },
                "conflict_totals": {
                    "summary": "冲突：行列总合计不相等",
                    "value": EX_TOTAL_CONFLICT,
                },
                "conflict_hall": {
                    "summary": "冲突：总量相等但结构无解（Hall 条件）",
                    "description": "最大流小于所需流量，返回最小割冲突见证",
                    "value": EX_HALL_CONFLICT,
                },
            }
        ),
    ]
):
    try:
        result = solver.analyze(
            matrix=payload.matrix,
            row_totals=payload.row_totals,
            col_totals=payload.col_totals,
        )
    except solver.SuppressionConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "status": "conflict",
                "reasons": exc.reasons,
                "witness": exc.witness,
            },
        )
    return result


@app.get(
    "/method",
    tags=["核查"],
    summary="算法说明",
)
def method() -> dict:
    return solver.fraction_explanation()


# ---------------------------------------------------------------------------
# 底表版本登记
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/base-versions",
    response_model=BaseVersionRecord,
    status_code=201,
    responses={
        409: {"model": ErrorResponse, "description": "版本已登记，禁止覆盖"},
    },
    tags=["登记与撤回"],
    summary="登记一个有效的底表版本",
)
def register_base(
    registry: RegistryDep,
    payload: Annotated[
        BaseRegisterRequest,
        Body(
            openapi_examples={
                "register_base": {
                    "summary": "登记院区 A 的 11 月底表快照",
                    "value": EX_BASE_REGISTER,
                }
            }
        ),
    ],
):
    return registry.register_base(version=payload.version, note=payload.note)


@app.get(
    "/api/v1/base-versions",
    response_model=list[BaseVersionRecord],
    tags=["登记与撤回"],
    summary="列出底表版本（可按有效性过滤）",
)
def list_bases(
    registry: RegistryDep,
    valid: Annotated[
        bool | None,
        Query(description="true=仅有效；false=仅已撤回；不带参数=全部"),
    ] = None,
):
    return registry.list_bases(valid=valid)


@app.get(
    "/api/v1/base-versions/{version}",
    response_model=BaseVersionRecord,
    responses={404: {"model": ErrorResponse, "description": "底表版本不存在"}},
    tags=["登记与撤回"],
    summary="查询单个底表版本状态",
)
def get_base(
    registry: RegistryDep,
    version: Annotated[
        VersionStr,
        Path(
            description="底表版本号",
            openapi_examples={"base_a": {"value": EX_BASE_VERSION}},
        ),
    ],
):
    record = registry.get_base(version)
    if record is None:
        raise RegistryError(
            "BASE_VERSION_NOT_FOUND", f"底表版本 {version} 尚未登记", 404
        )
    return record


# ---------------------------------------------------------------------------
# 月报版本登记
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/reports",
    response_model=ReportVersionRecord,
    status_code=201,
    responses={
        404: {"model": ErrorResponse, "description": "引用的底表版本不存在"},
        409: {"model": ErrorResponse, "description": "底表已失效或月报版本已登记"},
        422: {"description": "矩阵/合计结构或类型校验失败"},
    },
    tags=["登记与撤回"],
    summary="登记月报版本（只接受已登记且有效的底表）",
)
def register_report(
    registry: RegistryDep,
    payload: Annotated[
        ReportRegisterRequest,
        Body(
            openapi_examples={
                "feasible_report": {
                    "summary": "可行月报：引用有效底表，6 个遮盖格",
                    "value": EX_REPORT_REGISTER,
                },
                "conflict_report": {
                    "summary": "无解月报：同样引用有效底表，但核查时将 409",
                    "value": EX_REPORT_REGISTER_CONFLICT,
                },
            }
        ),
    ],
):
    return registry.register_report(
        version=payload.version,
        base_version=payload.base_version,
        matrix=payload.matrix,
        row_totals=payload.row_totals,
        col_totals=payload.col_totals,
    )


@app.get(
    "/api/v1/reports",
    response_model=list[ReportVersionRecord],
    tags=["登记与撤回"],
    summary="列出月报版本（可按底表版本/有效性过滤）",
)
def list_reports(
    registry: RegistryDep,
    base_version: Annotated[
        VersionStr | None,
        Query(description="按依赖的底表版本过滤"),
    ] = None,
    valid: Annotated[
        bool | None,
        Query(description="true=仅有效；false=仅已失效；不带参数=全部"),
    ] = None,
):
    return registry.list_reports(base_version=base_version, valid=valid)


@app.get(
    "/api/v1/reports/{version}",
    response_model=ReportVersionRecord,
    responses={404: {"model": ErrorResponse, "description": "月报版本不存在"}},
    tags=["登记与撤回"],
    summary="查询单个月报版本状态",
)
def get_report(
    registry: RegistryDep,
    version: Annotated[
        VersionStr,
        Path(
            description="月报版本号",
            openapi_examples={
                "report_001": {"value": EX_REPORT_VERSION},
                "report_conflict": {"value": EX_CONFLICT_REPORT_VERSION},
            },
        ),
    ],
):
    record = registry.get_report(version)
    if record is None:
        raise RegistryError(
            "REPORT_VERSION_NOT_FOUND", f"月报版本 {version} 尚未登记", 404
        )
    return record


# ---------------------------------------------------------------------------
# 按有效月报运行核查并登记结果
# ---------------------------------------------------------------------------


def _normalize_result(
    result: dict[str, Any],
    payload_matrix: list[list[int | None]],
) -> dict[str, Any]:
    """补齐无遮盖格场景下 solver 省略的完整矩阵字段，统一入库结构。"""
    if result.get("hidden_count", 0) == 0:
        result = dict(result)
        result.setdefault(
            "minimizer_matrix",
            [[cell for cell in row] for row in payload_matrix],
        )
        result.setdefault(
            "maximizer_matrix",
            [[cell for cell in row] for row in payload_matrix],
        )
    return result


def execute_report_check(
    registry: Registry, report_version: str
) -> dict[str, Any]:
    """核查编排：读取有效月报 -> 事务外求解 -> 事务内复验并登记。

    供 API 与并发测试复用。求解期间不持有任何数据库事务/连接，因此并发
    撤回不会等待求解结束；求解完成后若月报已失效则抛 :class:`RegistryError`，
    不登记也不返回核查结果。
    """
    report = registry.get_report(report_version)
    if report is None:
        raise RegistryError(
            "REPORT_VERSION_NOT_FOUND",
            f"月报版本 {report_version} 尚未登记",
            status_code=404,
        )
    if not report["valid"]:
        raise RegistryError(
            "REPORT_VERSION_INVALID",
            f"月报版本 {report_version} 已失效，不能运行核查",
            status_code=409,
        )

    # LP 求解可能耗时较长，放在所有数据库事务之外执行。
    try:
        result = solver.analyze(
            matrix=report["matrix"],
            row_totals=report["row_totals"],
            col_totals=report["col_totals"],
        )
    except solver.SuppressionConflict:
        # 无解：不登记任何核查结果，交由路由层转成 409 冲突响应。
        raise
    result = _normalize_result(result, report["matrix"])

    # 写事务内重新校验：求解期间底表/月报可能已被撤回。
    return registry.register_check_result(report_version, result)


@app.post(
    "/api/v1/reports/{version}/checks",
    response_model=CheckCreatedResponse,
    status_code=201,
    responses={
        404: {"model": ErrorResponse, "description": "月报版本不存在"},
        409: {
            "model": CheckConflictResponse,
            "description": (
                "约束无解时返回 conflict 信封且不登记核查结果；"
                "月报在结果登记前已失效时返回标准 error 信封"
                "（code=REPORT_VERSION_INVALID），同样不登记"
            ),
        },
    },
    tags=["核查"],
    summary="按有效月报版本运行遮盖核查，有解才登记并返回结果及版本",
)
def run_report_check(
    registry: RegistryDep,
    version: Annotated[
        VersionStr,
        Path(
            description="月报版本号",
            openapi_examples={"report_001": {"value": EX_REPORT_VERSION}},
        ),
    ],
):
    try:
        check = execute_report_check(registry, version)
    except solver.SuppressionConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "status": "conflict",
                "reasons": exc.reasons,
                "witness": exc.witness,
            },
        )
    return {
        "check_version": check["version"],
        "report_version": check["report_version"],
        "base_version": check["base_version"],
        "valid": True,
        "created_at": check["created_at"],
        "result": check["result"],
    }


@app.get(
    "/api/v1/checks",
    response_model=list[CheckRecord],
    tags=["登记与撤回"],
    summary="列出核查结果（可按月报版本/有效性过滤）",
)
def list_checks(
    registry: RegistryDep,
    report_version: Annotated[
        VersionStr | None,
        Query(description="按月报版本过滤"),
    ] = None,
    valid: Annotated[
        bool | None,
        Query(description="true=仅有效；false=仅已失效；不带参数=全部"),
    ] = None,
):
    return registry.list_checks(report_version=report_version, valid=valid)


@app.get(
    "/api/v1/checks/{version}",
    response_model=CheckRecord,
    responses={404: {"model": ErrorResponse, "description": "核查结果版本不存在"}},
    tags=["登记与撤回"],
    summary="查询单个核查结果版本",
)
def get_check(
    registry: RegistryDep,
    version: Annotated[
        VersionStr,
        Path(description="核查结果版本号，如 CHK-000001"),
    ],
):
    record = registry.get_check(version)
    if record is None:
        raise RegistryError(
            "CHECK_VERSION_NOT_FOUND", f"核查结果版本 {version} 不存在", 404
        )
    return record


# ---------------------------------------------------------------------------
# 撤回底表：级联失效并返回须停用清单
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/base-versions/{version}/withdraw",
    response_model=WithdrawResponse,
    responses={404: {"model": ErrorResponse, "description": "底表版本不存在"}},
    tags=["登记与撤回"],
    summary="撤回底表版本，级联停用依赖它的月报与核查结果",
)
def withdraw_base(
    registry: RegistryDep,
    version: Annotated[
        VersionStr,
        Path(
            description="要撤回的底表版本号",
            openapi_examples={"base_a": {"value": EX_BASE_VERSION}},
        ),
    ],
    payload: Annotated[
        WithdrawRequest | None,
        Body(
            openapi_examples={
                "withdraw_with_reason": {
                    "summary": "发现底表漏数，撤回并级联停用",
                    "value": EX_WITHDRAW_BODY,
                }
            }
        ),
    ] = None,
):
    return registry.withdraw_base(version, reason=payload.reason if payload else None)
