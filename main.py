import os
import json
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Request, HTTPException, Header
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from dotenv import load_dotenv

from qwen_client import ask_qwen

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PLUGIN_SECRET = os.getenv("PLUGIN_SECRET", "change-me-secret")

# Промокоды из .env — формат: "RbAi-CODE1,RbAi-CODE2,RbAi-CODE3"
RAW_PROMOS = os.getenv("PROMO_CODES", "")
PROMO_CREDITS = 100  # сколько кредитов даёт один промокод
NEW_USER_CREDITS = 10  # бесплатные кредиты при первом старте

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Хранилища (в проде заменить на БД) ---
tasks_queue: dict[str, list[dict]] = {}
user_sessions: dict[int, str] = {}
user_credits: dict[int, int] = {}          # telegram user_id -> кредиты
used_promos: dict[str, int] = {}           # промокод -> user_id кто использовал
user_last_daily: dict[int, date] = {}      # telegram user_id -> дата последнего начисления

DAILY_CREDITS = 3


def get_credits(user_id: int) -> int:
    return user_credits.get(user_id, NEW_USER_CREDITS)


def give_daily_credits(user_id: int) -> bool:
    """Начисляет 3 ежедневных кредита если сегодня ещё не начислялись. Возвращает True если начислил."""
    today = date.today()
    if user_last_daily.get(user_id) != today:
        user_credits[user_id] = get_credits(user_id) + DAILY_CREDITS
        user_last_daily[user_id] = today
        return True
    return False


def spend_credit(user_id: int) -> bool:
    """Списывает 1 кредит. Возвращает False если кредитов нет."""
    credits = get_credits(user_id)
    if credits <= 0:
        return False
    user_credits[user_id] = credits - 1
    return True


def get_valid_promos() -> list[str]:
    return [p.strip() for p in RAW_PROMOS.split(",") if p.strip()]


# --- Telegram handlers ---

PLUGIN_URL = "https://create.roblox.com/store/asset/131488088668627/RobloxAi"

async def start_command(update: Update, context):
    user_id = update.effective_user.id
    if user_id not in user_credits:
        user_credits[user_id] = NEW_USER_CREDITS
    give_daily_credits(user_id)
    await update.message.reply_text(
        f"Привет! Я бот-мост между тобой и Roblox Studio.\n"
        f"У тебя {get_credits(user_id)} кредитов (1 запрос = 1 кредит).\n"
        f"Каждый день +{DAILY_CREDITS} бесплатных кредита.\n\n"
        f"Плагин для Studio: {PLUGIN_URL}\n\n"
        "Используй /help чтобы увидеть все команды."
    )


async def help_command(update: Update, context):
    user_id = update.effective_user.id
    credits = get_credits(user_id)
    await update.message.reply_text(
        "Команды:\n\n"
        "/start — начало работы\n"
        "/help — список команд\n"
        "/connect <session_id> — привязать к Roblox Studio плагину\n"
        "/status — статус подключения и очереди\n"
        "/clear — очистить очередь задач\n"
        "/balance — посмотреть баланс кредитов\n"
        "/redeem <промокод> — активировать промокод\n\n"
        f"Твой баланс: {credits} кредитов\n"
        "1 кредит = 1 запрос к AI\n\n"
        f"Плагин для Studio: {PLUGIN_URL}\n\n"
        "Пиши что хочешь создать в Roblox Studio — AI сгенерирует код!"
    )


async def balance_command(update: Update, context):
    user_id = update.effective_user.id
    got_daily = give_daily_credits(user_id)
    credits = get_credits(user_id)
    daily_msg = f" (+{DAILY_CREDITS} ежедневных начислено!)" if got_daily else ""
    await update.message.reply_text(f"Твой баланс: {credits} кредитов{daily_msg}")


async def redeem_command(update: Update, context):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Укажи промокод: /redeem <код>")
        return

    code = context.args[0].strip()
    valid_promos = get_valid_promos()

    if code not in valid_promos:
        await update.message.reply_text("Промокод не найден или недействителен.")
        return

    if code in used_promos:
        await update.message.reply_text("Этот промокод уже был использован.")
        return

    used_promos[code] = user_id
    user_credits[user_id] = get_credits(user_id) + PROMO_CREDITS
    await update.message.reply_text(
        f"Промокод активирован! +{PROMO_CREDITS} кредитов.\n"
        f"Твой баланс: {get_credits(user_id)} кредитов."
    )


