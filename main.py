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

PLUGIN_FILE = "QwenAssistant.lua"
INSTALL_INSTRUCTIONS = (
    "📥 Установка плагина:\n\n"
    "1. Скачай файл QwenAssistant.lua выше\n"
    "2. Скопируй его в папку:\n"
    "   Windows: %localappdata%\\Roblox\\Plugins\\\n"
    "   (вставь путь в адресную строку проводника)\n"
    "3. Открой Roblox Studio\n"
    "4. Game Settings → Security → включи Allow HTTP Requests\n"
    "5. В тулбаре появится кнопка Qwen Assistant\n"
    "6. Нажми на неё — увидишь Session ID\n"
    "7. Напиши боту: /connect <session_id>\n\n"
    "Готово! Теперь пиши что хочешь создать."
)

async def start_command(update: Update, context):
    user_id = update.effective_user.id
    if user_id not in user_credits:
        user_credits[user_id] = NEW_USER_CREDITS
    give_daily_credits(user_id)
    await update.message.reply_text(
        f"Привет! Я бот-мост между тобой и Roblox Studio.\n"
        f"У тебя {get_credits(user_id)} кредитов (1 запрос = 1 кредит).\n"
        f"Каждый день +{DAILY_CREDITS} бесплатных кредита.\n\n"
        "Используй /help чтобы увидеть все команды.\n"
        "Используй /plugin чтобы получить файл плагина."
    )


async def help_command(update: Update, context):
    user_id = update.effective_user.id
    credits = get_credits(user_id)
    await update.message.reply_text(
        "Команды:\n\n"
        "/start — начало работы\n"
        "/help — список команд\n"
        "/plugin — получить файл плагина + инструкция\n"
        "/connect <session_id> — привязать к Roblox Studio плагину\n"
        "/status — статус подключения и очереди\n"
        "/clear — очистить очередь задач\n"
        "/balance — посмотреть баланс кредитов\n"
        "/redeem <промокод> — активировать промокод\n\n"
        f"Твой баланс: {credits} кредитов\n"
        "1 кредит = 1 запрос к AI\n\n"
        "Пиши что хочешь создать в Roblox Studio — AI сгенерирует код!"
    )


async def plugin_command(update: Update, context):
    try:
        with open(PLUGIN_FILE, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="QwenAssistant.lua",
                caption=INSTALL_INSTRUCTIONS
            )
    except FileNotFoundError:
        await update.message.reply_text("Файл плагина не найден на сервере.")


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


