import streamlit as st
import requests
import pandas as pd
import uuid
import json
import time

# ==========================================
# ⚙️ 配置：后端 API 地址
# ==========================================
API_URL = "http://127.0.0.1:8000/api/v1/query"

st.set_page_config(
    page_title="DBOps Copilot",
    page_icon="🛡️",
    layout="wide"
)

# ==========================================
# 🎨 1. 企业级 CSS 样式
# ==========================================
st.markdown("""
<style>
    /* 聊天气泡优化 */
    .stChatMessage {
        padding: 1rem;
        border-radius: 8px;
        margin-bottom: 0.5rem;
    }

    /* 日志样式 (仿终端) */
    .log-step {
        font-family: 'Courier New', monospace;
        font-size: 0.9em;
        color: #333;
        margin-bottom: 4px;
        display: flex;
        align-items: center;
    }
    .log-icon { margin-right: 8px; }

    /* 安全拦截警告框 */
    .safety-box {
        background-color: #fff3cd; 
        border: 1px solid #ffeeba; 
        color: #856404; 
        padding: 15px; 
        border-radius: 5px; 
        border-left: 5px solid #ffc107;
        margin: 10px 0;
    }

    /* 错误提示框 */
    .error-box {
        background-color: #f8d7da;
        border: 1px solid #f5c6cb;
        color: #721c24;
        padding: 10px;
        border-radius: 5px;
        margin: 10px 0;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 🧠 2. 状态管理
# ==========================================
if "messages" not in st.session_state:
    st.session_state["messages"] = [{
        "role": "assistant",
        "content": {
            "type": "text",
            "text": "👋 **你好！我是 DBOps 智能助手。**\n\n我可以帮你查询数据（如“统计上海用户”），或生成变更脚本（如“修改积分”）。\n\n**核心能力：**\n- 🔍 **流式思考**：实时展示 RAG 检索与 SQL 生成过程\n- 🛡️ **安全哨兵**：自动拦截高危 DML 操作\n- 📊 **智能分析**：自动绘制图表与数据解读"
        }
    }]

if "session_id" not in st.session_state:
    st.session_state["session_id"] = str(uuid.uuid4())


# ==========================================
# 📊 3. 辅助函数：智能画图
# ==========================================
def smart_visualize(df: pd.DataFrame):
    """根据数据特征智能选择图表类型"""
    if df.empty or len(df) < 2:
        return

    num_cols = df.select_dtypes(include=['number']).columns
    cat_cols = df.select_dtypes(exclude=['number']).columns

    # 只有当存在数字列时才画图
    if len(num_cols) > 0:
        target_col = num_cols[0]  # 取第一列数字作为 Y 轴

        # 场景 A: 时间序列 -> 折线图
        if len(cat_cols) > 0 and any(t in cat_cols[0].lower() for t in ['date', 'time', 'day', 'month', 'year']):
            st.caption(f"📈 趋势分析 ({target_col})")
            chart_df = df.set_index(cat_cols[0])
            st.line_chart(chart_df[target_col])

        # 场景 B: 分类数据 (且行数适中) -> 柱状图
        elif len(cat_cols) > 0 and len(df) <= 20:
            st.caption(f"📊 分布对比 ({target_col})")
            chart_df = df.set_index(cat_cols[0])
            st.bar_chart(chart_df[target_col])


# ==========================================
# 🛠️ 4. 核心渲染函数 (处理历史消息)
# ==========================================
def render_message_content(content):
    """
    渲染消息体，包含：
    1. 思考过程 (折叠的 logs)
    2. 主内容 (文本/警告/错误)
    3. 数据表格 & 图表
    """
    # --- A. 渲染思考过程 (如果有日志) ---
    logs = content.get("logs", [])
    if logs:
        # 计算总耗时 (假定 logs 最后一个时间点减去第一个)
        duration_label = "Process Log"
        with st.expander(f"🧠 思考链路 ({len(logs)} steps)", expanded=False):
            for log in logs:
                step_name = log.get('step', '').upper()
                msg = log.get('msg', '')
                details = log.get('details', '')

                st.markdown(f"<div class='log-step'><b>[{step_name}]</b>&nbsp;{msg}</div>", unsafe_allow_html=True)
                if details:
                    # 如果是 SQL，高亮显示
                    if "```sql" in details:
                        st.markdown(details)
                    else:
                        st.caption(details)
            st.caption("✅ Execution Finished")

    # --- B. 渲染主内容 ---
    msg_type = content.get("result_type", "text")  # result_type: success, safety_warning, error, text
    text = content.get("text", "")

    # 1. 安全拦截
    if msg_type == "safety_warning":
        st.markdown(
            f"""<div class="safety-box">
            <div style="font-weight:bold; font-size:1.1em; margin-bottom:5px;">🛡️ Copilot 安全拦截</div>
            检测到高风险变更意图。基于 DBOps 规范，已为您生成 SQL 脚本，但<b>禁止自动执行</b>。
            </div>""",
            unsafe_allow_html=True
        )
        if content.get("sql"):
            st.code(content["sql"], language="sql")
        st.markdown(text)  # 可能包含分析师的补充说明

    # 2. 系统错误
    elif msg_type == "error":
        st.markdown(f"""<div class="error-box">❌ {text}</div>""", unsafe_allow_html=True)

    # 3. 正常回复 / 纯文本
    else:
        st.markdown(text)

    # --- C. 渲染数据 (表格 + 图表) ---
    data = content.get("data", [])
    if data and len(data) > 0:
        df = pd.DataFrame(data)
        st.dataframe(df, use_container_width=True, hide_index=True)
        smart_visualize(df)


# ==========================================
# 📺 5. 侧边栏
# ==========================================
with st.sidebar:
    st.header("控制台")
    st.caption(f"Session: {st.session_state['session_id'][:8]}...")

    # API 健康检查
    if st.button("📡 检查连接"):
        try:
            requests.get("[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)", timeout=1)
            st.toast("✅ 后端服务在线", icon="🟢")
        except:
            st.error("无法连接后端，请检查 uvicorn 是否启动")

    st.markdown("---")
    if st.button("🗑️ 清空对话"):
        st.session_state["messages"] = []
        st.session_state["session_id"] = str(uuid.uuid4())
        st.rerun()

# ==========================================
# 💬 6. 渲染历史消息流
# ==========================================
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        render_message_content(msg["content"])

# ==========================================
# 🎮 7. 处理用户输入 (流式核心)
# ==========================================
if prompt := st.chat_input("输入 SQL 需求..."):
    # 1. 立即上屏用户问题
    st.session_state["messages"].append({"role": "user", "content": {"text": prompt}})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 2. 助手回复区域
    with st.chat_message("assistant"):
        # 占位符：用于流式更新状态
        status_container = st.status("🚀 Agent 启动中...", expanded=True)
        placeholder = st.empty()

        # 临时变量：用于收集流中的所有信息，最后存入 History
        collected_logs = []
        final_response_content = {
            "text": "",
            "data": [],
            "logs": [],
            "result_type": "text",
            "sql": None
        }

        try:
            # 构造请求
            payload = {
                "query": prompt,
                "session_id": st.session_state["session_id"],
                "user_id": "streamlit_ui"
            }

            # 🔥 发起流式请求 (stream=True)
            response = requests.post(API_URL, json=payload, stream=True, timeout=60)

            if response.status_code == 200:
                # 逐行读取流数据 (NDJSON)
                for line in response.iter_lines():
                    if line:
                        event = json.loads(line.decode('utf-8'))
                        evt_type = event.get("type")

                        # === A. 处理过程日志 (Log) ===
                        if evt_type == "log":
                            step = event.get("step")
                            msg = event.get("msg")
                            # 实时更新 Status 标题
                            status_container.update(label=f"🔄 {msg}", state="running")
                            # 在 Status 内部打印详情
                            status_container.markdown(f"**[{step.upper()}]** {msg}")
                            if event.get("details"):
                                status_container.caption(event["details"])

                            # 收集日志
                            collected_logs.append(event)

                        # === B. 处理中间状态 (Status) ===
                        elif evt_type == "status":
                            status_container.update(label=event.get("msg"), state="running")

                        # === C. 处理最终结果 (Result) ===
                        elif evt_type == "result":
                            status_str = event.get("status")  # success, safety_warning, error
                            final_msg = event.get("msg", "")
                            final_data = event.get("data", [])

                            # 填充最终内容包
                            final_response_content["text"] = final_msg
                            final_response_content["data"] = final_data
                            final_response_content["sql"] = event.get("sql")

                            # 映射 Result Type
                            if status_str == "safety_warning":
                                final_response_content["result_type"] = "safety_warning"
                                status_container.update(label="🛡️ 安全拦截触发", state="error", expanded=False)
                            elif status_str == "error":
                                final_response_content["result_type"] = "error"
                                status_container.update(label="❌ 执行出错", state="error", expanded=False)
                            else:
                                final_response_content["result_type"] = "success"
                                status_container.update(label="✅ 执行完成", state="complete", expanded=False)

                        # === D. 处理错误 (Stream Error) ===
                        elif evt_type == "error":
                            status_container.error(event.get("msg"))
                            collected_logs.append(event)

                # 流结束，保存日志
                final_response_content["logs"] = collected_logs

                # 🔥 渲染最终结果 (使用 helper 函数)
                placeholder.empty()  # 清理掉可能的临时显示
                render_message_content(final_response_content)

                # 存入 History
                st.session_state["messages"].append({
                    "role": "assistant",
                    "content": final_response_content
                })

            else:
                status_container.update(label="❌ 服务器连接失败", state="error")
                st.error(f"HTTP Error {response.status_code}: {response.text}")

        except Exception as e:
            status_container.update(label="❌ 客户端错误", state="error")
            st.error(f"UI Error: {str(e)}")