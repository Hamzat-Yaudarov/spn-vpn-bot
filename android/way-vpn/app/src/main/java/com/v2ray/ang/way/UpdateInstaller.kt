package com.v2ray.ang.way

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.security.MessageDigest


object UpdateVerifier {
    fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes)
        .joinToString("") { "%02x".format(it) }

    fun normalizeFingerprint(value: String): String = value.replace(":", "").lowercase()
}

class UpdateInstaller(private val context: Context) {
    private val client = OkHttpClient()

    @Suppress("DEPRECATION")
    private fun signingFingerprints(packageName: String? = null, archivePath: String? = null): Set<String> {
        val manager = context.packageManager
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            PackageManager.GET_SIGNING_CERTIFICATES
        } else {
            PackageManager.GET_SIGNATURES
        }
        val info = if (archivePath != null) manager.getPackageArchiveInfo(archivePath, flags)
        else manager.getPackageInfo(packageName ?: context.packageName, flags)
        val signatures = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info?.signingInfo?.apkContentsSigners.orEmpty()
        } else {
            info?.signatures.orEmpty()
        }
        return signatures.map { UpdateVerifier.sha256(it.toByteArray()) }.toSet()
    }

    suspend fun downloadAndVerify(manifest: UpdateManifest): File = withContext(Dispatchers.IO) {
        require(manifest.apkUrl.startsWith("https://")) { "APK URL must use HTTPS" }
        val expectedSha = UpdateVerifier.normalizeFingerprint(manifest.sha256)
        val expectedCertificate = UpdateVerifier.normalizeFingerprint(manifest.signingCertSha256)
        require(expectedSha.length == 64 && expectedCertificate.length == 64) { "Update manifest is incomplete" }
        require(expectedCertificate in signingFingerprints()) { "Update signing certificate does not match installed app" }

        val bytes = client.newCall(Request.Builder().url(manifest.apkUrl).get().build()).execute().use { response ->
            require(response.isSuccessful) { "APK download failed" }
            response.body.bytes()
        }
        require(UpdateVerifier.sha256(bytes) == expectedSha) { "APK SHA-256 mismatch" }
        val target = File(context.cacheDir, "WayVPN-${manifest.versionName}-release.apk")
        target.outputStream().use { it.write(bytes) }
        require(expectedCertificate in signingFingerprints(archivePath = target.absolutePath)) {
            "APK is signed by an unexpected certificate"
        }
        target
    }

    fun launchInstaller(apk: File) {
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.cache", apk)
        context.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }
}
