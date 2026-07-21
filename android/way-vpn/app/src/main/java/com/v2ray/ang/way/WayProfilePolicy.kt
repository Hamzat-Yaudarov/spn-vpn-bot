package com.v2ray.ang.way

import java.util.Base64


object WayProfilePolicy {
    private val allowedSchemes = listOf("vless://", "trojan://", "ss://")

    fun filterSupported(content: String): String {
        val trimmed = content.trim()
        val plain = if (trimmed.lineSequence().any { line -> allowedSchemes.any { line.trim().startsWith(it, true) } }) {
            trimmed
        } else {
            runCatching {
                val padded = trimmed + "=".repeat((4 - trimmed.length % 4) % 4)
                Base64.getMimeDecoder().decode(padded).toString(Charsets.UTF_8)
            }.getOrDefault(trimmed)
        }
        return plain.lineSequence()
            .map(String::trim)
            .filter { line -> allowedSchemes.any { line.startsWith(it, true) } }
            .distinct()
            .joinToString("\n", postfix = "\n")
            .also { require(it.isNotBlank()) { "Профиль не содержит поддерживаемых серверов" } }
    }
}
