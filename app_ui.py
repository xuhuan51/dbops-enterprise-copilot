import streamlit as st
import requests
import pandas as pd
import json
import uuid
import re

# ==========================================
# ⚙️ 配置
# ==========================================
API_URL = "http://127.0.0.1:8000/api/v1/query"

st.set_page_config(page_title="DBOps Copilot", page_icon="🛡️", layout="wide")

# ==========================================
# 1. CSS 样式
# ==========================================
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap');

    [data-testid="stSidebar"] {
        border-right: 1px solid #e2e8f0;
        background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);
    }

    /* 步骤卡片 */
    .step-active {
        margin: 5px 0; padding: 10px 14px; border-radius: 8px;
        font-size: 0.88em;
        background: linear-gradient(90deg, #eff6ff, #f0f9ff);
        border: 1px solid #93c5fd;
        animation: pulse-b 1.5s ease-in-out infinite;
    }
    .step-done {
        margin: 5px 0; padding: 10px 14px; border-radius: 8px;
        font-size: 0.88em;
        background: #f0fdf4; border: 1px solid #86efac;
    }
    .step-warn {
        margin: 5px 0; padding: 10px 14px; border-radius: 8px;
        font-size: 0.88em;
        background: linear-gradient(135deg, #fef3c7, #fffbeb);
        border: 1px solid #f59e0b; color: #92400e;
    }
    @keyframes pulse-b {
        0%,100% { border-color: #93c5fd; }
        50% { border-color: #3b82f6; }
    }
    .loading-dots::after {
        content: '';
        animation: dots 1.5s steps(4, end) infinite;
    }
    @keyframes dots {
        0% { content: ''; } 25% { content: '.'; }
        50% { content: '..'; } 75% { content: '...'; }
    }

    /* SQL 代码块 */
    .sql-block {
        background: #0f172a; color: #e2e8f0;
        padding: 16px 20px; border-radius: 10px;
        font-family: 'JetBrains Mono', 'Consolas', monospace;
        font-size: 0.84em; line-height: 1.7;
        border-left: 4px solid #10b981;
        overflow-x: auto; margin: 8px 0;
        white-space: pre-wrap; word-break: break-word;
    }
    .sql-block .kw { color: #7dd3fc; font-weight: 600; }
    .sql-block .str { color: #86efac; }
    .sql-block .fn { color: #fbbf24; }
    .sql-block .num { color: #f9a8d4; }

    /* 精选上下文 */
    .ctx-card {
        background: #f8fafc; border: 1px solid #e2e8f0;
        border-radius: 10px; padding: 12px 16px; margin: 5px 0;
    }
    .ctx-card h4 { margin: 0 0 8px 0; font-size: 0.88em; color: #475569; }
    .tag-table {
        display: inline-block; background: linear-gradient(135deg, #dbeafe, #ede9fe);
        color: #3730a3; padding: 3px 10px; border-radius: 20px;
        font-size: 0.82em; font-weight: 600; margin: 2px 4px 2px 0;
    }
    .tag-col {
        display: inline-block; background: #f0fdf4; color: #166534;
        padding: 2px 8px; border-radius: 12px; font-size: 0.78em;
        margin: 2px 3px 2px 0; border: 1px solid #bbf7d0;
    }
    .tag-join {
        display: inline-block; background: #fdf4ff; color: #86198f;
        padding: 3px 10px; border-radius: 20px; font-size: 0.8em;
        margin: 2px 4px 2px 0; border: 1px solid #f0abfc;
        font-family: 'JetBrains Mono', monospace;
    }
    .rule-item {
        background: #fffbeb; border: 1px solid #fde68a;
        border-radius: 8px; padding: 8px 12px; margin: 4px 0;
        font-size: 0.82em; color: #92400e; line-height: 1.5;
    }
    .result-ok {
        background: linear-gradient(135deg, #ecfdf5, #f0fdf4);
        border: 1px solid #6ee7b7; border-radius: 8px;
        padding: 10px 14px; font-size: 0.9em;
        color: #065f46; font-weight: 500;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. 状态管理
# ==========================================
if "messages" not in st.session_state:
    st.session_state["messages"] = [{
        "role": "assistant",
        "content": "👋 **你好！我是 DBOps 智能助手。**\n\n我可以帮你查询数据、分析趋势或生成报表。\n\n**试一试：**\n- 🔍 *查询最近一个月销售额最高的5个商品*\n- 📊 *统计各品牌销量占比*"
    }]
if "session_id" not in st.session_state:
    st.session_state["session_id"] = str(uuid.uuid4())


# ==========================================
# 3. 辅助函数：SQL 高亮
# ==========================================
def highlight_sql(sql_text: str) -> str:
    if not sql_text:
        return ""
    import html as html_mod
    s = html_mod.escape(sql_text)
    keywords = [
        'SELECT', 'FROM', 'WHERE', 'JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'INNER JOIN',
        'ON', 'AND', 'OR', 'NOT', 'IN', 'AS', 'ORDER BY', 'GROUP BY', 'HAVING',
        'LIMIT', 'OFFSET', 'DESC', 'ASC', 'BETWEEN', 'LIKE', 'IS', 'NULL',
        'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'DISTINCT', 'UNION', 'ALL',
        'WITH', 'NOT IN', 'IS NOT',
    ]
    funcs = ['SUM', 'COUNT', 'AVG', 'MAX', 'MIN', 'COALESCE', 'IFNULL',
             'DATE', 'NOW', 'CURDATE', 'DATE_SUB', 'INTERVAL', 'CONCAT',
             'ROUND', 'CAST', 'DATE_FORMAT', 'YEAR', 'MONTH', 'DAY']
    s = re.sub(r"(&#x27;[^&]*?&#x27;)", r'<span class="str">\1</span>', s)
    for f in funcs:
        s = re.sub(rf'\b({f})\s*\(', rf'<span class="fn">\1</span>(', s, flags=re.IGNORECASE)
    for kw in sorted(keywords, key=len, reverse=True):
        s = re.sub(rf'\b({re.escape(kw)})\b', rf'<span class="kw">\1</span>', s, flags=re.IGNORECASE)
    s = re.sub(r'\b(\d+)\b', r'<span class="num">\1</span>', s)
    return s


# ==========================================
# 4. 辅助函数：渲染上下文 (修复了这里缺失的问题)
# ==========================================
def render_context_html(ctx: dict) -> str:
    """将后端 SELECTOR_DONE 的 payload 渲染为 HTML"""
    parts = []

    # 兼容处理：有些payload可能是字符串，尝试解析
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except:
            return f'<div class="ctx-card">{ctx}</div>'

    if not isinstance(ctx, dict):
        return ""

    tables = ctx.get("tables", {})
    if tables:
        html = ""
        for tbl, cols in tables.items():
            html += f'<span class="tag-table">📦 {tbl}</span> '
            # 如果 cols 是列表
            if isinstance(cols, list):
                for c in cols:
                    html += f'<span class="tag-col">{c}</span> '
            html += "<br>"
        parts.append(f'<div class="ctx-card"><h4>🎯 AI 精选的表和列</h4>{html}</div>')

    joins = ctx.get("joins", [])
    if joins:
        jhtml = "".join(f'<span class="tag-join">🔗 {j}</span> ' for j in joins)
        parts.append(f'<div class="ctx-card"><h4>🔗 关联路径</h4>{jhtml}</div>')

    rules = ctx.get("rules", [])
    if rules:
        rhtml = "".join(f'<div class="rule-item">📋 {r}</div>' for r in rules)
        parts.append(f'<div class="ctx-card"><h4>📚 命中的业务规则</h4>{rhtml}</div>')

    values = ctx.get("values", [])
    if values:
        vhtml = ""
        for v in values:
            vhtml += (f'<div class="rule-item">🏷️ "{v.get("user_input", "")}" → '
                      f'<code>{v.get("table", "")}.{v.get("column", "")}</code> = '
                      f'<code>{v.get("db_value", "")}</code></div>')
        parts.append(f'<div class="ctx-card"><h4>🏷️ 值映射</h4>{vhtml}</div>')

    return "\n".join(parts)


# ==========================================
# 5. 图表渲染 (终极修复版：兼容 Dict/List + 长度对齐)
# ==========================================
def render_chart(chart_config):
    """
    渲染图表 (修复数据格式兼容性问题)
    """
    if not chart_config:
        return

    # 1. 结构兼容处理
    raw_data = chart_config.get("data")
    if isinstance(raw_data, dict) and ("x_axis_data" in raw_data or "x" in raw_data or "labels" in raw_data):
        chart_data = raw_data
    else:
        chart_data = chart_config

    chart_type = chart_config.get("type", "bar")
    title = chart_data.get("title", "")

    try:
        # 2. 字段映射
        x_raw = (chart_data.get("x_axis_data") or chart_data.get("x") or
                 chart_data.get("labels") or chart_data.get("categories") or [])

        y_raw = (chart_data.get("series_data") or chart_data.get("y_axis_data") or
                 chart_data.get("y") or chart_data.get("values") or
                 chart_data.get("data") or [])

        # 🛡️ 类型清洗：如果是字典，强制转为列表值
        if isinstance(x_raw, dict):
            x_raw = list(x_raw.values())
        if isinstance(y_raw, dict):
            y_raw = list(y_raw.values())

        # 再次检查空值
        if not x_raw or not y_raw:
            st.warning("⚠️ 图表数据为空，无法渲染")
            return

        # 🛡️ 长度对齐：取交集长度，防止 Pandas 报错
        min_len = min(len(x_raw), len(y_raw))
        if min_len == 0:
            st.warning("⚠️ 有效数据长度为 0")
            return

        x_data = x_raw[:min_len]
        series_data = y_raw[:min_len]

        # 3. 构建 DataFrame
        df = pd.DataFrame({
            "Label_Raw": x_data,
            "Value_Raw": series_data
        })

        # 4. 数据清洗
        df["Label"] = df["Label_Raw"].astype(str)
        # 强力转数字：去掉非数字字符 -> 转 float -> 填充 0
        df["Value"] = pd.to_numeric(
            df["Value_Raw"].astype(str).str.replace(r'[^\d\.\-]', '', regex=True),
            errors='coerce'
        ).fillna(0.0).astype(float)

        # 5. 排序 (按数值降序)
        df = df.sort_values(by="Value", ascending=False).reset_index(drop=True)

        # 6. 绘图 (Plotly)
        import plotly.graph_objects as go

        n = len(df)
        # 动态配色
        colors = [
            f'rgb({int(59 + i / (max(n, 1)) * (-43))}, {int(130 + i / (max(n, 1)) * (55))}, {int(246 + i / (max(n, 1)) * (-117))})'
            for i in range(n)]

        if chart_type == "bar":
            fig = go.Figure(go.Bar(
                x=df["Label"],
                y=df["Value"],
                marker=dict(color=colors),
                text=[f'{v:,.0f}' for v in df["Value"]],  # 整数显示更清爽
                textposition='outside'
            ))
            # 动态高度
            chart_height = max(450, len(df) * 20) if len(df) > 20 else 450

            fig.update_layout(
                title=dict(text=title, font=dict(size=16)),
                height=chart_height,
                margin=dict(l=20, r=20, t=50, b=100),
                xaxis_tickangle=-45
            )
            st.plotly_chart(fig, use_container_width=True)

        elif chart_type == "line":
            st.line_chart(df, x="Label", y="Value")

        elif chart_type == "pie":
            fig = go.Figure(go.Pie(
                labels=df["Label"], values=df["Value"],
                textinfo='label+percent'
            ))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.bar_chart(df.set_index("Label"))

    except Exception as e:
        # 打印详细堆栈，方便排查
        import traceback
        st.error(f"❌ 图表渲染崩溃: {str(e)}")
        # 仅在需要时展开
        with st.expander("查看详细报错信息"):
            st.text(traceback.format_exc())
            st.json(chart_config)


# ==========================================
# 6. 核心：处理流式响应
# ==========================================
def handle_response(prompt):
    status_container = st.status("🚀 正在处理...", expanded=True)

    with status_container:
        progress_bar = st.progress(0, text="正在连接...")
        ph_intent = st.empty()
        ph_retrieval = st.empty()
        ph_selection = st.empty()
        ph_generate = st.empty()
        ph_verify = st.empty()
        ph_exec = st.empty()

    answer_box = st.empty()
    chart_box = st.empty()

    full_answer = ""
    final_sql = ""

    # 初始化变量，防止未定义错误
    last_chart_payload = None
    current_sql_result = None

    try:
        resp = requests.post(
            API_URL,
            json={"query": prompt, "db_id": "ecommerce",
                  "session_id": st.session_state["session_id"]},
            stream=True, timeout=120
        )

        if resp.status_code != 200:
            st.error(f"服务器错误: {resp.status_code}")
            return

        for line in resp.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line.decode('utf-8'))
            except json.JSONDecodeError:
                continue

            evt_type = data.get("type", "")
            step = data.get("step", "")
            msg = data.get("msg", "")
            payload = data.get("payload")

            # ═══════════════════════════════════════════
            # 步骤处理逻辑
            # ═══════════════════════════════════════════

            if step == "ROUTER":
                progress_bar.progress(10, text="意图识别...")
                intent = msg.split(":")[-1].strip() if ":" in msg else "DATA_QUERY"
                ph_intent.markdown(f'<div class="step-done">🧠 <b>意图识别</b>：<code>{intent}</code></div>',
                                   unsafe_allow_html=True)

            elif step == "RETRIEVAL_START":
                ph_retrieval.markdown(
                    '<div class="step-active">🔍 <b>检索中...</b><span class="loading-dots"></span></div>',
                    unsafe_allow_html=True)

            elif step == "RETRIEVAL_DONE":
                ph_retrieval.markdown(f'<div class="step-done">🔍 <b>检索完成</b>：{msg}</div>', unsafe_allow_html=True)

            elif step == "SELECTOR_START":
                ph_selection.markdown(
                    '<div class="step-active">🎯 <b>精选列...</b><span class="loading-dots"></span></div>',
                    unsafe_allow_html=True)

            elif step == "SELECTOR_DONE":
                ctx_html = render_context_html(payload) if payload else ""
                ph_selection.markdown(f'<div class="step-done">🎯 <b>精选完成</b></div>{ctx_html}',
                                      unsafe_allow_html=True)

            elif step == "GENERATE_START":
                ph_generate.markdown(
                    '<div class="step-active">📝 <b>生成SQL...</b><span class="loading-dots"></span></div>',
                    unsafe_allow_html=True)

            elif step == "GENERATE_DONE" or step == "GENERATE_REPAIRED":
                final_sql = payload or ""
                ph_generate.markdown(f'<div class="step-done">📝 <b>SQL生成完毕</b></div>', unsafe_allow_html=True)

            elif step == "VERIFY_START":
                ph_verify.markdown('<div class="step-active">🧐 <b>审计中...</b></div>', unsafe_allow_html=True)

            elif step == "VERIFY_PASS":
                ph_verify.markdown('<div class="step-done">✅ <b>审计通过</b></div>', unsafe_allow_html=True)

            elif step == "VERIFY_REJECT":
                ph_verify.markdown(f'<div class="step-warn">❌ <b>审计驳回</b>: {msg}</div>', unsafe_allow_html=True)

            elif step == "GENERATE_REPAIR_START":
                ph_generate.markdown(f'<div class="step-active">🔧 <b>正在修正SQL...</b></div>', unsafe_allow_html=True)

            elif step == "EXECUTE_START":
                ph_exec.markdown('<div class="step-active">🚀 <b>执行中...</b></div>', unsafe_allow_html=True)

            # ── 执行完成：暂存数据 ──
            elif step == "EXECUTE_DONE":
                progress_bar.progress(92, text="查询完成")
                row_count = len(payload) if payload else 0
                current_sql_result = payload

                ph_exec.markdown(
                    f'<div class="result-ok">📊 查询成功，获取 {row_count} 行数据</div>',
                    unsafe_allow_html=True
                )
                if payload and len(payload) > 0:
                    with st.expander(f"📋 查看原始数据（共 {row_count} 行）", expanded=False):
                        st.dataframe(pd.DataFrame(payload), use_container_width=True)

            elif step == "EXECUTE_ERROR":
                ph_exec.markdown(f'<div class="step-warn">⚠️ {msg}</div>', unsafe_allow_html=True)

            # ── 最终回答 ──
            elif step == "ANALYSIS" and evt_type == "answer":
                progress_bar.progress(100, text="完成")
                full_answer = payload or ""
                answer_box.markdown(full_answer)
                status_container.update(label="✅ 执行完成", state="complete", expanded=False)

            # ── 图表处理 (自动补全 + 存历史) ──
            elif step == "ANALYSIS" and evt_type == "chart":
                chart_config = payload

                # 自动救援逻辑
                raw_data = chart_config.get("data")
                is_data_empty = False
                if raw_data is None:
                    is_data_empty = True
                elif isinstance(raw_data, dict) and not raw_data:
                    is_data_empty = True

                # 如果后端数据为空，但前端有 SQL 结果，尝试自动生成
                if is_data_empty and current_sql_result and len(current_sql_result) > 0:
                    try:
                        df = pd.DataFrame(current_sql_result)
                        cols = df.columns.tolist()
                        x_col = None
                        y_col = None

                        # 找 String 列
                        for c in cols:
                            if df[c].dtype == 'object' or df[c].dtype == 'string':
                                x_col = c
                                break
                        if not x_col: x_col = cols[0]

                        # 找 Numeric 列
                        for c in cols:
                            if pd.api.types.is_numeric_dtype(df[c]):
                                y_col = c
                                break
                        if not y_col and len(cols) > 1: y_col = cols[1]

                        if x_col and y_col:
                            # 强制转为 list, 剩下的交给 render_chart 里的暴力清洗
                            chart_config["data"] = {
                                "x_axis_data": df[x_col].tolist(),
                                "series_data": df[y_col].tolist(),
                                "title": "自动生成的分析图表 (Auto-Generated)"
                            }
                            st.toast("🔧 前端已自动补全缺失的图表数据", icon="🛡️")
                    except Exception as e:
                        print(f"Auto-chart failed: {e}")

                # 存入历史前更新变量
                last_chart_payload = chart_config

                with chart_box:
                    render_chart(chart_config)

            elif evt_type == "error":
                st.error(f"❌ {msg}")
                return

        # 保存历史
        st.session_state["messages"].append({
            "role": "assistant",
            "content": full_answer,
            "chart": last_chart_payload,
            "sql": final_sql,
        })

    except Exception as e:
        st.error(f"连接失败: {e}")


# ==========================================
# 7. 侧边栏
# ==========================================
with st.sidebar:
    st.markdown("## 🛡️ DBOps Copilot")
    st.caption("自然语言 → SQL 智能查询助手")
    st.markdown("---")
    if st.button("🗑️ 清空对话", use_container_width=True):
        st.session_state["messages"] = []
        st.rerun()
    st.markdown("---")
    st.markdown("##### 🖥️ 系统状态")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("🟢 **数据库**")
    with c2:
        st.markdown("🟢 **知识库**")
    st.markdown("---")
    st.markdown("##### 💡 快捷提问")
    for q in ["最近一个月销售额最高的5个商品", "统计一下每个品牌的总销售额，并按降序排列",
              "我想找一下有多少买过'小米 14 PRO'的用户"]:
        if st.button(f"📌 {q}", use_container_width=True, key=f"q_{q}"):
            st.session_state["_quick_query"] = q
            st.rerun()

# ==========================================
# 8. 主聊天区
# ==========================================
for msg_item in st.session_state["messages"]:
    with st.chat_message(msg_item["role"]):
        st.markdown(msg_item["content"])
        if msg_item.get("chart"):
            render_chart(msg_item["chart"])

quick_query = st.session_state.pop("_quick_query", None)
prompt = st.chat_input("请输入业务问题...")
active_prompt = quick_query or prompt

if active_prompt:
    with st.chat_message("user"):
        st.markdown(active_prompt)
    st.session_state["messages"].append({"role": "user", "content": active_prompt})
    with st.chat_message("assistant"):
        handle_response(active_prompt)