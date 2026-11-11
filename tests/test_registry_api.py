"""版本登记 / 撤回 API 测试。

覆盖：
* 底表、月报、核查结果的登记、查询与“已登记版本不得覆盖”；
* 月报只能引用已登记且有效的底表；
* 核查仅对有效月报运行，有解才登记；无解约 409 且不落库；
* 撤回底表级联失效并返回须停用清单，重复撤回幂等；
* 撤回不等待求解：求解期间撤回，结果登记前复验失败则报错且不登记。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import solver
from app.db import Registry, RegistryError
from app.main import app, execute_report_check, get_registry
from app.schemas import (
    EX_BASE_VERSION,
    EX_CONFLICT_REPORT_VERSION,
    EX_HALL_CONFLICT,
    EX_RANGE,
    EX_REPORT_REGISTER,
    EX_REPORT_REGISTER_CONFLICT,
    EX_REPORT_VERSION,
    EX_WITHDRAW_BODY,
)


@pytest.fixture()
def registry(tmp_path):
    reg = Registry(str(tmp_path / "test.db"))
    app.dependency_overrides[get_registry] = lambda: reg
    yield reg
    app.dependency_overrides.clear()


@pytest.fixture()
def client(registry):
    return TestClient(app)


def _register_base(client, version=EX_BASE_VERSION, note="snapshot"):
    return client.post(
        "/api/v1/base-versions", json={"version": version, "note": note}
    )


def _register_report(client, payload=None):
    return client.post(
        "/api/v1/reports", json=payload or EX_REPORT_REGISTER
    )


# ---------------------------------------------------------------------------
# 底表登记
# ---------------------------------------------------------------------------


def test_register_base_and_get(client):
    resp = _register_base(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["version"] == EX_BASE_VERSION
    assert body["valid"] is True
    assert body["invalidated_at"] is None
    assert body["created_at"]

    got = client.get(f"/api/v1/base-versions/{EX_BASE_VERSION}")
    assert got.status_code == 200
    assert got.json()["version"] == EX_BASE_VERSION

    listed = client.get("/api/v1/base-versions").json()
    assert [b["version"] for b in listed] == [EX_BASE_VERSION]


def test_register_base_no_overwrite(client):
    assert _register_base(client).status_code == 201
    resp = _register_base(client, note="覆盖尝试")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BASE_VERSION_ALREADY_REGISTERED"
    # 原始备注未被覆盖
    assert client.get(f"/api/v1/base-versions/{EX_BASE_VERSION}").json()["note"] == (
        "snapshot"
    )


def test_get_unknown_base_404(client):
    resp = client.get("/api/v1/base-versions/NOPE")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BASE_VERSION_NOT_FOUND"


def test_blank_version_rejected(client):
    resp = client.post("/api/v1/base-versions", json={"version": "   "})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 月报登记：只接受已登记且有效的底表
# ---------------------------------------------------------------------------


def test_register_report_requires_registered_base(client):
    resp = _register_report(client)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BASE_VERSION_NOT_FOUND"


def test_register_report_requires_valid_base(client):
    _register_base(client)
    withdraw = client.post(
        f"/api/v1/base-versions/{EX_BASE_VERSION}/withdraw",
        json={"reason": "漏数"},
    )
    assert withdraw.status_code == 200
    resp = _register_report(client)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BASE_VERSION_INVALID"


def test_register_report_happy_path_and_no_overwrite(client):
    _register_base(client)
    resp = _register_report(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["version"] == EX_REPORT_VERSION
    assert body["base_version"] == EX_BASE_VERSION
    assert body["valid"] is True
    assert body["matrix"] == EX_RANGE["matrix"]

    # 已登记月报版本不得覆盖，即使引用另一个有效底表
    other = client.post(
        "/api/v1/base-versions", json={"version": "BASE-2026-11-B"}
    )
    assert other.status_code == 201
    payload = dict(EX_REPORT_REGISTER, base_version="BASE-2026-11-B")
    dup = client.post("/api/v1/reports", json=payload)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "REPORT_VERSION_ALREADY_REGISTERED"


def test_report_matrix_validation_422(client):
    _register_base(client)
    bad = dict(EX_REPORT_REGISTER, matrix=[[-1, None]], col_totals=[1, 0])
    resp = client.post("/api/v1/reports", json=bad)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 核查：有效月报、有解才登记
# ---------------------------------------------------------------------------


def test_check_unknown_report_404(client):
    resp = client.post("/api/v1/reports/MISSING/checks")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "REPORT_VERSION_NOT_FOUND"


def test_check_feasible_registers_and_returns_versioned_result(client):
    _register_base(client)
    _register_report(client)
    resp = client.post(f"/api/v1/reports/{EX_REPORT_VERSION}/checks")
    assert resp.status_code == 201
    body = resp.json()
    assert body["check_version"].startswith("CHK-")
    assert body["report_version"] == EX_REPORT_VERSION
    assert body["base_version"] == EX_BASE_VERSION
    assert body["valid"] is True
    assert body["result"]["status"] == "feasible"
    assert body["result"]["hidden_count"] == 6
    check_version = body["check_version"]

    # 核查结果可按版本查询，且出现在列表中
    got = client.get(f"/api/v1/checks/{check_version}")
    assert got.status_code == 200
    assert got.json()["version"] == check_version
    listed = client.get("/api/v1/checks").json()
    assert [c["version"] for c in listed] == [check_version]

    # 再次核查生成新的递增版本
    second = client.post(f"/api/v1/reports/{EX_REPORT_VERSION}/checks")
    assert second.status_code == 201
    assert second.json()["check_version"] != check_version
    assert len(client.get("/api/v1/checks").json()) == 2


def test_check_conflict_returns_409_and_registers_nothing(client):
    _register_base(client)
    _register_report(client, EX_REPORT_REGISTER_CONFLICT)
    resp = client.post(
        f"/api/v1/reports/{EX_CONFLICT_REPORT_VERSION}/checks"
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["witness"]["max_flow"] < body["witness"]["required_flow"]
    # 无解不登记任何核查结果
    assert client.get("/api/v1/checks").json() == []


def test_check_on_invalid_report_rejected(client):
    _register_base(client)
    _register_report(client)
    client.post(
        f"/api/v1/base-versions/{EX_BASE_VERSION}/withdraw",
        json=EX_WITHDRAW_BODY,
    )
    resp = client.post(f"/api/v1/reports/{EX_REPORT_VERSION}/checks")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REPORT_VERSION_INVALID"
    assert client.get("/api/v1/checks").json() == []


def test_check_no_hidden_cells_registers_full_matrix(client):
    _register_base(client)
    payload = {
        "version": "RPT-FIXED",
        "base_version": EX_BASE_VERSION,
        "matrix": [[1, 2], [3, 4]],
        "row_totals": [3, 7],
        "col_totals": [4, 6],
    }
    assert client.post("/api/v1/reports", json=payload).status_code == 201
    resp = client.post("/api/v1/reports/RPT-FIXED/checks")
    assert resp.status_code == 201
    result = resp.json()["result"]
    assert result["hidden_count"] == 0
    assert result["minimizer_matrix"] == payload["matrix"]
    assert result["maximizer_matrix"] == payload["matrix"]


# ---------------------------------------------------------------------------
# 撤回：级联失效 + 停用清单 + 幂等
# ---------------------------------------------------------------------------


def test_withdraw_unknown_base_404(client):
    resp = client.post(
        "/api/v1/base-versions/GHOST/withdraw", json={"reason": "x"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BASE_VERSION_NOT_FOUND"


def test_withdraw_without_body_uses_default_reason(client):
    _register_base(client)
    _register_report(client)
    resp = client.post(f"/api/v1/base-versions/{EX_BASE_VERSION}/withdraw")
    assert resp.status_code == 200
    assert resp.json()["decommissioned_reports"] == [EX_REPORT_VERSION]
    base = client.get(f"/api/v1/base-versions/{EX_BASE_VERSION}").json()
    assert base["invalidate_reason"] == "人工撤回"


def test_withdraw_cascades_and_returns_decommission_list(client):
    _register_base(client)
    _register_report(client)
    _register_report(client, EX_REPORT_REGISTER_CONFLICT)

    # 为月报 001 登记两份核查
    for _ in range(2):
        chk = client.post(f"/api/v1/reports/{EX_REPORT_VERSION}/checks")
        assert chk.status_code == 201
    check_versions = [
        c["version"] for c in client.get("/api/v1/checks").json()
    ]
    assert len(check_versions) == 2

    # 另一个有效底表及其月报不应受影响
    client.post(
        "/api/v1/base-versions", json={"version": "BASE-2026-11-B"}
    )
    untouched_payload = dict(EX_REPORT_REGISTER, version="RPT-OTHER")
    r = client.post(
        "/api/v1/reports",
        json=dict(untouched_payload, base_version="BASE-2026-11-B"),
    )
    assert r.status_code == 201

    resp = client.post(
        f"/api/v1/base-versions/{EX_BASE_VERSION}/withdraw",
        json=EX_WITHDRAW_BODY,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["already_withdrawn"] is False
    assert set(body["decommissioned_reports"]) == {
        EX_REPORT_VERSION,
        EX_CONFLICT_REPORT_VERSION,
    }
    assert set(body["decommissioned_checks"]) == set(check_versions)

    # 底表、月报、核查全部标记失效并记录原因
    assert client.get(f"/api/v1/base-versions/{EX_BASE_VERSION}").json()[
        "valid"
    ] is False
    report = client.get(f"/api/v1/reports/{EX_REPORT_VERSION}").json()
    assert report["valid"] is False
    assert EX_BASE_VERSION in report["invalidate_reason"]
    for cv in check_versions:
        assert client.get(f"/api/v1/checks/{cv}").json()["valid"] is False

    # 过滤视图
    assert client.get("/api/v1/reports", params={"valid": True}).json() == [
        client.get("/api/v1/reports/RPT-OTHER").json()
    ]
    invalid_checks = client.get(
        "/api/v1/checks", params={"valid": False}
    ).json()
    assert {c["version"] for c in invalid_checks} == set(check_versions)


def test_withdraw_idempotent(client):
    _register_base(client)
    _register_report(client)
    first = client.post(
        f"/api/v1/base-versions/{EX_BASE_VERSION}/withdraw", json={}
    )
    assert first.status_code == 200
    assert first.json()["already_withdrawn"] is False
    assert first.json()["decommissioned_reports"] == [EX_REPORT_VERSION]

    second = client.post(
        f"/api/v1/base-versions/{EX_BASE_VERSION}/withdraw",
        json={"reason": "再次撤回"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["already_withdrawn"] is True
    assert body["decommissioned_reports"] == []
    assert body["decommissioned_checks"] == []


# ---------------------------------------------------------------------------
# 并发：撤回不等求解；求解期间失效则不登记
# ---------------------------------------------------------------------------


class _BlockOnSolve:
    """挂起 solver.analyze 直到事件放行，用于模拟长求解与撤回并发。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._original = None

    def __enter__(self):
        self._original = solver.analyze

        def blocked_analyze(*args, **kwargs):
            self.entered.set()
            assert self.release.wait(timeout=10), "求解挂起超时"
            return self._original(*args, **kwargs)

        solver.analyze = blocked_analyze
        return self

    def __exit__(self, *exc):
        solver.analyze = self._original


