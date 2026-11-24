# LightCode⁴ for Android

- **Your computer**: pair with the QR code from LightCode⁴ → Settings → Phone, then control its sessions, approve steps and get notifications.
- **On this phone**: chat with Claude or any OpenAI-compatible model using your own API key (works when the computer is off).

Build: `./gradlew assembleRelease` (or open this folder in Android Studio). The APK is in `app/build/outputs/apk/release/`.
Releases are signed with `app/lightcode4.keystore` so new versions install over old ones. It is a sideload key for this
open-source app; for a Play Store release, sign with your own private key (set LC4_KEYSTORE and LC4_KEYSTORE_PASS).
