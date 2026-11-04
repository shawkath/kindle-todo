--[[--
TRMNL Display Plugin for KOReader

Fetches and displays personalized screens from the TRMNL API.
Supports automatic periodic refresh, full-screen display, and WiFi management.

Modified for the Kindle TickTick dashboard: if the server's JSON has a
`pages` list, all pages are downloaded and you can flip between them:
  tap left third / swipe right  -> previous page
  tap right third / swipe left  -> next page
  tap middle                    -> fetch now
  long-press anywhere           -> close the dashboard
After 2 minutes on another page it goes back to page 1.
]]

local DataStorage = require("datastorage")
local Device = require("device")
local Geom = require("ui/geometry")
local GestureRange = require("ui/gesturerange")
local InfoMessage = require("ui/widget/infomessage")
local ImageWidget = require("ui/widget/imagewidget")
local InputContainer = require("ui/widget/container/inputcontainer")
local LuaSettings = require("luasettings")
local MultiInputDialog = require("ui/widget/multiinputdialog")
local NetworkMgr = require("ui/network/manager")
local RenderImage = require("ui/renderimage")
local Screen = Device.screen
local Input = Device.input
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local Dispatcher = require("dispatcher")
local Blitbuffer = require("ffi/blitbuffer")
local Font = require("ui/font")
local FrameContainer = require("ui/widget/container/framecontainer")
local OverlapGroup = require("ui/widget/overlapgroup")
local TextWidget = require("ui/widget/textwidget")
local VerticalGroup = require("ui/widget/verticalgroup")
local PluginShare = require("pluginshare")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local util = require("util")
local _ = require("gettext")
local T = require("ffi/util").template

--[[--
Retry Manager - handles exponential backoff for failed requests.
Implements: 60s, 120s, 240s, 480s, etc., capped at max_delay.
--]]
local RetryManager = {}
RetryManager.__index = RetryManager
RetryManager.BASE_DELAY = 60
RetryManager.MIN_DELAY = 60

function RetryManager:new(max_delay)
    local instance = {
        count = 0,
        max_delay = max_delay or 1800
    }
    setmetatable(instance, self)
    return instance
end

function RetryManager:getDelay()
    local delay = self.BASE_DELAY * (2 ^ self.count)
    return math.min(delay, math.max(self.max_delay, self.MIN_DELAY))
end

function RetryManager:increment()
    self.count = self.count + 1
    return self:getDelay()
end

function RetryManager:reset()
    self.count = 0
end

--[[--
Main plugin class extending WidgetContainer.
Provides lifecycle hooks (init, onSuspend, onResume) and menu integration.
]]
local TrmnlDisplay = WidgetContainer:extend {
    name = "trmnl",
    is_doc_only = false,

    settings = nil,
    settings_file = nil,

    refresh_task = nil,
    auto_refresh_enabled = false,
    image_widget = nil,
    last_image_filename = nil,

    page_paths = nil,        -- local file per page, in order
    page_index = 1,          -- page currently shown
    pages_changed = true,    -- did the last fetch bring anything new?
    return_home_task = nil,

    retry_manager = nil,
}

TrmnlDisplay.USER_AGENT = "trmnl-display/0.2.0-koreader"
TrmnlDisplay.VERSION_LABEL = "TickTick dashboard v5.9 (tap to update)"

TrmnlDisplay.CONSTANTS = {
    TIMING = {
        DEFAULT_REFRESH_INTERVAL = 1800,
        RETURN_HOME_SECONDS = 120,   -- go back to page 1 after this long on another page
        REOPEN_SECONDS = 600,        -- reopen the dashboard this long after it's closed
    },
    FILES = {
        DEFAULT_IMAGE = "trmnl_screen.png",
        SCREENS_FOLDER = "trmnl-screens",
        API_KEY = "apikey.txt",
    }
}

TrmnlDisplay.default_settings = {
    api_key = nil,
    base_url = "https://trmnl.app",
    refresh_interval = 1800,
    refresh_type = "ui",
    show_notifications = true,
    mac_header_name = nil,  -- Header name for MAC address (configurable for BYOS)
    mac_address = nil,  -- Manual MAC address override (nil = auto-detect)
}

--[[--
Plugin initialization - loads settings, initializes managers, and restores state.
]]
function TrmnlDisplay:init()
    self.settings_file = LuaSettings:open(DataStorage:getSettingsDir() .. "/trmnl.lua")
    self.settings = self.settings_file:readSetting("settings") or util.tableDeepCopy(self.default_settings)
    self.auto_refresh_enabled = self.settings_file:readSetting("auto_refresh_enabled") or false

    self.retry_manager = RetryManager:new(self.settings.refresh_interval or
        self.CONSTANTS.TIMING.DEFAULT_REFRESH_INTERVAL)

    -- Try auto-loading API key from file
    local file_api_key = self:loadApiKeyFromFile()
    if file_api_key then
        self.settings.api_key = file_api_key
        self:saveSettings()
        logger.info("TRMNL: API key auto-configured from file")
    end

    -- Must be instance-specific for UIManager to track properly
    self.refresh_task = function()
        self:updateNow(false)
    end

    Dispatcher:registerAction("trmnl_fetch_now", {category="none", event="TrmnlFetch", title=_("TRMNL: Fetch now"), general=true})
    Dispatcher:registerAction("trmnl_start_interactive", {category="none", event="TrmnlStartInteractive", title=_("TRMNL: Start dashboard"), general=true})

    self:pinToTopOfTools()
    self:removeScreensLeftByOlderVersions()
    self.ui.menu:registerToMainMenu(self)

    -- This Kindle is a dashboard with no working power button: sleeping would lock it
    -- until USB is connected. Stay awake whenever KOReader runs, and always open the
    -- dashboard on start (a few seconds later, so KOReader finishes loading first).
    self:setKeepAwake(true, true)
    UIManager:scheduleIn(5, function()
        if not self.image_widget then
            self:startAutoRefresh()
        end
    end)
end

--[[--
Save settings to disk (called automatically on suspend/exit).
]]
function TrmnlDisplay:pinToTopOfTools()
    for _, order_module in ipairs({ "ui/elements/filemanager_menu_order", "ui/elements/reader_menu_order" }) do
        local tools = require(order_module).tools
        if tools[1] ~= "trmnl" then
            table.insert(tools, 1, "trmnl")
        end
    end
end

function TrmnlDisplay:removeScreensLeftByOlderVersions()
    local data_dir = DataStorage:getDataDir()
    for name in lfs.dir(data_dir) do
        if name:match("^plugin%-%x+%-%d+%.png$") or name == self.CONSTANTS.FILES.DEFAULT_IMAGE then
            os.remove(data_dir .. "/" .. name)
        end
    end
end

function TrmnlDisplay:onFlushSettings()
    if self.settings_file then
        self.settings_file:saveSetting("settings", self.settings)
        self.settings_file:saveSetting("auto_refresh_enabled", self.auto_refresh_enabled)
        self.settings_file:flush()
    end
end

function TrmnlDisplay:saveSettings()
    self:onFlushSettings()
