package com.v2ray.ang.way

import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test


class SecureStoreInstrumentedTest {
    @Test
    fun encryptedValuesAndHwidSurviveStoreRecreationAndClearOnLogout() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val first = SecureStore(context)
        first.clearAll()
        first.put(SecureStore.REFRESH_TOKEN, "refresh-secret")
        val hwid = first.installationHwid()

        val recreated = SecureStore(context)
        assertEquals("refresh-secret", recreated.get(SecureStore.REFRESH_TOKEN))
        assertEquals(hwid, recreated.installationHwid())
        assertTrue(InstallationIdentity.isValid(hwid))
        assertNotEquals(android.provider.Settings.Secure.getString(context.contentResolver, android.provider.Settings.Secure.ANDROID_ID), hwid)

        recreated.clearAll()
        assertNull(recreated.get(SecureStore.REFRESH_TOKEN))
    }
}
