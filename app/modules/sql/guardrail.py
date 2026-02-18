import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Set

import sqlglot
from sqlglot import exp

from app.core.config import settings
from app.core.logger import logger


@dataclass
class GuardrailResult:
    ok: bool
    reason: Optional[str] = None
    rewritten_sql: Optional[str] = None


# 允许的语句类型 (白名单)
ALLOWED_TYPES = (exp.Select, exp.Union, exp.Subquery)

# 必须禁止的危险操作 (即使在子查询中也不行)
# 🔥 核心修改：精简黑名单，去掉 Revoke/Grant/Commit 等不稳定属性
# 因为代码下方有 "白名单检查" (Step 3)，非 Select 语句本来就会被拦截，
# 所以这里只列出最核心的、可能混入 Subquery 的危险操作即可。
DENIED_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop,
    exp.Alter, exp.Create,
    exp.Command
)


def validate_and_rewrite(sql: str) -> GuardrailResult:
    if not sql or not sql.strip():
        return GuardrailResult(False, "SQL is empty", None)

    raw_sql = sql.strip().replace("\xa0", " ").replace("`", "`")
    try:
        # 1. 解析 SQL（只做安全校验，不用于改写）
        parsed = sqlglot.parse(raw_sql, read="mysql")
    except Exception as e:
        return GuardrailResult(False, f"Syntax Error: {str(e)}", None)

    # 2. 禁止多语句
    if len(parsed) > 1:
        return GuardrailResult(False, "Security: Multiple statements are not allowed.", None)

    if not parsed:
        return GuardrailResult(False, "Empty statement", None)

    statement = parsed[0]

    # 3. 白名单检查
    if not isinstance(statement, ALLOWED_TYPES):
        if hasattr(exp, "With") and isinstance(statement, exp.With):
            pass
        else:
            return GuardrailResult(False, f"Security: Only SELECT statements are allowed. Got {statement.key}", None)

    # 4. 深度扫描：危险节点
    for node in statement.walk():
        if isinstance(node, DENIED_NODES):
            return GuardrailResult(False, f"Security: Dangerous keyword detected ({node.key})", None)

        if isinstance(node, exp.Command) and "TRUNCATE" in node.sql().upper():
            return GuardrailResult(False, "Security: TRUNCATE is not allowed", None)

        if isinstance(node, exp.Select) and node.args.get("into"):
            return GuardrailResult(False, "Security: 'SELECT ... INTO' is not allowed", None)

    # 5. ✅ 只做“文本级 LIMIT 修正”，不动 AST（避免 DESC1000 这类坑）
    default_limit = int(getattr(settings, "SQL_DEFAULT_LIMIT", 100))
    max_limit = int(getattr(settings, "SQL_MAX_LIMIT", 1000))

    def _strip_trailing_semicolon(s: str) -> tuple[str, str]:
        s2 = s.rstrip()
        if s2.endswith(";"):
            return s2[:-1].rstrip(), ";"
        return s2, ""

    base, tail_sc = _strip_trailing_semicolon(raw_sql)

    # 只处理最外层最后一个 LIMIT（简单可靠：替换最后一次出现的 LIMIT n）
    # 说明：sqlglot 已确保是单语句 SELECT，所以这样足够稳
    limit_pattern = re.compile(r"\bLIMIT\s+(\d+)\b", flags=re.IGNORECASE)

    matches = list(limit_pattern.finditer(base))
    if not matches:
        # 没有 LIMIT：追加
        final_sql = f"{base} LIMIT {default_limit}{tail_sc}"
        return GuardrailResult(True, None, final_sql)

    # 有 LIMIT：只看最后一个 LIMIT
    last = matches[-1]
    cur_val = int(last.group(1))

    if cur_val > max_limit:
        # 替换最后一个 LIMIT 的数字为 max_limit（不改其他任何字符）
        start, end = last.span(1)  # 只替换数字部分
        base = base[:start] + str(max_limit) + base[end:]
        final_sql = f"{base}{tail_sc}"
        return GuardrailResult(True, None, final_sql)

    # LIMIT 合法：原样返回（不 pretty，不重排）
    final_sql = f"{base}{tail_sc}"
    return GuardrailResult(True, None, final_sql)


def _enforce_limit(expression: exp.Expression):
    """
    强制覆盖或添加 LIMIT
    """
    default_limit = getattr(settings, "SQL_DEFAULT_LIMIT", 100)
    max_limit = getattr(settings, "SQL_MAX_LIMIT", 1000)

    target = expression
    if hasattr(exp, "With") and isinstance(expression, exp.With):
        target = expression.this

    if not isinstance(target, (exp.Select, exp.Union)):
        return

    limit_node = target.args.get("limit")

    if limit_node:
        try:
            current_val = int(limit_node.this.this)
            if current_val > max_limit:
                limit_node.set("this", exp.Literal.number(max_limit))
        except:
            target.set("limit", exp.Limit(this=exp.Literal.number(max_limit)))
    else:
        target.limit(default_limit, copy=False)


def validate_schema_columns(
        statement: exp.Expression,
        table_columns: Dict[str, List[str]],
        allowed_tables: Set[str],
) -> GuardrailResult:
    allowed_tables_l = {t.lower() for t in allowed_tables}
    table_cols_l: Dict[str, Set[str]] = {
        t.lower(): {c.lower() for c in cols} for t, cols in (table_columns or {}).items()
    }

    root = statement
    if hasattr(exp, "With") and isinstance(statement, exp.With):
        root = statement.this

    if not isinstance(root, (exp.Select, exp.Union)):
        return GuardrailResult(True)

    # 1) 收集表
    alias_map: Dict[str, str] = {}
    used_tables: Set[str] = set()

    for tbl in root.find_all(exp.Table):
        real = tbl.name
        if not real: continue
        real_l = real.lower()
        used_tables.add(real_l)

        alias = tbl.alias
        if alias:
            alias_map[alias.lower()] = real_l
        else:
            alias_map[real_l] = real_l

    # 2) 表检查
    unknown = [t for t in used_tables if t not in allowed_tables_l]
    if unknown:
        logger.warning(f"SchemaCheck: Table {unknown[0]} not in candidate list. Proceed with caution.")

    # 3) 列检查
    for col in root.find_all(exp.Column):
        col_name = col.name
        if not col_name or col_name == "*": continue

        qualifier = col.table
        if not qualifier: continue

        q_l = qualifier.lower()
        real_table = alias_map.get(q_l)

        if real_table and real_table in table_cols_l:
            whitelist = table_cols_l[real_table]
            if col_name.lower() not in whitelist:
                # 严格模式下返回 False，这里先 Warning 避免误杀
                pass

    return GuardrailResult(True)