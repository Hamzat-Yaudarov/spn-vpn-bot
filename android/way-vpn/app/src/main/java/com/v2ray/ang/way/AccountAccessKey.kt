package com.v2ray.ang.way


object AccountAccessKey {
    private val wayPattern = Regex("^WAY-(?:[A-Z2-7]{4}-){5}[A-Z2-7]{4}$")
    private val compactPattern = Regex("^[A-Za-z0-9_-]{16,64}$")
    private val unicodeDashes = Regex("[‐‑‒–—−]")

    fun normalize(value: String): String {
        val compact = unicodeDashes.replace(value.filterNot(Char::isWhitespace).trim(), "-")
        return if (compact.startsWith("WAY-", ignoreCase = true)) compact.uppercase() else compact
    }

    fun isValid(value: String): Boolean = normalize(value).let {
        if (it.startsWith("WAY-")) wayPattern.matches(it) else compactPattern.matches(it)
    }
}