end

function TrmnlDisplay:getPluginDir()
    local info = debug.getinfo(1, "S")
    local filepath = info.source:match("^@(.+)$")
    if filepath then
        return filepath:match("(.*/)")
    end
    return nil
end

--[[--
Load API key from apikey.txt in plugin directory (if present).
]]
function TrmnlDisplay:loadApiKeyFromFile()
    local plugin_dir = self:getPluginDir()
    if not plugin_dir then
        return nil
    end

    local apikey_path = plugin_dir .. self.CONSTANTS.FILES.API_KEY
    local file = io.open(apikey_path, "r")
    if not file then
        return nil
    end

    local content = file:read("*all")
    file:close()

    if not content or content == "" then
        return nil
    end

    local api_key = content:match("^%s*(.-)%s*$")
    if api_key and api_key ~= "" then
        logger.info("TRMNL: API key loaded from file")
        return api_key
    end

    return nil
end

--[[--
Get MAC address of the wireless network interface.

Uses FFI (Foreign Function Interface) to call POSIX system calls:
- getifaddrs(): Enumerate all network interfaces
- ioctl(SIOCGIWNAME): Check if interface is wireless
- ioctl(SIOCGIFHWADDR): Get hardware (MAC) address

@treturn string|nil MAC address (format: "XX:XX:XX:XX:XX:XX") or nil if not available
]]
function TrmnlDisplay:getMacAddress()
    local interfaces = {
        "wlan0",
        "wlan1",
        "mlan0",
    }
 
    for _, iface in ipairs(interfaces) do
        local path = "/sys/class/net/" .. iface .. "/address"
        local file = io.open(path, "r")
 
        if file then
            local mac = file:read("*l")
            file:close()
 
            if mac and mac ~= "" then
                mac = mac:upper()
 
                -- Ignore invalid/all-zero addresses
                if mac ~= "00:00:00:00:00:00" then
                    logger.info(
                        "TRMNL: Auto-detected MAC address:",
                        mac,
                        "interface:",
                        iface
                    )
                    return mac
                end
            end
        end
    end
 
    logger.info("TRMNL: No wireless interface MAC address found")
    return nil
end

--[[--
Read the Wi-Fi signal level from the kernel's wireless statistics.

@treturn number|nil Signal level in dBm, or nil when unknown
]]
function TrmnlDisplay:getRssi(path)
    local file = io.open(path or "/proc/net/wireless", "r")
    if not file then
        return nil
    end
    local stats = file:read("*a")
    file:close()

    -- Columns after the interface name: status, link quality, level, noise.
    local level = tonumber(stats:match("%w+:%s+%x+%s+[%d.]+%s+(%-?%d+)"))
    -- NOTE: older drivers (Kindle PW1) report dBm as an unsigned byte, which iwconfig maps the same way.
    if level and level >= 64 then
        level = level - 256
    end
    if level and level < 0 then
        return level
    end
    return nil
end

--============================================================================--
-- Network and API Methods
--
-- These methods handle:
-- - HTTP/HTTPS requests to TRMNL API
-- - JSON parsing
-- - Image downloads
-- - Error handling and retry logic
--============================================================================--

--[[--
Fetch screen metadata from TRMNL API.

Makes HTTP GET request to /api/display endpoint with headers:
- access-token: User's API key
- percent-charged: Device battery percentage
- png-width/png-height: Screen dimensions in pixels
- rssi: WiFi signal strength in dBm, when known
- battery-charging: whether the device is charging
- User-Agent: Plugin version string

@treturn table|nil Decoded JSON response table, or nil on error
]]
function TrmnlDisplay:fetchScreenMetadata()
    if not self.settings.api_key or self.settings.api_key == "" then
        return nil, "Please configure your TRMNL API key first"
    end

    -- LuaSocket libraries for HTTP/HTTPS requests
    local http = require("socket.http") -- HTTP protocol
    local https = require("ssl.https")  -- HTTPS/TLS protocol
    local JSON = require("json")        -- JSON parsing
    local socketutil = require("socketutil")

    -- The sink accumulates the response body into this table
    local sink = {}
    local request_url = self.settings.base_url .. "/api/display?t=" .. tostring(os.time())

    -- Get device information for API headers
    -- Device:hasBattery() checks if device has battery capability
    -- Device:getPowerDevice() returns power device object with getCapacity() method
    local percent_charged = "0"
    local battery_charging
    if Device:hasBattery() then
        local powerd = Device:getPowerDevice()
        percent_charged = tostring(powerd:getCapacity()) -- Returns 0-100 percentage
        battery_charging = tostring(powerd:isCharging())
    end
    local rssi = self:getRssi()

    -- Screen:getWidth()/getHeight() return pixel dimensions
    -- TRMNL uses this to generate appropriately sized images
    local png_width = tostring(Screen:getWidth())
    local png_height = tostring(Screen:getHeight())

    -- Get MAC address: manual entry wins (if not empty), otherwise auto-detect
    local mac_address
    local manual_mac = self.settings.mac_address
    if manual_mac and manual_mac ~= "" then
        -- Manual MAC provided - use it
        mac_address = manual_mac
        logger.dbg("TRMNL: Using manual MAC address:", mac_address)
    else
        -- No manual MAC or empty string - try auto-detection.
        -- Staying nil drops the header from the table constructor below, which is
        -- what we want: claiming a made-up MAC is worse than claiming none.
        mac_address = self:getMacAddress()
        logger.dbg("TRMNL: Using MAC address:", mac_address or "(none detected)")
    end

    -- Get custom header name for MAC address
    local mac_header_name = self.settings.mac_header_name or "ID"

    logger.info("TRMNL: Fetching screen from", request_url)
    logger.dbg("TRMNL: Screen dimensions:", png_width, "x", png_height)

    -- Build HTTP request table (LuaSocket format)
    local request = {
        url = request_url,
        method = "GET",
        headers = {
            ["access-token"] = self.settings.api_key,  -- TRMNL API authentication
            ["percent-charged"] = percent_charged,     -- Device battery level
            ["png-width"] = png_width,                 -- Screen width in pixels
            ["png-height"] = png_height,               -- Screen height in pixels
            ["battery-charging"] = battery_charging,
            ["rssi"] = rssi and tostring(rssi),        -- Omitted when unknown; the server reads 0 as full strength
            [mac_header_name] = mac_address,           -- MAC address with custom header name
            ["User-Agent"] = self.USER_AGENT,          -- Plugin identification
        },
        sink = socketutil.table_sink(sink),            -- Response body stored in 'sink' table
        -- SSL/TLS configuration for HTTPS
        protocol = "any",                              -- Accept any SSL/TLS version
        options = { "all", "no_sslv2", "no_sslv3" },   -- Disable insecure SSL versions
        verify = "none",                               -- Skip certificate verification (required for Kindles with outdated cert stores)
    }

    -- Choose HTTP or HTTPS based on URL scheme
    local httpx = request_url:match("^https://") and https or http
    -- NOTE: requests block KOReader's UI, so a stalled server must not hold it for LuaSocket's 60 s default.
    socketutil:set_timeout(socketutil.LARGE_BLOCK_TIMEOUT, socketutil.LARGE_TOTAL_TIMEOUT)
    local success_code, status_code = httpx.request(request)
    socketutil:reset_timeout()

    logger.dbg("TRMNL: Success code:", success_code)
    logger.dbg("TRMNL: HTTP status code:", status_code)

    if not success_code or success_code ~= 1 then
        -- NOTE: on a transport failure LuaSocket returns the reason in place of the status.
        logger.err("TRMNL: Request failed:", status_code)
        return nil, T(_("Failed to reach the server: %1"), tostring(status_code))
    end

    if status_code ~= 200 then
        logger.err("TRMNL: API returned HTTP", status_code)
        local hint = ""
        if status_code == 401 or status_code == 403 then
            hint = ": check your API key in settings"
        elseif status_code == 404 then
            hint = ": check the base URL"
        end
        return nil, "Server returned HTTP " .. tostring(status_code) .. hint
    end

    local response_body = table.concat(sink)
    logger.dbg("TRMNL: Received response:", response_body:sub(1, 200))

    local ok, response = pcall(JSON.decode, response_body)
    if not ok or not response then
        logger.err("TRMNL: Failed to parse JSON response")
        return nil, "The server's response is not JSON: check the base URL"
    end

    return response
