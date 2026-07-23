package com.v2ray.ang.way

import com.v2ray.ang.enums.EConfigType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import okio.Buffer


class WaySecurityTest {
    @Test
    fun profilePolicyAllowsOnlyWayProtocols() {
        val filtered = WayProfilePolicy.filterSupported(
            "vless://one\nvmess://blocked\ntrojan://two\nss://three\nhttp://blocked\n"
        )
        assertTrue(filtered.contains("vless://one"))
        assertTrue(filtered.contains("trojan://two"))
        assertTrue(filtered.contains("ss://three"))
        assertFalse(filtered.contains("vmess://"))
        assertFalse(filtered.contains("http://"))
        assertTrue(WayProfilePolicy.isSupported(EConfigType.VLESS))
        assertTrue(WayProfilePolicy.isSupported(EConfigType.TROJAN))
        assertTrue(WayProfilePolicy.isSupported(EConfigType.SHADOWSOCKS))
        assertFalse(WayProfilePolicy.isSupported(EConfigType.VMESS))
    }

    @Test
    fun profilePolicyAcceptsOnlyAllowedPrimaryProtocolInCustomXrayConfig() {
        val vlessConfig = """{"outbounds":[
            {"tag":"proxy","protocol":"vless","settings":{}},
            {"tag":"direct","protocol":"freedom","settings":{}}
        ]}"""
        val vmessConfig = """{"outbounds":[
            {"tag":"proxy","protocol":"vmess","settings":{}},
            {"tag":"fallback","protocol":"vless","settings":{}}
        ]}"""

        assertTrue(WayProfilePolicy.isSupported(EConfigType.CUSTOM, vlessConfig))
        assertFalse(WayProfilePolicy.isSupported(EConfigType.CUSTOM, vmessConfig))
        assertFalse(WayProfilePolicy.isSupported(EConfigType.CUSTOM, null))
    }

    @Test
    fun profilePolicyAcceptsUrlSafeBase64Subscriptions() {
        val encoded = java.util.Base64.getUrlEncoder().withoutPadding()
            .encodeToString("vless://one\ntrojan://two\n".toByteArray())
        val filtered = WayProfilePolicy.filterSupported(encoded)
        assertTrue(filtered.contains("vless://one"))
        assertTrue(filtered.contains("trojan://two"))
    }

    @Test
    fun generatedHwidMatchesRemnawaveContract() {
        repeat(100) { assertTrue(InstallationIdentity.isValid(InstallationIdentity.generate())) }
        assertFalse(InstallationIdentity.isValid("android-id"))
    }

    @Test
    fun rememberedServerWinsOtherwiseLowestPositiveLatencyWins() {
        val nodes = listOf("one" to 50L, "two" to 20L, "offline" to -1L)
        assertEquals("one", NodeSelector.select("one", nodes))
        assertEquals("two", NodeSelector.select(null, nodes))
    }

    @Test
    fun updateFingerprintNormalizationIsStable() {
        assertEquals("aabbcc", UpdateVerifier.normalizeFingerprint("AA:BB:CC"))
        assertEquals(64, UpdateVerifier.sha256("Way VPN".toByteArray()).length)
    }

    @Test
    fun expiredOfflineProfileCannotStartVpn() {
        assertFalse(WayRuntimePolicy.isProfileUsable(1_000, nowEpochSecond = 1_000))
        assertFalse(WayRuntimePolicy.isProfileUsable(999, nowEpochSecond = 1_000))
        assertTrue(WayRuntimePolicy.isProfileUsable(1_001, nowEpochSecond = 1_000))
        assertEquals(1_750_000_000L, WayRuntimePolicy.parseExpiry("2025-06-15T15:06:40Z"))
    }

    @Test
    fun accountAccessKeyIsStrictButHumanInputIsNormalized() {
        val key = "WAY-ABCD-EFGH-JKLM-NPQR-STUV-WXYZ"
        assertTrue(AccountAccessKey.isValid("  ${key.lowercase()} \n"))
        assertEquals(key, AccountAccessKey.normalize(" ${key.lowercase()} "))
        assertTrue(AccountAccessKey.isValid("0RU56PWo-tACBZuo"))
        assertEquals("0RU56PWo-tACBZuo", AccountAccessKey.normalize(" 0RU56PWo-tACBZuo "))
        val subscriptionUrl = "https://sub.wayspn.online/LotbHJ8UExGg6pS-"
        assertTrue(AccountAccessKey.isValid(subscriptionUrl))
        assertEquals(subscriptionUrl, AccountAccessKey.normalize(" $subscriptionUrl\n"))
        assertTrue(AccountAccessKey.isValid("https://another-vpn.example/sub/LotbHJ8UExGg6pS-"))
        assertTrue(AccountAccessKey.isValid("https://another-vpn.example/?token=LotbHJ8UExGg6pS-"))
        assertFalse(AccountAccessKey.isValid("https://another-vpn.example/"))
        assertFalse(AccountAccessKey.isValid("http://sub.wayspn.online/LotbHJ8UExGg6pS-"))
        assertFalse(AccountAccessKey.isValid("https://user:password@sub.wayspn.online/LotbHJ8UExGg6pS-"))
        assertFalse(AccountAccessKey.isValid("WAY-TOO-SHORT"))
        assertFalse(AccountAccessKey.isValid("WAY-ABCD-EFGH-JKLM-NPQR-STUV-WXY0"))
    }

    @Test
    fun remnawaveExpiryHeaderIsParsedWithoutTrustingOtherFields() {
        assertEquals(
            "2027-01-15T08:00:00Z",
            SubscriptionResponseHeaders.expiryIso("upload=1; download=2; total=3; expire=1800000000"),
        )
        assertEquals(null, SubscriptionResponseHeaders.expiryIso("upload=1; expire=0"))
        assertEquals(null, SubscriptionResponseHeaders.expiryIso("expire=not-a-number"))
    }

    @Test
    fun shortSubscriptionBodyDoesNotRequireFillingTheMaximumBuffer() {
        val content = "vless://example\n".toByteArray()
        val result = SubscriptionBodyReader.readAtMost(Buffer().write(content), 4L * 1024L * 1024L + 1L)
        assertArrayEquals(content, result)
    }

    @Test
    fun subscriptionBodyReaderStopsOneBytePastTheAllowedSize() {
        val result = SubscriptionBodyReader.readAtMost(Buffer().writeUtf8("1234567890"), 5)
        assertEquals(5, result.size)
        assertEquals("12345", result.toString(Charsets.UTF_8))
    }
}