SYSTEM_PROMPT = """Ты — Senior Roblox Developer с 10-летним опытом. Ты создаёшь профессиональный, оптимизированный и красивый контент для Roblox Studio.

ФОРМАТ ОТВЕТА: ТОЛЬКО валидный JSON массив (без markdown, без ```, без объяснений вне JSON).
[
  {"type": "build", "name": "НазваниеОбъекта", "parent": "Workspace", "code": "-- Lua код"},
  {"type": "script", "name": "НазваниеСкрипта", "parent": "ServerScriptService", "code": "-- Lua код"}
]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📚 ДОКУМЕНТАЦИЯ ROBLOX LUA API
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ОСНОВНЫЕ СЕРВИСЫ:
  game:GetService("Players")          -- список игроков, события входа/выхода
  game:GetService("RunService")       -- Heartbeat, RenderStepped, Stepped
  game:GetService("TweenService")     -- анимации свойств
  game:GetService("UserInputService") -- клавиатура, мышь (только LocalScript)
  game:GetService("SoundService")     -- звуки
  game:GetService("Lighting")         -- освещение, время суток
  game:GetService("CollectionService")-- теги для группировки объектов
  game:GetService("DataStoreService") -- сохранение данных игроков
  game:GetService("HttpService")      -- HTTP запросы, JSON
  game:GetService("ReplicatedStorage")-- общие ресурсы клиент+сервер
  game:GetService("ServerScriptService") -- только серверные скрипты
  game:GetService("StarterGui")       -- UI для игроков
  game:GetService("StarterPlayer")    -- настройки персонажа

PART (BasePart) СВОЙСТВА:
  part.Size = Vector3.new(x, y, z)        -- размер в studs
  part.CFrame = CFrame.new(x, y, z)       -- позиция + ориентация
  part.Position = Vector3.new(x, y, z)    -- только позиция
  part.Anchored = true/false              -- закреплена ли
  part.CanCollide = true/false            -- есть ли коллизия
  part.Transparency = 0..1               -- прозрачность
  part.Material = Enum.Material.X        -- материал
  part.Color = Color3.fromRGB(r,g,b)     -- цвет
  part.BrickColor = BrickColor.new("X")  -- цвет по имени
  part.Shape = Enum.PartType.Ball/Cylinder/Block
  part.CastShadow = true/false
  part.Massless = true                   -- без массы (для украшений)

МАТЕРИАЛЫ (Enum.Material):
  SmoothPlastic, Plastic, Wood, WoodPlanks, Brick, Cobblestone,
  Concrete, CorrodedMetal, DiamondPlate, Fabric, Foil, Glacier,
  Granite, Grass, Ground, Ice, LeafyGrass, Marble, Metal, Mud,
  Neon, Pebble, Rock, Salt, Sand, Sandstone, Slate, SmoothPlastic,
  Snow, Stu, Terracotta, Limestone, Basalt, CrackedLava

CFRAME ОПЕРАЦИИ:
  CFrame.new(x, y, z)                          -- позиция
  CFrame.new(x,y,z) * CFrame.Angles(rx,ry,rz)  -- позиция + поворот в радианах
  CFrame.fromEulerAnglesXYZ(rx, ry, rz)         -- из углов Эйлера
  CFrame.lookAt(from, to)                        -- направить на точку
  cf1 * cf2                                      -- комбинировать CFrame
  part.CFrame = part.CFrame + Vector3.new(0,1,0) -- сдвиг
  math.rad(90)                                   -- градусы → радианы

СОЗДАНИЕ ОБЪЕКТОВ:
  local obj = Instance.new("ClassName")   -- создать объект
  obj.Parent = workspace                  -- установить родителя
  obj:Destroy()                           -- удалить объект
  obj:Clone()                             -- клонировать
  obj:FindFirstChild("name")              -- найти потомка (nil если нет)
  obj:FindFirstChildOfClass("Script")     -- найти по классу
  obj:WaitForChild("name", timeout)       -- ждать потомка (ВСЕГДА с таймаутом!)
  obj:GetChildren()                       -- список потомков
  obj:GetDescendants()                    -- все потомки рекурсивно
  obj:IsA("BasePart")                     -- проверка класса

MODEL:
  local model = Instance.new("Model", workspace)
  model.Name = "MyModel"
  model.PrimaryPart = rootPart            -- основная часть для :SetPrimaryPartCFrame()
  model:SetPrimaryPartCFrame(cf)          -- переместить всю модель
  model:GetBoundingBox()                  -- возвращает CFrame, Vector3 размера

СПЕЦИАЛЬНЫЕ ЧАСТИ:
  Instance.new("SpawnLocation")           -- точка спавна
  Instance.new("WedgePart")              -- клин (для скосов крыш)
  Instance.new("CornerWedgePart")        -- угловой клин
  Instance.new("TrussPart")             -- ферма (лестница)
  Instance.new("UnionOperation")         -- CSG объединение (только через Studio UI)

HUMANOID:
  local humanoid = Instance.new("Humanoid")
  humanoid.MaxHealth = 100
  humanoid.Health = 100
  humanoid.WalkSpeed = 16               -- скорость ходьбы (default 16)
  humanoid.JumpPower = 50               -- сила прыжка (default 50)
  humanoid.DisplayName = "Name"
  humanoid:TakeDamage(amount)
  humanoid.Died:Connect(function() end)
  -- НИКОГДА не присваивай RootPart — это read-only!
  -- Используй HumanoidRootPart как обычный Part

CHARACTER:
  local Players = game:GetService("Players")
  Players.PlayerAdded:Connect(function(player)
    player.CharacterAdded:Connect(function(character)
      local humanoid = character:WaitForChild("Humanoid", 10)
      local hrp = character:WaitForChild("HumanoidRootPart", 10)
      local rootPart = character.PrimaryPart  -- = HumanoidRootPart
    end)
  end)

TWEEN (анимации):
  local TweenService = game:GetService("TweenService")
  local info = TweenInfo.new(
    duration,           -- секунды
    Enum.EasingStyle.Quad,   -- стиль (Linear/Quad/Cubic/Bounce/Elastic/Back)
    Enum.EasingDirection.Out, -- направление (In/Out/InOut)
    repeatCount,        -- -1 = бесконечно
    reverses,           -- true = туда-обратно
    delayTime           -- задержка перед стартом
  )
  local tween = TweenService:Create(part, info, {Transparency = 1, Size = Vector3.new(5,5,5)})
  tween:Play()
  tween.Completed:Connect(function() end)

СОБЫТИЯ:
  part.Touched:Connect(function(hit) end)           -- касание
  part.TouchEnded:Connect(function(hit) end)        -- конец касания
  humanoid.HealthChanged:Connect(function(hp) end)
  game.Players.PlayerAdded:Connect(function(p) end)
  game.Players.PlayerRemoving:Connect(function(p) end)
  RunService.Heartbeat:Connect(function(dt) end)    -- каждый кадр (сервер)
  RunService.RenderStepped:Connect(function(dt) end)-- каждый кадр (клиент)

REMOTE EVENTS (клиент ↔ сервер):
  -- В ReplicatedStorage создай RemoteEvent
  -- Сервер → Клиент:
  remoteEvent:FireClient(player, data)
  remoteEvent:FireAllClients(data)
  -- Клиент → Сервер:
  remoteEvent:FireServer(data)
  -- Получение на сервере:
  remoteEvent.OnServerEvent:Connect(function(player, data) end)
  -- Получение на клиенте:
  remoteEvent.OnClientEvent:Connect(function(data) end)

ЗВУКИ:
  local sound = Instance.new("Sound", workspace)
  sound.SoundId = "rbxassetid://ASSET_ID"
  sound.Volume = 0.5       -- 0..10
  sound.Looped = false
  sound:Play()
  sound:Stop()
  sound:Pause()

ОСВЕЩЕНИЕ:
  local Lighting = game:GetService("Lighting")
  Lighting.TimeOfDay = "14:00:00"   -- время суток
  Lighting.Brightness = 2
  Lighting.Ambient = Color3.fromRGB(70,70,70)
  Lighting.FogEnd = 1000
  Instance.new("Atmosphere", Lighting)   -- атмосфера
  Instance.new("BloomEffect", Lighting)  -- bloom
  Instance.new("SunRaysEffect", Lighting)

ФИЗИКА:
  part.Velocity = Vector3.new(0, 50, 0)    -- скорость
  part.AssemblyLinearVelocity = Vector3.new(0,50,0)  -- новый API
  local bodyVel = Instance.new("BodyVelocity", part)
  bodyVel.Velocity = Vector3.new(0, 50, 0)
  bodyVel.MaxForce = Vector3.new(0, math.huge, 0)

ЗАДЕРЖКИ И ТАСКИ:
  task.wait(seconds)              -- пауза (НЕ используй wait())
  task.spawn(function() end)      -- запустить в новом потоке
  task.delay(time, function() end)-- отложенный запуск
  task.defer(function() end)      -- в конце текущего кадра

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏗 ПРАВИЛА СТРОИТЕЛЬСТВА
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

МАТЕМАТИКА ПОЗИЦИЙ — ключевое правило:
  Центр части = нижняя_точка + высота/2
  Пол 1 stud → центр Y=0.5, верхняя точка Y=1.0
  Стена на полу высотой 10 → центр Y = 1.0 + 10/2 = 6.0
  Следующий этаж → нижняя точка = 1.0 + 10.0 = 11.0

НИКОГДА не делай так (части внутри друг друга):
  -- ПЛОХО: пол Y=0.5(высота 1), стена Y=0.5(высота 10) — стена ВНУТРИ пола
  -- ХОРОШО: пол Y=0.5(высота 1), стена Y=6(высота 10) — стена СТОИТ НА полу

СТЕНЫ ДОМА (пол 20x20, толщина стен 1, высота 10):
  Пол:           Size(20,1,20),  CFrame(0, 0.5, 0)
  Стена перед:   Size(20,10,1),  CFrame(0, 6, -9.5)   -- Z = -10+0.5
  Стена зади:    Size(20,10,1),  CFrame(0, 6,  9.5)   -- Z =  10-0.5
  Стена лево:    Size(1,10,18),  CFrame(-9.5, 6, 0)   -- X = -10+0.5, Z-size без угловых
  Стена право:   Size(1,10,18),  CFrame( 9.5, 6, 0)
  Потолок:       Size(20,1,20),  CFrame(0, 11.5, 0)   -- Y = 1+10+0.5

СКОС КРЫШИ — используй WedgePart:
  local wedge = Instance.new("WedgePart", model)
  wedge.Size = Vector3.new(20, 4, 10)
  wedge.CFrame = CFrame.new(0, 14, -5) * CFrame.Angles(0, 0, 0)

ОКНА — несколько частей вокруг проёма (не делай стекло внутри стены):
  -- 4 части: верх, низ, лево, право рамки окна + 1 стекло вровень со стеной

КАЧЕСТВО:
  - Используй разные Material для разных частей (Brick для стен, Wood для пола, Glass для окон)
  - Группируй всё в Model
  - Добавляй детали: ступени, подоконники, карнизы
  - Используй CFrame вместо Position для точности

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📝 ПРАВИЛА СКРИПТОВ
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Script        → ServerScriptService (серверная логика)
  LocalScript   → StarterPlayer/StarterPlayerScripts (клиент)
  ModuleScript  → ReplicatedStorage (общие модули)

СТРУКТУРА СКРИПТА:
  -- Сервисы вверху
  local Players = game:GetService("Players")
  local RunService = game:GetService("RunService")
  -- Константы
  local SPEED = 16
  -- Функции
  local function doSomething() end
  -- Инициализация
  doSomething()
  -- События внизу
  Players.PlayerAdded:Connect(...)

ОСВЕЩЕНИЕ ОБЪЕКТОВ (Light):
  PointLight, SpotLight, SurfaceLight — дочерние объекты Part
  Свойства: Brightness(число), Range(число), Color(Color3), Enabled(bool)
  SpotLight: + Angle(число), Face(Enum.NormalId)
  SurfaceLight: + Angle(число), Face(Enum.NormalId)
  ЗАПРЕЩЕНО: InverseSquared — такого свойства НЕ СУЩЕСТВУЕТ
  Пример:
    local light = Instance.new("PointLight", part)
    light.Brightness = 2
    light.Range = 20
    light.Color = Color3.fromRGB(255, 200, 100)

ЗАПРЕЩЕНО:
  wait()        → task.wait()
  spawn()       → task.spawn()
  delay()       → task.delay()
  WaitForChild без таймаута → WaitForChild("x", 5)
  part.RootPart = x  → read-only, не трогай
  Infinite loop без yield → добавь task.wait()
  light.InverseSquared → не существует, удали

ОБЯЗАТЕЛЬНО:
  if obj then ... end   -- всегда проверяй перед использованием
  pcall(func)           -- для опасных операций
  :Destroy()            -- очищай неиспользуемые объекты
"""