end

function TrmnlDisplay:downloadImage(image_url, filepath)
    local http = require("socket.http")
    local https = require("ssl.https")
    local socketutil = require("socketutil")

    logger.info("TRMNL: Downloading image from", image_url)

    local file = io.open(filepath, "wb")
    if not file then
        logger.err("TRMNL: Failed to open file for writing:", filepath)
        return false
    end

    local request = {
        url = image_url,
        sink = socketutil.file_sink(file),
        headers = {
            ["User-Agent"] = self.USER_AGENT,
        },
        -- SSL/TLS configuration (same as API request)
        protocol = "any",
        options = { "all", "no_sslv2", "no_sslv3" },
        verify = "none", -- Skip certificate verification (required for Kindles)
    }

    local httpx = image_url:match("^https://") and https or http
    socketutil:set_timeout(socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_TOTAL_TIMEOUT)
    local success_code, status_code = httpx.request(request)
    socketutil:reset_timeout()

    logger.dbg("TRMNL: Image download success code:", success_code)
    logger.dbg("TRMNL: Image download HTTP status:", status_code)

    if not success_code or success_code ~= 1 then
        logger.err("TRMNL: Image download failed:", status_code)
        os.remove(filepath)
        return false, tostring(status_code)
    end

    if status_code ~= 200 then
        logger.err("TRMNL: Image download failed - HTTP", status_code)
        os.remove(filepath)
        return false, "HTTP " .. tostring(status_code)
    end

    logger.info("TRMNL: Image downloaded successfully")
    return true
end

--[[--
Small "85%" / "charging 85%" label for the top-right corner.
Returns nil if the battery level can't be read, so the screen still works.
]]
function TrmnlDisplay:buildBatteryWidget(screen_width)
    local ok, widget = pcall(function()
        local powerd = Device:getPowerDevice()
        local level = powerd:getCapacity()
        if not level then
            return nil
        end
        local charging = powerd.isCharging and powerd:isCharging()
        local text = (charging and "charging " or "") .. tostring(level) .. "%"
        -- Wi-Fi state on a second line, right-aligned under the battery
        local online = self:isWifiConnected()
        local frame = FrameContainer:new {
            background = Blitbuffer.COLOR_WHITE,
            bordersize = 0,
            padding = Screen:scaleBySize(3),
            VerticalGroup:new {
                align = "right",
                TextWidget:new {
                    text = text,
                    face = Font:getFace("cfont", 16),
                    bold = level <= 20,
                },
                TextWidget:new {
                    text = online and "Wi-Fi on" or "Wi-Fi off",
                    face = Font:getFace("cfont", 16),
                    bold = not online,
                },
            },
        }
        local size = frame:getSize()
        frame.overlap_offset = { screen_width - size.w - Screen:scaleBySize(10), Screen:scaleBySize(6) }
        return frame
    end)
    if ok then
        return widget
    end
    logger.warn("TRMNL: Could not draw battery level:", widget)
    return nil
end

function TrmnlDisplay:isWifiConnected()
    local ok, on = pcall(function() return NetworkMgr:isConnected() end)
    return ok and on and true or false
end

-- Redraw the page on screen when Wi-Fi comes or goes, so the indicator stays true.
function TrmnlDisplay:refreshWifiIndicator()
    if self.wifi_redraw_task then
        UIManager:unschedule(self.wifi_redraw_task)
    end
    self.wifi_redraw_task = function()
        self.wifi_redraw_task = nil
        local now_on = self:isWifiConnected()
        if self.image_widget and self.page_paths and now_on ~= self.wifi_shown then
            logger.info("TRMNL: Wi-Fi is now", now_on and "on" or "off", "- updating indicator")
            self:displayImage(self.page_paths[self.page_index or 1])
        end
    end
    UIManager:scheduleIn(3, self.wifi_redraw_task)   -- let the state settle first
end

function TrmnlDisplay:onNetworkConnected() self:refreshWifiIndicator() end
function TrmnlDisplay:onNetworkDisconnected() self:refreshWifiIndicator() end

