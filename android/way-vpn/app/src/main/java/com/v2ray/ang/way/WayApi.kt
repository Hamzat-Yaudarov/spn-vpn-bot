package com.v2ray.ang.way

import android.os.Build
import com.google.gson.Gson
import com.v2ray.ang.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.security.MessageDigest
import java.util.concurrent.TimeUnit


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

data class MeResponse(val tg_id: Long, val username: String?)

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

class WayApiException(val status: Int, val errorCode: String, override val message: String) : Exception(message)

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
