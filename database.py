import sqlite3
import json
import os
import threading
from datetime import datetime
from contextlib import contextmanager
from config import settings
from logging_config import get_logger

logger = get_logger(__name__)

DB_PATH = settings.DB_PATH

# 线程本地存储连接（简单的连接池模式）
_thread_local = threading.local()

# 初始化标志
_db_initialized = False
_init_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    """获取当前线程的数据库连接（带 WAL 模式）"""
    if not hasattr(_thread_local, 'conn') or _thread_local.conn is None:
        conn = sqlite3.connect(
            DB_PATH,
            check_same_thread=False,
            timeout=30.0
        )
        # 开启 WAL 模式：读写不阻塞，并发性能大幅提升
        conn.execute("PRAGMA journal_mode=WAL")
        # 同步模式 NORMAL：平衡性能与安全
        conn.execute("PRAGMA synchronous=NORMAL")
        # 缓存大小：-32768 = 32MB 页缓存
        conn.execute("PRAGMA cache_size=-32768")
        # 临时表存内存
        conn.execute("PRAGMA temp_store=MEMORY")
        # 启用外键约束
        conn.execute("PRAGMA foreign_keys=ON")

        conn.row_factory = sqlite3.Row
        _thread_local.conn = conn
        logger.debug("db_connection_created", thread_id=threading.get_ident())

    return _thread_local.conn


@contextmanager
def get_db_connection():
    """上下文管理器：自动提交/回滚"""
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_columns(cursor: sqlite3.Cursor, table: str, columns: list):
    """补齐旧表缺失的列（轻量级自动迁移）"""
    existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, col_type in columns:
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")
            logger.info("db_column_added", table=table, column=name)


