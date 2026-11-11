"""底表版本、月报版本与核查结果的 SQLite 登记中心。

登记模型
--------
* ``base_versions``：病例底表版本，登记后可被撤回（valid=0），版本号唯一且
  已登记（含已撤回）的版本永远不能覆盖。
* ``reports``：待发布月报，登记时必须引用一个**已登记且有效**的底表版本；
  保存计数矩阵、公开行/列合计。底表被撤回时级联失效。
* ``checks``：按有效月报运行遮盖核查（``app.solver.analyze``）后，仅在有解
  且月报仍然有效时才登记的核查结果，版本号全局递增。

并发约定
--------
SQLite 启用 WAL 与 ``busy_timeout``；所有写操作在 ``BEGIN IMMEDIATE``
事务中完成。遮盖核查的 LP 求解发生在事务之外（可能耗时较长），因此撤回
底表只需要拿到一个短暂的写事务，不必等待任何求解结束。求解完成后的结果
登记会在写事务内重新校验月报有效性：若月报已在求解期间失效，则报错且
不写入任何核查结果。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# 错误码 -> HTTP 状态码由 main 层的异常处理器统一映射。
BASE_NOT_FOUND = "BASE_VERSION_NOT_FOUND"
BASE_ALREADY_REGISTERED = "BASE_VERSION_ALREADY_REGISTERED"
BASE_ALREADY_WITHDRAWN = "BASE_VERSION_ALREADY_WITHDRAWN"
BASE_INVALID = "BASE_VERSION_INVALID"
REPORT_NOT_FOUND = "REPORT_VERSION_NOT_FOUND"
REPORT_ALREADY_REGISTERED = "REPORT_VERSION_ALREADY_REGISTERED"
REPORT_INVALID = "REPORT_VERSION_INVALID"
CHECK_NOT_FOUND = "CHECK_VERSION_NOT_FOUND"


class RegistryError(Exception):
    """登记中心业务错误，携带稳定错误码与 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS base_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    note TEXT,
    valid INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidate_reason TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    base_version TEXT NOT NULL,
    matrix TEXT NOT NULL,
    row_totals TEXT NOT NULL,
    col_totals TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidate_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_base_version ON reports(base_version);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    report_version TEXT NOT NULL,
    base_version TEXT NOT NULL,
    result TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidate_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_report_version ON checks(report_version);
