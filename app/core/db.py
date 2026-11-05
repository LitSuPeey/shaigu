# -*- coding: utf-8 -*-
"""SQLite 连接管理：每线程只读 + 全局写连接（WAL 模式并发安全）。"""
import os
import sqlite3
import threading

_tls = threading.local()
_WRITE_CONN = None
_WRITE_LOCK = threading.Lock()
_DB_PATH = None


def set_db_path(path: str) -> None:
    global _DB_PATH, _WRITE_CONN
    with _WRITE_LOCK:
        if _WRITE_CONN is not None:
            try:
                _WRITE_CONN.close()
            except Exception:
                pass
        _WRITE_CONN = None
    _DB_PATH = path
    _tls.conn = None
    _tls.path = None


def db_path() -> str:
    return _DB_PATH or ""


def reader() -> sqlite3.Connection:
    """每线程独立的只读连接（WAL 下可与写并发）。"""
    path = _DB_PATH
    cur = getattr(_tls, "conn", None)
    if getattr(_tls, "path", None) == path and cur is not None:
        return cur
    abs_path = os.path.abspath(path).replace("\\", "/")
    conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True, timeout=60)
    # 性能优化：适度 page cache（多线程各连接独立，不宜过大）、内存临时表、只读模式
    conn.execute("PRAGMA cache_size = -65536")    # 64MB page cache（负数=KB）
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA query_only = ON")
    _tls.conn = conn
    _tls.path = path
    return conn


def writer() -> sqlite3.Connection:
    """全局写连接（需加锁使用，WAL 模式）。首次使用时自动创建 data 目录与库文件。"""
    global _WRITE_CONN
    if _WRITE_CONN is not None:
        return _WRITE_CONN
    with _WRITE_LOCK:
        if _WRITE_CONN is None:
            parent = os.path.dirname(os.path.abspath(_DB_PATH))
            if parent:
                os.makedirs(parent, exist_ok=True)   # 空库首启：data 目录可能不存在
            conn = sqlite3.connect(_DB_PATH, timeout=60, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size = -65536")    # 64MB page cache
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.row_factory = sqlite3.Row
            _WRITE_CONN = conn
    return _WRITE_CONN


def write_lock() -> threading.Lock:
    return _WRITE_LOCK


def ensure_schema(conn: sqlite3.Connection) -> None:
    """确保所有表/索引存在（首次启动或库被外部清空时）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            code TEXT PRIMARY KEY, name TEXT, sector TEXT,
            exchange TEXT, listing_date TEXT);
        CREATE TABLE IF NOT EXISTS daily (
            code TEXT, date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            amount REAL, turnover REAL, outstanding_share REAL,
            PRIMARY KEY (code, date));
        CREATE INDEX IF NOT EXISTS idx_daily_code ON daily(code);
        CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date);
        CREATE TABLE IF NOT EXISTS valuation (
            code TEXT, date TEXT, pe_ttm REAL, pb REAL,
            PRIMARY KEY (code, date));
        CREATE INDEX IF NOT EXISTS idx_valuation_code ON valuation(code);
        CREATE INDEX IF NOT EXISTS idx_valuation_date ON valuation(date);
        CREATE TABLE IF NOT EXISTS dividend (
            code TEXT, ex_date TEXT, cash_per_10 REAL,
            PRIMARY KEY (code, ex_date));
        CREATE TABLE IF NOT EXISTS sector_map (code TEXT PRIMARY KEY, sector TEXT);
        CREATE TABLE IF NOT EXISTS sector_boards (name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS ant_index (
            symbol TEXT, date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, date));
        CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS snapshot (
            code TEXT PRIMARY KEY, date TEXT,
            close REAL, prev_close REAL, pct_chg REAL,
            volume REAL, amount REAL, turnover REAL,
            pe_ttm REAL, pb REAL, pe_pct REAL, pb_pct REAL,
            div_yield REAL,
            -- 多窗口估值分位（1/3/5/10 年；pe_pct/pb_pct 即 10 年档）
            pe_pct_1y REAL, pe_pct_3y REAL, pe_pct_5y REAL,
            pb_pct_1y REAL, pb_pct_3y REAL, pb_pct_5y REAL,
            val_n_1y REAL, val_n_3y REAL, val_n_5y REAL, val_n_10y REAL);
    """)
    conn.commit()


def db_stats(conn: sqlite3.Connection) -> dict:
    """数据库统计。"""
    s = {}
    s["stocks"] = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    s["daily_rows"] = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    s["val_rows"] = conn.execute("SELECT COUNT(*) FROM valuation").fetchone()[0]
    s["div_rows"] = conn.execute("SELECT COUNT(*) FROM dividend").fetchone()[0]
    s["sectors"] = conn.execute(
        "SELECT COUNT(DISTINCT sector) FROM meta").fetchone()[0]
    s["daily_max"] = (conn.execute("SELECT MAX(date) FROM daily").fetchone() or ("",))[0]
    s["val_max"] = (conn.execute("SELECT MAX(date) FROM valuation").fetchone() or ("",))[0]
    return s