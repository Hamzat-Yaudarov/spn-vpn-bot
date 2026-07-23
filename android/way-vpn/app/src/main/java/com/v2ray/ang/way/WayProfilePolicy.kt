package com.v2ray.ang.way

import com.google.gson.JsonParser
import com.v2ray.ang.enums.EConfigType
import java.util.Base64


object WayProfilePolicy {
    private val allowedSchemes = listOf("vless://", "trojan://", "ss://")
    private val allowedProtocolNames = setOf("vless", "trojan", "shadowsocks")
    private val knownProtocolNames = EConfigType.entries
        .map { it.name.lowercase() }
        .toSet()
    private val allowedTypes = setOf(
        EConfigType.VLESS,
        EConfigType.TROJAN,
        EConfigType.SHADOWSOCKS,
    )

    fun isSupported(configType: EConfigType, rawConfig: String? = null): Boolean {
        if (configType in allowedTypes) return true
        if (configType != EConfigType.CUSTOM || rawConfig.isNullOrBlank()) return false

        return runCatching {
            val outbounds = JsonParser.parseString(rawConfig)
                .asJsonObject
                .getAsJsonArray("outbounds")
            val primaryProtocol = outbounds
                .asSequence()
                .mapNotNull { outbound ->
                    outbound.asJsonObject.get("protocol")?.asString?.lowercase()
                }
                .firstOrNull(knownProtocolNames::contains)
            primaryProtocol in allowedProtocolNames
        }.getOrDefault(false)
    }

    fun filterSupported(content: String): String {
        val trimmed = content.trim()
        val plain = if (trimmed.lineSequence().any { line -> allowedSchemes.any { line.trim().startsWith(it, true) } }) {
            trimmed
        } else {
            val padded = trimmed + "=".repeat((4 - trimmed.length % 4) % 4)
            sequenceOf(
                Base64.getMimeDecoder(),
                Base64.getUrlDecoder(),
                Base64.getDecoder(),
            ).mapNotNull { decoder ->
                runCatching { decoder.decode(padded).toString(Charsets.UTF_8) }.getOrNull()
            }.firstOrNull { decoded ->
                decoded.lineSequence().any { line ->
                    allowedSchemes.any { line.trim().startsWith(it, true) }
                }
            } ?: trimmed
        }
        return plain.lineSequence()
            .map(String::trim)
            .filter { line -> allowedSchemes.any { line.startsWith(it, true) } }
            .distinct()
            .joinToString("\n", postfix = "\n")
            .also { require(it.isNotBlank()) { "Профиль не содержит поддерживаемых серверов" } }
    }
}
