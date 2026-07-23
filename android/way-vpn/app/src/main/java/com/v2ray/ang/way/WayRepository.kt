package com.v2ray.ang.way

import android.content.Context
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.security.SecureRandom
import java.time.Instant
import java.time.temporal.ChronoUnit


data class LoginRequest(val challengeId: String, val verifier: String, val telegramUrl: String, val pollSeconds: Int)

class WayRepository(context: Context) {
    private val api = WayApi()
    val secureStore = SecureStore(context.applicationContext)
    private val refreshMutex = Mutex()
    private val loginExchangeMutex = Mutex()

    private fun onlineOnlyFallbackExpiry(): String = Instant.now().plus(24, ChronoUnit.HOURS).toString()

    private fun verifier(): String = ByteArray(48).also(SecureRandom()::nextBytes).let {
        java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(it)
    }

    suspend fun beginLogin(): LoginRequest {
        val verifier = verifier()
        val challenge = api.createChallenge(WayApi.sha256(verifier))
        secureStore.put(SecureStore.PENDING_CHALLENGE, challenge.challenge_id)
        secureStore.put(SecureStore.PENDING_VERIFIER, verifier)
        return LoginRequest(challenge.challenge_id, verifier, challenge.telegram_url, challenge.poll_interval_seconds)
    }

    suspend fun pendingLogin(): LoginRequest? {
        val challenge = secureStore.get(SecureStore.PENDING_CHALLENGE) ?: return null
        val verifier = secureStore.get(SecureStore.PENDING_VERIFIER) ?: return null
        return LoginRequest(challenge, verifier, "", 2)
    }

    suspend fun finishLogin(request: LoginRequest): Boolean {
        return loginExchangeMutex.withLock {
            if (accessToken() != null) {
                secureStore.put(SecureStore.PENDING_CHALLENGE, null)
                secureStore.put(SecureStore.PENDING_VERIFIER, null)
                return@withLock true
            }
            withContext(NonCancellable) {
                try {
                    val tokens = api.exchange(request.challengeId, request.verifier)
                    saveTokens(tokens)
                    secureStore.put(SecureStore.PENDING_CHALLENGE, null)
                    secureStore.put(SecureStore.PENDING_VERIFIER, null)
                    true
                } catch (error: WayApiException) {
                    if (error.errorCode == "authorization_pending") false else throw error
                }
            }
        }
    }

    suspend fun loginWithAccessKey(value: String) {
        val normalized = AccountAccessKey.normalize(value)
        if (AccountAccessKey.isSubscriptionUrl(normalized)) {
            addSubscriptionUrl(normalized)
            return
        }
        val tokens = api.exchangeAccessKey(normalized)
        saveTokens(tokens)
        secureStore.put(SecureStore.ACCOUNT_ACCESS_KEY, normalized)
        secureStore.put(SecureStore.PENDING_CHALLENGE, null)
        secureStore.put(SecureStore.PENDING_VERIFIER, null)
    }

    suspend fun addSubscriptionUrl(value: String) {
        val normalized = AccountAccessKey.normalize(value)
        if (!AccountAccessKey.isSubscriptionUrl(normalized)) {
            throw IllegalArgumentException("Нужна полная HTTPS-ссылка VPN-подписки")
        }
        secureStore.put(SecureStore.ACCESS_TOKEN, null)
        secureStore.put(SecureStore.REFRESH_TOKEN, null)
        secureStore.put(SecureStore.ACCOUNT_ACCESS_KEY, normalized)
        secureStore.put(SecureStore.LAST_PROFILE, null)
        secureStore.put(SecureStore.PROFILE_EXPIRES_AT, null)
        secureStore.put(SecureStore.SELECTED_SUBSCRIPTION, null)
        secureStore.put(SecureStore.SELECTED_SERVER, null)
        secureStore.put(SecureStore.LAST_PAYMENT, null)
        secureStore.put(SecureStore.PENDING_CHALLENGE, null)
        secureStore.put(SecureStore.PENDING_VERIFIER, null)
    }

    suspend fun accountAccessKey(): String? = secureStore.get(SecureStore.ACCOUNT_ACCESS_KEY)

    suspend fun isDirectSubscription(): Boolean = accountAccessKey()?.let(AccountAccessKey::isSubscriptionUrl) == true