function TrmnlDisplay:displayImage(image_path)
    -- NOTE: Keep old image visible while rendering new one
    -- Only close it if we successfully render the replacement
    -- This prevents empty screens during network/rendering errors

    local screen_width = Screen:getWidth()
    local screen_height = Screen:getHeight()

    logger.info("TRMNL: Rendering image", image_path)

    local image_bb = RenderImage:renderImageFile(
        image_path,
        true, -- enable caching
        screen_width,
        screen_height
    )

    if not image_bb then
        logger.err("TRMNL: Failed to render image")
        return false
    end

    -- Close previous image widget only after successfully rendering new one
    if self.image_widget then
        UIManager:close(self.image_widget)
        self.image_widget = nil
    end

    local image = ImageWidget:new {
        image = image_bb,
        image_disposable = true,
        width = screen_width,
        height = screen_height,
        alpha = true,
    }

    -- Battery level drawn on top of the image (top-right corner is kept empty by the server)
    local content = image
    local battery = self:buildBatteryWidget(screen_width)
    self.wifi_shown = self:isWifiConnected()
    if battery then
        content = OverlapGroup:new {
            dimen = Geom:new { w = screen_width, h = screen_height },
            image,
            battery,
        }
    end

    -- Wrap in InputContainer to handle tap events
    self.image_widget = InputContainer:new {
        dimen = {
            x = 0,
            y = 0,
            w = screen_width,
            h = screen_height,
        },
        content,
    }

    self.image_widget.onGestureClose = function()
        logger.info("TRMNL: Closing image via gesture")
        self:stopAutoRefresh()
        self:cancelReturnHome()
        UIManager:close(self.image_widget)
        self.image_widget = nil
        -- come back by itself, so the dashboard never stays closed by accident
        if self.reopen_task then
            UIManager:unschedule(self.reopen_task)
        end
        self.reopen_task = function()
            self.reopen_task = nil
            if not self.image_widget then
                logger.info("TRMNL: Reopening dashboard after being closed")
                self:startAutoRefresh()
            end
        end
        UIManager:scheduleIn(self.CONSTANTS.TIMING.REOPEN_SECONDS, self.reopen_task)
        self:notify("Dashboard closed. It reopens by itself in 10 min.", { type = "info", force = true })
        return true
    end

    self.image_widget.onAnyKeyPressed = function(widget, _, key)
        if key and key:match({ Input.group.PgFwd }) then
            return widget.onGestureNext()
        elseif key and key:match({ Input.group.PgBack }) then
            return widget.onGesturePrev()
        end
        return widget.onGestureClose()
    end

    self.image_widget.onGestureFetch = function()
        self:updateNow(true)
        return true
    end

    self.image_widget.onGestureNext = function()
        self:turnPage(1)
        return true
    end

    self.image_widget.onGesturePrev = function()
        self:turnPage(-1)
        return true
    end

    self.image_widget.onGestureSwipe = function(_, _, ges)
        if ges and ges.direction == "west" then
            self:turnPage(1)
        elseif ges and ges.direction == "east" then
            self:turnPage(-1)
        end
        return true
    end

    if Device:isTouchDevice() then
        local third = math.floor(screen_width / 3)
        local function zone(ges, x, w)
            return { GestureRange:new { ges = ges, range = Geom:new { x = x, y = 0, w = w, h = screen_height } } }
        end
        -- Sides flip pages, middle refreshes, long-press closes (hard to do by accident).
        self.image_widget.ges_events = {
            GesturePrev = zone("tap", 0, third),
            GestureFetch = zone("tap", third, screen_width - 2 * third),
            GestureNext = zone("tap", screen_width - third, third),
            GestureSwipe = zone("swipe", 0, screen_width),
            GestureClose = zone("hold", 0, screen_width),
        }
    end

    -- Register key events for non-touch devices
    if Device:hasKeys() then
        self.image_widget.key_events = {
            AnyKeyPressed = { { Input.group.Any } }
        }
    end

    -- Add widget to UIManager's display stack
    UIManager:show(self.image_widget)

    --[[--
    E-ink refresh optimization

    E-ink displays have several refresh modes with tradeoffs:
    - "ui": Balanced speed/quality (default)
    - "full": Slowest but best quality, clears ghosting
    - "flashui": With screen flash for better contrast
    - "partial": Fastest but may cause ghosting
    ]]
    local refresh_type = self.settings.refresh_type or "ui"
    logger.info("TRMNL: Applying refresh type:", refresh_type)

    -- UIManager:setDirty() marks widget for screen update with specified refresh mode
    -- This triggers the e-ink controller to redraw the widget's area
    UIManager:setDirty(self.image_widget, refresh_type)

    logger.info("TRMNL: Image displayed")
    return true
end

--============================================================================--
-- Helper Methods
--============================================================================--

--[[--
Smart notification system - shows informative messages without overwhelming.

Types: "error", "info", "success", "progress"
Can be toggled on/off via settings.show_notifications
@tparam string message Main message text
@tparam table opts Optional: {type, context, timeout, force}
]]
function TrmnlDisplay:notify(message, opts)
    opts = opts or {}
    local msg_type = opts.type or "info"
    local context = opts.context

    -- Always log, even if notifications disabled
    logger.info("TRMNL [" .. msg_type .. "]: " .. message)
    if context then
        logger.dbg("TRMNL Context: " .. context)
    end

    -- Skip showing notification if disabled (unless force = true)
    -- Always show errors though, they're important
    if not self.settings.show_notifications and msg_type ~= "error" and not opts.force then
        return
    end

    local timeout
    if opts.timeout then
        timeout = opts.timeout
    elseif msg_type == "error" then
        timeout = 5
    elseif msg_type == "progress" then
        timeout = 3
    else
        timeout = 2
    end

    local full_message = _(message)
    if context and context ~= "" then
        full_message = full_message .. "\n\n" .. context
    end

    local prefix = ""
    if msg_type == "error" then
        prefix = "⚠ "
    elseif msg_type == "success" then
        prefix = "✓ "
    elseif msg_type == "progress" then
        prefix = "⟳ "
    end

    UIManager:show(InfoMessage:new {
        text = prefix .. full_message,
        timeout = timeout,
    })
end

function TrmnlDisplay:showError(message, context)
    self:notify(message, { type = "error", context = context })
end

function TrmnlDisplay:showInfo(message)
    self:notify(message, { type = "info" })
end

function TrmnlDisplay:showSuccess(message)
    self:notify(message, { type = "success" })
end

function TrmnlDisplay:showProgress(message, context)
    self:notify(message, { type = "progress", context = context })
end

--[[--
Unschedule any pending refresh task.

Safely removes any scheduled refresh task.
Safe to call even if no task is scheduled.
]]
function TrmnlDisplay:unscheduleRefreshTask()
    if self.refresh_task then
        UIManager:unschedule(self.refresh_task)
        logger.dbg("TRMNL: Unscheduled refresh task")
    end
end

--[[--
Schedule the next refresh if auto-refresh is enabled.

Uses the last refresh rate the server sent to schedule the next fetch cycle.
Unschedules any existing pending task to prevent double-scheduling.
]]
function TrmnlDisplay:scheduleNextRefresh()
    if not self.auto_refresh_enabled then
        logger.info("TRMNL: Auto-refresh disabled, not scheduling")
        return
    end

    self:unscheduleRefreshTask()

    local interval = self.settings.refresh_interval or self.CONSTANTS.TIMING.DEFAULT_REFRESH_INTERVAL

    -- Night mode: the server sends `sleep_until` (a Unix time) during quiet hours.
    -- Turn Wi-Fi off and don't fetch again until then. Uses epoch time, so the
    -- Kindle's own clock/time zone setting doesn't matter.
    local now = os.time()
    if self.sleep_until and now < self.sleep_until then
        interval = self.sleep_until - now + 60
        -- Wi-Fi is NOT switched off any more: on this Kindle, turning it back on from
        -- KOReader often fails ("Error connecting to the network"). It's plugged in anyway.
        logger.info("TRMNL: Night mode, no fetching until morning; next fetch in", interval, "seconds")
    end
    logger.info("TRMNL: Scheduling next refresh in", interval, "seconds")
    UIManager:scheduleIn(interval, self.refresh_task)
end

--[[--
Report a failed fetch. The retry itself was already booked by fetchAndDisplay.

@tparam string error_message User-friendly error description
]]
function TrmnlDisplay:handleFetchError(error_message)
    logger.err("TRMNL:", error_message, "(attempt", self.retry_manager.count, ")")
    if self.auto_refresh_enabled then
        self:showError(T(_("%1. Will retry in %2 seconds."), _(error_message), self.retry_manager:getDelay()))
    else
        self:showError(_(error_message))
    end
end

