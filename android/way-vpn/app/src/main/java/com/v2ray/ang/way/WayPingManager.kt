package com.v2ray.ang.way

import android.content.Context
import android.os.SystemClock
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.v2ray.ang.core.CoreConfigManager
import com.v2ray.ang.core.CoreNativeManager
import com.v2ray.ang.handler.MmkvManager
import com.v2ray.ang.handler.SpeedtestManager
import com.v2ray.ang.util.JsonUtil
import com.v2ray.ang.util.Utils
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import libv2ray.CoreCallbackHandler
import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import java.util.concurrent.TimeUnit
import kotlin.math.roundToLong


enum class WayPingMode(
    val title: String,
    val shortTitle: String,
    val description: String,
) {
    AUTO(
        title = "Авто",
        shortTitle = "Авто",
        description = "HTTP GET через VPN, затем TCP и ICMP",
    ),
    TCP(
        title = "TCP",
        shortTitle = "TCP",
        description = "Доступность адреса и VPN-порта",
    ),
    HTTP_GET(
        title = "HTTP GET",
        shortTitle = "GET",
        description = "Полный запрос через выбранный Xray-сервер",
    ),
    HTTP_HEAD(
        title = "HTTP HEAD",
        shortTitle = "HEAD",
        description = "Запрос только заголовков через Xray-сервер",
    ),
    ICMP(
        title = "ICMP",
        shortTitle = "ICMP",
        description = "Системный echo-запрос до адреса сервера",
    ),
}


data class WayPingResult(
    val latencyMs: Long,
    val method: WayPingMode,
    val detail: String,
    val usableForAutoSelect: Boolean = latencyMs >= 0,
) {
    val isSuccess: Boolean
        get() = latencyMs >= 0

    companion object {
        fun failure(method: WayPingMode, detail: String) =
            WayPingResult(-1L, method, detail, usableForAutoSelect = false)
    }
}


class WayPingManager(context: Context) {
    companion object {
        private val HTTP_PROBE_URLS = listOf(
            "https://cp.cloudflare.com/generate_204",
            "https://www.gstatic.com/generate_204",
        )
        private val ICMP_TIME_PATTERN =
            Regex("""(?:time[=<])\s*([0-9]+(?:\.[0-9]+)?)\s*ms""", RegexOption.IGNORE_CASE)

        internal fun parseIcmpLatency(output: String): Long? =
            ICMP_TIME_PATTERN.find(output)
                ?.groupValues
                ?.getOrNull(1)
                ?.toDoubleOrNull()
                ?.roundToLong()

        internal fun bestSuccessfulLatency(samples: Iterable<Long>): Long =
            samples.filter { it >= 0 }.minOrNull() ?: -1L
    }

    private val appContext = context.applicationContext

    suspend fun measure(
        guid: String,
        mode: WayPingMode,
        onStage: suspend (String) -> Unit = {},
    ): WayPingResult = withContext(Dispatchers.IO) {
        when (mode) {
            WayPingMode.AUTO -> measureAuto(guid, onStage)
            WayPingMode.TCP -> {
                onStage("TCP: подключение к порту…")
                measureTcp(guid)
            }
            WayPingMode.HTTP_GET -> {
                onStage("GET: запуск тестового туннеля…")
                measureHttp(guid, "GET", WayPingMode.HTTP_GET, onStage)
            }
            WayPingMode.HTTP_HEAD -> {
                onStage("HEAD: запуск тестового туннеля…")
                measureHttp(guid, "HEAD", WayPingMode.HTTP_HEAD, onStage)
            }
            WayPingMode.ICMP -> {
                onStage("ICMP: echo-запрос…")
                measureIcmp(guid)
            }
        }
    }

    private suspend fun measureAuto(
        guid: String,
        onStage: suspend (String) -> Unit,
    ): WayPingResult {
        onStage("Авто: проверяем VPN через GET…")
        val http = measureHttp(guid, "GET", WayPingMode.HTTP_GET, onStage)
        if (http.isSuccess) {
            return http.copy(detail = "GET через VPN")
        }

        onStage("Авто: GET не ответил, проверяем TCP…")
        val tcp = measureTcp(guid)
        if (tcp.isSuccess) {
            return tcp.copy(detail = "TCP доступен · GET не ответил")
        }

        onStage("Авто: проверяем ICMP…")
        val icmp = measureIcmp(guid)
        if (icmp.isSuccess) {
            return icmp.copy(
                detail = "ICMP доступен · VPN-порт не подтверждён",
                usableForAutoSelect = false,
            )
        }
        return WayPingResult.failure(WayPingMode.AUTO, "GET, TCP и ICMP без ответа")
    }

    private fun measureTcp(guid: String): WayPingResult {
        val config = MmkvManager.decodeServerConfig(guid)
            ?: return WayPingResult.failure(WayPingMode.TCP, "конфигурация недоступна")
        val port = config.serverPort?.toIntOrNull()
            ?: return WayPingResult.failure(WayPingMode.TCP, "не указан порт")
        val host = config.server.orEmpty()
        if (host.isBlank()) return WayPingResult.failure(WayPingMode.TCP, "не указан адрес")

        val latency = SpeedtestManager.socketConnectTime(host, port, timeoutMs = 1_800, attempts = 2)
        return if (latency >= 0) {
            WayPingResult(latency, WayPingMode.TCP, "TCP-порт доступен")
        } else {
            WayPingResult.failure(WayPingMode.TCP, "TCP-порт не ответил")
        }
    }

