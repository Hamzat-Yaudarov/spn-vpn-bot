package com.v2ray.ang.way

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import java.security.KeyStore
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec


private val Context.waySecureDataStore by preferencesDataStore(name = "way_secure_store")

class SecureStore(private val context: Context) {
    companion object {
        const val REFRESH_TOKEN = "refresh_token"
        const val ACCESS_TOKEN = "access_token"
        const val HWID = "installation_hwid"
        const val LAST_PROFILE = "last_profile"
        const val PROFILE_EXPIRES_AT = "profile_expires_at"
        const val PENDING_CHALLENGE = "pending_challenge"
        const val PENDING_VERIFIER = "pending_verifier"
        const val SELECTED_SUBSCRIPTION = "selected_subscription"
        const val SELECTED_SERVER = "selected_server"
        const val LAST_PAYMENT = "last_payment"
        private const val KEY_ALIAS = "way_vpn_secure_store_v1"
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val GCM_TAG_BITS = 128
        private const val IV_BYTES = 12
    }

    private fun preference(name: String): Preferences.Key<String> = stringPreferencesKey(name)

    private fun secretKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .setKeySize(256)
                .build()
        )
        return generator.generateKey()
    }

    private fun encrypt(name: String, value: String): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        cipher.updateAAD(name.toByteArray(Charsets.UTF_8))
        val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        return Base64.encodeToString(cipher.iv + encrypted, Base64.NO_WRAP)
    }

    private fun decrypt(name: String, encoded: String): String? = runCatching {
        val bytes = Base64.decode(encoded, Base64.NO_WRAP)
        require(bytes.size > IV_BYTES)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(GCM_TAG_BITS, bytes.copyOfRange(0, IV_BYTES)))
        cipher.updateAAD(name.toByteArray(Charsets.UTF_8))
        cipher.doFinal(bytes.copyOfRange(IV_BYTES, bytes.size)).toString(Charsets.UTF_8)
    }.getOrNull()

    suspend fun put(name: String, value: String?) {
        context.waySecureDataStore.edit { preferences ->
            if (value == null) preferences.remove(preference(name))
            else preferences[preference(name)] = encrypt(name, value)
        }
    }

    suspend fun get(name: String): String? {
        val encoded = context.waySecureDataStore.data.first()[preference(name)] ?: return null
        return decrypt(name, encoded)
    }

    suspend fun installationHwid(): String {
        get(HWID)?.takeIf(InstallationIdentity::isValid)?.let { return it }
        val generated = InstallationIdentity.generate()
        put(HWID, generated)
        return generated
    }

    suspend fun clearAll() {
        context.waySecureDataStore.edit { it.clear() }
    }
}