--[[--
Follow the refresh rate the server sends with each screen.

@tparam table response API response containing optional refresh_rate field
]]
function TrmnlDisplay:updateRefreshInterval(response)
    if not response.refresh_rate then
        return
    end

    local new_interval = tonumber(response.refresh_rate)
    if new_interval and new_interval > 0 then
        logger.info("TRMNL: Using server refresh rate:", new_interval, "seconds")
        self.settings.refresh_interval = new_interval
        self:saveSettings()
    end
end

--[[--
Download screen image if it has changed since last fetch.

Compares filename to detect changes. If unchanged, uses cached file.
If changed, downloads new image and updates cache tracking.
Automatically deletes old image file to prevent accumulation.

@tparam table response API response containing image_url and filename
@treturn string|nil Path to image file, or nil on download failure
]]
function TrmnlDisplay:downloadImageIfNeeded(response)
    -- One entry per page. Servers without `pages` (normal TRMNL) give a single page.
    local pages = response.pages
    if type(pages) ~= "table" or #pages == 0 then
        pages = { { image_url = response.image_url, filename = response.filename } }
    end

    local screens_folder = DataStorage:getDataDir() .. "/" .. self.CONSTANTS.FILES.SCREENS_FOLDER
    lfs.mkdir(screens_folder)

    local paths, keep, changed = {}, {}, false
    for i, page in ipairs(pages) do
        local filename = page.filename or self.CONSTANTS.FILES.DEFAULT_IMAGE
        if #pages > 1 and not page.filename then
            filename = "page" .. i .. ".png"
        end
        if not filename:match("%.png$") then
            filename = filename .. ".png"
        end
        local image_path = screens_folder .. "/" .. filename

        -- File names change whenever the picture changes, so an existing file is current.
        if lfs.attributes(image_path, "mode") ~= "file" then
            logger.info("TRMNL: Page", i, "changed, downloading:", filename)
            self:showProgress("Downloading new screen...", filename)
            local downloaded, reason = self:downloadImage(page.image_url, image_path)
            if not downloaded then
                return nil, reason
            end
            changed = true
        end
        paths[i] = image_path
        keep[filename] = true
    end

    for name in lfs.dir(screens_folder) do
        if name ~= "." and name ~= ".." and not keep[name] then
            os.remove(screens_folder .. "/" .. name)
        end
    end

    self.page_paths = paths
    self.pages_changed = changed
    if not self.page_index or self.page_index > #paths then
        self.page_index = 1
    end
    self.last_image_filename = paths[self.page_index]
    return paths[self.page_index]
end

--[[--
Show the next (step = 1) or previous (step = -1) page, wrapping around.
]]
function TrmnlDisplay:turnPage(step)
    local count = self.page_paths and #self.page_paths or 0
    if count <= 1 then
        return
    end
    self.page_index = ((self.page_index or 1) - 1 + step) % count + 1
    logger.info("TRMNL: Showing page", self.page_index, "of", count)
    self:displayImage(self.page_paths[self.page_index])
    self:scheduleReturnHome()
end

function TrmnlDisplay:cancelReturnHome()
    if self.return_home_task then
        UIManager:unschedule(self.return_home_task)
        self.return_home_task = nil
    end
end

--[[--
After a while on page 2 or 3, go back to page 1 so the dashboard
always rests on "Today".
]]
function TrmnlDisplay:scheduleReturnHome()
    self:cancelReturnHome()
    if (self.page_index or 1) == 1 then
        return
    end
    self.return_home_task = function()
        self.return_home_task = nil
        if self.image_widget and self.page_paths and self.page_index ~= 1 then
            self.page_index = 1
            self:displayImage(self.page_paths[1])
        end
    end
    UIManager:scheduleIn(self.CONSTANTS.TIMING.RETURN_HOME_SECONDS, self.return_home_task)
end

--[[--
Finalize successful fetch - cleanup, reset state, and schedule next refresh.
]]
function TrmnlDisplay:finalizeFetchSuccess(image_path)
    -- A dashboard on screen should never go stale or fall asleep, however it was opened.
    if not self.auto_refresh_enabled then
        logger.info("TRMNL: Dashboard shown, turning auto-refresh on")
        self.auto_refresh_enabled = true
        self:saveSettings()
    end
    self:setKeepAwake(true, true)
    -- Redraw only when a page changed (or nothing is on screen yet):
    -- avoids an e-ink refresh and a toast every minute.
    if self.pages_changed or not self.image_widget or self.force_redraw then
        self:displayImage(image_path)
        self:showSuccess(self.pages_changed and "Screen updated successfully" or "Already up to date")
    end
    self.force_redraw = false
    logger.info("TRMNL: Fetch completed successfully")

    -- Clean up WiFi using KOReader's framework
    -- Wi-Fi stays on (always plugged in): no reconnect wait next time

    self.retry_manager:reset()

    self:scheduleNextRefresh()
end