def _backfill_total_tokens(cursor: sqlite3.Cursor):
    """为老记录补齐 total_tokens 实列。

    该列是后加的：老库里只有 token_usage JSON，聚合统计得逐行解析。
    这里一次性回填，之后 get_stats 就能直接 SUM，不必每次全表扫 JSON。
    解析失败（空串 / 非法 JSON / 历史脏数据）一律记 0，不能让统计因此崩掉。
    """
    rows = cursor.execute(
        "SELECT id, token_usage FROM diagnosis_records WHERE total_tokens IS NULL"
    ).fetchall()
    if not rows:
        return

    updates = []
    for record_id, raw in rows:
        value = 0
        if raw:
            try:
                value = int((json.loads(raw) or {}).get("total_tokens", 0) or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                value = 0
        updates.append((value, record_id))

    cursor.executemany("UPDATE diagnosis_records SET total_tokens = ? WHERE id = ?", updates)
    logger.info("db_total_tokens_backfilled", rows=len(updates))


def init_db():
    """初始化数据库表（线程安全，仅执行一次）"""
    global _db_initialized
    if _db_initialized:
        return

    with _init_lock:
        if _db_initialized:
            return

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS diagnosis_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    fault_description TEXT NOT NULL,
                    status TEXT,
                    diagnosis TEXT,
                    review TEXT,
                    rebuttal TEXT,
                    final_review TEXT,
                    cost TEXT,
                    workorder TEXT,
                    debate_round INTEGER,
                    token_usage TEXT,  -- 完整 Token 用量明细（含按节点归因）
                    total_tokens INTEGER DEFAULT 0,  -- 冗余总用量，专供聚合统计，避免全表解析 JSON
                    correlation_id TEXT  -- 追踪 ID：把历史记录与那一轮的结构化日志串起来
                )
            """)
            # 迁移旧库：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
            # 因此需要显式检查并补齐缺失字段，否则旧库会报 "no such column"
            _ensure_columns(cursor, "diagnosis_records", [
                ("debate_round", "INTEGER"),
                ("token_usage", "TEXT"),
                ("total_tokens", "INTEGER DEFAULT 0"),
                ("correlation_id", "TEXT"),
            ])
            _backfill_total_tokens(cursor)
            # 创建索引加速查询
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON diagnosis_records(created_at DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON diagnosis_records(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fault_desc ON diagnosis_records(fault_description)")

        _db_initialized = True
        logger.info("database_initialized", path=DB_PATH)


def close_db_connections():
    """关闭当前线程的数据库连接（用于测试或清理）"""
    if hasattr(_thread_local, 'conn') and _thread_local.conn:
        _thread_local.conn.close()
        _thread_local.conn = None


def save_diagnosis_record(fault_description: str, result: dict, token_usage: dict = None):
    """保存诊断记录（含 Token 使用统计）。

    返回新记录的 id（失败返回 None）。前端需要它来拼工单下载地址——
    工单接口是 /records/{id}/workorder.md，没有 id 就只能让用户去历史页翻，
    这是个不必要的来回。
    """
    init_db()
    usage = token_usage or {}
    try:
        total_tokens = int((usage or {}).get("total_tokens", 0) or 0)
    except (TypeError, ValueError):
        total_tokens = 0

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO diagnosis_records (
                created_at, fault_description, status,
                diagnosis, review, rebuttal, final_review,
                cost, workorder, debate_round, token_usage, total_tokens, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            fault_description,
            result.get("status", ""),
            json.dumps(result.get("diagnosis", {}), ensure_ascii=False),
            json.dumps(result.get("review", {}), ensure_ascii=False),
            json.dumps(result.get("rebuttal", {}), ensure_ascii=False),
            json.dumps(result.get("final_review", {}), ensure_ascii=False),
            json.dumps(result.get("cost", {}), ensure_ascii=False),
            json.dumps(result.get("workorder", {}), ensure_ascii=False),
            result.get("debate_round", 0),
            json.dumps(usage, ensure_ascii=False),
            total_tokens,
            result.get("correlation_id", "")
        ))
        # lastrowid 必须在同一个连接作用域内取；出了 with 块游标就失效了。
        record_id = cursor.lastrowid

    logger.info("diagnosis_saved", fault_desc=fault_description[:50],
                correlation_id=result.get("correlation_id", ""), record_id=record_id)
    return record_id


def get_distinct_statuses():
    """获取所有不同的状态值"""
    init_db()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT status FROM diagnosis_records ORDER BY status")
        return [row[0] for row in cursor.fetchall() if row[0]]


def _build_filter_sql(keyword: str, status: str):
    """构造关键词/状态过滤条件，供 get_records 与 count_records 共用。

    两处各写一份过滤逻辑，早晚会出现"列表筛了、总数没筛"这类不一致。
    """
    sql = " WHERE 1=1"
    params = []
    if keyword:
        sql += " AND fault_description LIKE ?"
        params.append(f"%{keyword}%")
    if status:
        sql += " AND status = ?"
        params.append(status)
    return sql, params


# 需要从 JSON 文本反解析成 dict 的列
_JSON_COLUMNS = ("diagnosis", "review", "rebuttal", "final_review", "cost", "workorder", "token_usage")


def _parse_json_columns(row: dict) -> dict:
    """把行里的 JSON 文本列反解析成 dict，就地修改并返回。

    get_records / get_record_by_id 共用。此前两份逐字相同的循环各写一遍，
    加一个 JSON 列就要改两处，漏一处就会出现"列表里有、详情里没有"。
    解析失败（历史脏数据 / 空串）一律回退成 {}：一条坏记录不该把整个查询打挂。
    """
    for col in _JSON_COLUMNS:
        raw = row.get(col)
        if raw:
            try:
                row[col] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                row[col] = {}
        else:
            row[col] = {}
    return row


def count_records(keyword: str = "", status: str = "") -> int:
    """统计符合条件的记录总数。

    必须与 get_records 分开：get_records 带 LIMIT，拿它的行数当"总数"，
    分页时 total 会永远等于 limit，调用方无法判断"还有没有更多"。
    """
    init_db()
    where, params = _build_filter_sql(keyword, status)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM diagnosis_records{where}", params)
        return cursor.fetchone()[0]


def get_records(keyword: str = "", status: str = "", limit: int = 100, offset: int = 0):
    """按关键词/状态查询诊断历史，JSON 字段反解析为 dict。

    带 offset 才真的能翻页。此前只有 limit：total 修对之后调用方知道了"还有更多"，
    却没有任何办法把第二页取出来，等于只修了一半。
    """
    init_db()
    # 负数 offset 在不同 SQLite 版本上行为不一致，这里统一夹到 0
    offset = max(0, int(offset))
    where, params = _build_filter_sql(keyword, status)
    sql = f"SELECT * FROM diagnosis_records{where} ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()]

    return [_parse_json_columns(r) for r in rows]


def get_record_by_id(record_id: int):
    """根据 ID 获取单条记录"""
    init_db()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM diagnosis_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()

    if not row:
        return None

    return _parse_json_columns(dict(row))


def delete_record(record_id: int) -> bool:
    """删除指定记录"""
    init_db()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM diagnosis_records WHERE id = ?", (record_id,))
        return cursor.rowcount > 0


def get_stats():
    """获取统计信息"""
    init_db()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM diagnosis_records")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT status, COUNT(*) FROM diagnosis_records GROUP BY status")
        by_status = dict(cursor.fetchall())

        cursor.execute("SELECT AVG(debate_round) FROM diagnosis_records WHERE debate_round > 0")
        avg_debate = cursor.fetchone()[0] or 0

        # 直接聚合冗余列。此前是 SELECT token_usage 全表取回、逐行 json.loads，
        # 每打开一次统计页就要把全部历史读进内存解析一遍；旧库的脏数据/空值
        # 由 init_db 的 _backfill_total_tokens 一次性清洗回填。
        # 用 COALESCE 兜底，避免空表时返回 NULL 把调用方带进 None 运算。
        cursor.execute("SELECT COALESCE(SUM(total_tokens), 0) FROM diagnosis_records")
        total_tokens = cursor.fetchone()[0] or 0

    # 数据库文件大小。原本是 Streamlit 统计页在前端 `Path(DB_PATH).stat()` 读的，
    # 迁到 React 之后前端拿不到 DB 路径（也不该知道），所以由这里一并返回。
    # 读不到就给 0 而不是抛异常：这只是个展示用的次要指标，
    # 不该因为 stat 失败把整个统计接口打挂。
    try:
        db_size_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    except OSError:
        db_size_bytes = 0

    return {
        "total_records": total,
        "by_status": by_status,
        "avg_debate_rounds": round(avg_debate, 1),
        "total_tokens": total_tokens,
        "db_size_bytes": db_size_bytes,
    }