package com.v2ray.ang.way

import android.os.Build
import com.google.gson.Gson
import com.v2ray.ang.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.EOFException
import java.io.IOException
import java.security.MessageDigest
import java.time.Instant
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLException


data class ChallengeResponse(
    val challenge_id: String,
    val telegram_url: String,
    val expires_at: String,
    val poll_interval_seconds: Int,
)

data class TokenResponse(
    val session_id: String,
    val access_token: String,
    val refresh_token: String,
    val expires_in: Int,
)

data class AccessKeyResponse(val access_key: String)

data class MeResponse(val tg_id: Long, val username: String?, val auth_scope: String? = null)

data class TrafficDto(
    val enabled: Boolean,
    val used_bytes: Long,
    val limit_bytes: Long,
    val reset_at: String?,
)

data class DevicesSummaryDto(val limit: Int, val max_limit: Int)

data class SubscriptionDto(
    val id: Long,
    val title: String,
    val plan_kind: String,
    val type_index: Int,
    val status: String,
    val subscription_until: String?,
    val offline_allowed_until: String?,
    val traffic: TrafficDto,
    val devices: DevicesSummaryDto,
)

data class SubscriptionsResponse(val subscriptions: List<SubscriptionDto>)

data class DeviceDto(
    val hwid: String,
    val platform: String?,
    val os_version: String?,
    val device_model: String?,
    val user_agent: String?,
)

data class DevicesResponse(val devices: List<DeviceDto>)
data class PaymentResponse(val invoice_id: String, val pay_url: String, val provider: String, val amount: Double)
data class PaymentStatusResponse(val invoice_id: String, val status: String)
data class UpdateManifest(
    val versionCode: Int,
    val versionName: String,
    val minSupportedVersionCode: Int,
    val apkUrl: String,
    val sha256: String,
    val signingCertSha256: String,
    val releaseNotes: List<String>,
)

data class ProfileResponse(val content: String, val expiresAt: String?)

object SubscriptionResponseHeaders {
    private val expirePattern = Regex("(?:^|;)\\s*expire=(\\d+)(?:;|$)", RegexOption.IGNORE_CASE)

    fun expiryIso(subscriptionUserInfo: String?): String? {
        val epoch = expirePattern.find(subscriptionUserInfo.orEmpty())
            ?.groupValues?.getOrNull(1)?.toLongOrNull()
            ?.takeIf { it > 0 }
            ?: return null
        return runCatching { Instant.ofEpochSecond(epoch).toString() }.getOrNull()
    }
}

class WayApiException(val status: Int, val errorCode: String, override val message: String) : Exception(message)

private class HwidHeadersNotSupportedException : IOException()

class WayApi {
    private val gson = Gson()
    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .followRedirects(false)
        .followSslRedirects(false)
        .build()
    private val subscriptionClient = client.newBuilder()
        .followRedirects(true)
        .followSslRedirects(false)
        // Некоторые subscription-прокси преждевременно закрывают HTTP/2 или
        // gzip-поток. Для небольшого текстового профиля HTTP/1.1 надёжнее.
        .protocols(listOf(Protocol.HTTP_1_1))
        .retryOnConnectionFailure(true)
        .build()
    private val apiBase = BuildConfig.WAY_API_BASE_URL.trimEnd('/').also {
        require(it.startsWith("https://")) { "Way VPN API must use HTTPS" }
    }

    private fun requestBuilder(path: String, accessToken: String? = null): Request.Builder =
        Request.Builder()
            .url("$apiBase/mobile/api/v1$path")
            .header("Accept", "application/json")
            .header("User-Agent", "WayVPN/${BuildConfig.VERSION_NAME} Android/${Build.VERSION.RELEASE}")
            .apply { if (!accessToken.isNullOrBlank()) header("Authorization", "Bearer $accessToken") }

    private suspend fun <T> executeJson(request: Request, clazz: Class<T>): T = withContext(Dispatchers.IO) {
        client.newCall(request).execute().use { response ->
            val body = response.body.string()
            if (!response.isSuccessful) {
                val code = Regex("\\\"code\\\"\\s*:\\s*\\\"([^\\\"]+)\\\"").find(body)?.groupValues?.get(1) ?: "http_${response.code}"
                val message = Regex("\\\"message\\\"\\s*:\\s*\\\"([^\\\"]+)\\\"").find(body)?.groupValues?.get(1) ?: "Ошибка сервера (${response.code})"
                throw WayApiException(response.code, code, message)
            }
            gson.fromJson(body, clazz)
        }
    }

    private fun jsonBody(value: Any) = gson.toJson(value).toRequestBody(jsonMediaType)

    suspend fun createChallenge(codeChallenge: String): ChallengeResponse = executeJson(
        requestBuilder("/auth/challenges").post(jsonBody(mapOf("code_challenge" to codeChallenge, "device_name" to "${Build.MANUFACTURER} ${Build.MODEL}"))).build(),
        ChallengeResponse::class.java,
    )