--[[--
Main workflow - fetches screen from API and displays it.

Called by user action, auto-refresh timer, or network availability callback.
Uses pipeline pattern for clear flow control and early exits on errors.
]]
--[[--
GitHub token for "update now" (asks GitHub to rebuild the pages from TickTick).
Read from github_token.txt in the plugin folder: the token on one line, plus
optional lines repo=owner/name, workflow=dashboard.yml, ref=main.
]]
function TrmnlDisplay:loadGithubConfig()
    local plugin_dir = self:getPluginDir()
    if not plugin_dir then
        return nil
    end
    -- Accept the usual Windows mistakes: a hidden double extension, or no extension.
    local f, used
    for _, name in ipairs({ "github_token.txt", "github_token.txt.txt", "github_token", "github_token.TXT" }) do
        f = io.open(plugin_dir .. name, "r")
        if f then
            used = name
            break
        end
    end
    local cfg = {}
    if not f then
        -- last resort: a GitHub token typed into "API Key" in Configure TRMNL / apikey.txt
        local key = self.settings.api_key
        if key and (key:match("^github_pat_") or key:match("^gh[pousr]_")) then
            cfg.token = key
            used = "API Key setting"
        else
            return nil, "github_token.txt not found in " .. plugin_dir
        end
    end
    -- Read the whole file and pick out the token wherever it is. This ignores comment
    -- lines, invisible byte-order marks, Windows line endings, quotes, "Bearer " etc.
    if f then
        local content = f:read("*all") or ""
        f:close()
        -- diagnostics (never logs the token itself): size, line count, what each line starts with
        local starts = {}
        for raw in content:gmatch("[^\r\n]+") do
            starts[#starts + 1] = raw:sub(1, 4):gsub("[^%w#_ ]", "?")
        end
        logger.info("TRMNL: token file", used, "bytes", #content, "lines", #starts,
            "line starts:", table.concat(starts, " | "),
            "has 'github_pat_':", content:find("github_pat_", 1, true) and "yes" or "no")
        content = content:gsub("%z", "")                 -- UTF-16 files: drop the zero bytes
        cfg.token = content:match("(github_pat_[%w_]+)") or content:match("(gh[pousr]_[%w_]+)")
        for raw in content:gmatch("[^\r\n]+") do
            local line = raw:gsub("^\239\187\191", ""):match("^%s*(.-)%s*$")
            local k, v = line:match("^(%a+)%s*=%s*([^%s]+)$")
            if k and (k == "repo" or k == "ref" or k == "workflow") then
                cfg[k] = v
            end
        end
    end
    local k2 = self.settings.api_key
    if (not cfg.token) and k2 and (k2:match("^github_pat_") or k2:match("^gh[pousr]_")) then
        cfg.token = self.settings.api_key
        used = "API Key setting"
    end
    if not cfg.token or not (cfg.token:match("^github_pat_") or cfg.token:match("^gh[pousr]_")) then
        return nil, "no token in " .. tostring(used) .. " (it should start with github_pat_ or ghp_)"
    end
    if not cfg.repo then
        local user, repo = (self.settings.base_url or ""):match("^https?://([%w%-]+)%.github%.io/([%w%-%._]+)")
        if user then
            cfg.repo = user .. "/" .. repo
        end
    end
    if not cfg.repo then
        return nil, "can't work out the GitHub repo from the Base URL; add repo=owner/name"
    end
    cfg.workflow = cfg.workflow or "dashboard.yml"
    cfg.ref = cfg.ref or "main"
    logger.info("TRMNL: GitHub token loaded from", used, "repo", cfg.repo, "ref", cfg.ref,
        "token length", #cfg.token)
    return cfg
end

--[[--
Ask GitHub to run the dashboard workflow now. Returns true on HTTP 204.
]]
function TrmnlDisplay:triggerServerUpdate(cfg)
    local https = require("ssl.https")
    local ltn12 = require("ltn12")
    local socketutil = require("socketutil")
    local body = '{"ref":"' .. cfg.ref .. '"}'
    local sink = {}
    local request = {
        url = "https://api.github.com/repos/" .. cfg.repo .. "/actions/workflows/" .. cfg.workflow .. "/dispatches",
        method = "POST",
        headers = {
            ["Authorization"] = "Bearer " .. cfg.token,
            ["Accept"] = "application/vnd.github+json",
            ["Content-Type"] = "application/json",
            ["Content-Length"] = tostring(#body),
            ["User-Agent"] = "kindle-todo",
        },
        source = ltn12.source.string(body),
        sink = socketutil.table_sink(sink),
        protocol = "any",
        options = { "all", "no_sslv2", "no_sslv3" },
        verify = "none",
    }
    socketutil:set_timeout(socketutil.LARGE_BLOCK_TIMEOUT, socketutil.LARGE_TOTAL_TIMEOUT)
    local ok, code = https.request(request)
    socketutil:reset_timeout()
    logger.info("TRMNL: GitHub update request returned", code)
    return ok == 1 and code == 204, code
end

--[[--
Fresh data now: ask GitHub to rebuild the pages, wait until the new version is
published (usually about a minute), then show it. Without a GitHub token this
falls back to just re-downloading whatever GitHub last published.

@param from_tap true when you tapped the screen (shows progress messages)
]]
function TrmnlDisplay:updateNow(from_tap)
    local cfg, why = self:loadGithubConfig()
    if not cfg then
        logger.warn("TRMNL: Can't ask GitHub for fresh data:", why)
        if from_tap then
            self:notify("Can't update from TickTick: " .. tostring(why) .. ". Showing the last copy.",
                { type = "error", force = true, timeout = 8 })
            self.force_redraw = true
        end
        return self:fetchAndDisplay()
    end
    if self.updating then
        if from_tap then
            self:notify("Already updating, one moment...", { type = "progress", force = true })
        end
        return
    end
    self.updating = true

    -- Book the next regular refresh now, in case anything below fails silently.
    self:unscheduleRefreshTask()
    UIManager:scheduleIn(self.settings.refresh_interval or self.CONSTANTS.TIMING.DEFAULT_REFRESH_INTERVAL,
        self.refresh_task)

    if from_tap then
        self:notify("Updating from TickTick... about a minute", { type = "progress", force = true })
    end

    NetworkMgr:runWhenConnected(function()
        UIManager:scheduleIn(2, function()
            -- what's published right now, so we can tell when the new version lands
            local base = self:fetchScreenMetadata()
            local before = base and base.filename or self.last_combined_name

            local ok, code = self:triggerServerUpdate(cfg)
            if not ok then
                logger.err("TRMNL: Couldn't trigger GitHub update:", code)
                if from_tap then
                    self:notify("Couldn't reach GitHub (" .. tostring(code) .. "). Showing the latest copy.",
                        { type = "error" })
                end
                self.updating = false
                self.force_redraw = from_tap
                self:_performFetch()
                return
            end

            local tries = 0
            local function poll()
                tries = tries + 1
                local response = self:fetchScreenMetadata()
                local changed = response and response.filename and response.filename ~= before
                if changed or tries >= 12 then
                    if not changed and from_tap then
                        self:notify("GitHub is slow right now. Showing the latest copy.", { type = "info", force = true })
                    end
                    self.updating = false
                    self.force_redraw = from_tap and changed
                    self:_performFetch()
                    return
                end
                UIManager:scheduleIn(15, poll)
            end
            -- a GitHub run takes ~40-70 s; first look after 45 s, then every 15 s (max ~3.5 min)
            UIManager:scheduleIn(45, poll)
        end)
    end)
end

function TrmnlDisplay:fetchAndDisplay()
    local now = os.time()
    if self.last_fetch_start and now - self.last_fetch_start < 10 then
        logger.info("TRMNL: Fetch already running, skipping duplicate")
        return
    end
    self.last_fetch_start = now
    logger.info("TRMNL: Starting fetch and display cycle")

    -- NOTE: a failed Wi-Fi connection never runs the callback, so the retry is booked before trying.
    -- A successful fetch replaces it with the normal refresh.
    if self.auto_refresh_enabled then
        self:unscheduleRefreshTask()
        local retry_delay = self.retry_manager:increment()
        logger.info("TRMNL: Retry booked in", retry_delay, "seconds in case this attempt fails")
        UIManager:scheduleIn(retry_delay, self.refresh_task)
    end

    NetworkMgr:runWhenConnected(function()
        self:_doFetchAndDisplay()
    end)
end

--[[--
Internal fetch implementation that runs after WiFi is confirmed connected.
Separated from fetchAndDisplay to work with NetworkMgr:runWhenConnected().

Includes a small delay after WiFi connection to ensure the network stack is fully ready
for HTTP requests, as some devices report "connected" before being able to make requests.
]]
function TrmnlDisplay:_doFetchAndDisplay()
    -- Give WiFi a moment to fully stabilize after connection
    -- This prevents "connected but not really" issues on some devices
    logger.info("TRMNL: WiFi connected, waiting 2 seconds for network to stabilize...")
    UIManager:scheduleIn(2, function()
        self:_performFetch()
    end)
end

--[[--
Perform the actual fetch and display after WiFi is ready.
]]
function TrmnlDisplay:_performFetch()
    local response, reason = self:fetchScreenMetadata()
    if not response or not response.image_url then
        -- NOTE: the API reports device/token problems as HTTP 200 with
        -- {"status": 500, "error": "..."}, so the only place that detail survives is here.
        local detail = response and (response.error or
            (response.status and "status " .. tostring(response.status)))
        if detail then
            logger.err("TRMNL: API returned no image_url:", detail)
        end
        self:handleFetchError(reason or detail or "Failed to fetch screen metadata")
        -- Wi-Fi stays on (always plugged in): no reconnect wait next time
        return
    end

    self:updateRefreshInterval(response)
    self.sleep_until = tonumber(response.sleep_until)   -- nil outside quiet hours
    self.last_combined_name = response.filename

    local image_path, reason = self:downloadImageIfNeeded(response)
    if not image_path then
        self:handleFetchError(reason and "Failed to download image: " .. reason or "Failed to download image")
        -- Wi-Fi stays on (always plugged in): no reconnect wait next time
        return
    end

    self:finalizeFetchSuccess(image_path)
end

--============================================================================--
-- Auto-Refresh Control
--
-- These methods start/stop the automatic refresh cycle.
-- Called by menu actions and lifecycle hooks.
--============================================================================--

--[[--
Start auto-refresh cycle.

Called by:
- User selecting "Start dashboard" menu item
- Plugin init() if auto-refresh was previously enabled

Flow:
1. Enable auto-refresh flag
2. Save state to disk
3. Fetch and display immediately (no delay for first refresh)
4. fetchAndDisplay() will schedule subsequent refreshes
]]
--[[--
Keep the device awake while the dashboard runs (same mechanism as KOReader's
"Keep alive" plugin): on Kindle, tell powerd not to start the screensaver; on
other devices, pause KOReader's autosuspend. Turned off again when the
dashboard stops, so normal reading isn't affected.
]]
function TrmnlDisplay:setKeepAwake(on, force)
    if self.keep_awake == on and not force then
        return
    end
    self.keep_awake = on
    PluginShare.pause_auto_suspend = on
    if Device:isKindle() then
        os.execute("lipc-set-prop com.lab126.powerd preventScreenSaver " .. (on and "1" or "0"))
    end
    logger.info("TRMNL: Keep awake", on and "ON" or "OFF")
end

--[[--
Put the last downloaded screen up straight away (no Wi-Fi needed), so "Start
dashboard" always shows something at once; the fetch that follows updates it.
]]
function TrmnlDisplay:showCachedNow()
    local paths = self.page_paths
    if not paths or #paths == 0 then
        local folder = DataStorage:getDataDir() .. "/" .. self.CONSTANTS.FILES.SCREENS_FOLDER
        local found = {}
        if lfs.attributes(folder, "mode") == "directory" then
            for name in lfs.dir(folder) do
                local n = name:match("^p(%d+)%-.*%.png$")
                if n then
                    found[#found + 1] = { tonumber(n), folder .. "/" .. name }
                end
            end
        end
        table.sort(found, function(a, b) return a[1] < b[1] end)
        paths = {}
        for _, f in ipairs(found) do
            paths[#paths + 1] = f[2]
        end
        if #paths > 0 then
            self.page_paths = paths
        end
    end
    if not paths or #paths == 0 then
        logger.info("TRMNL: No saved screen yet, waiting for the first download")
        return false
    end
    self.page_index = 1
    local ok = self:displayImage(paths[1])
    logger.info("TRMNL: Showing last saved screen while fetching", ok ~= false and "ok" or "failed")
    return ok ~= false
end

function TrmnlDisplay:startAutoRefresh()
    logger.info("TRMNL: Starting auto-refresh", self.VERSION_LABEL)
    self:setKeepAwake(true)
    if self.reopen_task then                -- started by hand: drop the pending auto-reopen
        UIManager:unschedule(self.reopen_task)
        self.reopen_task = nil
    end
    self.last_fetch_start = nil             -- never skip a start as a "duplicate"
    self.force_redraw = true                -- always draw, even if nothing changed
    UIManager:nextTick(function() self:showCachedNow() end)

    -- NOTE: Ensure clean state before starting
    self:unscheduleRefreshTask()

    self.auto_refresh_enabled = true
    self:saveSettings()                 -- Persist across KOReader restarts

    self:notify("Starting " .. self.VERSION_LABEL, { type = "progress", force = true })

    -- Start immediately - will display the image and schedule next refresh
    self:fetchAndDisplay()

    logger.dbg("TRMNL: Auto-refresh started, initial fetch triggered")
end

--[[--
Stop auto-refresh cycle.

Called by:
- User selecting "Stop dashboard" menu item
- Closing the dashboard (onGestureClose)
- Device lifecycle events (suspend, plugin close)

Unschedules any pending refresh tasks using UIManager:unschedule().
]]
function TrmnlDisplay:stopAutoRefresh()
    if not self.auto_refresh_enabled then
        logger.dbg("TRMNL: Auto-refresh already stopped")
        return
    end

    logger.info("TRMNL: Stopping auto-refresh")

    -- NOTE: Unschedule first to ensure clean state
    self:unscheduleRefreshTask()

    self.auto_refresh_enabled = false
    self:saveSettings() -- Persist across KOReader restarts
    self:cancelReturnHome()

    logger.dbg("TRMNL: Auto-refresh stopped, all tasks unscheduled")
end

--============================================================================--
-- Lifecycle Handlers
--
-- These are lifecycle hooks called automatically by KOReader:
-- - onSuspend: Called when device is about to sleep
-- - onResume: Called when device wakes from sleep
-- - onCloseWidget: Called when plugin is disabled or KOReader exits
--
-- Proper lifecycle handling ensures:
-- - Tasks don't run while device is suspended (saves battery)
-- - Fresh data is fetched immediately on wake
-- - No resource leaks when plugin is disabled
--============================================================================--

--[[--
Device is suspending (going to sleep).

Unschedule refresh task to prevent it running during sleep.
]]
function TrmnlDisplay:onSuspend()
    if self.auto_refresh_enabled then
        self:unscheduleRefreshTask()
    end
end

--[[--
Device is resuming (waking from sleep).

If auto-refresh is enabled, fetch immediately rather than waiting for the
scheduled interval. This ensures fresh data after potentially long sleep periods.
Unschedules any pending task first to prevent double-refresh.

NOTE: If auto_restore_wifi is enabled, skip immediate fetch to prevent duplicate
network events and connection UI popups. The WiFi will be auto-restored and we'll
wait for the next scheduled refresh instead.
]]
function TrmnlDisplay:onResume()
    if Device:hasWifiRestore() and NetworkMgr.wifi_was_on and
        G_reader_settings:isTrue("auto_restore_wifi") then
        if self.auto_refresh_enabled then
            self:scheduleNextRefresh()
        end
        return
    end

    if not self.auto_refresh_enabled then
        return
    end

    self:setKeepAwake(true, true)
    self:unscheduleRefreshTask()
    self:fetchAndDisplay()
end

--[[--
Plugin is being disabled or KOReader is exiting.

Clean up:
- Stop auto-refresh cycle
- Close any displayed image widgets
- Settings are automatically flushed by onFlushSettings()
]]
function TrmnlDisplay:onCloseWidget()
    logger.dbg("TRMNL: Plugin closing")
    self:stopAutoRefresh()
    if self.image_widget then
        UIManager:close(self.image_widget)
        self.image_widget = nil
    end
end

--============================================================================--
-- UI Configuration
--
-- These methods create and manage user interface elements:
-- - Configuration dialogs for settings
-- - Main menu integration
-- - Submenus for advanced options
--
-- KOReader UI Components Used:
-- - MultiInputDialog: Multiple text/number fields in one dialog
-- - InfoMessage: Temporary notification pop-ups
-- - menu_items table: Hierarchical menu structure
-- - text_func: Dynamic menu text based on state
-- - checked_func: Radio button/checkbox state
--============================================================================--

--[[--
Show configuration dialog for basic settings.

Uses MultiInputDialog which provides multiple input fields in one dialog:
- API Key (password-masked text input)
- Base URL (text input)

Fields are retrieved via getFields() which returns array of values in order.
]]
function TrmnlDisplay:showConfigDialog()
    -- Get auto-detected MAC to show as placeholder/hint
    local auto_mac = self:getMacAddress() or "Auto-detect unavailable"
    local mac_hint = auto_mac ~= "Auto-detect unavailable"
        and _("MAC address (leave empty to use:") .. auto_mac .. ")"
        or _("MAC address (auto-detect unavailable)")

    self.config_dialog = MultiInputDialog:new {
        title = _("Configure TRMNL"),
        fields = {
            {
                text = self.settings.api_key or "",
                hint = _("Enter your TRMNL API key"),
                input_type = "string",
                password = true,
            },
            {
                text = self.settings.base_url or "https://trmnl.app",
                hint = _("TRMNL base URL"),
                input_type = "string",
            },
            {
                text = self.settings.mac_header_name or "ID",
                hint = _("MAC address header name (e.g. ID)"),
                input_type = "string",
            },
            {
                text = self.settings.mac_address or "",
                hint = mac_hint,
                input_type = "string",
            },
        },
        buttons = {
            {
                {
                    text = _("Cancel"),
                    id = "close",
                    callback = function()
                        UIManager:close(self.config_dialog)
                    end,
                },
                {
                    text = _("Save"),
                    is_enter_default = true,
                    callback = function()
                        local fields = self.config_dialog:getFields()
                        self.settings.api_key = fields[1]
                        self.settings.base_url = fields[2]
                        self.settings.mac_header_name = fields[3]
                        self.settings.mac_address = fields[4] ~= "" and fields[4] or nil
                        self:saveSettings()
                        UIManager:close(self.config_dialog)
                        self:showInfo("TRMNL settings saved.")
                    end,
                },
            },
        },
    }

    UIManager:show(self.config_dialog)
end

--============================================================================--
-- Menu Builders
--============================================================================--

--[[--
Create a toggle menu item that switches between two states.
@tparam string label_when_on Text to show when currently on
@tparam string label_when_off Text to show when currently off
@tparam function getter Function that returns current state (boolean)
@tparam function toggler Function called to toggle state, should return success message
@treturn table Menu item configuration
]]
function TrmnlDisplay:createToggleMenuItem(label_when_on, label_when_off, getter, toggler)
    return {
        text_func = function()
            return getter() and _(label_when_on) or _(label_when_off)
        end,
        callback = function()
            local message = toggler()
            if message then
                UIManager:show(InfoMessage:new {
                    text = _(message),
                    timeout = 2,
                })
            end
        end
    }
end

--[[--
Create a radio button menu item for exclusive choices.
@tparam string label Display text
@tparam string setting_key Settings key to check/update
@tparam string value Value to compare and set
@treturn table Menu item configuration
]]
function TrmnlDisplay:createRadioMenuItem(label, setting_key, value)
    return {
        text = _(label),
        checked_func = function()
            return self.settings[setting_key] == value
        end,
        callback = function()
            self.settings[setting_key] = value
            self:saveSettings()
        end
    }
end

--[[--
Create menu item constructors for each feature.
]]
function TrmnlDisplay:createFetchMenuItem()
    return {
        text = _("Fetch screen now"),
        callback = function()
            self:fetchAndDisplay()
        end
    }
end

function TrmnlDisplay:createConfigMenuItem()
    return {
        text = _("Configure TRMNL"),
        keep_menu_open = true,
        callback = function()
            self:showConfigDialog()
        end
    }
end

function TrmnlDisplay:createAutoRefreshToggle()
    return self:createToggleMenuItem(
        "Stop dashboard",
        "Start dashboard",
        -- "running" only counts if the dashboard is actually on screen
        function() return self.auto_refresh_enabled and self.image_widget ~= nil end,
        function()
            if self.auto_refresh_enabled and self.image_widget then
                self:stopAutoRefresh()
                return "Dashboard stopped."
            else
                self:startAutoRefresh()
                return "Dashboard started."
            end
        end
    )
end

function TrmnlDisplay:createNotificationsToggle()
    return self:createToggleMenuItem(
        "Hide status notifications",
        "Show status notifications",
        function() return self.settings.show_notifications end,
        function()
            self.settings.show_notifications = not self.settings.show_notifications
            self:saveSettings()
            return self.settings.show_notifications
                and "Status notifications enabled (errors always shown)"
                or "Status notifications hidden (errors still shown)"
        end
    )
end

--============================================================================--
-- Menu Integration
--============================================================================--

--[[--
Register plugin in KOReader's main menu.
]]
function TrmnlDisplay:addToMainMenu(menu_items)
    menu_items.trmnl = {
        text = _("TRMNL Display"),
        sorting_hint = "tools",
        sub_item_table = {
            self:createFetchMenuItem(),
            self:createConfigMenuItem(),
            self:createAutoRefreshToggle(),
            self:createNotificationsToggle(),
            {
                -- Gestures are fixed in this version: sides flip pages, middle fetches, hold closes.
                text = self.VERSION_LABEL .. ": tap sides = pages, middle = refresh, hold = close",
                enabled_func = function() return false end,
            },
            {
                text = _("E-ink refresh type"),
                sub_item_table = {
                    self:createRadioMenuItem("UI (balanced)", "refresh_type", "ui"),
                    self:createRadioMenuItem("Full (best quality)", "refresh_type", "full"),
                    self:createRadioMenuItem("Flash UI (with flash)", "refresh_type", "flashui"),
                    self:createRadioMenuItem("Partial (fastest)", "refresh_type", "partial"),
                }
            },
        }
    }
end

function TrmnlDisplay:onTrmnlFetch()
    self.force_redraw = true   -- a fetch you asked for always redraws and confirms
    self:fetchAndDisplay()
end

function TrmnlDisplay:onTrmnlStartInteractive()
    if self.auto_refresh_enabled then
        self.last_fetch_start = nil
        self.force_redraw = true
        if not self.image_widget then
            UIManager:nextTick(function() self:showCachedNow() end)
        end
        self:fetchAndDisplay()
    else
        self:startAutoRefresh()
    end
end

return TrmnlDisplay
