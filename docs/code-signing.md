# macOS Developer ID signing and notarization

The 1.4.0 build recipe retains the existing PyInstaller/Tk GUI and CLI. Official
macOS builds require Developer ID Application signing, hardened runtime, Apple
notarization and a stapled ticket. Historical tags keep their own build recipe.
Windows downloads are not Authenticode signed. No Electron runtime is added.

## GitHub secrets

The dedicated signing step uses the same secret names/formats as Tagcast:

| Secret | Value |
|---|---|
| `CSC_LINK` | Base64-encoded, password-protected Developer ID Application `.p12`, including its private key |
| `CSC_KEY_PASSWORD` | The certificate export password |
| `APPLE_ID` | Apple Developer account email |
| `APPLE_APP_SPECIFIC_PASSWORD` | App-specific password, not the Apple account password |
| `APPLE_TEAM_ID` | Ten-character Apple team identifier matching the certificate |

The build imports the certificate into a temporary keychain, selects exactly
one matching Developer ID identity and passes only its public fingerprint to
PyInstaller. PyInstaller signs the nested Python/Tk/native libraries and both
executables before sealing the app. No JIT entitlement, library-validation
exception or debugging entitlement is needed for this Python/Tk application.
The original keychain search list is restored and the temporary keychain and
certificate file are removed in a `finally` block.

Signing secrets are not passed to dependency installation, PyInstaller or the
GUI/CLI checks. Only the necessary security/notary command arguments use them;
their output and argument lists are not included in failure messages. Reports
contain verification booleans, archive names and hashes, not credentials.

## Test without publishing

Run **Native downloads** manually on the reviewed branch or `main`, enable
**sign_macos** and leave **tag empty**. This creates verified CI artifacts only;
it does not create a release, update Homebrew or replace existing downloads.
Ordinary pushes and pull requests keep ad-hoc Mac integrity signatures and
receive no signing credentials. Signing configuration rejects PR/push events,
missing credentials, non-Mac hosts and invalid team/certificate encodings before
build work begins.

With the five variables supplied securely on macOS, the equivalent command is:

```sh
python scripts/build_app.py --sign-macos
```

## Release verification

After signing, the build submits a temporary ZIP to Apple's notary service,
waits for acceptance and staples the ticket to the app. It archives that app,
extracts the final ZIP outside the checkout and checks:

- The complete app signature, Developer ID authority, matching team and hardened
  runtime on the app and both GUI/CLI executables.
- The stapled notarization ticket and Gatekeeper assessment.
- The existing real GUI/CLI checks, including drag and drop, SQLite, metadata,
  album details, background information, update UI, artwork and FFmpeg conversion.

Only then are checksums and `build/reports/macos-signing.json` written. The release
publication job checks both signing reports against their exact final archive
hashes before uploading any downloads. A failed signed build never falls back
to an unsigned archive. No live music library or attached player is used by CI.

The notary wait is bounded at 20 minutes and Mac CI jobs allow 60 minutes.
An Apple delay or timeout is not acceptance; inspect the result before retrying.
Notarization does not replace testing on the actual player. The existing
Homebrew workflow still verifies and tests the final downloads before updating
the cask.

Official releases and manual builds with a nonempty **tag** select signing
automatically when the tag includes this recipe. Use an empty tag for tests.
Older tags are not silently rebuilt with a different signing recipe.
