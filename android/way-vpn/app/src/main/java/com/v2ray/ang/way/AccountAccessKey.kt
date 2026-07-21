package com.v2ray.ang.way


object AccountAccessKey {
    private val pattern = Regex("^WAY-(?:[A-Z2-7]{4}-){5}[A-Z2-7]{4}$")

    fun normalize(value: String): String = value.filterNot(Char::isWhitespace).uppercase()

    fun isValid(value: String): Boolean = pattern.matches(normalize(value))
}
