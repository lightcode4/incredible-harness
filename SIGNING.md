# Signing the LightCode⁴ installers

Unsigned builds work, but macOS says the app is from an "unidentified developer" (right-click → Open the first time) and
Windows SmartScreen shows "Windows protected your PC" (More info → Run anyway). Signing removes those warnings.
The GitHub build already does everything; it only needs your certificates, added as repository secrets
(GitHub → the repository → Settings → Secrets and variables → Actions → New repository secret).
Builds without these secrets keep working, unsigned.

## macOS (Developer ID + notarization)

Needs an Apple Developer Program membership ($99/year): https://developer.apple.com/programs/

1. In Xcode → Settings → Accounts → Manage Certificates, create a **Developer ID Application** certificate.
2. In Keychain Access, right-click that certificate → Export → save as `cert.p12` with a password.
3. Create an app-specific password at https://account.apple.com (Sign-In and Security → App-Specific Passwords).
4. Add these secrets:

| Secret | Value |
|---|---|
| `MACOS_CERT_P12` | output of `base64 -i cert.p12 \| pbcopy` (it is now on your clipboard) |
| `MACOS_CERT_PASSWORD` | the password you chose in step 2 |
| `MACOS_SIGN_IDENTITY` | e.g. `Developer ID Application: Your Name (ABCDE12345)` (`security find-identity -v -p codesigning` shows it) |
| `APPLE_ID` | your Apple ID email |
| `APPLE_APP_PASSWORD` | the app-specific password from step 3 |
| `APPLE_TEAM_ID` | the 10-character team ID shown at https://developer.apple.com/account |

The build signs the app with the hardened runtime and `assets/entitlements.plist`, verifies it, notarizes the .dmg and
staples the ticket.

## Windows (code signing certificate)

Buy an OV or EV code signing certificate (e.g. from Sectigo, DigiCert or SSL.com), or use Azure Trusted Signing.
Export it as a `.pfx` file with a password, then add:

| Secret | Value |
|---|---|
| `WINDOWS_CERT_PFX` | output of `base64 -w0 cert.pfx` (PowerShell: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))`) |
| `WINDOWS_CERT_PASSWORD` | the .pfx password |

The build signs `LightCode4.exe` and then the installer, with a timestamp. Note: EV certificates on hardware tokens
can't be exported to a .pfx; use Azure Trusted Signing or the vendor's cloud signing for those.
