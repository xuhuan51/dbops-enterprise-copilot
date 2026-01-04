import os
import json
import re
from datetime import datetime, date
from decimal import Decimal

import pymysql
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from openai import OpenAI

from app.core.prompts import SCHEMA_ENRICH_PROMPT

# ===========================
# 0. 配置加载
# ===========================
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
load_dotenv(os.path.join(project_root, ".env"))

OUT_PATH = os.path.join(project_root, "data", "schema_catalog_enriched.jsonl")
SAMPLE_N = int(os.getenv("SAMPLE_N", "5"))
ALWAYS_LLM = os.getenv("ALWAYS_LLM", "false").lower() == "true"
TARGET_DBS = [x.strip() for x in os.getenv("TARGET_DBS", "").split(",") if x.strip()]

# LLM 配置 (兼容 Ollama/vLLM)
LLM_API_KEY = os.getenv("LLM_API_KEY", "EMPTY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:14b")
ENABLE_LLM = os.getenv("ENABLE_LLM", "true").lower() == "true" and bool(LLM_BASE_URL)

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL) if ENABLE_LLM else None

# ===========================
# 1. 基础工具函数
# ===========================

def get_conn(db_name: Optional[str] = None):
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )

import re

def get_logical_name(table_name: str) -> str:
    # 1) 按周：log_xxx_2025w074 / log_xxx_2025W074
    m = re.match(r"^(.*)_(\d{4})[wW](\d{2,3})$", table_name)
    if m:
        return f"{m.group(1)}_*"

    # 2) 按日期/年月：t_log_20240101 / t_log_202310
    m = re.match(r"^(.*)_(\d{6,8})$", table_name)
    if m:
        return f"{m.group(1)}_*"

    # 3) 按数字分片：t_order_001 / t_order_0007
    m = re.match(r"^(.*)_(\d{2,6})$", table_name)
    if m:
        return f"{m.group(1)}_*"

    return table_name


def _safe_str(val: Any) -> str:
    try:
        return "" if val is None else str(val)
    except Exception:
        return ""

def mask_sensitive_data(val: Any) -> Any:
    if val is None:
        return None
    s = str(val)

    if re.fullmatch(r"1\d{10}", s):
        return s[:3] + "****" + s[-4:]
    if re.fullmatch(r"\d{15}|\d{17}[\dXx]", s):
        return s[:4] + "**********" + s[-4:]
    if "@" in s and "." in s:
        name, domain = s.split("@", 1)
        return (name[:2] + "***@" + domain) if len(name) > 2 else ("***@" + domain)
    if len(s) > 80:
        return s[:80] + "..."

    # ✅ 关键兜底：确保可 JSON 序列化
    if isinstance(val, (datetime, date, Decimal)):
        return str(val)

    return val


def stable_unique(seq: List[str]) -> List[str]:
    """保持顺序去重"""
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def extract_json_object(text: str) -> Optional[dict]:
    """从文本中粗暴抽 JSON 对象兜底（用于 openai-compatible 不支持 response_format 的情况）"""
    if not text:
        return None
    # 找到第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    chunk = text[start:end + 1]
    try:
        return json.loads(chunk)
    except Exception:
        return None

# ===========================
# 2. 规则抽取
# ===========================

TIME_NAME_HINTS = ("time", "date", "dt", "ts", "created", "updated", "create", "update")
METRIC_NAME_HINTS = ("amount", "amt", "price", "fee", "cost", "qty", "quantity", "count", "num", "score", "latency", "duration", "rt", "total")
JOIN_NAME_HINTS = ("_id", "uid", "oid", "sku", "order", "user", "member", "customer", "supplier", "vendor", "code", "no", "uuid")