    private suspend fun measureHttp(
        guid: String,
        method: String,
        mode: WayPingMode,
        onStage: suspend (String) -> Unit,
    ): WayPingResult {
        val configResult = CoreConfigManager.getV2rayConfig4Speedtest(appContext, guid)
        if (!configResult.status || configResult.content.isBlank()) {
            return WayPingResult.failure(mode, "не удалось собрать Xray-конфигурацию")
        }

        val localPort = runCatching { Utils.findRandomFreePort() }.getOrElse {
            return WayPingResult.failure(mode, "не удалось открыть тестовый порт")
        }
        val probeConfig = buildProbeConfig(configResult.content, localPort)
            ?: return WayPingResult.failure(mode, "некорректная Xray-конфигурация")

        CoreNativeManager.initCoreEnv(appContext)
        val controller = CoreNativeManager.newCoreController(NoopCoreCallback)
        return try {
            controller.startLoop(probeConfig, 0)
            if (!controller.isRunning) {
                WayPingResult.failure(mode, "тестовый Xray-туннель не запустился")
            } else {
                onStage("${mode.shortTitle}: запрос через сервер…")
                val samples = executeHttpRequests(localPort, method)
                val latency = bestSuccessfulLatency(samples)
                if (latency >= 0) {
                    WayPingResult(latency, mode, "${mode.shortTitle} через VPN")
                } else {
                    WayPingResult.failure(mode, "${mode.shortTitle} через VPN не ответил")
                }
            }
        } catch (_: Exception) {
            WayPingResult.failure(mode, "${mode.shortTitle} через VPN не ответил")
        } finally {
            runCatching { controller.stopLoop() }
        }
    }

    private fun buildProbeConfig(content: String, localPort: Int): String? = runCatching {
        val root = JsonParser.parseString(content).asJsonObject.deepCopy()
        val inbound = JsonObject().apply {
            addProperty("tag", "way-ping-in")
            addProperty("listen", "127.0.0.1")
            addProperty("port", localPort)
            addProperty("protocol", "socks")
            add("settings", JsonObject().apply {
                addProperty("auth", "noauth")
                addProperty("udp", false)
            })
        }
        root.add("inbounds", JsonArray().apply { add(inbound) })
        // API/metrics listeners may contain fixed ports and conflict between parallel tests.
        // Routing, balancers and observatories stay intact because a custom profile may
        // depend on them to reach its primary outbound.
        listOf("api", "metrics").forEach(root::remove)
        JsonUtil.toJson(root)
    }.getOrNull()

    private fun executeHttpRequests(localPort: Int, method: String): List<Long> {
        val client = OkHttpClient.Builder()
            .proxy(Proxy(Proxy.Type.SOCKS, InetSocketAddress("127.0.0.1", localPort)))
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(6, TimeUnit.SECONDS)
            .callTimeout(8, TimeUnit.SECONDS)
            .followRedirects(false)
            .build()
        val samples = mutableListOf<Long>()

        for (url in HTTP_PROBE_URLS) {
            repeat(2) {
                val request = Request.Builder()
                    .url(url)
                    .method(method, null)
                    .header("Connection", "close")
                    .build()
                val startedAt = SystemClock.elapsedRealtimeNanos()
                val latency = runCatching {
                    client.newCall(request).execute().use { response ->
                        if (response.code !in 200..299) return@use -1L
                        if (method == "GET") {
                            response.body.byteStream().use { stream ->
                                val buffer = ByteArray(4_096)
                                var readTotal = 0
                                while (readTotal < 65_536) {
                                    val read = stream.read(buffer)
                                    if (read < 0) break
                                    readTotal += read
                                }
                            }
                        }
                        (SystemClock.elapsedRealtimeNanos() - startedAt) / 1_000_000L
                    }
                }.getOrDefault(-1L)
                samples += latency
                if (latency < 0) return@repeat
            }
            if (samples.any { it >= 0 }) break
        }
        return samples
    }

    private fun measureIcmp(guid: String): WayPingResult {
        val host = MmkvManager.decodeServerConfig(guid)?.server.orEmpty()
        if (host.isBlank()) return WayPingResult.failure(WayPingMode.ICMP, "не указан адрес")

        val processLatency = runCatching {
            val process = ProcessBuilder(
                "/system/bin/ping",
                "-c", "2",
                "-W", "2",
                host,
            ).redirectErrorStream(true).start()
            if (!process.waitFor(6, TimeUnit.SECONDS)) {
                process.destroyForcibly()
                null
            } else {
                parseIcmpLatency(process.inputStream.bufferedReader().use { it.readText() })
            }
        }.getOrNull()
        if (processLatency != null) {
            return WayPingResult(processLatency, WayPingMode.ICMP, "ICMP echo")
        }

        val fallback = runCatching {
            val address = InetAddress.getAllByName(host).firstOrNull() ?: return@runCatching -1L
            val startedAt = SystemClock.elapsedRealtimeNanos()
            if (address.isReachable(2_500)) {
                (SystemClock.elapsedRealtimeNanos() - startedAt) / 1_000_000L
            } else {
                -1L
            }
        }.getOrDefault(-1L)
        return if (fallback >= 0) {
            WayPingResult(fallback, WayPingMode.ICMP, "ICMP/системная проверка")
        } else {
            WayPingResult.failure(WayPingMode.ICMP, "ICMP заблокирован или нет ответа")
        }
    }

    private object NoopCoreCallback : CoreCallbackHandler {
        override fun startup(): Long = 0
        override fun shutdown(): Long = 0
        override fun onEmitStatus(l: Long, s: String?): Long = 0
    }
}