    suspend fun exchange(challengeId: String, verifier: String): TokenResponse = executeJson(
        requestBuilder("/auth/exchange").post(jsonBody(mapOf("challenge_id" to challengeId, "code_verifier" to verifier))).build(),
        TokenResponse::class.java,
    )

    suspend fun exchangeAccessKey(accessKey: String): TokenResponse = executeJson(
        requestBuilder("/auth/key-exchange").post(jsonBody(mapOf(
            "access_key" to accessKey,
            "device_name" to "${Build.MANUFACTURER} ${Build.MODEL}",
        ))).build(),
        TokenResponse::class.java,
    )

    suspend fun issueAccessKey(accessToken: String): AccessKeyResponse = executeJson(
        requestBuilder("/auth/access-key", accessToken).post(ByteArray(0).toRequestBody(null)).build(),
        AccessKeyResponse::class.java,
    )

    suspend fun refresh(refreshToken: String): TokenResponse = executeJson(
        requestBuilder("/auth/refresh").post(jsonBody(mapOf("refresh_token" to refreshToken))).build(),
        TokenResponse::class.java,
    )

    suspend fun logout(accessToken: String) = executeJson(
        requestBuilder("/auth/logout", accessToken).post(ByteArray(0).toRequestBody(null)).build(),
        Map::class.java,
    )

    suspend fun me(accessToken: String): MeResponse = executeJson(
        requestBuilder("/me", accessToken).get().build(), MeResponse::class.java,
    )

    suspend fun subscriptions(accessToken: String): SubscriptionsResponse = executeJson(
        requestBuilder("/subscriptions", accessToken).get().build(), SubscriptionsResponse::class.java,
    )

    suspend fun profile(accessToken: String, subscriptionId: Long, hwid: String): ProfileResponse = withContext(Dispatchers.IO) {
        val request = requestBuilder("/subscriptions/$subscriptionId/profile", accessToken)
            .post(jsonBody(mapOf(
                "hwid" to hwid,
                "device_os" to "Android",
                "os_version" to Build.VERSION.RELEASE,
                "device_model" to "${Build.MANUFACTURER} ${Build.MODEL}",
                "user_agent" to "WayVPN/${BuildConfig.VERSION_NAME}",
            )))
            .build()
        client.newCall(request).execute().use { response ->
            val content = response.body.string()
            if (!response.isSuccessful) {
                val code = Regex("\\\"code\\\"\\s*:\\s*\\\"([^\\\"]+)\\\"").find(content)?.groupValues?.get(1) ?: "profile_${response.code}"
                throw WayApiException(response.code, code, "Не удалось получить профиль")
            }
            ProfileResponse(content, response.header("X-Way-Subscription-Expires-At"))
        }
    }

    private fun directSubscriptionRequest(subscriptionUrl: String, hwid: String, includeDeviceHeaders: Boolean): Request {
        val builder = Request.Builder()
            .url(AccountAccessKey.normalize(subscriptionUrl))
            .header("Accept", "text/plain, application/octet-stream;q=0.9, */*;q=0.1")
            .header("Accept-Encoding", "identity")
            .header("Cache-Control", "no-cache")
            .header("Connection", "close")
            .header("User-Agent", "WayVPN/${BuildConfig.VERSION_NAME} Android/${Build.VERSION.RELEASE}")
            .get()
        if (includeDeviceHeaders) {
            builder
                .header("x-hwid", hwid)
                .header("x-device-os", "Android")
                .header("x-ver-os", Build.VERSION.RELEASE)
                .header("x-device-model", "${Build.MANUFACTURER} ${Build.MODEL}".take(120))
        }
        return builder.build()
    }

    private fun hasPrematureEof(error: Throwable): Boolean {
        var current: Throwable? = error
        while (current != null) {
            if (current is EOFException) return true
            current = current.cause
        }
        return false
    }