async def connect_command(update: Update, context):
    if not context.args:
        await update.message.reply_text("Укажи session_id из плагина: /connect <session_id>")
        return
    session_id = context.args[0]
    user_sessions[update.effective_user.id] = session_id
    if session_id not in tasks_queue:
        tasks_queue[session_id] = []
    await update.message.reply_text(
        f"Подключено к сессии: {session_id}\n"
        "Теперь пиши что хочешь создать в Roblox Studio!"
    )


async def clear_command(update: Update, context):
    user_id = update.effective_user.id
    session_id = user_sessions.get(user_id)
    if not session_id:
        await update.message.reply_text("Ты не подключён.")
        return
    tasks_queue[session_id] = []
    await update.message.reply_text("Очередь задач очищена.")


async def status_command(update: Update, context):
    user_id = update.effective_user.id
    session_id = user_sessions.get(user_id)
    if not session_id:
        await update.message.reply_text("Ты не подключён. Используй /connect <session_id>")
        return
    pending = len([t for t in tasks_queue.get(session_id, []) if not t["done"]])
    credits = get_credits(user_id)
    await update.message.reply_text(
        f"Сессия: {session_id}\n"
        f"Задач в очереди: {pending}\n"
        f"Кредитов: {credits}"
    )


SYSTEM_PROMPT = """Ты — AI-ассистент для создания объектов и скриптов в Roblox Studio.
Пользователь описывает что хочет создать, ты генерируешь Lua код для Roblox Studio.

ВАЖНО: Твой ответ должен содержать ТОЛЬКО валидный JSON (без markdown, без ```).
Формат ответа — JSON массив задач:
[
  {
    "type": "script",
    "name": "имя скрипта",
    "parent": "Workspace",
    "code": "print('hello')"
  },
  {
    "type": "build",
    "name": "имя объекта",
    "parent": "Workspace",
    "code": "local part = Instance.new('Part', workspace)\\npart.Size = Vector3.new(10, 1, 10)\\npart.Position = Vector3.new(0, 0.5, 0)\\npart.Anchored = true\\npart.Name = 'Floor'"
  }
]

Типы задач:
- "script" — создать Script в Studio с кодом внутри (серверный скрипт)
- "build" — выполнить код для создания объектов (Part, Model и т.д.)

Поле "parent" — куда поместить (Workspace, ServerScriptService, ReplicatedStorage и т.д.)
Поле "code" — валидный Roblox Lua код.

ЗАПРЕЩЕНО в коде:
- WaitForChild() без таймаута — используй WaitForChild("name", 5) с таймаутом
- Присваивать RootPart напрямую — это read-only свойство
- Использовать require() для внешних модулей
- Предполагать что объекты уже существуют в Workspace без проверки (всегда проверяй через FindFirstChild)
- Бесконечные циклы без wait() внутри

ОБЯЗАТЕЛЬНО:
- Все WaitForChild с таймаутом: WaitForChild("X", 5)
- Перед доступом к объекту проверяй его существование: if obj then ... end
- Humanoid создавай сам через Instance.new если нужен, не жди его
- RootPart не трогай, используй PrimaryPart для Model
"""

REVIEW_PROMPT = """Проверь этот Roblox Lua JSON код на ошибки. Исправь если найдёшь:
1. WaitForChild без таймаута → добавь второй аргумент (число секунд)
2. Присваивание RootPart → убери, это read-only
3. Обращение к объектам без проверки существования → добавь if obj then
4. Бесконечные циклы без wait() → добавь wait(0.1) внутрь

Верни ТОЛЬКО исправленный JSON (без markdown, без объяснений). Если ошибок нет — верни тот же JSON.

JSON для проверки:
"""


