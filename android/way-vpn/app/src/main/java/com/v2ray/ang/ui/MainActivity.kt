package com.v2ray.ang.ui

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.browser.customtabs.CustomTabsIntent
import androidx.core.app.ActivityCompat
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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
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
    private var loginJob: Job? = null

    private val vpnPermission = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        if (it.resultCode == RESULT_OK) startVpnCore()
        else showStatus("Для подключения требуется разрешение Android VPN")
    }
    private val notificationPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(binding.root)
        enforceWayVpnSettings()
        setupActions()
        handlePaymentReturn(intent)
        requestNotificationPermission()

        lifecycleScope.launch {
            if (repository.accessToken() != null || repository.secureStore.get(SecureStore.REFRESH_TOKEN) != null) {
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

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handlePaymentReturn(intent)
    }

    override fun onResume() {
        super.onResume()
        lifecycleScope.launch { checkLastPayment() }
    }

    private fun setupActions() {
        binding.loginButton.setOnClickListener { lifecycleScope.launch { beginLogin() } }
        binding.connectButton.setOnClickListener { lifecycleScope.launch { toggleConnection() } }
        binding.syncButton.setOnClickListener { lifecycleScope.launch { syncProfile(true) } }
        binding.paymentButton.setOnClickListener { showPaymentChoices() }
        binding.devicesButton.setOnClickListener { lifecycleScope.launch { showDevices() } }
        binding.vpnSettingsButton.setOnClickListener { openVpnSettings() }
        binding.updateButton.setOnClickListener { lifecycleScope.launch { checkUpdates() } }
        binding.logoutButton.setOnClickListener { confirmLogout() }

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
        binding.loginPanel.visibility = View.VISIBLE
        binding.accountPanel.visibility = View.GONE
    }

    private suspend fun showAccountAndLoad() {
        binding.loginPanel.visibility = View.GONE
        binding.accountPanel.visibility = View.VISIBLE
        try {
            val me = repository.me()
            binding.userName.text = me.username?.let { "@$it" } ?: "Telegram ID ${me.tg_id}"
            loadSubscriptions()
            refreshServerSpinner()
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
            CustomTabsIntent.Builder().build().launchUrl(this, Uri.parse(request.telegramUrl))
            binding.loginStatus.text = "Подтвердите вход отдельной кнопкой в боте и вернитесь в Way VPN."
            pollLogin(request)
        } catch (error: Exception) {
            binding.loginButton.isEnabled = true
            binding.loginStatus.text = error.message ?: "Не удалось начать вход"
        }
    }

    private fun pollLogin(request: LoginRequest) {
        if (loginJob?.isActive == true) return
        binding.loginButton.isEnabled = false
        loginJob = lifecycleScope.launch {
            repeat(150) {
                try {
                    if (repository.finishLogin(request)) {
                        binding.loginButton.isEnabled = true
                        showAccountAndLoad()
                        return@launch
                    }
                } catch (error: Exception) {
                    binding.loginStatus.text = error.message ?: "Запрос входа завершён"
                    binding.loginButton.isEnabled = true
                    return@launch
                }
                delay(request.pollSeconds.coerceAtLeast(2) * 1_000L)
            }
            binding.loginStatus.text = "Ссылка входа истекла. Создайте новую."
            binding.loginButton.isEnabled = true
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
        binding.subscriptionExpiry.text = "Действует до $expiry · устройств ${subscription.devices.limit}"
        binding.trafficInfo.text = if (subscription.traffic.enabled) {
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
        return try {
            val response = repository.profile(subscription.id)
            val filtered = WayProfilePolicy.filterSupported(response.content)
            val expiresAt = response.expiresAt ?: subscription.offline_allowed_until
            val expiryEpoch = WayRuntimePolicy.parseExpiry(expiresAt)
                ?: error("Сервер не передал корректный срок действия профиля")
            repository.secureStore.put(SecureStore.LAST_PROFILE, filtered)
            repository.secureStore.put(SecureStore.PROFILE_EXPIRES_AT, expiresAt)
            MmkvManager.encodeSettings(WayRuntimePolicy.PROFILE_EXPIRY_SETTING, expiryEpoch)
            importAndSelectProfile(filtered)
            if (showResult) showStatus("Профиль обновлён. Выбран быстрый сервер.")
            true
        } catch (error: WayApiException) {
            if (error.errorCode == "hwid_limit_reached") {
                showStatus("Достигнут лимит устройств. Удалите старое устройство.")
            } else {
                showStatus(error.message)
            }
            false
        } catch (error: Exception) {
            showStatus(error.message ?: "Не удалось обновить профиль")
            false
        } finally {
            binding.syncButton.isEnabled = true
        }
    }

    private suspend fun importAndSelectProfile(profile: String) = withContext(Dispatchers.IO) {
        val remembered = repository.secureStore.get(SecureStore.SELECTED_SERVER)
        val imported = AngConfigManager.importBatchConfig(profile, WAY_SUBSCRIPTION_ID, false)
        require(imported.first > 0) { "Профиль не содержит доступных узлов" }
        val guids = MmkvManager.decodeServerList(WAY_SUBSCRIPTION_ID)
        require(guids.isNotEmpty()) { "Список серверов пуст" }
        val measured = guids.map { guid ->
                val config = MmkvManager.decodeServerConfig(guid)
                val delay = config?.serverPort?.toIntOrNull()?.let { port ->
                    SpeedtestManager.socketConnectTime(config.server.orEmpty(), port, 1_500)
                } ?: -1
                serverFingerprint(guid) to delay
            }
        val selectedFingerprint = NodeSelector.select(remembered, measured)
        val selected = guids.firstOrNull { serverFingerprint(it) == selectedFingerprint } ?: guids.first()
        MmkvManager.setSelectServer(selected)
        repository.secureStore.put(SecureStore.SELECTED_SERVER, serverFingerprint(selected))
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
    }

    private suspend fun toggleConnection() {
        if (CoreServiceManager.isRunning()) {
            CoreServiceManager.stopVService(this)
            showStatus("Отключение…")
            return
        }
        if (MmkvManager.decodeServerList(WAY_SUBSCRIPTION_ID).isEmpty()) {
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
        binding.connectionStatus.text = if (running) "Подключено" else "Не подключено"
        binding.connectButton.text = if (running) "Отключить" else "Подключить"
        binding.connectButton.setBackgroundColor(getColor(if (running) R.color.color_fab_active else R.color.color_fab_inactive))
    }

    private fun showStatus(message: String) {
        binding.connectionStatus.text = message
    }

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
        choices += listOf(
            PaymentChoice("Новая обычная — 1 месяц", "regular_1m", null),
            PaymentChoice("Новая обычная — 3 месяца", "regular_3m", null),
            PaymentChoice("Новая с антиглушилкой — 1 месяц", "bypass_1m", null),
            PaymentChoice("Новая с антиглушилкой — 3 месяца", "bypass_3m", null),
        )
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
            .setTitle("Выйти из Way VPN?")
            .setMessage("VPN будет остановлен. Токены, HWID и сохранённые профили будут удалены с устройства.")
            .setPositiveButton("Выйти") { _, _ ->
                lifecycleScope.launch {
                    CoreServiceManager.stopVService(this@MainActivity)
                    MmkvManager.removeSubscription(WAY_SUBSCRIPTION_ID)
                    MmkvManager.encodeSettings(WayRuntimePolicy.PROFILE_EXPIRY_SETTING, 0L)
                    repository.logout()
                    subscriptions = emptyList()
                    serverGuids = emptyList()
                    showLogin()
                    binding.loginStatus.text = "Данные удалены. Можно войти снова."
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
