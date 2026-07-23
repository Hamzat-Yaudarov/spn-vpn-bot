package com.v2ray.ang.handler

import com.v2ray.ang.AppConfig
import com.v2ray.ang.dto.IPAPIInfo
import com.v2ray.ang.dto.UrlContentRequest
import com.v2ray.ang.util.HttpUtil
import com.v2ray.ang.util.JsonUtil
import com.v2ray.ang.util.LogUtil
import java.io.IOException
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.UnknownHostException

object SpeedtestManager {

    /**
     * Measures the time taken to establish a TCP connection to a given URL and port.
     *
     * @param url The URL to connect to.
     * @param port The port to connect to.
     * @return The connection time in milliseconds, or -1 if the connection failed.
     */
    fun socketConnectTime(
        url: String,
        port: Int,
        timeoutMs: Int = 1500,
        attempts: Int = 2,
    ): Long {
        try {
            val addresses = InetAddress.getAllByName(url)
                .distinctBy(InetAddress::getHostAddress)
                .sortedBy { address -> if (address is Inet4Address) 0 else 1 }
                .take(4)
            if (addresses.isEmpty()) return -1

            repeat(attempts.coerceIn(1, 3)) {
                val successful = mutableListOf<Long>()
                addresses.forEach { address ->
                    val latency = connectAddress(address, port, timeoutMs)
                    if (latency >= 0) successful += latency
                }
                successful.minOrNull()?.let { return it }
            }
        } catch (e: UnknownHostException) {
            LogUtil.e(AppConfig.TAG, "Unknown host: $url", e)
        } catch (e: Exception) {
            LogUtil.e(AppConfig.TAG, "Failed to establish socket connection to $url:$port", e)
        }
        return -1
    }

    private fun connectAddress(address: InetAddress, port: Int, timeoutMs: Int): Long {
        val startedAt = System.nanoTime()
        return try {
            Socket().use { socket ->
                socket.tcpNoDelay = true
                socket.connect(InetSocketAddress(address, port), timeoutMs)
            }
            (System.nanoTime() - startedAt) / 1_000_000L
        } catch (_: IOException) {
            -1L
        } catch (_: SecurityException) {
            -1L
        }
    }

    fun getRemoteIPInfo(): String? {
        val url = MmkvManager.decodeSettingsString(AppConfig.PREF_IP_API_URL)
            .takeIf { !it.isNullOrBlank() } ?: AppConfig.IP_API_URL

        val proxyUsername = SettingsManager.getSocksUsername()
        val proxyPassword = SettingsManager.getSocksPassword()
        val httpPort = SettingsManager.getHttpPort()
        if (httpPort == 0) return null
        val content = HttpUtil.getUrlContent(
            UrlContentRequest(
                url = url,
                timeout = 5000,
                httpPort = httpPort,
                proxyUsername = proxyUsername,
                proxyPassword = proxyPassword
            )
        ) ?: return null
        val ipInfo = JsonUtil.fromJsonSafe(content, IPAPIInfo::class.java) ?: return null

        val ip = listOf(
            ipInfo.ip,
            ipInfo.clientIp,
            ipInfo.ip_addr,
            ipInfo.query
        ).firstOrNull { !it.isNullOrBlank() }

        val country = listOf(
            ipInfo.country_code,
            ipInfo.country,
            ipInfo.countryCode,
            ipInfo.location?.country_code
        ).firstOrNull { !it.isNullOrBlank() }

        return "(${country ?: "unknown"}) ${ip ?: "unknown"}"
    }
}