def test_withdraw_during_solve_blocks_result_registration(registry):
    registry.register_base(EX_BASE_VERSION, note="snapshot")
    registry.register_report(
        EX_REPORT_VERSION,
        EX_BASE_VERSION,
        EX_RANGE["matrix"],
        EX_RANGE["row_totals"],
        EX_RANGE["col_totals"],
    )

    holder: dict = {}

    def worker():
        try:
            execute_report_check(registry, EX_REPORT_VERSION)
        except Exception as exc:  # noqa: BLE001 - 线程内异常回传主线程断言
            holder["error"] = exc

    with _BlockOnSolve() as blocker:
        thread = threading.Thread(target=worker)
        thread.start()
        assert blocker.entered.wait(timeout=10), "求解未如期开始"

        # 求解仍在进行时撤回：撤回立即完成，不等待求解结束
        outcome = registry.withdraw_base(EX_BASE_VERSION, reason="漏数")
        assert outcome["decommissioned_reports"] == [EX_REPORT_VERSION]
        assert registry.get_report(EX_REPORT_VERSION)["valid"] is False

        blocker.release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert isinstance(holder.get("error"), RegistryError)
    assert holder["error"].code == "REPORT_VERSION_INVALID"
    # 失效后不登记任何核查结果
    assert registry.list_checks() == []
    assert registry.list_checks(valid=True) == []


