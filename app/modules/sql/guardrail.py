import re
from dataclasses import dataclass
from app.core.config import settings

DENY_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter", "truncate", "create", "replace",
    "grant", "revoke", "commit", "rollback", "set", "call", "load", "outfile", "dumpfile"
]

@dataclass
class GuardrailResult:
    ok: bool
    reason: str | None
    rewritten_sql: str | None

def _strip_comments(sql: str) -> str:
    # 去掉 -- 注释 和 /* */ 注释（够用）
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--.*?$", " ", sql, flags=re.M)
    return sql

def _normalize(sql: str) -> str:
    sql = _strip_comments(sql).strip()
    # 连续空白归一
    sql = re.sub(r"\s+", " ", sql)
    return sql

def _has_multiple_statements(sql: str) -> bool:
    # 禁止分号多语句；允许末尾一个分号（会被去掉）
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].strip()
    return ";" in s

def _is_select(sql: str) -> bool:
    s = sql.lstrip().lower()
    # 允许 with ... select ...
    if s.startswith("with "):
        return " select " in s or s.startswith("with") and "select" in s
    return s.startswith("select ")

def _contains_deny(sql: str) -> str | None:
    low = sql.lower()
    for kw in DENY_KEYWORDS:
        # 单词边界，避免误伤字段名
        if re.search(rf"\b{re.escape(kw)}\b", low):
            return kw
    return None

def _rewrite_limit(sql: str) -> tuple[str, bool]:
    """
    - 无 LIMIT：追加 LIMIT default
    - 有 LIMIT 且 > max：改成 max
    返回 (new_sql, truncated_flag)
    """
    default_limit = settings.SQL_DEFAULT_LIMIT
    max_limit = settings.SQL_MAX_LIMIT

    low = sql.lower()
    m = re.search(r"\blimit\s+(\d+)(\s+offset\s+\d+)?\b", low)
    if not m:
        return f"{sql} LIMIT {default_limit}", False

    # 有 limit，检查大小
    try:
        n = int(m.group(1))
    except Exception:
        return sql, False

    if n > max_limit:
        # 用正则替换第一个 limit 数字
        new_sql = re.sub(r"(?i)\blimit\s+\d+", f"LIMIT {max_limit}", sql, count=1)
        return new_sql, True

    return sql, False

def validate_and_rewrite(sql: str) -> GuardrailResult:
    if not sql or not sql.strip():
        return GuardrailResult(False, "SQL 为空", None)

    sql_n = _normalize(sql)
    # 去掉末尾分号
    if sql_n.endswith(";"):
        sql_n = sql_n[:-1].strip()

    if _has_multiple_statements(sql_n):
        return GuardrailResult(False, "禁止多语句（包含分号）", None)

    if not _is_select(sql_n):
        return GuardrailResult(False, "仅允许 SELECT（含 WITH...SELECT）", None)

    hit = _contains_deny(sql_n)
    if hit:
        return GuardrailResult(False, f"命中禁用关键字: {hit}", None)

    rewritten, _ = _rewrite_limit(sql_n)
    return GuardrailResult(True, None, rewritten)
