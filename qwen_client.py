import os
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")

# Qwen через OpenRouter (без платёжной информации при регистрации)
client = AsyncOpenAI(
    api_key=DASHSCOPE_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)


async def ask_qwen(system_prompt: str, user_message: str, model: str = "qwen/qwen-plus") -> str:
    """Отправляет запрос в Qwen и возвращает ответ."""
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.7,
        max_tokens=4096,
    )
    return response.choices[0].message.content
