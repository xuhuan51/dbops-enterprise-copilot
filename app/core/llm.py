import os
import re
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# 1. 优先读取环境变量
CURRENT_MODEL = os.getenv("LLM_MODEL_NAME", "qwen2.5:14b")

# 初始化客户端
client = OpenAI(
    api_key=os.getenv("LLM_API_KEY", "ollama"),
    base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
)


def extract_json_from_text(text: str) -> str:
    """
    🧹 专用清洗函数：从大模型的废话中提取 JSON
    改了个名字，防止和局部变量冲突
    """
    try:
        # 1. 尝试找到第一个 '{' 和最后一个 '}'
        start = text.find('{')
        end = text.rfind('}')

        if start != -1 and end != -1:
            # 截取中间这一段，这才是真正的 JSON
            return text[start:end + 1]

        # 2. 如果没找到大括号，就把 markdown 符号去掉试试
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return text.strip()
    except Exception:
        return text


def chat_completion(prompt: str, model: str = None) -> str:
    """
    通用 LLM 调用函数
    """
    target_model = model or CURRENT_MODEL

    try:
        # 📸 [监控 1] 发送前打印
        print("\n" + "=" * 40)
        print(f"🚀 [Send to LLM]: {prompt[:50]}... (Prompt Sent)")
        print("-" * 40)

        response = client.chat.completions.create(
            model=target_model,
            messages=[
                {"role": "system", "content": "You are a strict JSON data assistant. Output ONLY valid JSON object."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0,
            max_tokens=1024
        )
        raw_content = response.choices[0].message.content.strip()

        # 📸 [监控 2] 打印原始回复
        print(f"🧠 [LLM Raw Response]:\n{raw_content}")

        # 🛑 调用清洗函数 (注意这里名字改了)
        final_json = extract_json_from_text(raw_content)

        # 📸 [监控 3] 打印清洗结果
        print(f"✨ [Cleaned JSON]: {final_json}")
        print("=" * 40 + "\n")

        return final_json

    except Exception as e:
        print(f"❌ LLM 调用失败: {e}")
        # 返回空 JSON 防止报错
        return "{}"