package com.v2ray.ang.way

import java.security.SecureRandom


object InstallationIdentity {
    const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789=-"

    fun generate(random: SecureRandom = SecureRandom()): String = buildString(32) {
        repeat(32) { append(ALPHABET[random.nextInt(ALPHABET.length)]) }
    }

    fun isValid(value: String?): Boolean = value?.length == 32 && value.all(ALPHABET::contains)
}

object NodeSelector {
    fun select(remembered: String?, candidates: List<Pair<String, Long>>): String? {
        candidates.firstOrNull { it.first == remembered }?.let { return it.first }
        return candidates.minByOrNull { (_, delay) -> if (delay > 0) delay else Long.MAX_VALUE }?.first
    }
}
