package com.v2ray.ang.ui

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.browser.customtabs.CustomTabsIntent
import androidx.core.app.ActivityCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.v2ray.ang.AppConfig
import com.v2ray.ang.BuildConfig
import com.v2ray.ang.R
import com.v2ray.ang.core.CoreServiceManager
import com.v2ray.ang.databinding.ActivityMainBinding
import com.v2ray.ang.handler.AngConfigManager
import com.v2ray.ang.handler.MmkvManager
import com.v2ray.ang.handler.SpeedtestManager
import com.v2ray.ang.util.QRCodeDecoder
import com.v2ray.ang.way.AccountAccessKey
import com.v2ray.ang.way.DeviceDto
import com.v2ray.ang.way.LoginRequest
import com.v2ray.ang.way.NodeSelector
import com.v2ray.ang.way.SecureStore
import com.v2ray.ang.way.SubscriptionDto
import com.v2ray.ang.way.UpdateInstaller
import com.v2ray.ang.way.WayApiException
import com.v2ray.ang.way.WayProfilePolicy
import com.v2ray.ang.way.WayRepository
import com.v2ray.ang.way.WayRuntimePolicy
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlin.math.roundToInt


class MainActivity : AppCompatActivity() {
    companion object {
        private const val WAY_SUBSCRIPTION_ID = "way_api_managed"
    }

    private val binding by lazy { ActivityMainBinding.inflate(layoutInflater) }
    private val repository by lazy { WayRepository(this) }
    private var subscriptions: List<SubscriptionDto> = emptyList()
    private var serverGuids: List<String> = emptyList()
    private val serverLatency = mutableMapOf<String, Long>()
    private var loginJob: Job? = null
    private var subscriptionScopedSession = false
    private var statusOverride: String? = null
    private var statusOverrideUntil = 0L
    private var lastProfileError: String? = null

    private enum class Page { HOME, SERVERS, SUPPORT, SETTINGS }

