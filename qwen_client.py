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


async def ask_qwen(system_prompt: str, user_message: str, model: str = "qwen/qwen-plus", system_override: bool = False) -> str:
    """Отправляет запрос в Qwen и возвращает ответ."""
    messages = []
    if system_override:
        # user_message уже содержит всё (для проверки кода)
        messages = [{"role": "user", "content": user_message}]
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.3,
        max_tokens=4096,
    )
    return response.choices[0].message.content
