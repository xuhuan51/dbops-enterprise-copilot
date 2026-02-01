import os
import re
import json
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple


# -----------------------------
# Utils
# -----------------------------
def norm_name(name: str) -> str:
    """
    Normalize column/table names:
    - camelCase -> snake_case
    - keep [a-z0-9_]
    """
    if name is None:
        return ""
    s = str(name).strip()
    # CamelCase -> snake_case
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    # non-alnum -> _
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s)
    s = s.strip("_").lower()
    return s


def quote_ident(ident: str) -> str:
    """SQLite identifier quoting with double quotes."""
    return '"' + ident.replace('"', '""') + '"'


def is_numeric_type(sqlite_type: str) -> bool:
    t = (sqlite_type or "").upper()
    # SQLite is dynamic typing; these are common numeric affinities
    return any(k in t for k in ["INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"])


def safe_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if p <= 0:
        return sorted_vals[0]
    if p >= 100:
        return sorted_vals[-1]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    d0 = sorted_vals[f] * (c - k)
    d1 = sorted_vals[c] * (k - f)
    return d0 + d1


# -----------------------------
# Main builder
# -----------------------------
def build_bird_catalog():
    # ====== Path config (adjust to your project) ======
    DB_ROOT = Path("../data/bird/dev_databases")          # contains many db_id folders
    TABLES_JSON = Path("../data/bird/metadata/dev_tables.json")
    OUTPUT_FILE = Path("../data/bird/metadata/schema_catalog.json")

    # Catalog metadata (unified fields, even if you later use MySQL)
    DATASET = "bird"
    DATASOURCE = "bird_sqlite"

    # Sampling config
    SAMPLE_ROWS = 200            # numeric profiling uses up to N rows
    SAMPLE_VALUES_TEXT = 10      # store up to N unique sample values for TEXT
    SAMPLE_VALUES_GENERIC = 5    # store up to N unique samples for non-text if no profile

    if not TABLES_JSON.exists():
        print(f"❌ 找不到文件: {TABLES_JSON}")
        return
    if not DB_ROOT.exists():
        print(f"❌ 找不到目录: {DB_ROOT}")
        return

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 1) Load dev_tables.json (Spider-style schema metadata)
    with open(TABLES_JSON, "r", encoding="utf-8") as f:
        raw_tables_data = json.load(f)

    meta_map = {item["db_id"]: item for item in raw_tables_data}

    catalog: List[Dict[str, Any]] = []
    db_folders = [d for d in os.listdir(DB_ROOT) if (DB_ROOT / d).is_dir()]

    for db_id in db_folders:
        db_dir = DB_ROOT / db_id

        # Find sqlite file (robust)
        sqlite_files = list(db_dir.glob("*.sqlite")) + list(db_dir.glob("*.db"))
        if not sqlite_files:
            # sometimes nested; but in BIRD it should be here
            continue
        db_path = sqlite_files[0]

        print(f"📦 正在处理数据库: {db_id} -> {db_path.name}")
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        db_meta = meta_map.get(db_id, {})

        # Map table original -> table comment (natural language)
        # dev_tables.json usually has:
        # - table_names_original: list[str]
        # - table_names: list[str] (NL name)
        table_names_original = db_meta.get("table_names_original", [])
        table_names_nl = db_meta.get("table_names", [])

        # Build a lookup: (table_idx, col_name_original) -> col_comment_nl
        # column_names_original: list[[table_idx, col_name_original]]
        # column_names: list[[table_idx, col_name_nl]]
        col_lookup: Dict[Tuple[int, str], str] = {}
        cno = db_meta.get("column_names_original", [])
        cn = db_meta.get("column_names", [])
        if cno and cn and len(cno) == len(cn):
            for i, (t_idx, c_name) in enumerate(cno):
                # c_name might be "*"
                try:
                    nl_name = cn[i][1]
                except Exception:
                    nl_name = ""
                col_lookup[(t_idx, c_name)] = nl_name or ""

        # List tables (exclude sqlite internal)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cursor.fetchall() if r[0] and not r[0].startswith("sqlite_")]

        # For each table, prefetch FK info
        # PRAGMA foreign_key_list(table) columns:
        # (id, seq, table, from, to, on_update, on_delete, match)
        fk_map: Dict[str, List[Dict[str, str]]] = {}
        for table in tables:
            try:
                cursor.execute(f"PRAGMA foreign_key_list({quote_ident(table)})")
                fks = cursor.fetchall()
                fk_map[table] = [{"to_table": fk[2], "from_col": fk[3], "to_col": fk[4]} for fk in fks]
            except Exception:
                fk_map[table] = []

        for table in tables:
            # Table comment from dev_tables.json if possible
            table_comment = ""
            table_idx = -1
            if table_names_original and table in table_names_original:
                table_idx = table_names_original.index(table)
                if 0 <= table_idx < len(table_names_nl):
                    table_comment = table_names_nl[table_idx] or ""

            # Column info
            cursor.execute(f"PRAGMA table_info({quote_ident(table)})")
            # rows: (cid, name, type, notnull, dflt_value, pk)
            columns = cursor.fetchall()

            # Sample rows for text columns + numeric profiling
            sample_rows: List[Tuple[Any, ...]] = []
            try:
                cursor.execute(f"SELECT * FROM {quote_ident(table)} LIMIT {SAMPLE_ROWS}")
                sample_rows = cursor.fetchall()
            except Exception:
                sample_rows = []

            # FK list for this table
            fks_this = fk_map.get(table, [])

            for col_idx, col in enumerate(columns):
                col_name = col[1]
                col_type = col[2] or ""
                is_pk = bool(col[5])

                # Column comment from dev_tables.json (match by table_idx + original col name)
                col_comment = ""
                if table_idx != -1:
                    col_comment = col_lookup.get((table_idx, col_name), "") or ""

                # Determine FK
                fk_to = []
                for fk in fks_this:
                    if fk.get("from_col") == col_name:
                        fk_to.append({"table": fk.get("to_table", ""), "column": fk.get("to_col", "")})

                is_fk = len(fk_to) > 0

                # Build samples / numeric profile
                col_samples: List[str] = []
                num_profile: Optional[Dict[str, Any]] = None

                if sample_rows:
                    raw_vals = []
                    for r in sample_rows:
                        if col_idx < len(r):
                            v = r[col_idx]
                            if v is None:
                                continue
                            raw_vals.append(v)

                    if is_numeric_type(col_type):
                        nums = [safe_float(v) for v in raw_vals]
                        nums = [v for v in nums if v is not None]
                        nums.sort()
                        if nums:
                            num_profile = {
                                "min": nums[0],
                                "max": nums[-1],
                                "p50": percentile(nums, 50),
                                "p95": percentile(nums, 95),
                                "count": len(nums),
                            }
                        # keep a few example values too
                        if raw_vals:
                            uniq = []
                            seen = set()
                            for v in raw_vals:
                                sv = str(v)
                                if sv not in seen:
                                    seen.add(sv)
                                    uniq.append(sv)
                                if len(uniq) >= SAMPLE_VALUES_GENERIC:
                                    break
                            col_samples = uniq
                    else:
                        # TEXT-like: keep more unique samples
                        uniq = []
                        seen = set()
                        for v in raw_vals:
                            sv = str(v).strip()
                            if not sv:
                                continue
                            if sv not in seen:
                                seen.add(sv)
                                uniq.append(sv)
                            if len(uniq) >= SAMPLE_VALUES_TEXT:
                                break
                        col_samples = uniq

                column_norm = norm_name(col_name)
                table_norm = norm_name(table)

                # Build doc_text for retrieval (very important for Milvus/embedding)
                # You can add aliases here if you want, e.g. from your synonym dictionary.
                doc_parts = [
                    f"dataset: {DATASET}",
                    f"datasource: {DATASOURCE}",
                    f"db: {db_id}",
                    f"table: {table}",
                ]
                if table_comment:
                    doc_parts.append(f"table_comment: {table_comment}")
                doc_parts.extend([
                    f"column: {col_name}",
                    f"column_norm: {column_norm}",
                    f"type: {col_type}",
                ])
                if col_comment:
                    doc_parts.append(f"comment: {col_comment}")
                if is_pk:
                    doc_parts.append("is_pk: true")
                if is_fk:
                    doc_parts.append("is_fk: true")
                if fk_to:
                    # compact
                    fk_str = "; ".join([f"{x['table']}.{x['column']}" for x in fk_to if x.get("table")])
                    if fk_str:
                        doc_parts.append(f"fk_to: {fk_str}")
                if col_samples:
                    doc_parts.append("samples: " + "; ".join(col_samples[:SAMPLE_VALUES_TEXT]))
                if num_profile:
                    doc_parts.append(
                        f"num_profile: min={num_profile['min']} max={num_profile['max']} "
                        f"p50={num_profile['p50']} p95={num_profile['p95']} n={num_profile['count']}"
                    )

                doc_text = " | ".join(doc_parts)

                catalog.append({
                    "dataset": DATASET,
                    "datasource": DATASOURCE,
                    "db_id": db_id,
                    "db_path": str(db_path),        # optional but helpful for debugging
                    "table": table,
                    "table_norm": table_norm,
                    "table_comment": table_comment,
                    "column": col_name,
                    "column_norm": column_norm,
                    "column_comment": col_comment,
                    "column_type": col_type,
                    "is_pk": is_pk,
                    "is_fk": is_fk,
                    "fk_to": fk_to,                 # list
                    "samples": col_samples,
                    "num_profile": num_profile,      # dict or None
                    "doc_text": doc_text,
                })

        conn.close()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    print("\n✅ 全量 Schema Catalog 已生成！")
    print(f"📍 路径: {OUTPUT_FILE}")
    print(f"📊 总计索引字段数: {len(catalog)}")


if __name__ == "__main__":
    build_bird_catalog()