    suspend fun hasLogin(): Boolean =
        accessToken() != null || secureStore.get(SecureStore.REFRESH_TOKEN) != null || isDirectSubscription()

    suspend fun ensureAccountAccessKey(): String {
        accountAccessKey()?.takeIf(AccountAccessKey::isValid)?.let { return AccountAccessKey.normalize(it) }
        return rotateAccountAccessKey()
    }

    suspend fun rotateAccountAccessKey(): String = authorized { token ->
        api.issueAccessKey(token).access_key.also {
            secureStore.put(SecureStore.ACCOUNT_ACCESS_KEY, it)
        }
    }

    private suspend fun saveTokens(tokens: TokenResponse) {
        secureStore.put(SecureStore.ACCESS_TOKEN, tokens.access_token)
        secureStore.put(SecureStore.REFRESH_TOKEN, tokens.refresh_token)
    }

    suspend fun accessToken(): String? = secureStore.get(SecureStore.ACCESS_TOKEN)

    private suspend fun refreshedAccessToken(): String = refreshMutex.withLock {
        val refresh = secureStore.get(SecureStore.REFRESH_TOKEN)
            ?: throw WayApiException(401, "not_authenticated", "Требуется вход")
        val tokens = api.refresh(refresh)
        saveTokens(tokens)
        tokens.access_token
    }

    private suspend fun <T> authorized(block: suspend (String) -> T): T {
        val token = accessToken() ?: refreshedAccessToken()
        return try {
            block(token)
        } catch (error: WayApiException) {
            if (error.status != 401) throw error
            block(refreshedAccessToken())
        }
    }

    suspend fun me(): MeResponse = if (isDirectSubscription()) {
        MeResponse(0, null, "direct_subscription")
    } else authorized(api::me)

    suspend fun subscriptions(): List<SubscriptionDto> = if (isDirectSubscription()) {
        val expiresAt = secureStore.get(SecureStore.PROFILE_EXPIRES_AT)
        val active = WayRuntimePolicy.parseExpiry(expiresAt)?.let(WayRuntimePolicy::isProfileUsable) ?: true
        listOf(
            SubscriptionDto(
                id = 0,
                title = "Внешняя подписка",
                plan_kind = "external",
                type_index = 1,
                status = if (active) "active" else "expired",
                subscription_until = expiresAt,
                offline_allowed_until = expiresAt,
                traffic = TrafficDto(false, 0, 0, null),
                devices = DevicesSummaryDto(0, 0),
            )
        )
    } else authorized { api.subscriptions(it).subscriptions }

    suspend fun profile(subscriptionId: Long): ProfileResponse = accountAccessKey()
        ?.takeIf(AccountAccessKey::isSubscriptionUrl)
        ?.let {
            api.directSubscriptionProfile(it, secureStore.installationHwid()).let { response ->
                response.copy(expiresAt = response.expiresAt ?: onlineOnlyFallbackExpiry())
            }
        }
        ?: authorized { api.profile(it, subscriptionId, secureStore.installationHwid()) }

    suspend fun devices(subscriptionId: Long): List<DeviceDto> {
        if (isDirectSubscription()) throw WayApiException(403, "external_subscription", "Устройствами управляет сервер этой подписки")
        return authorized { api.devices(it, subscriptionId).devices }
    }

    suspend fun deleteDevice(subscriptionId: Long, hwid: String) {
        if (isDirectSubscription()) throw WayApiException(403, "external_subscription", "Устройствами управляет сервер этой подписки")
        authorized { api.deleteDevice(it, subscriptionId, hwid) }
    }
    suspend fun createSubscriptionPayment(tariffCode: String, provider: String, subscriptionId: Long?): PaymentResponse = authorized {
        api.createPayment(it, mapOf(
            "tariff_code" to tariffCode,
            "provider" to provider,
            "payment_target" to if (subscriptionId == null) "new" else "renew",
            "subscription_id" to subscriptionId,
        ))
    }
    suspend fun paymentStatus(invoiceId: String): PaymentStatusResponse = authorized { api.paymentStatus(it, invoiceId) }
    suspend fun updateManifest(): UpdateManifest = api.updateManifest()

    suspend fun logout() {
        accessToken()?.let { runCatching { api.logout(it) } }
        secureStore.clearAll()
    }
}
