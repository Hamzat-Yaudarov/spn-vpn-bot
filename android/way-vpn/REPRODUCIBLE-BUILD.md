# Reproducible build

## Toolchain

- Temurin/OpenJDK 17
- Gradle Wrapper 9.5.1
- Android SDK Platform 37.0 and Build Tools 37.0.0
- macOS or Linux with the Android command-line tools

Install the SDK packages:

```sh
sdkmanager "platforms;android-37.0" "build-tools;37.0.0" "platform-tools"
```

Verify the pinned native library before building:

```sh
shasum -a 256 app/libs/libv2ray.aar
# 7846eb7f663d1d8ae931034faa7a56cccc82d618c2d029198e6e91a77fd8de1e
```

Set `sdk.dir` in an untracked `local.properties`, then build and test:

```sh
./gradlew testDebugUnitTest assembleDebug --no-daemon
```

For a release, inject an RSA-4096 signing key without committing it or its password:

```sh
./gradlew assembleRelease --no-daemon \
  -Pandroid.injected.signing.store.file=/absolute/path/way-vpn-release.jks \
  -Pandroid.injected.signing.store.password="$WAY_KEYSTORE_PASSWORD" \
  -Pandroid.injected.signing.key.alias=way-vpn-release \
  -Pandroid.injected.signing.key.password="$WAY_KEYSTORE_PASSWORD"
```

Verify the result independently:

```sh
apksigner verify --verbose --print-certs app/build/outputs/apk/release/WayVPN-1.1.6-universal-release.apk
shasum -a 256 app/build/outputs/apk/release/WayVPN-1.1.6-universal-release.apk
```

The expected v1 certificate fingerprint is `980d9e8dcdeaebbdcbaea80ec1417880b0ebc57a3cc20ba75a4b8f2104060957`. APK bytes can differ when signed with another certificate; clients deliberately reject that as an update.
