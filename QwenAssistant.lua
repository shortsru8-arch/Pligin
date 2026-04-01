-- QwenAssistant Plugin для Roblox Studio
-- Устанавливается в: %localappdata%/Roblox/Plugins/

local HttpService = game:GetService("HttpService")
local ServerScriptService = game:GetService("ServerScriptService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local ChangeHistoryService = game:GetService("ChangeHistoryService")

-- ============ НАСТРОЙКИ ============
local SERVER_URL = "https://web-production-48ea1.up.railway.app"
local PLUGIN_SECRET = "qwen-roblox-secret-2024"
local POLL_INTERVAL = 3                                 -- Секунд между проверками
-- ===================================

-- Генерируем уникальный session_id
local SESSION_ID = HttpService:GenerateGUID(false):sub(1, 8)

-- Создаём тулбар и кнопку
local toolbar = plugin:CreateToolbar("Qwen Assistant")
local toggleButton = toolbar:CreateButton(
	"QwenAssistant",
	"Подключить Qwen AI к Studio (Session: " .. SESSION_ID .. ")",
	"rbxassetid://4458901886"  -- иконка (можно заменить)
)

-- UI для отображения статуса
local widgetInfo = DockWidgetPluginGuiInfo.new(
	Enum.InitialDockState.Float,
	false,  -- изначально скрыт
	false,
	300,
	200,
	200,
	150
)
local widget = plugin:CreateDockWidgetPluginGui("QwenAssistantWidget", widgetInfo)
widget.Title = "Qwen Assistant"

-- UI элементы
local frame = Instance.new("Frame")
frame.Size = UDim2.new(1, 0, 1, 0)
frame.BackgroundColor3 = Color3.fromRGB(30, 30, 30)
frame.Parent = widget

local statusLabel = Instance.new("TextLabel")
statusLabel.Size = UDim2.new(1, -20, 0, 30)
statusLabel.Position = UDim2.new(0, 10, 0, 10)
statusLabel.BackgroundTransparency = 1
statusLabel.TextColor3 = Color3.fromRGB(200, 200, 200)
statusLabel.TextXAlignment = Enum.TextXAlignment.Left
statusLabel.Font = Enum.Font.SourceSansBold
statusLabel.TextSize = 16
statusLabel.Text = "Session ID: " .. SESSION_ID
statusLabel.Parent = frame

local connectionLabel = Instance.new("TextLabel")
connectionLabel.Size = UDim2.new(1, -20, 0, 25)
connectionLabel.Position = UDim2.new(0, 10, 0, 45)
connectionLabel.BackgroundTransparency = 1
connectionLabel.TextColor3 = Color3.fromRGB(150, 150, 150)
connectionLabel.TextXAlignment = Enum.TextXAlignment.Left
connectionLabel.Font = Enum.Font.SourceSans
connectionLabel.TextSize = 14
connectionLabel.Text = "Статус: отключён"
connectionLabel.Parent = frame

local logLabel = Instance.new("TextLabel")
logLabel.Size = UDim2.new(1, -20, 0, 80)
logLabel.Position = UDim2.new(0, 10, 0, 75)
logLabel.BackgroundTransparency = 1
logLabel.TextColor3 = Color3.fromRGB(100, 200, 100)
logLabel.TextXAlignment = Enum.TextXAlignment.Left
logLabel.TextYAlignment = Enum.TextYAlignment.Top
logLabel.Font = Enum.Font.Code
logLabel.TextSize = 12
logLabel.TextWrapped = true
logLabel.Text = ""
logLabel.Parent = frame

-- Состояние
local isRunning = false
local executedTaskIds = {}  -- локальный кэш выполненных задач

-- Маппинг parent строки на реальный объект
local function getParent(parentName)
	if parentName == "Workspace" or parentName == "workspace" then
		return workspace
	elseif parentName == "ServerScriptService" then
		return ServerScriptService
	elseif parentName == "ReplicatedStorage" then
		return ReplicatedStorage
	else
		return workspace
	end
end

-- Выполнить задачу от Qwen
local function executeTask(task)
	local success, err = pcall(function()
		if task.type == "script" then
			-- Создаём Script с кодом внутри
			local parent = getParent(task.parent or "Workspace")
			local script = Instance.new("Script")
			script.Name = task.name or "QwenScript"
			script.Source = task.code or ""
			script.Parent = parent
			logLabel.Text = "Создан скрипт: " .. script.Name

		elseif task.type == "build" then
			-- Выполняем Lua код для создания объектов
			local buildFunc, loadErr = loadstring(task.code)
			if buildFunc then
				buildFunc()
				logLabel.Text = "Построено: " .. (task.name or "объект")
			else
				warn("[QwenAssistant] Ошибка компиляции: " .. tostring(loadErr))
				logLabel.Text = "Ошибка: " .. tostring(loadErr)
			end
		end

		-- Записываем в историю для Ctrl+Z
		ChangeHistoryService:SetWaypoint("Qwen: " .. (task.name or "задача"))
	end)

	if not success then
		warn("[QwenAssistant] Ошибка выполнения задачи: " .. tostring(err))
		logLabel.Text = "Ошибка: " .. tostring(err)
	end

	return success
end

-- Отметить задачу как выполненную на сервере
local function markTaskDone(taskId)
	pcall(function()
		HttpService:RequestAsync({
			Url = SERVER_URL .. "/api/tasks/" .. SESSION_ID .. "/" .. tostring(taskId) .. "/done",
			Method = "POST",
			Headers = {
				["Authorization"] = "Bearer " .. PLUGIN_SECRET,
				["Content-Type"] = "application/json"
			},
			Body = "{}"
		})
	end)
end

-- Поллинг задач с сервера
local function pollTasks()
	local success, result = pcall(function()
		local response = HttpService:RequestAsync({
			Url = SERVER_URL .. "/api/tasks/" .. SESSION_ID,
			Method = "GET",
			Headers = {
				["Authorization"] = "Bearer " .. PLUGIN_SECRET
			}
		})

		if response.StatusCode == 200 then
			local data = HttpService:JSONDecode(response.Body)
			connectionLabel.Text = "Статус: подключён ✓"
			connectionLabel.TextColor3 = Color3.fromRGB(100, 200, 100)

			if data.tasks and #data.tasks > 0 then
				for _, task in ipairs(data.tasks) do
					logLabel.Text = "Выполняю: " .. (task.name or "задача")
					local ok = executeTask(task)
					if ok then
						markTaskDone(task.id)
					end
					wait(0.5)  -- небольшая пауза между задачами
				end
			end
		end
	end)

	if not success then
		connectionLabel.Text = "Статус: ошибка подключения"
		connectionLabel.TextColor3 = Color3.fromRGB(200, 100, 100)
	end
end

-- Главный цикл
local pollThread = nil

local function startPolling()
	isRunning = true
	connectionLabel.Text = "Статус: подключаюсь..."
	connectionLabel.TextColor3 = Color3.fromRGB(200, 200, 100)
	logLabel.Text = "В Telegram боте напиши:\n/connect " .. SESSION_ID

	pollThread = coroutine.create(function()
		while isRunning do
			pollTasks()
			wait(POLL_INTERVAL)
		end
	end)
	coroutine.resume(pollThread)
end

local function stopPolling()
	isRunning = false
	connectionLabel.Text = "Статус: отключён"
	connectionLabel.TextColor3 = Color3.fromRGB(150, 150, 150)
	logLabel.Text = ""
end

-- Кнопка включения/выключения
toggleButton.Click:Connect(function()
	widget.Enabled = not widget.Enabled

	if widget.Enabled then
		if not isRunning then
			startPolling()
		end
	else
		stopPolling()
	end
end)

print("[QwenAssistant] Плагин загружен. Session ID: " .. SESSION_ID)