def test_solve_completes_before_withdraw_registers_normally(registry):
    registry.register_base(EX_BASE_VERSION)
    registry.register_report(
        EX_REPORT_VERSION,
        EX_BASE_VERSION,
        EX_RANGE["matrix"],
        EX_RANGE["row_totals"],
        EX_RANGE["col_totals"],
    )
    check = execute_report_check(registry, EX_REPORT_VERSION)
    assert check["valid"] is True
    assert check["result"]["status"] == "feasible"

    outcome = registry.withdraw_base(EX_BASE_VERSION)
    assert outcome["decommissioned_checks"] == [check["version"]]
    assert registry.get_check(check["version"])["valid"] is False


# ---------------------------------------------------------------------------
# Swagger 文档
# ---------------------------------------------------------------------------


def test_openapi_documents_full_lifecycle_examples(client):
    schema = client.get("/openapi.json").json()

    base_post = schema["paths"]["/api/v1/base-versions"]["post"]
    assert base_post["requestBody"]["content"]["application/json"]["examples"]

    report_post = schema["paths"]["/api/v1/reports"]["post"]
    report_examples = report_post["requestBody"]["content"]["application/json"][
        "examples"
    ]
    assert "feasible_report" in report_examples
    assert "conflict_report" in report_examples

    check_post = schema["paths"]["/api/v1/reports/{version}/checks"]["post"]
    check_param_examples = check_post["parameters"][0]["examples"]
    assert check_param_examples["report_001"]["value"] == EX_REPORT_VERSION

    withdraw_post = schema["paths"][
        "/api/v1/base-versions/{version}/withdraw"
    ]["post"]
    withdraw_param_examples = withdraw_post["parameters"][0]["examples"]
    assert withdraw_param_examples["base_a"]["value"] == EX_BASE_VERSION
    assert withdraw_post["requestBody"]["content"]["application/json"][
        "examples"
    ]

    assert client.get("/docs").status_code == 200


def test_hall_conflict_payload_still_valid_for_report(client):
    # EX_HALL_CONFLICT 本身可通过月报结构校验（无解在核查阶段才暴露）
    _register_base(client)
    resp = _register_report(client, EX_REPORT_REGISTER_CONFLICT)
    assert resp.status_code == 201
    assert resp.json()["matrix"] == EX_HALL_CONFLICT["matrix"]
