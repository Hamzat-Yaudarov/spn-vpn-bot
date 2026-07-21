package com.v2ray.ang.way

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec


/** Шифрует синхронные runtime-хранилища Xray/MMKV ключом, защищённым Keystore. */
object WayMmkvKey {
    private const val ALIAS = "way_vpn_mmkv_wrapper_v1"
    private const val PREFS = "way_mmkv_key_wrapper"
    private const val VALUE = "wrapped_key"
    private const val KEYSTORE = "AndroidKeyStore"
    private const val IV_SIZE = 12
    lateinit var value: String
        private set

    fun initialize(context: Context) {
        if (::value.isInitialized) return
        val preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val wrapped = preferences.getString(VALUE, null)
        value = wrapped?.let(::decrypt) ?: generateKey().also { plain ->
            preferences.edit().putString(VALUE, encrypt(plain)).commit()
        }
    }

    private fun wrappingKey(): SecretKey {
        val store = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return generator.generateKey()
    }

    private fun generateKey(): String {
        val alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        val random = SecureRandom()
        return buildString(16) { repeat(16) { append(alphabet[random.nextInt(alphabet.length)]) } }
    }

    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, wrappingKey())
        return Base64.encodeToString(cipher.iv + cipher.doFinal(plain.toByteArray()), Base64.NO_WRAP)
    }

    private fun decrypt(wrapped: String): String {
        val bytes = Base64.decode(wrapped, Base64.NO_WRAP)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, wrappingKey(), GCMParameterSpec(128, bytes.copyOfRange(0, IV_SIZE)))
        return cipher.doFinal(bytes.copyOfRange(IV_SIZE, bytes.size)).toString(Charsets.UTF_8)
    }
}
