# -*- coding: utf-8 -*-
"""数据库连接与初始化。

配置优先级（从高到低）：
1. 环境变量 `TRASH_SORTER_DB_URL`
2. `backend/config/settings.local.yaml` 的 `database.url`（**本地文件，已 gitignore**）
3. `backend/config/settings.yaml` 的 `database.url`
4. 兜底 SQLite：`data/trash_sorter.db`（换机器/没装 MySQL 也能跑）

设计原则：**数据库不可用时系统必须照常工作**。
所有对外函数都不抛异常，失败只返回状态，由界面显示“数据库未连接”。
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

_LOGGER = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BACKEND_DIR / "config"
ROOT_DIR = BACKEND_DIR.parent
DATA_DIR = ROOT_DIR / "data"

_lock = threading.Lock()
_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None
_last_error: Optional[str] = None


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("读取 %s 失败: %s", path.name, exc)
        return {}


def database_config() -> dict[str, Any]:
    """合并本地配置与默认配置（本地优先）。"""
    merged = dict(_read_yaml(CONFIG_DIR / "settings.yaml").get("database") or {})
    merged.update(_read_yaml(CONFIG_DIR / "settings.local.yaml").get("database") or {})
    return merged


def resolve_url() -> str:
    env = os.environ.get("TRASH_SORTER_DB_URL")
    if env:
        return env
    url = str(database_config().get("url") or "").strip()
    if url:
        return url
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + (DATA_DIR / "trash_sorter.db").as_posix()


def _engine_options(url: str) -> dict[str, Any]:
    cfg = database_config()
    opts: dict[str, Any] = {
        "future": True,
        "pool_pre_ping": True,
        "echo": bool(cfg.get("echo", False)),
    }
    if url.startswith("sqlite"):
        opts["connect_args"] = {"check_same_thread": False}
    else:
        opts["pool_size"] = int(cfg.get("pool_size", 5))
        opts["max_overflow"] = int(cfg.get("max_overflow", 5))
        opts["pool_recycle"] = int(cfg.get("pool_recycle", 1800))
    return opts


def init(force: bool = False) -> bool:
    """建立引擎并建表。返回是否可用（不抛异常）。"""
    global _engine, _session_factory, _last_error
    with _lock:
        if _engine is not None and not force:
            return _last_error is None
        url = resolve_url()
        try:
            engine = create_engine(url, **_engine_options(url))
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            Base.metadata.create_all(engine)
            _ensure_indexes(engine)
            _engine = engine
            _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
            _last_error = None
            safe_url = url.split("@")[-1] if "@" in url else url
            _LOGGER.info("数据库已连接：%s", safe_url)
            return True
        except Exception as exc:  # noqa: BLE001
            _engine = None
            _session_factory = None
            _last_error = str(exc)
            _LOGGER.warning("数据库不可用（记录功能将关闭）：%s", exc)
            return False


def available() -> bool:
    return _engine is not None and _last_error is None


def error_text() -> Optional[str]:
    return _last_error


def session() -> Session:
    """取得一个会话。调用方负责 `with` 或 close。"""
    if _session_factory is None:
        raise RuntimeError("数据库尚未初始化")
    return _session_factory()


def status() -> dict[str, Any]:
    """给前端显示用的状态（不抛异常）。"""
    info: dict[str, Any] = {
        "configured": bool(resolve_url()),
        "available": available(),
        "error": _last_error,
    }
    if not available():
        return info
    try:
        with session() as db:
            row = db.execute(text("SELECT VERSION(), DATABASE()")).first()
            if row is not None:
                info["version"] = str(row[0])
                info["database"] = str(row[1])
            info["records"] = int(db.execute(text("SELECT COUNT(*) FROM sort_records")).scalar() or 0)
            try:
                info["size_bytes"] = int(
                    db.execute(
                        text(
                            "SELECT COALESCE(SUM(data_length + index_length), 0) "
                            "FROM information_schema.TABLES WHERE table_schema = DATABASE()"
                        )
                    ).scalar()
                    or 0
                )
            except Exception:  # noqa: BLE001  SQLite 没有 information_schema
                info["size_bytes"] = None
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    return info

# 已存在的老库不会因为 create_all 得到新索引，这里做一次幂等补齐
_INDEX_STATEMENTS = (
    "CREATE INDEX idx_records_verified_time ON sort_records (verified, created_at)",
    "CREATE INDEX idx_records_dataset_time ON sort_records (in_dataset, created_at)",
    "CREATE INDEX idx_dataset_split_class ON dataset_images (split, class_name)",
)


def _ensure_indexes(engine: Engine) -> None:
    """补齐复合索引（已存在则忽略；MySQL/SQLite 都不支持 IF NOT EXISTS 索引）。"""
    for sql in _INDEX_STATEMENTS:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            _LOGGER.info("已补索引：%s", sql.split(" ON ")[1].split(" ")[0])
        except Exception:  # noqa: BLE001
            continue