def infer_capabilities_by_rule(columns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """规则推断 time/metric/join_keys（domain 单独推断）"""
    caps = {
        "time_cols": [],
        "metric_cols": [],
        "join_keys": [],
    }

    for col in columns:
        name = _safe_str(col.get("name")).lower()
        ctype = _safe_str(col.get("type")).lower()

        # Time: 类型优先；其次名字提示 + (int/char 也允许 timestamp 字符串)
        if any(t in ctype for t in ("datetime", "timestamp", "date")):
            caps["time_cols"].append(col["name"])
        elif any(k in name for k in TIME_NAME_HINTS):
            caps["time_cols"].append(col["name"])

        # Metric: 数值 + 名字提示，且不是 *_id
        is_num = any(x in ctype for x in ("int", "decimal", "float", "double"))
        if is_num and any(k in name for k in METRIC_NAME_HINTS) and not name.endswith("_id"):
            caps["metric_cols"].append(col["name"])

        # Join keys: *_id 或 常见实体键
        if name in ("id", "uuid") or name.endswith("_id") or any(k in name for k in JOIN_NAME_HINTS):
            caps["join_keys"].append(col["name"])

    caps["time_cols"] = stable_unique(caps["time_cols"])
    caps["metric_cols"] = stable_unique(caps["metric_cols"])
    caps["join_keys"] = stable_unique(caps["join_keys"])
    return caps

def infer_domain_by_name(db: str, table: str) -> str:
    s = f"{db}.{table}".lower()
    if any(x in s for x in ("trade", "order", "pay", "bill", "refund", "settle")):
        return "trade"
    if any(x in s for x in ("user", "member", "customer", "account", "login", "profile")):
        return "user"
    if any(x in s for x in ("scm", "stock", "sku", "supplier", "purchase", "warehouse", "wh")):
        return "scm"
    if any(x in s for x in ("mkt", "act", "coupon", "promo", "campaign", "live")):
        return "marketing"
    if any(x in s for x in ("log", "record", "trace", "err", "audit", "metric")):
        return "log"
    return "other"

# ===========================
# 3. LLM 增强
# ===========================

COLUMN_NAME_NOSENSE_RE = re.compile(r"^(c|col|f|field)_?\d+$", re.IGNORECASE)

def should_call_llm(table_comment: str, columns: List[Dict[str, Any]]) -> bool:
    """
    触发 LLM 的条件（更靠谱）：
    - ALWAYS_LLM
    - 表注释为空，且列注释空比例高
    - 无语义列名比例高（c1/col1/f_01/field3...）
    """
    if ALWAYS_LLM:
        return True

    if not ENABLE_LLM:
        return False

    col_comments = [_safe_str(c.get("comment")) for c in columns]
    empty_comment_ratio = 0.0
    if columns:
        empty_comment_ratio = sum(1 for x in col_comments if not x.strip()) / len(columns)

    nosense_ratio = 0.0
    if columns:
        nosense_ratio = sum(1 for c in columns if COLUMN_NAME_NOSENSE_RE.match(_safe_str(c.get("name")))) / len(columns)

    # 经验阈值
    if (not (table_comment or "").strip() and empty_comment_ratio >= 0.6) or nosense_ratio >= 0.3:
        return True

    return False

def llm_enrich_table(table_info: Dict[str, Any], samples: List[Dict[str, Any]], columns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """调用 LLM 生成 Table Card 元信息（带幻觉校验）"""
    if not ENABLE_LLM or client is None:
        return {}

    valid_cols = set([c["name"] for c in columns])

    cols_for_prompt = [f"{c['name']}({c['type']})" for c in columns[:120]]  # 防止超长
    prompt = SCHEMA_ENRICH_PROMPT

    try:
        # 兼容：部分 OpenAI-compatible 不支持 response_format
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        content = resp.choices[0].message.content or ""
        data = None
        try:
            data = json.loads(content)
        except Exception:
            data = extract_json_object(content)

        if not isinstance(data, dict):
            return {}

        # 🛡️ 幻觉校验（列名必须存在）
        data["join_keys"] = [c for c in data.get("join_keys", []) if c in valid_cols]
        data["time_cols"] = [c for c in data.get("time_cols", []) if c in valid_cols]
        data["metric_cols"] = [c for c in data.get("metric_cols", []) if c in valid_cols]

        # 合法值兜底
        if data.get("domain") not in ("trade", "user", "scm", "marketing", "log", "other"):
            data["domain"] = "other"
        if data.get("risk") not in ("sensitive", "none"):
            data["risk"] = "none"

        # 截断 synonyms 防爆
        syn = data.get("synonyms", [])
        if isinstance(syn, list):
            data["synonyms"] = [str(x)[:80] for x in syn[:30]]
        else:
            data["synonyms"] = []

        if "summary" in data and isinstance(data["summary"], str):
            data["summary"] = data["summary"][:200]

        return data

    except Exception as e:
        print(f"    ⚠️ LLM Enrich Failed: {type(e).__name__}: {e}")
        return {}

# ===========================
# 4. 主流程
# ===========================

def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    processed_logical_tables = set()

    with open(OUT_PATH, "w", encoding="utf-8") as f_out:
        for db in TARGET_DBS:
            print(f"📦 Scanning DB: {db} ...")
            try:
                conn = get_conn(db)
                cur = conn.cursor()

                # 只扫 BASE TABLE
                cur.execute("""
                    SELECT table_name, table_comment
                    FROM information_schema.tables
                    WHERE table_schema=%s AND table_type='BASE TABLE'
                    ORDER BY table_name
                """, (db,))
                tables = cur.fetchall()

                for t_row in tables:
                    table = t_row.get("table_name") or t_row.get("TABLE_NAME")
                    t_comment = t_row.get("table_comment") or t_row.get("TABLE_COMMENT") or ""

                    logical_table = get_logical_name(table)
                    full_logical_key = f"{db}.{logical_table}"

                    # 分片去重：只处理一个代表分片
                    if full_logical_key in processed_logical_tables:
                        continue
                    processed_logical_tables.add(full_logical_key)

                    print(f"  👉 Processing: {table} -> {logical_table}")

                    # 1) Columns（按顺序）
                    cur.execute("""
                        SELECT COLUMN_NAME, DATA_TYPE, COLUMN_COMMENT, COLUMN_KEY
                        FROM information_schema.columns
                        WHERE table_schema=%s AND table_name=%s
                        ORDER BY ORDINAL_POSITION
                    """, (db, table))
                    raw_cols = cur.fetchall()

                    columns = []
                    pk = []
                    for rc in raw_cols:
                        col_obj = {
                            "name": rc["COLUMN_NAME"],
                            "type": rc["DATA_TYPE"],
                            "comment": rc.get("COLUMN_COMMENT") or "",
                            "key": rc.get("COLUMN_KEY") or ""
                        }
                        columns.append(col_obj)
                        if (rc.get("COLUMN_KEY") or "") == "PRI":
                            pk.append(rc["COLUMN_NAME"])

                    # 2) Indexes（保序）
                    cur.execute("""
                        SELECT INDEX_NAME, COLUMN_NAME
                        FROM information_schema.statistics
                        WHERE table_schema=%s AND table_name=%s
                        ORDER BY INDEX_NAME, SEQ_IN_INDEX
                    """, (db, table))
                    idx_rows = cur.fetchall()
                    indexes: Dict[str, List[str]] = {}
                    for ir in idx_rows:
                        idx = ir["INDEX_NAME"]
                        indexes.setdefault(idx, []).append(ir["COLUMN_NAME"])

                    # 3) Rule features + domain
                    rule_caps = infer_capabilities_by_rule(columns)
                    rule_domain = infer_domain_by_name(db, table)

                    # 4) Sampling（优先关键列，稳定去重）
                    priority_cols = stable_unique(rule_caps["time_cols"] + rule_caps["metric_cols"] + rule_caps["join_keys"])
                    if priority_cols:
                        select_cols = priority_cols[:12]
                    else:
                        select_cols = [c["name"] for c in columns[:10]]

                    samples: List[Dict[str, Any]] = []
                    if select_cols:
                        try:
                            col_str = ",".join([f"`{c}`" for c in select_cols])
                            cur.execute(f"SELECT {col_str} FROM `{table}` LIMIT {SAMPLE_N}")
                            rows = cur.fetchall()
                            for r in rows:
                                samples.append({k: mask_sensitive_data(v) for k, v in r.items()})
                        except Exception as e:
                            print(f"    ⚠️ Sampling failed: {type(e).__name__}: {e}")

                    # 5) LLM enrich（按条件触发）
                    llm_result: Dict[str, Any] = {}
                    if should_call_llm(t_comment, columns):
                        print("    🧠 Calling LLM for enrichment...")
                        llm_result = llm_enrich_table(
                            {"db": db, "table": table, "comment": t_comment},
                            samples,
                            columns
                        )

                    # 6) Merge features（LLM 追加/增强）
                    final_features = {
                        "join_keys": stable_unique(rule_caps["join_keys"] + llm_result.get("join_keys", [])),
                        "time_cols": stable_unique(rule_caps["time_cols"] + llm_result.get("time_cols", [])),
                        "metric_cols": stable_unique(rule_caps["metric_cols"] + llm_result.get("metric_cols", [])),
                    }
                    final_domain = llm_result.get("domain") or rule_domain
                    final_summary = llm_result.get("summary") or (t_comment.strip() if t_comment.strip() else f"{logical_table}（未命名表）")
                    synonyms = llm_result.get("synonyms", [])
                    if not isinstance(synonyms, list):
                        synonyms = []

                    # 7) TableCard Text（embedding 用）
                    col_desc_list = []
                    for c in columns[:80]:
                        role = []
                        if c["name"] in final_features["join_keys"]:
                            role.append("JOIN")
                        if c["name"] in final_features["time_cols"]:
                            role.append("TIME")
                        if c["name"] in final_features["metric_cols"]:
                            role.append("METRIC")
                        role_str = f"[{','.join(role)}]" if role else ""
                        comment = (c.get("comment") or "").strip()
                        if comment:
                            col_desc_list.append(f"- {c['name']} ({c['type']}) {comment} {role_str}".strip())
                        else:
                            col_desc_list.append(f"- {c['name']} ({c['type']}) {role_str}".strip())

                    samples_preview = samples[:2]  # 控制长度
                    text_block = "\n".join([
                        "[TableCard]",
                        f"DB: {db}",
                        f"Table: {logical_table} (Physical: {table})",
                        f"Domain: {final_domain}",
                        f"Summary: {final_summary}",
                        f"Synonyms: {', '.join([str(x) for x in synonyms[:20]])}",
                        "Capabilities:",
                        f"- Time: {final_features['time_cols']}",
                        f"- Metrics: {final_features['metric_cols']}",
                        f"- Joins: {final_features['join_keys']}",
                        "Columns:",
                        *col_desc_list[:40],
                        "Samples:",
                        json.dumps(samples_preview, ensure_ascii=False, default=str),
                    ]).strip()

                    record = {
                        "db": db,
                        "table": table,                 # 物理表名
                        "logical_table": logical_table, # 逻辑表名
                        "domain": final_domain,
                        "table_comment": t_comment,
                        "columns": columns,
                        "pk": pk,
                        "indexes": indexes,
                        "samples": samples,
                        "features": final_features,
                        "llm": llm_result,
                        "text": text_block,
                    }
                    f_out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

                    f_out.flush()

                cur.close()
                conn.close()

            except Exception as e:
                print(f"❌ Error processing DB {db}: {type(e).__name__}: {e}")

    print(f"\n✅ Extraction Done! Output: {OUT_PATH}")

if __name__ == "__main__":
    main()
