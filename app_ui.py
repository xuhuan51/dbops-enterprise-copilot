import streamlit as st
import requests
import pandas as pd
import json
import uuid

# ==========================================
# ⚙️ 配置：后端 API 地址
# ==========================================
# 确保这个地址和 main.py 启动的地址一致
API_URL = "http://127.0.0.1:8000/api/v1/query"

st.set_page_config(
    page_title="DBOps Copilot",
    page_icon="🛡️",
    layout="wide"
)

# ==========================================
# 1. CSS 样式优化
# ==========================================
st.markdown("""
<style>
    .stChatMessage { padding: 1rem; border-radius: 8px; margin-bottom: 0.5rem; }
    .log-step { font-family: 'Courier New', monospace; font-size: 0.85em; color: #555; margin-bottom: 4px; }
    .sql-block { background-color: #f6f8fa; padding: 10px; border-radius: 5px; font-family: monospace; font-size: 0.9em; }
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
# 3. 辅助函数：渲染图表
# ==========================================
def render_chart(chart_config):
    """
    根据后端返回的 ECharts 配置渲染 Streamlit 原生图表
    (为了简单起见，这里把配置转为 Streamlit 的图表，也可以用 st_echarts)
    """
    if not chart_config or not chart_config.get("data"):
        return

    chart_type = chart_config.get("type", "bar")
    chart_data = chart_config.get("data", {})

    title = chart_data.get("title", "")
    if title:
        st.caption(f"📊 {title}")

    # 构造 DataFrame
    try:
        x_data = chart_data.get("x_axis_data", [])
        series_data = chart_data.get("series_data", [])

        if not x_data or not series_data:
            return

        df = pd.DataFrame({
            "Label": x_data,
            "Value": series_data
        }).set_index("Label")

        if chart_type == "line":
            st.line_chart(df)
        elif chart_type == "bar":
            st.bar_chart(df)
        elif chart_type == "pie":
            # Streamlit 原生不支持 Pie，用 Bar 代替或引入 plotly/echarts
            st.bar_chart(df)
        else:
            st.table(df)

    except Exception as e:
        st.warning(f"图表渲染失败: {e}")


# ==========================================
# 4. 核心逻辑：处理流式响应
# ==========================================
def handle_response(prompt):
    # 1. 占位符
    status_box = st.status("🚀 正在思考...", expanded=True)
    answer_box = st.empty()
    chart_box = st.empty()

    full_answer = ""
    logs = []

    try:
        # 2. 发起请求
        response = requests.post(
            API_URL,
            json={
                "query": prompt,
                "db_id": "ecommerce",
                "session_id": st.session_state["session_id"]
            },
            stream=True,
            timeout=60
        )

        if response.status_code == 200:
            # 3. 逐行读取 NDJSON
            for line in response.iter_lines():
                if line:
                    try:
                        data = json.loads(line.decode('utf-8'))
                        evt_type = data.get("type")
                        step = data.get("step", "")
                        msg = data.get("msg", "")
                        payload = data.get("payload")

                        # --- A. 处理日志 (Log) ---
                        if evt_type == "log":
                            status_box.write(f"**[{step}]** {msg}")
                            logs.append(f"[{step}] {msg}")

                        # --- B. 处理 SQL (SQL) ---
                        elif evt_type == "sql":
                            status_box.markdown(f"```sql\n{payload}\n```")
                            logs.append(f"[SQL] Generated SQL")

                        # --- C. 处理数据 (Data) ---
                        elif evt_type == "data":
                            df = pd.DataFrame(payload)
                            status_box.dataframe(df, use_container_width=True, height=200)
                            status_box.write(f"✅ 获取 {len(payload)} 行数据")

                        # --- D. 处理回答 (Answer) ---
                        elif evt_type == "answer":
                            full_answer = payload
                            answer_box.markdown(full_answer)
                            status_box.update(label="✅ 完成", state="complete", expanded=False)

                        # --- E. 处理图表 (Chart) ---
                        elif evt_type == "chart":
                            with chart_box:
                                render_chart(payload)

                        # --- F. 错误处理 ---
                        elif evt_type == "error":
                            status_box.error(msg)
                            st.error(f"❌ {msg}")
                            return

                    except json.JSONDecodeError:
                        continue

            # 4. 保存对话历史
            st.session_state["messages"].append({
                "role": "assistant",
                "content": full_answer,
                "chart": payload if evt_type == "chart" else None
            })

        else:
            st.error(f"服务器错误: {response.status_code}")
            st.code(response.text)

    except Exception as e:
        st.error(f"连接失败: {e}")
        status_box.update(label="❌ 连接失败", state="error")


# ==========================================
# 5. 界面渲染
# ==========================================

# 渲染侧边栏
with st.sidebar:
    st.header("控制台")
    if st.button("🗑️ 清空对话"):
        st.session_state["messages"] = []
        st.rerun()

# 渲染历史消息
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        # 如果历史消息里有图表配置，也渲染出来
        if msg.get("chart"):
            render_chart(msg["chart"])

# 处理用户输入
if prompt := st.chat_input("请输入您的问题..."):
    # 显示用户消息
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state["messages"].append({"role": "user", "content": prompt})

    # 显示助手消息
    with st.chat_message("assistant"):
        handle_response(prompt)