    private fun fetchDirectSubscription(
        subscriptionUrl: String,
        hwid: String,
        includeDeviceHeaders: Boolean,
    ): ProfileResponse {
        subscriptionClient
            .newCall(directSubscriptionRequest(subscriptionUrl, hwid, includeDeviceHeaders))
            .execute()
            .use { response ->
                if (!response.request.url.isHttps) {
                    throw WayApiException(
                        400,
                        "insecure_subscription_redirect",
                        "Сервер подписки перенаправил на небезопасный адрес",
                    )
                }
                if (!response.isSuccessful && response.header("X-Hwid-Not-Supported") != null) {
                    throw HwidHeadersNotSupportedException()
                }
                if (!response.isSuccessful) {
                    if (response.header("X-Hwid-Max-Devices-Reached") != null) {
                        throw WayApiException(
                            409,
                            "hwid_limit_reached",
                            "Достигнут лимит устройств этой подписки",
                        )
                    }
                    val message = when (response.code) {
                        401, 403 -> "Ссылка подписки отклонена сервером или истекла"
                        404 -> "Ссылка подписки не найдена на сервере"
                        else -> "Сервер подписки вернул HTTP ${response.code}"
                    }
                    throw WayApiException(
                        response.code,
                        "subscription_http_${response.code}",
                        message,
                    )
                }
                val body = response.body
                val maxBytes = 4L * 1024L * 1024L
                if (body.contentLength() > maxBytes) {
                    throw WayApiException(
                        413,
                        "subscription_too_large",
                        "Профиль подписки слишком большой",
                    )
                }
                val bytes = body.source().readByteArray(maxBytes + 1)
                if (bytes.size > maxBytes) {
                    throw WayApiException(
                        413,
                        "subscription_too_large",
                        "Профиль подписки слишком большой",
                    )
                }
                val expiry = response.header("X-Way-Subscription-Expires-At")
                    ?.takeIf(String::isNotBlank)
                    ?: SubscriptionResponseHeaders.expiryIso(
                        response.header("Subscription-Userinfo")
                            ?: response.header("X-Subscription-Userinfo")
                    )
                return ProfileResponse(bytes.toString(Charsets.UTF_8), expiry)
            }
    }

    private fun fetchDirectSubscriptionWithRetries(
        subscriptionUrl: String,
        hwid: String,
    ): ProfileResponse {
        var includeDeviceHeaders = true
        var eofRetries = 0
        while (true) {
            try {
                return fetchDirectSubscription(subscriptionUrl, hwid, includeDeviceHeaders)
            } catch (_: HwidHeadersNotSupportedException) {
                if (!includeDeviceHeaders) {
                    throw WayApiException(
                        400,
                        "subscription_device_headers_rejected",
                        "Сервер подписки отклонил запрос устройства",
                    )
                }
                includeDeviceHeaders = false
                eofRetries = 0
            } catch (error: IOException) {
                if (!hasPrematureEof(error)) throw error
                if (eofRetries >= 2) {
                    throw WayApiException(
                        0,
                        "subscription_truncated_response",
                        "Сервер подписки преждевременно закрыл ответ",
                    )
                }
                eofRetries += 1
            }
        }
    }

    suspend fun directSubscriptionProfile(subscriptionUrl: String, hwid: String): ProfileResponse = withContext(Dispatchers.IO) {
        if (!AccountAccessKey.isSubscriptionUrl(subscriptionUrl)) {
            throw WayApiException(400, "invalid_subscription_url", "Некорректная HTTPS-ссылка подписки")
        }
        try {
            fetchDirectSubscriptionWithRetries(subscriptionUrl, hwid)
        } catch (error: WayApiException) {
            throw error
        } catch (error: UnknownHostException) {
            throw WayApiException(0, "subscription_host_not_found", "Не удалось найти сервер подписки")
        } catch (error: SocketTimeoutException) {
            throw WayApiException(0, "subscription_timeout", "Сервер подписки не ответил вовремя")
        } catch (error: SSLException) {
            throw WayApiException(0, "subscription_tls_error", "HTTPS-сертификат сервера подписки не прошёл проверку")
        }
    }

    suspend fun devices(accessToken: String, subscriptionId: Long): DevicesResponse = executeJson(
        requestBuilder("/subscriptions/$subscriptionId/devices", accessToken).get().build(), DevicesResponse::class.java,
    )

    suspend fun deleteDevice(accessToken: String, subscriptionId: Long, hwid: String) = executeJson(
        requestBuilder("/subscriptions/$subscriptionId/devices/$hwid", accessToken).delete().build(), Map::class.java,
    )

    suspend fun createPayment(accessToken: String, body: Map<String, Any?>): PaymentResponse = executeJson(
        requestBuilder("/payments/subscription", accessToken).post(jsonBody(body)).build(), PaymentResponse::class.java,
    )

    suspend fun paymentStatus(accessToken: String, invoiceId: String): PaymentStatusResponse = executeJson(
        requestBuilder("/payments/$invoiceId", accessToken).get().build(), PaymentStatusResponse::class.java,
    )

    suspend fun updateManifest(): UpdateManifest = withContext(Dispatchers.IO) {
        val url = BuildConfig.WAY_UPDATE_MANIFEST_URL.also { require(it.startsWith("https://")) }
        client.newCall(Request.Builder().url(url).get().build()).execute().use { response ->
            if (!response.isSuccessful) throw WayApiException(response.code, "update_manifest", "Манифест обновления недоступен")
            gson.fromJson(response.body.string(), UpdateManifest::class.java)
        }
    }

    companion object {
        fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.US_ASCII))
            .let { digest -> java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(digest) }
    }
}