async def handle_message(update: Update, context):
    user_id = update.effective_user.id
    session_id = user_sessions.get(user_id)

    if not session_id:
        await update.message.reply_text(
            "Сначала подключись к Roblox Studio: /connect <session_id>\n"
            "(session_id покажет плагин при запуске)"
        )
        return

    # Начисляем ежедневные кредиты если нужно
    give_daily_credits(user_id)

    # Проверяем кредиты
    if not spend_credit(user_id):
        await update.message.reply_text(
            "У тебя закончились кредиты!\n"
            "Активируй промокод: /redeem <код>"
        )
        return

    user_text = update.message.text
    credits_left = get_credits(user_id)
    await update.message.reply_text(f"Генерирую код... (осталось кредитов: {credits_left})")

    try:
        response = await ask_qwen(SYSTEM_PROMPT, user_text)

        # 3 круга проверки кода перед отправкой
        for i in range(3):
            reviewed = await ask_qwen("", REVIEW_PROMPT + response, system_override=True)
            reviewed = reviewed.strip()
            if reviewed.startswith("```"):
                reviewed = reviewed.split("\n", 1)[1] if "\n" in reviewed else reviewed[3:]
                if reviewed.endswith("```"):
                    reviewed = reviewed[:-3]
                reviewed = reviewed.strip()
            try:
                json.loads(reviewed)
                response = reviewed
            except json.JSONDecodeError:
                break

        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()

        generated_tasks = json.loads(clean)
        if not isinstance(generated_tasks, list):
            generated_tasks = [generated_tasks]

        task_id_start = len(tasks_queue.get(session_id, []))
        for i, task in enumerate(generated_tasks):
            task["id"] = task_id_start + i
            task["done"] = False
            tasks_queue[session_id].append(task)

        summary_lines = []
        for task in generated_tasks:
            emoji = "📝" if task["type"] == "script" else "🏗"
            summary_lines.append(f"{emoji} {task['type']}: {task.get('name', 'unnamed')} → {task.get('parent', 'Workspace')}")

        await update.message.reply_text(
            f"Готово! Отправлено {len(generated_tasks)} задач(и) в Roblox Studio:\n\n"
            + "\n".join(summary_lines)
            + "\n\nПлагин подхватит их автоматически."
        )

    except json.JSONDecodeError:
        # Возвращаем кредит если запрос не удался
        user_credits[user_id] = get_credits(user_id) + 1
        await update.message.reply_text("Qwen вернул некорректный ответ. Кредит возвращён. Попробуй переформулировать.")
        logger.error(f"Failed to parse Qwen response: {response[:500]}")
    except Exception as e:
        user_credits[user_id] = get_credits(user_id) + 1
        await update.message.reply_text(f"Ошибка: {str(e)}\nКредит возвращён.")
        logger.error(f"Error handling message: {e}")


# --- FastAPI + Telegram ---

application: Application = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("redeem", redeem_command))
    application.add_handler(CommandHandler("connect", connect_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await application.initialize()
    await application.start()

    webhook_url = os.getenv("WEBHOOK_URL", "")
    if webhook_url:
        await application.bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"Webhook set to {webhook_url}/webhook")
    else:
        await application.updater.start_polling()
        logger.info("Started polling mode")

    yield

    if application.updater and application.updater.running:
        await application.updater.stop()
    await application.stop()
    await application.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


@app.get("/api/tasks/{session_id}")
async def get_tasks(session_id: str, authorization: str = Header(default="")):
    if authorization != f"Bearer {PLUGIN_SECRET}":
        raise HTTPException(status_code=401, detail="Invalid secret")
    pending_tasks = [t for t in tasks_queue.get(session_id, []) if not t["done"]]
    return {"tasks": pending_tasks}


@app.post("/api/tasks/{session_id}/{task_id}/done")
async def mark_task_done(session_id: str, task_id: int, authorization: str = Header(default="")):
    if authorization != f"Bearer {PLUGIN_SECRET}":
        raise HTTPException(status_code=401, detail="Invalid secret")
    for task in tasks_queue.get(session_id, []):
        if task["id"] == task_id:
            task["done"] = True
            return {"ok": True}
    raise HTTPException(status_code=404, detail="Task not found")


@app.get("/health")
async def health():
    return {"status": "ok"}
