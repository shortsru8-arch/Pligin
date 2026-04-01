import os
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

# Основной ключ — генерация кода
client_main = AsyncOpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)

# Второй ключ — проверка кода
client_review = AsyncOpenAI(
    api_key=os.getenv("GROQ_REVIEW_KEY"),
    base_url="https://api.groq.com/openai/v1",
)

MODEL = "llama-3.3-70b-versatile"


async def ask_qwen(system_prompt: str, user_message: str, model: str = MODEL, system_override: bool = False) -> str:
    """Генерация кода — основной ключ."""
    messages = [{"role": "user", "content": user_message}] if system_override else [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    response = await client_main.chat.completions.create(
        model=model, messages=messages, temperature=0.3, max_tokens=4096,
    )
    return response.choices[0].message.content


async def review_code(prompt: str) -> str:
    """Проверка кода — второй ключ."""
    response = await client_review.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=4096,
    )
    return response.choices[0].message.content