    private val vpnPermission = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        if (it.resultCode == RESULT_OK) startVpnCore()
        else showStatus("Для подключения требуется разрешение Android VPN")
    }
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        configureSystemBars()
        setContentView(binding.root)
        applySystemBarInsets()
        enforceWayVpnSettings()
        setupActions()
        showPage(Page.HOME)
        handleAuthReturn(intent)
        handlePaymentReturn(intent)
        requestNotificationPermission()

        lifecycleScope.launch {
            if (repository.hasLogin()) {
                showAccountAndLoad()
            } else {
                showLogin()
                repository.pendingLogin()?.let(::pollLogin)
            }
        }
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                while (isActive) {
                    updateConnectionState()
                    delay(1_000)
                }
            }
        }
    }

    private fun configureSystemBars() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = Color.parseColor("#0B0D12")
        window.navigationBarColor = Color.parseColor("#0B0D12")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            window.isStatusBarContrastEnforced = false
            window.isNavigationBarContrastEnforced = false
        }
        WindowInsetsControllerCompat(window, window.decorView).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }
    }

    private fun applySystemBarInsets() {
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { view, insets ->
            val safe = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            view.setPadding(safe.left, safe.top, safe.right, safe.bottom)
            insets
        }
        ViewCompat.requestApplyInsets(binding.root)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleAuthReturn(intent)
        handlePaymentReturn(intent)
    }

    override fun onResume() {
        super.onResume()
        lifecycleScope.launch {
            checkLastPayment()
            repository.pendingLogin()?.let(::pollLogin)
        }
    }

    private fun setupActions() {
        binding.loginButton.setOnClickListener { lifecycleScope.launch { beginLogin() } }
        binding.checkLoginButton.setOnClickListener { lifecycleScope.launch { checkPendingLoginNow() } }
        binding.keyLoginButton.setOnClickListener { lifecycleScope.launch { addSubscriptionFromInput() } }
        binding.connectButton.setOnClickListener { lifecycleScope.launch { toggleConnection() } }
        binding.syncButton.setOnClickListener { lifecycleScope.launch { syncProfile(true) } }
        binding.paymentButton.setOnClickListener { showPaymentChoices() }
        binding.devicesButton.setOnClickListener { lifecycleScope.launch { showDevices() } }
        binding.vpnSettingsButton.setOnClickListener { openVpnSettings() }
        binding.updateButton.setOnClickListener { lifecycleScope.launch { checkUpdates() } }
        binding.logoutButton.setOnClickListener { confirmLogout() }
        binding.pingAllButton.setOnClickListener { lifecycleScope.launch { measureAllServers() } }
        binding.navHome.setOnClickListener { showPage(Page.HOME) }
        binding.navServers.setOnClickListener { showPage(Page.SERVERS) }
        binding.navSupport.setOnClickListener { showPage(Page.SUPPORT) }
        binding.navSettings.setOnClickListener { showPage(Page.SETTINGS) }
        binding.supportTelegramButton.setOnClickListener {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://t.me/WaySPN_robot")))
        }
        binding.copyKeyButton.setOnClickListener { copyAccessKey() }
        binding.qrKeyButton.setOnClickListener { showAccessKeyQr() }
        binding.rotateKeyButton.setOnClickListener { confirmRotateAccessKey() }

        binding.subscriptionSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onNothingSelected(parent: AdapterView<*>?) = Unit
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                subscriptions.getOrNull(position)?.let { selected ->
                    lifecycleScope.launch { repository.secureStore.put(SecureStore.SELECTED_SUBSCRIPTION, selected.id.toString()) }
                    renderSubscription(selected)
                    refreshServerSpinner()
                }
            }
        }
        binding.serverSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onNothingSelected(parent: AdapterView<*>?) = Unit
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                serverGuids.getOrNull(position)?.let { guid ->
                    MmkvManager.setSelectServer(guid)
                    lifecycleScope.launch {
                        repository.secureStore.put(SecureStore.SELECTED_SERVER, serverFingerprint(guid))
                    }
                }
            }
        }
    }

    private fun enforceWayVpnSettings() {
        MmkvManager.encodeSettings(AppConfig.PREF_MODE, AppConfig.VPN)
        MmkvManager.encodeSettings(AppConfig.PREF_IPV6_ENABLED, true)
        MmkvManager.encodeSettings(AppConfig.PREF_VPN_BYPASS_LAN, "2")
        MmkvManager.encodeSettings(AppConfig.PREF_PER_APP_PROXY, false)
        MmkvManager.encodeSettings(AppConfig.PREF_PROXY_SHARING, false)
        MmkvManager.encodeSettings(AppConfig.PREF_ENABLE_LOCAL_PROXY, false)
        MmkvManager.encodeSettings(AppConfig.PREF_APPEND_HTTP_PROXY, false)
        MmkvManager.encodeSettings(AppConfig.PREF_ROOT_MODE_ENABLE, false)
        MmkvManager.encodeSettings(AppConfig.PREF_SPEED_ENABLED, true)
        MmkvManager.encodeSettings(AppConfig.PREF_USE_HEV_TUNNEL, false)
        MmkvManager.encodeSettings(AppConfig.PREF_VPN_DNS, "1.1.1.1,2606:4700:4700::1111")
        MmkvManager.encodeSettings(AppConfig.PREF_REMOTE_DNS, "1.1.1.1,2606:4700:4700::1111")
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ActivityCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun showLogin() {
        subscriptionScopedSession = false
        binding.loginPanel.visibility = View.VISIBLE
        binding.accountPanel.visibility = View.GONE
        binding.loginButton.isEnabled = true
        binding.keyLoginButton.isEnabled = true
        binding.checkLoginButton.isEnabled = true
        binding.checkLoginButton.visibility = View.GONE
    }

    private suspend fun showAccountAndLoad() {
        binding.loginPanel.visibility = View.GONE
        binding.accountPanel.visibility = View.VISIBLE
        try {
            val me = repository.me()
            val directSubscription = me.auth_scope == "direct_subscription"
            val subscriptionSession = me.auth_scope == "subscription" || directSubscription
            subscriptionScopedSession = subscriptionSession
            binding.userName.text = if (subscriptionSession) {
                if (directSubscription) "VPN-подписка" else "Вход по ссылке подписки"
            } else {
                me.username?.let { "@$it" } ?: "Telegram ID ${me.tg_id}"
            }
            binding.accessKeyText.text = repository.ensureAccountAccessKey()
            binding.rotateKeyButton.visibility = if (subscriptionSession) View.GONE else View.VISIBLE
            binding.paymentButton.visibility = if (directSubscription) View.GONE else View.VISIBLE
            binding.devicesButton.visibility = if (directSubscription) View.GONE else View.VISIBLE
            binding.deviceIdText.text = "Device ID: ${repository.secureStore.installationHwid()}\nWay VPN ${BuildConfig.VERSION_NAME} · Android ${Build.VERSION.RELEASE}"
            loadSubscriptions()
            if (directSubscription && MmkvManager.decodeServerList(WAY_SUBSCRIPTION_ID).isEmpty()) {
                val restored = repository.secureStore.get(SecureStore.LAST_PROFILE) != null && restoreOfflineProfile()
                if (!restored) syncProfile(false)
            }
            refreshServerSpinner()
            showPage(Page.HOME)
        } catch (error: Exception) {
            if (error is WayApiException && error.status == 401) {
                repository.logout()
                showLogin()
            } else {
                showStatus(error.message ?: "Не удалось загрузить кабинет")
            }
        }
    }

    private suspend fun beginLogin() {
        binding.loginButton.isEnabled = false
        binding.loginStatus.text = "Создаём одноразовый запрос входа…"
        try {
            val request = repository.beginLogin()
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(request.telegramUrl)))
            binding.loginStatus.text = "Подтвердите вход в боте, затем нажмите «Вернуться в Way VPN»."
            binding.checkLoginButton.visibility = View.VISIBLE
            pollLogin(request)
        } catch (error: Exception) {
            binding.loginButton.isEnabled = true
            binding.checkLoginButton.visibility = View.GONE
            binding.loginStatus.text = error.message ?: "Не удалось начать вход"
        }
    }

    private suspend fun addSubscriptionFromInput() {
        val subscriptionUrl = binding.accessKeyInput.text?.toString().orEmpty()
        if (subscriptionUrl.isBlank()) {
            binding.loginStatus.text = "Введите полную HTTPS-ссылку VPN-подписки."
            return
        }
        binding.keyLoginButton.isEnabled = false
        binding.loginButton.isEnabled = false
        binding.loginStatus.text = "Добавляем VPN-подписку…"
        try {
            repository.addSubscriptionUrl(subscriptionUrl)
            CoreServiceManager.stopVService(this)
            MmkvManager.removeSubscription(WAY_SUBSCRIPTION_ID)
            MmkvManager.encodeSettings(WayRuntimePolicy.PROFILE_EXPIRY_SETTING, 0L)
            showAccountAndLoad()
        } catch (error: Exception) {
            binding.loginStatus.text = error.message ?: "Не удалось добавить VPN-подписку"
            binding.keyLoginButton.isEnabled = true
            binding.loginButton.isEnabled = true
        }
    }

    private fun pollLogin(request: LoginRequest) {
        if (loginJob?.isActive == true) return
        binding.loginButton.isEnabled = false
        binding.checkLoginButton.visibility = View.VISIBLE
        loginJob = lifecycleScope.launch {
            repeat(100) {
                try {
                    if (repository.finishLogin(request)) {
                        binding.loginButton.isEnabled = true
                        binding.checkLoginButton.visibility = View.GONE
                        showAccountAndLoad()
                        return@launch
                    }
                } catch (error: Exception) {
                    val transient = error is WayApiException && (error.status == 429 || error.status >= 500)
                    if (transient) {
                        binding.loginStatus.text = "Сервер временно занят. Продолжаем проверять вход…"
                    } else {
                        binding.loginStatus.text = error.message ?: "Запрос входа завершён"
                        binding.loginButton.isEnabled = true
                        binding.checkLoginButton.visibility = View.GONE
                        return@launch
                    }
                }
                delay(request.pollSeconds.coerceAtLeast(2) * 1_000L)
            }
            binding.loginStatus.text = "Ссылка входа истекла. Создайте новую."
            binding.loginButton.isEnabled = true
            binding.checkLoginButton.visibility = View.GONE
        }
    }

    private suspend fun checkPendingLoginNow() {
        if (loginJob?.isActive == true) {
            binding.loginStatus.text = "Подтверждение получено. Завершаем безопасный вход…"
            return
        }
        val request = repository.pendingLogin()
        if (request == null) {
            binding.loginStatus.text = "Нет активного запроса. Нажмите «Войти через Telegram» ещё раз."
            binding.loginButton.isEnabled = true
            binding.checkLoginButton.isEnabled = true
            binding.checkLoginButton.visibility = View.GONE
            return
        }
        binding.checkLoginButton.isEnabled = false
        binding.loginStatus.text = "Проверяем подтверждение…"
        try {
            if (repository.finishLogin(request)) {
                binding.checkLoginButton.isEnabled = true
                binding.checkLoginButton.visibility = View.GONE
                showAccountAndLoad()
            } else {
                binding.loginStatus.text = "Бот ещё не получил подтверждение. Подтвердите вход и повторите проверку."
                binding.checkLoginButton.isEnabled = true
                pollLogin(request)
            }
        } catch (error: Exception) {
            binding.loginStatus.text = error.message ?: "Не удалось проверить вход"
            binding.loginButton.isEnabled = true
            binding.checkLoginButton.isEnabled = true
        }
    }

    private fun handleAuthReturn(intent: Intent?) {
        val uri = intent?.data
        val isHttpsReturn = uri?.scheme == "https" && uri.host == "wayspn.ru" && uri.path == "/mobile/auth-return"
        val isCustomReturn = uri?.scheme == "wayvpn" && uri.host == "auth-return"
        if (isHttpsReturn || isCustomReturn) {
            lifecycleScope.launch {
                checkPendingLoginNow()
            }
        }
    }

    private suspend fun loadSubscriptions() {
        subscriptions = repository.subscriptions()
        val labels = subscriptions.map {
            "${it.title} #${it.type_index} — ${if (it.status == "active") "активна" else "истекла"}"
        }
        binding.subscriptionSpinner.adapter = ArrayAdapter(this, R.layout.item_way_spinner, labels).apply {
            setDropDownViewResource(R.layout.item_way_spinner)
        }
        val storedId = repository.secureStore.get(SecureStore.SELECTED_SUBSCRIPTION)?.toLongOrNull()
        val index = subscriptions.indexOfFirst { it.id == storedId }.takeIf { it >= 0 } ?: 0
        if (subscriptions.isNotEmpty()) {
            binding.subscriptionSpinner.setSelection(index, false)
            renderSubscription(subscriptions[index])
        } else {
            binding.subscriptionExpiry.text = "Нет подписок"
            binding.trafficInfo.text = "Оформите подписку в разделе оплаты"
        }
    }

    private fun selectedSubscription(): SubscriptionDto? = subscriptions.getOrNull(binding.subscriptionSpinner.selectedItemPosition)

    private fun renderSubscription(subscription: SubscriptionDto) {
        val expiry = subscription.subscription_until?.let(::formatDate) ?: "неизвестно"
        binding.subscriptionExpiry.text = if (subscription.plan_kind == "external") {
            "Действует до $expiry · внешняя подписка"
        } else {
            "Действует до $expiry · устройств ${subscription.devices.limit}"
        }
        binding.trafficInfo.text = if (subscription.plan_kind == "external") {
            "Трафик и устройства управляются сервером подписки"
        } else if (subscription.traffic.enabled) {
            "Трафик: ${formatBytes(subscription.traffic.used_bytes)} из ${formatBytes(subscription.traffic.limit_bytes)}"
        } else {
            "Трафик без учёта лимита"
        }
    }

    private suspend fun syncProfile(showResult: Boolean): Boolean {
        val subscription = selectedSubscription() ?: run {
            showStatus("Выберите или оформите подписку")
            return false
        }
        binding.syncButton.isEnabled = false
        showStatus("Обновляем защищённый профиль…")
        var stage = "получение профиля"
        return try {
            val response = repository.profile(subscription.id)
            val expiresAt = response.expiresAt ?: subscription.offline_allowed_until
            val expiryEpoch = WayRuntimePolicy.parseExpiry(expiresAt)
                ?: error("Сервер не передал корректный срок действия профиля")
            stage = "разбор и импорт серверов"
            importAndSelectProfile(response.content)
            MmkvManager.encodeSettings(WayRuntimePolicy.PROFILE_EXPIRY_SETTING, expiryEpoch)

            // Сбой локального зашифрованного кэша не должен удалять уже
            // импортированные рабочие серверы. Он влияет только на офлайн-режим.
            stage = "сохранение офлайн-профиля"
            val cached = try {
                repository.secureStore.put(SecureStore.LAST_PROFILE, response.content)
                repository.secureStore.put(SecureStore.PROFILE_EXPIRES_AT, expiresAt)
                true
            } catch (error: CancellationException) {
                throw error
            } catch (_: Exception) {
                false
            }
            lastProfileError = null
            if (showResult) {
                showStatus(
                    if (cached) "Профиль обновлён. Выбран быстрый сервер."
                    else "Серверы загружены. Офлайн-кэш временно недоступен."
                )
            }
            true
        } catch (error: CancellationException) {
            throw error
        } catch (error: WayApiException) {
            val message = if (error.errorCode == "hwid_limit_reached") {
                "Достигнут лимит устройств этой подписки."
            } else {
                error.message
            }
            lastProfileError = message
            showStatus(message)
            renderServerList()
            false
        } catch (error: Exception) {
            val message = subscriptionLoadError(error, stage)
            lastProfileError = message
            showStatus(message)
            renderServerList()
            false
        } finally {
            binding.syncButton.isEnabled = true
        }
    }

    private fun subscriptionLoadError(error: Exception, stage: String): String = when (error) {
        is java.net.UnknownHostException -> "Не удалось найти сервер подписки. Проверьте адрес и интернет."
        is java.net.SocketTimeoutException -> "Сервер подписки не ответил вовремя. Повторите обновление."
        is javax.net.ssl.SSLException -> "Сертификат HTTPS-сервера подписки не прошёл проверку."
        else -> error.message
            ?.takeIf(String::isNotBlank)
            ?.takeUnless { it.contains("https://", ignoreCase = true) }
            ?.let { "Не удалось загрузить подписку: $it" }
            ?: "Сбой на этапе «$stage» (${error.javaClass.simpleName.ifBlank { "Exception" }})"
    }

    private suspend fun importAndSelectProfile(profile: String) = withContext(Dispatchers.IO) {
        val remembered = try {
            repository.secureStore.get(SecureStore.SELECTED_SERVER)
        } catch (error: CancellationException) {
            throw error
        } catch (_: Exception) {
            null
        }
        val imported = AngConfigManager.importBatchConfig(profile, WAY_SUBSCRIPTION_ID, false)
        require(imported.first > 0) { "Профиль не содержит доступных узлов" }
        MmkvManager.decodeServerList(WAY_SUBSCRIPTION_ID)
            .filter { guid ->
                val configType = MmkvManager.decodeServerConfig(guid)?.configType
                configType == null || !WayProfilePolicy.isSupported(
                    configType,
                    MmkvManager.decodeServerRaw(guid),
                )
            }
            .forEach(MmkvManager::removeServer)
        val guids = MmkvManager.decodeServerList(WAY_SUBSCRIPTION_ID)
        require(guids.isNotEmpty()) {
            "Профиль не содержит серверов VLESS, Trojan или Shadowsocks"
        }

        // Список должен появиться сразу после успешного импорта, а не после
        // последовательного ожидания TCP-пинга каждого узла.
        val initial = guids.firstOrNull { serverFingerprint(it) == remembered } ?: guids.first()
        MmkvManager.setSelectServer(initial)
        try {
            repository.secureStore.put(SecureStore.SELECTED_SERVER, serverFingerprint(initial))
        } catch (error: CancellationException) {
            throw error
        } catch (_: Exception) {
            // Выбор остаётся сохранённым в MMKV; шифрованное запоминание необязательно.
        }
        withContext(Dispatchers.Main) { refreshServerSpinner() }

        val measured = coroutineScope {
            guids.map { guid ->
                async {
                val config = MmkvManager.decodeServerConfig(guid)
                val delay = config?.serverPort?.toIntOrNull()?.let { port ->
                    SpeedtestManager.socketConnectTime(config.server.orEmpty(), port, 1_500)
                } ?: -1
                serverFingerprint(guid) to delay
                }
            }.awaitAll()
        }
        serverLatency.clear()
        guids.zip(measured).forEach { (guid, result) -> serverLatency[guid] = result.second }
        val selectedFingerprint = NodeSelector.select(remembered, measured)
        val selected = guids.firstOrNull { serverFingerprint(it) == selectedFingerprint } ?: guids.first()
        MmkvManager.setSelectServer(selected)
        try {
            repository.secureStore.put(SecureStore.SELECTED_SERVER, serverFingerprint(selected))
        } catch (error: CancellationException) {
            throw error
        } catch (_: Exception) {
            // Выбор остаётся сохранённым в MMKV; шифрованное запоминание необязательно.
        }
        withContext(Dispatchers.Main) { refreshServerSpinner() }
    }

    private fun serverFingerprint(guid: String): String {
        val config = MmkvManager.decodeServerConfig(guid) ?: return ""
        return "${config.configType}:${config.server}:${config.serverPort}:${config.remarks}"
    }

    private fun refreshServerSpinner() {
        serverGuids = MmkvManager.decodeServerList(WAY_SUBSCRIPTION_ID)
        val labels = serverGuids.map { MmkvManager.decodeServerConfig(it)?.remarks?.ifBlank { "Сервер" } ?: "Сервер" }
        binding.serverSpinner.adapter = ArrayAdapter(this, R.layout.item_way_spinner, labels).apply {
            setDropDownViewResource(R.layout.item_way_spinner)
        }
        val selected = serverGuids.indexOf(MmkvManager.getSelectServer()).takeIf { it >= 0 } ?: 0
        if (serverGuids.isNotEmpty()) binding.serverSpinner.setSelection(selected, false)
        renderServerList()
        renderSelectedServer()
    }

    private suspend fun measureAllServers() {
        if (serverGuids.isEmpty()) {
            if (!syncProfile(true)) return
        }
        binding.pingAllButton.isEnabled = false
        binding.pingAllButton.text = "Проверяем…"
        val measured = withContext(Dispatchers.IO) {
            serverGuids.associateWith { guid ->
                val config = MmkvManager.decodeServerConfig(guid)
                config?.serverPort?.toIntOrNull()?.let { port ->
                    SpeedtestManager.socketConnectTime(config.server.orEmpty(), port, 1_500)
                } ?: -1L
            }
        }
        serverLatency.clear()
        serverLatency.putAll(measured)
        renderServerList()
        renderSelectedServer()
        binding.pingAllButton.text = "Пинг"
        binding.pingAllButton.isEnabled = true
    }

    private fun renderServerList() {
        val container = binding.serverListContainer
        container.removeAllViews()
        if (serverGuids.isEmpty()) {
            binding.pingAllButton.text = "Обновить"
            container.addView(TextView(this).apply {
                text = lastProfileError?.let { "Не удалось загрузить серверы:\n$it\n\nНажмите «Обновить», чтобы повторить." }
                    ?: "Серверы ещё не загружены. Нажмите «Обновить»."
                setTextColor(Color.parseColor("#9499A5"))
                textSize = 16f
                setPadding(dp(8), dp(24), dp(8), dp(24))
            })
            return
        }
        binding.pingAllButton.text = "Пинг"

        val selectedGuid = MmkvManager.getSelectServer()
        serverGuids.forEach { guid ->
            val config = MmkvManager.decodeServerConfig(guid) ?: return@forEach
            val latency = serverLatency[guid]
            val selected = guid == selectedGuid
            val card = com.google.android.material.card.MaterialCardView(this).apply {
                radius = dp(20).toFloat()
                setCardBackgroundColor(Color.parseColor(if (selected) "#26332F" else "#1B1E25"))
                strokeWidth = if (selected) dp(1) else 0
                strokeColor = Color.parseColor("#39E6A5")
                isClickable = true
                isFocusable = true
                layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
                    bottomMargin = dp(10)
                }
                setOnClickListener {
                    MmkvManager.setSelectServer(guid)
                    lifecycleScope.launch { repository.secureStore.put(SecureStore.SELECTED_SERVER, serverFingerprint(guid)) }
                    renderServerList()
                    renderSelectedServer()
                }
            }
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(18), dp(16), dp(18), dp(16))
            }
            row.addView(TextView(this).apply {
                text = config.remarks.ifBlank { "Way VPN сервер" }
                setTextColor(Color.parseColor(if (selected) "#39E6A5" else "#FFFFFF"))
                textSize = 17f
                setTypeface(typeface, android.graphics.Typeface.BOLD)
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                append("\n")
                append(listOfNotNull(
                    config.configType.name,
                    config.security?.takeIf(String::isNotBlank),
                    config.network?.takeIf(String::isNotBlank),
                ).joinToString(" · "))
            })
            row.addView(TextView(this).apply {
                text = when {
                    latency == null -> "—"
                    latency < 0 -> "нет ответа"
                    else -> "${latency} мс"
                }
                setTextColor(Color.parseColor(latencyColor(latency)))
                textSize = 14f
            })
            card.addView(row)
            container.addView(card)
        }
    }

    private fun renderSelectedServer() {
        val guid = MmkvManager.getSelectServer()
        val config = guid?.let(MmkvManager::decodeServerConfig)
        binding.currentServerName.text = config?.remarks?.ifBlank { "Way VPN сервер" } ?: "Автовыбор сервера"
        val details = config?.let {
            listOfNotNull(
                it.configType.name,
                it.security?.takeIf(String::isNotBlank),
                it.network?.takeIf(String::isNotBlank),
                serverLatency[guid]?.takeIf { value -> value >= 0 }?.let { value -> "${value} мс" },
            ).joinToString(" · ")
        } ?: "Будет выбран узел с минимальной задержкой"
        binding.currentServerDetails.text = details
        binding.protocolInfo.text = "Протокол:  ${config?.configType?.name ?: "—"}"
        binding.encryptionInfo.text = "Защита:  ${config?.security?.ifBlank { "Xray" } ?: "—"}"
    }

    private fun latencyColor(value: Long?): String = when {
        value == null || value < 0 -> "#777C87"
        value < 180 -> "#39E6A5"
        value < 400 -> "#F2B84B"
        else -> "#FF5B78"
    }

    private suspend fun toggleConnection() {
        if (CoreServiceManager.isRunning()) {
            CoreServiceManager.stopVService(this)
            showStatus("Отключение…")
            return
        }
        val profileUsable = WayRuntimePolicy.isProfileUsable(
            MmkvManager.decodeSettingsLong(WayRuntimePolicy.PROFILE_EXPIRY_SETTING, 0L)
        )
        if (MmkvManager.decodeServerList(WAY_SUBSCRIPTION_ID).isEmpty() || !profileUsable) {
            val online = syncProfile(false)
            if (!online && !restoreOfflineProfile()) return
        }
        val prepare = VpnService.prepare(this)
        if (prepare == null) startVpnCore() else vpnPermission.launch(prepare)
    }

    private suspend fun restoreOfflineProfile(): Boolean {
        val profile = repository.secureStore.get(SecureStore.LAST_PROFILE) ?: return false
        val expiry = repository.secureStore.get(SecureStore.PROFILE_EXPIRES_AT)
        val expiryEpoch = WayRuntimePolicy.parseExpiry(expiry)
        val stillValid = expiryEpoch != null && WayRuntimePolicy.isProfileUsable(expiryEpoch)
        if (!stillValid) {
            showStatus("Офлайн-профиль недоступен: подписка истекла")
            return false
        }
        return runCatching {
            MmkvManager.encodeSettings(WayRuntimePolicy.PROFILE_EXPIRY_SETTING, expiryEpoch)
            importAndSelectProfile(profile)
            showStatus("Используется последний зашифрованный профиль")
            true
        }.getOrElse {
            showStatus("Не удалось восстановить офлайн-профиль")
            false
        }
    }

    private fun startVpnCore() {
        enforceWayVpnSettings()
        if (!WayRuntimePolicy.isProfileUsable(
                MmkvManager.decodeSettingsLong(WayRuntimePolicy.PROFILE_EXPIRY_SETTING, 0L)
            )
        ) {
            showStatus("Подписка истекла: обновите профиль перед подключением")
            return
        }
        if (MmkvManager.getSelectServer().isNullOrBlank()) {
            showStatus("Сервер не выбран")
            return
        }
        CoreServiceManager.startVService(this)
        showStatus("Подключение…")
    }

    // Совместимые точки вызова для унаследованных внутренних классов v2rayNG.
    // Они не экспортированы и не открывают ручной импорт/редактирование профилей.
    fun restartV2Ray() {
        CoreServiceManager.stopVService(this)
        lifecycleScope.launch {
            delay(500)
            startVpnCore()
        }
    }

    fun refreshGroupTabTitles(refreshAll: Boolean = false) {
        refreshServerSpinner()
    }

    fun importConfigViaSub(): Boolean {
        lifecycleScope.launch { syncProfile(true) }
        return true
    }

    private fun updateConnectionState() {
        val running = CoreServiceManager.isRunning()
        val overrideActive = !running && android.os.SystemClock.elapsedRealtime() < statusOverrideUntil
        binding.connectionStatus.text = when {
            running -> "Статус:  Подключено"
            overrideActive -> statusOverride
            else -> "Статус:  Не подключено"
        }
        binding.connectionStatus.setTextColor(
            Color.parseColor(when {
                running -> "#39E6A5"
                overrideActive -> "#F2B84B"
                else -> "#9499A5"
            })
        )
        binding.connectButton.text = if (running) "W\nОТКЛЮЧИТЬ" else "W\nПОДКЛЮЧИТЬ"
        binding.connectButton.setBackgroundColor(Color.parseColor(if (running) "#39E6A5" else "#6C717D"))
        if (running) renderSelectedServer()
    }

    private fun showStatus(message: String) {
        statusOverride = message
        statusOverrideUntil = android.os.SystemClock.elapsedRealtime() + 15_000L
        binding.connectionStatus.text = message
        binding.connectionStatus.setTextColor(Color.parseColor("#F2B84B"))
    }

    private fun showPage(page: Page) {
        binding.homePage.visibility = if (page == Page.HOME) View.VISIBLE else View.GONE
        binding.serversPage.visibility = if (page == Page.SERVERS) View.VISIBLE else View.GONE
        binding.supportPage.visibility = if (page == Page.SUPPORT) View.VISIBLE else View.GONE
        binding.settingsPage.visibility = if (page == Page.SETTINGS) View.VISIBLE else View.GONE
        val active = Color.parseColor("#39E6A5")
        val inactive = Color.parseColor("#777C87")
        binding.navHome.setTextColor(if (page == Page.HOME) active else inactive)
        binding.navServers.setTextColor(if (page == Page.SERVERS) active else inactive)
        binding.navSupport.setTextColor(if (page == Page.SUPPORT) active else inactive)
        binding.navSettings.setTextColor(if (page == Page.SETTINGS) active else inactive)
    }

    private fun copyAccessKey() {
        val key = binding.accessKeyText.text?.toString().orEmpty()
        if (!AccountAccessKey.isValid(key)) return
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("Way VPN access", key))
        showStatus("Ключ или ссылка скопированы")
    }

    private fun showAccessKeyQr() {
        val key = binding.accessKeyText.text?.toString().orEmpty()
        if (!AccountAccessKey.isValid(key)) return
        val bitmap = QRCodeDecoder.createQRCode(key, 900) ?: run {
            showStatus("Не удалось создать QR-код")
            return
        }
        val image = ImageView(this).apply {
            setImageBitmap(bitmap)
            adjustViewBounds = true
            setPadding(dp(20), dp(20), dp(20), dp(20))
        }
        AlertDialog.Builder(this)
            .setTitle("Доступ Way VPN")
            .setMessage("Показывайте этот QR-код только своим устройствам.")
            .setView(image)
            .setPositiveButton("Закрыть", null)
            .show()
    }

    private fun confirmRotateAccessKey() {
        AlertDialog.Builder(this)
            .setTitle("Заменить ключ доступа?")
            .setMessage("Старый ключ перестанет подходить для новых входов. Уже открытые сессии на ваших устройствах сохранятся.")
            .setPositiveButton("Заменить") { _, _ ->
                lifecycleScope.launch {
                    runCatching { repository.rotateAccountAccessKey() }
                        .onSuccess {
                            binding.accessKeyText.text = it
                            showStatus("Ключ доступа заменён")
                        }
                        .onFailure { showStatus(it.message ?: "Не удалось заменить ключ") }
                }
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).roundToInt()

    private suspend fun showDevices() {
        val subscription = selectedSubscription() ?: return
        try {
            val devices = repository.devices(subscription.id)
            if (devices.isEmpty()) {
                AlertDialog.Builder(this).setTitle("Устройства").setMessage("Подключённых устройств пока нет").setPositiveButton("OK", null).show()
                return
            }
            val labels = devices.map { deviceLabel(it) }.toTypedArray()
            AlertDialog.Builder(this)
                .setTitle("Устройства — нажмите, чтобы удалить")
                .setItems(labels) { _, index -> confirmDeviceDelete(subscription.id, devices[index]) }
                .setNegativeButton("Закрыть", null)
                .show()
        } catch (error: Exception) {
            showStatus(error.message ?: "Не удалось загрузить устройства")
        }
    }

    private fun deviceLabel(device: DeviceDto): String = listOfNotNull(device.device_model, device.platform, device.os_version)
        .joinToString(" · ").ifBlank { device.hwid.take(12) }

    private fun confirmDeviceDelete(subscriptionId: Long, device: DeviceDto) {
        AlertDialog.Builder(this)
            .setTitle("Удалить устройство?")
            .setMessage(deviceLabel(device))
            .setPositiveButton("Удалить") { _, _ ->
                lifecycleScope.launch {
                    runCatching { repository.deleteDevice(subscriptionId, device.hwid) }
                        .onSuccess { showStatus("Устройство удалено. Теперь можно повторить синхронизацию.") }
                        .onFailure { showStatus(it.message ?: "Не удалось удалить устройство") }
                }
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private data class PaymentChoice(val title: String, val tariff: String, val subscriptionId: Long?)

    private fun showPaymentChoices() {
        val selected = selectedSubscription()
        val choices = mutableListOf<PaymentChoice>()
        if (selected != null) {
            choices += PaymentChoice("Продлить выбранную на 1 месяц", "${selected.plan_kind}_1m", selected.id)
            choices += PaymentChoice("Продлить выбранную на 3 месяца", "${selected.plan_kind}_3m", selected.id)
        }
        if (!subscriptionScopedSession) {
            choices += listOf(
                PaymentChoice("Новая обычная — 1 месяц", "regular_1m", null),
                PaymentChoice("Новая обычная — 3 месяца", "regular_3m", null),
                PaymentChoice("Новая с антиглушилкой — 1 месяц", "bypass_1m", null),
                PaymentChoice("Новая с антиглушилкой — 3 месяца", "bypass_3m", null),
            )
        }
        AlertDialog.Builder(this).setTitle("Купить или продлить").setItems(choices.map { it.title }.toTypedArray()) { _, index ->
            choosePaymentProvider(choices[index])
        }.setNegativeButton("Отмена", null).show()
    }

    private fun choosePaymentProvider(choice: PaymentChoice) {
        val providers = arrayOf("ЮKassa", "Crypto Pay")
        AlertDialog.Builder(this).setTitle("Способ оплаты").setItems(providers) { _, index ->
            lifecycleScope.launch {
                try {
                    val payment = repository.createSubscriptionPayment(
                        choice.tariff,
                        if (index == 0) "yookassa" else "cryptobot",
                        choice.subscriptionId,
                    )
                    require(payment.pay_url.startsWith("https://"))
                    repository.secureStore.put(SecureStore.LAST_PAYMENT, payment.invoice_id)
                    CustomTabsIntent.Builder().build().launchUrl(this@MainActivity, Uri.parse(payment.pay_url))
                } catch (error: Exception) {
                    showStatus(error.message ?: "Не удалось создать счёт")
                }
            }
        }.show()
    }

    private fun handlePaymentReturn(intent: Intent?) {
        if (intent?.data?.host == "wayspn.ru" && intent.data?.path == "/mobile/payment-return") {
            lifecycleScope.launch { checkLastPayment() }
        }
    }

    private suspend fun checkLastPayment() {
        val invoice = repository.secureStore.get(SecureStore.LAST_PAYMENT) ?: return
        runCatching { repository.paymentStatus(invoice) }.onSuccess { payment ->
            if (payment.status == "paid") {
                repository.secureStore.put(SecureStore.LAST_PAYMENT, null)
                showStatus("Оплата подтверждена сервером")
                loadSubscriptions()
                syncProfile(false)
            }
        }
    }

    private fun openVpnSettings() {
        val intent = Intent(Settings.ACTION_VPN_SETTINGS)
        runCatching { startActivity(intent) }.onFailure { startActivity(Intent(Settings.ACTION_SETTINGS)) }
    }

    private suspend fun checkUpdates() {
        binding.updateButton.isEnabled = false
        try {
            val manifest = repository.updateManifest()
            if (manifest.versionCode <= BuildConfig.VERSION_CODE) {
                AlertDialog.Builder(this).setTitle("Обновления").setMessage("Установлена актуальная версия ${BuildConfig.VERSION_NAME}").setPositiveButton("OK", null).show()
                return
            }
            AlertDialog.Builder(this)
                .setTitle("Доступна версия ${manifest.versionName}")
                .setMessage(manifest.releaseNotes.joinToString("\n• ", prefix = "• "))
                .setPositiveButton("Скачать и проверить") { _, _ ->
                    lifecycleScope.launch {
                        showStatus("Скачиваем и проверяем подпись обновления…")
                        runCatching { UpdateInstaller(this@MainActivity).downloadAndVerify(manifest) }
                            .onSuccess { UpdateInstaller(this@MainActivity).launchInstaller(it) }
                            .onFailure { showStatus(it.message ?: "Проверка обновления не пройдена") }
                    }
                }
                .setNegativeButton("Позже", null)
                .show()
        } catch (error: Exception) {
            showStatus(error.message ?: "Не удалось проверить обновления")
        } finally {
            binding.updateButton.isEnabled = true
        }
    }

    private fun confirmLogout() {
        AlertDialog.Builder(this)
            .setTitle("Удалить данные Way VPN?")
            .setMessage("VPN будет остановлен. Добавленная подписка, токены, HWID и сохранённые профили будут удалены с устройства.")
            .setPositiveButton("Удалить") { _, _ ->
                lifecycleScope.launch {
                    CoreServiceManager.stopVService(this@MainActivity)
                    MmkvManager.removeSubscription(WAY_SUBSCRIPTION_ID)
                    MmkvManager.encodeSettings(WayRuntimePolicy.PROFILE_EXPIRY_SETTING, 0L)
                    repository.logout()
                    subscriptions = emptyList()
                    serverGuids = emptyList()
                    showLogin()
                    binding.loginStatus.text = "Данные удалены. Можно добавить другую VPN-подписку."
                }
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun formatDate(value: String): String = runCatching {
        DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm")
            .withZone(ZoneId.systemDefault())
            .format(Instant.parse(value))
    }.getOrDefault(value)

    private fun formatBytes(value: Long): String {
        val gb = value.toDouble() / (1024.0 * 1024.0 * 1024.0)
        return "${(gb * 10).roundToInt() / 10.0} ГБ"
    }
}