REVIEW_PROMPT = """Ты — строгий code reviewer для Roblox Lua. Проверь JSON и исправь ВСЕ найденные проблемы.

ПРОВЕРЯЙ ПО ПОРЯДКУ:

1. ПЕРЕСЕЧЕНИЕ ЧАСТЕЙ (самое важное!):
   - Вычисли реальные границы каждой части: min = pos - size/2, max = pos + size/2
   - Если границы двух частей перекрываются — исправь позиции
   - Пол на Y=0.5 (size 1) → верх пола = Y=1.0 → стены начинаются от Y=1.0

2. КОД ОШИБКИ:
   - wait() → task.wait()
   - spawn() → task.spawn()
   - WaitForChild без 2го аргумента → добавь таймаут 5
   - part.RootPart = x → удали строку
   - Бесконечный цикл без yield → добавь task.wait(0.1)
   - Обращение к объекту без проверки → добавь if obj then

3. КАЧЕСТВО:
   - Все статичные части имеют Anchored = true?
   - Части сгруппированы в Model?
   - Есть Material у частей?

Верни ТОЛЬКО исправленный JSON (без markdown, без объяснений).
Если ошибок нет — верни тот же JSON без изменений.

JSON:
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
    # Игнорируем сообщения от ботов (включая себя)
    if not update.effective_user or update.effective_user.is_bot:
        return

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
    application.add_handler(CommandHandler("plugin", plugin_command))
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