CREATE INDEX IF NOT EXISTS idx_checks_base_version ON checks(base_version);
"""


class Registry:
    """线程安全的 SQLite 登记中心；每次操作使用独立短连接。"""

    def __init__(self, db_path: str):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # -- 连接管理 -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # WAL 持久化在数据库文件中，重复设置无副作用；busy_timeout 按连接生效。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _tx(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- 行映射 -------------------------------------------------------------

    @staticmethod
    def _base_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": row["version"],
            "note": row["note"],
            "valid": bool(row["valid"]),
            "created_at": row["created_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidate_reason": row["invalidate_reason"],
        }

    @staticmethod
    def _report_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": row["version"],
            "base_version": row["base_version"],
            "matrix": json.loads(row["matrix"]),
            "row_totals": json.loads(row["row_totals"]),
            "col_totals": json.loads(row["col_totals"]),
            "valid": bool(row["valid"]),
            "created_at": row["created_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidate_reason": row["invalidate_reason"],
        }

    @staticmethod
    def _check_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version": row["version"],
            "report_version": row["report_version"],
            "base_version": row["base_version"],
            "result": json.loads(row["result"]),
            "valid": bool(row["valid"]),
            "created_at": row["created_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidate_reason": row["invalidate_reason"],
        }

    # -- 底表版本 -----------------------------------------------------------

    def register_base(self, version: str, note: str | None = None) -> dict[str, Any]:
        """登记一个有效的底表版本；版本号已存在（含已撤回）一律拒绝覆盖。"""
        with self._tx(immediate=True) as conn:
            existing = conn.execute(
                "SELECT id FROM base_versions WHERE version = ?", (version,)
            ).fetchone()
            if existing is not None:
                raise RegistryError(
                    BASE_ALREADY_REGISTERED,
                    f"底表版本 {version} 已登记，已登记版本不得覆盖",
                    status_code=409,
                )
            now = _utc_now()
            conn.execute(
                "INSERT INTO base_versions(version, note, valid, created_at) "
                "VALUES (?, ?, 1, ?)",
                (version, note, now),
            )
            row = conn.execute(
                "SELECT * FROM base_versions WHERE version = ?", (version,)
            ).fetchone()
            return self._base_row(row)

    def get_base(self, version: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM base_versions WHERE version = ?", (version,)
            ).fetchone()
            return self._base_row(row) if row else None

    def list_bases(self, valid: bool | None = None) -> list[dict[str, Any]]:
        with self._tx() as conn:
            if valid is None:
                rows = conn.execute(
                    "SELECT * FROM base_versions ORDER BY id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM base_versions WHERE valid = ? ORDER BY id",
                    (1 if valid else 0,),
                ).fetchall()
            return [self._base_row(r) for r in rows]

    # -- 月报版本 -----------------------------------------------------------

    def register_report(
        self,
        version: str,
        base_version: str,
        matrix: list[list[int | None]],
        row_totals: list[int],
        col_totals: list[int],
    ) -> dict[str, Any]:
        """登记月报；仅当底表版本已登记且有效时接受，版本号不得覆盖。"""
        with self._tx(immediate=True) as conn:
            base = conn.execute(
                "SELECT * FROM base_versions WHERE version = ?", (base_version,)
            ).fetchone()
            if base is None:
                raise RegistryError(
                    BASE_NOT_FOUND,
                    f"底表版本 {base_version} 尚未登记，不能登记依赖它的月报",
                    status_code=404,
                )
            if not base["valid"]:
                raise RegistryError(
                    BASE_INVALID,
                    f"底表版本 {base_version} 已失效（被撤回），不能登记依赖它的月报",
                    status_code=409,
                )
            existing = conn.execute(
                "SELECT id FROM reports WHERE version = ?", (version,)
            ).fetchone()
            if existing is not None:
                raise RegistryError(
                    REPORT_ALREADY_REGISTERED,
                    f"月报版本 {version} 已登记，已登记版本不得覆盖",
                    status_code=409,
                )
            now = _utc_now()
            conn.execute(
                "INSERT INTO reports(version, base_version, matrix, row_totals, "
                "col_totals, valid, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (
                    version,
                    base_version,
                    json.dumps(matrix, ensure_ascii=False),
                    json.dumps(row_totals, ensure_ascii=False),
                    json.dumps(col_totals, ensure_ascii=False),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM reports WHERE version = ?", (version,)
            ).fetchone()
            return self._report_row(row)

    def get_report(self, version: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM reports WHERE version = ?", (version,)
            ).fetchone()
            return self._report_row(row) if row else None

    def list_reports(
        self,
        base_version: str | None = None,
        valid: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if base_version is not None:
            clauses.append("base_version = ?")
            params.append(base_version)
        if valid is not None:
            clauses.append("valid = ?")
            params.append(1 if valid else 0)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT * FROM reports{where} ORDER BY id", params
            ).fetchall()
            return [self._report_row(r) for r in rows]

    # -- 核查结果 -----------------------------------------------------------

    def register_check_result(
        self, report_version: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """登记一次有解核查的结果。

        在写事务内重新校验月报与底表有效性：月报在求解期间被撤回级联失效时
        抛出 :class:`RegistryError`，不写入任何核查记录。核查版本号由
        自增主键生成，全局递增、唯一。
        """
        with self._tx(immediate=True) as conn:
            report = conn.execute(
                "SELECT * FROM reports WHERE version = ?", (report_version,)
            ).fetchone()
            if report is None:
                raise RegistryError(
                    REPORT_NOT_FOUND,
                    f"月报版本 {report_version} 尚未登记",
                    status_code=404,
                )
            if not report["valid"]:
                raise RegistryError(
                    REPORT_INVALID,
                    f"月报版本 {report_version} 已失效，拒绝登记核查结果",
                    status_code=409,
                )
            base = conn.execute(
                "SELECT * FROM base_versions WHERE version = ?",
                (report["base_version"],),
            ).fetchone()
            if base is None or not base["valid"]:
                # 正常情况下底表撤回会级联使月报失效；此处为防御性校验。
                raise RegistryError(
                    BASE_INVALID,
                    f"月报 {report_version} 依赖的底表版本 "
                    f"{report['base_version']} 已失效，拒绝登记核查结果",
                    status_code=409,
                )
            now = _utc_now()
            cur = conn.execute(
                "INSERT INTO checks(version, report_version, base_version, "
                "result, valid, created_at) VALUES ('', ?, ?, ?, 1, ?)",
                (
                    report_version,
                    report["base_version"],
                    json.dumps(result, ensure_ascii=False),
                    now,
                ),
            )
            check_id = cur.lastrowid
            check_version = f"CHK-{check_id:06d}"
            conn.execute(
                "UPDATE checks SET version = ? WHERE id = ?",
                (check_version, check_id),
            )
            row = conn.execute(
                "SELECT * FROM checks WHERE id = ?", (check_id,)
            ).fetchone()
            return self._check_row(row)

    def get_check(self, version: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM checks WHERE version = ?", (version,)
            ).fetchone()
            return self._check_row(row) if row else None

    def list_checks(
        self,
        report_version: str | None = None,
        valid: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if report_version is not None:
            clauses.append("report_version = ?")
            params.append(report_version)
        if valid is not None:
            clauses.append("valid = ?")
            params.append(1 if valid else 0)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT * FROM checks{where} ORDER BY id", params
            ).fetchall()
            return [self._check_row(r) for r in rows]

    # -- 撤回与级联失效 -----------------------------------------------------

    def withdraw_base(
        self, version: str, reason: str | None = None
    ) -> dict[str, Any]:
        """撤回底表版本，级联失效依赖它的月报与核查结果。

        返回当前仍有效、因此必须停用的月报/核查版本列表。重复撤回是幂等的：
        返回 ``already_withdrawn=True`` 与空列表，不报错。
        """
        with self._tx(immediate=True) as conn:
            base = conn.execute(
                "SELECT * FROM base_versions WHERE version = ?", (version,)
            ).fetchone()
            if base is None:
                raise RegistryError(
                    BASE_NOT_FOUND,
                    f"底表版本 {version} 尚未登记，无法撤回",
                    status_code=404,
                )
            if not base["valid"]:
                return {
                    "base_version": version,
                    "already_withdrawn": True,
                    "decommissioned_reports": [],
                    "decommissioned_checks": [],
                    "invalidated_at": base["invalidated_at"],
                }

            now = _utc_now()
            base_reason = reason or "人工撤回"
            cascade_reason = f"依赖的底表版本 {version} 被撤回"

            report_rows = conn.execute(
                "SELECT version FROM reports WHERE base_version = ? AND valid = 1 "
                "ORDER BY id",
                (version,),
            ).fetchall()
            check_rows = conn.execute(
                "SELECT version FROM checks WHERE base_version = ? AND valid = 1 "
                "ORDER BY id",
                (version,),
            ).fetchall()
            decommissioned_reports = [r["version"] for r in report_rows]
            decommissioned_checks = [r["version"] for r in check_rows]

            conn.execute(
                "UPDATE base_versions SET valid = 0, invalidated_at = ?, "
                "invalidate_reason = ? WHERE version = ?",
                (now, base_reason, version),
            )
            conn.execute(
                "UPDATE reports SET valid = 0, invalidated_at = ?, "
                "invalidate_reason = ? WHERE base_version = ? AND valid = 1",
                (now, cascade_reason, version),
            )
            conn.execute(
                "UPDATE checks SET valid = 0, invalidated_at = ?, "
                "invalidate_reason = ? WHERE base_version = ? AND valid = 1",
                (now, cascade_reason, version),
            )
            return {
                "base_version": version,
                "already_withdrawn": False,
                "decommissioned_reports": decommissioned_reports,
                "decommissioned_checks": decommissioned_checks,
                "invalidated_at": now,
            }
