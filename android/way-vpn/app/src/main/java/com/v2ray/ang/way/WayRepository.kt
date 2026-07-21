package com.v2ray.ang.way

import android.content.Context
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.security.SecureRandom


data class LoginRequest(val challengeId: String, val verifier: String, val telegramUrl: String, val pollSeconds: Int)

class WayRepository(context: Context) {
    private val api = WayApi()
    val secureStore = SecureStore(context.applicationContext)
    private val refreshMutex = Mutex()

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
        return try {
            val tokens = api.exchange(request.challengeId, request.verifier)
            saveTokens(tokens)
            secureStore.put(SecureStore.PENDING_CHALLENGE, null)
            secureStore.put(SecureStore.PENDING_VERIFIER, null)
            true
        } catch (error: WayApiException) {
            if (error.errorCode == "authorization_pending") false else throw error
        }
    }

    suspend fun loginWithAccessKey(value: String) {
        val normalized = AccountAccessKey.normalize(value)
        require(AccountAccessKey.isValid(normalized)) { "Некорректный ключ доступа Way VPN" }
        val tokens = api.exchangeAccessKey(normalized)
        saveTokens(tokens)
        secureStore.put(SecureStore.ACCOUNT_ACCESS_KEY, normalized)
        secureStore.put(SecureStore.PENDING_CHALLENGE, null)
        secureStore.put(SecureStore.PENDING_VERIFIER, null)
    }

    suspend fun accountAccessKey(): String? = secureStore.get(SecureStore.ACCOUNT_ACCESS_KEY)

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

    suspend fun me(): MeResponse = authorized(api::me)
    suspend fun subscriptions(): List<SubscriptionDto> = authorized { api.subscriptions(it).subscriptions }
    suspend fun profile(subscriptionId: Long): ProfileResponse = authorized {
        api.profile(it, subscriptionId, secureStore.installationHwid())
    }
    suspend fun devices(subscriptionId: Long): List<DeviceDto> = authorized { api.devices(it, subscriptionId).devices }
    suspend fun deleteDevice(subscriptionId: Long, hwid: String) = authorized { api.deleteDevice(it, subscriptionId, hwid) }
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
