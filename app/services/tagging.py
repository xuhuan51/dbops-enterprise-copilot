import json
from openai import OpenAI
from app.core.config import settings
from app.core.prompts import SCHEMA_ENRICH_PROMPT

# 初始化 Client
client = OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)


def analyze_table_semantics(table, comment, cols, sample_data):
    """
    调用 LLM 分析表语义
    """
    # 🟢 修复点：使用清洗后的 key 'name'，而不是原始 SQL 的 'COLUMN_NAME'
    # 使用 .get 此时更安全，防止万一 key 不存在报错
    col_summary = ", ".join([str(c.get('name', '')) for c in cols[:15]])

    prompt = SCHEMA_ENRICH_PROMPT.format(
        table_name=table,
        table_comment=comment,
        columns_info=col_summary,
        sample_data=sample_data
    )

    try:
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful data assistant. Output JSON only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=512
        )
        content = response.choices[0].message.content.strip()

        # 清洗 Markdown 标记 (Robustness)
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[0]

        return json.loads(content)

    except Exception as e:
        # 打印简单错误信息，不要刷屏
        print(f"⚠️ LLM 分析异常 (已兜底): {e}")
        return {"keywords": table, "description": comment}