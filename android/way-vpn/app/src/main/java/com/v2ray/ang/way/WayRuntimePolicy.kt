package com.v2ray.ang.way

import java.time.Instant


object WayRuntimePolicy {
    const val PROFILE_EXPIRY_SETTING = "way_profile_expiry_epoch_second"

    fun parseExpiry(value: String?): Long? = runCatching {
        value?.let(Instant::parse)?.epochSecond
    }.getOrNull()

    fun isProfileUsable(expiryEpochSecond: Long, nowEpochSecond: Long = Instant.now().epochSecond): Boolean {
        return expiryEpochSecond > 0 && expiryEpochSecond > nowEpochSecond
    }
}
