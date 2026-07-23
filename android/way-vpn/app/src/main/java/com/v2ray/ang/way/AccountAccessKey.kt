package com.v2ray.ang.way

import java.net.URI


object AccountAccessKey {
    private val wayPattern = Regex("^WAY-(?:[A-Z2-7]{4}-){5}[A-Z2-7]{4}$")
    private val compactPattern = Regex("^[A-Za-z0-9_-]{16,64}$")
    private val unicodeDashes = Regex("[‐‑‒–—−]")

    fun normalize(value: String): String {
        val compact = unicodeDashes.replace(value.filterNot(Char::isWhitespace).trim(), "-")
        return if (compact.startsWith("WAY-", ignoreCase = true)) compact.uppercase() else compact
    }

    fun isValid(value: String): Boolean = normalize(value).let {
        when {
            it.startsWith("WAY-") -> wayPattern.matches(it)
            it.startsWith("https://") -> isSubscriptionUrl(it)
            else -> compactPattern.matches(it)
        }
    }

    fun isSubscriptionUrl(value: String): Boolean = runCatching {
        val uri = URI(normalize(value))
        uri.scheme.equals("https", ignoreCase = true) &&
            !uri.host.isNullOrBlank() &&
            uri.userInfo == null &&
            uri.rawFragment == null &&
            ((!uri.rawPath.isNullOrBlank() && uri.rawPath != "/") || !uri.rawQuery.isNullOrBlank())
    }.getOrDefault(false)
}
