# Windows Code Signing Setup

This document describes how to configure Windows code signing for Spark releases using an SSL.com Individual Validation (IV) certificate with eSigner.

## Overview

The release workflow automatically signs both the Windows `.exe` binary and the NSIS installer using SSL.com's CodeSignTool. This eliminates "Unknown Publisher" warnings for users downloading Spark on Windows.

## What Gets Signed

| Artefact | When |
|---|---|
| `spark-windows-x86_64.exe` | After PyApp binary build, before upload |
| `Spark-{version}-windows-x86_64-setup.exe` | After NSIS installer build, before upload |

## Prerequisites

1. An active SSL.com code signing certificate with eSigner access enabled.
2. TOTP authentication configured for eSigner (required for automated/CI signing).

## GitHub Repository Setup

### Step 1: Enable TOTP for eSigner

1. Log in to your [SSL.com account](https://www.ssl.com/login/).
2. Navigate to your code signing certificate order.
3. In the eSigner section, enable **TOTP authentication**.
4. You will be shown a TOTP secret (a base32-encoded string). **Copy this immediately** — you will need it for the GitHub secret.

> **Important:** This is different from your regular 2FA. The TOTP secret is specifically for eSigner API automation and allows CodeSignTool to generate OTPs without manual intervention.

### Step 2: Find Your Credential ID

1. In the SSL.com dashboard, navigate to your eSigner certificate.
2. Locate the **Credential ID** — a UUID string identifying your signing certificate.
3. Copy this value.

### Step 3: Add GitHub Repository Secrets

Go to your GitHub repository: **Settings → Secrets and variables → Actions → New repository secret**.

Add the following four secrets:

| Secret Name | Value | Description |
|---|---|---|
| `SSL_COM_USERNAME` | Your SSL.com account email | The email you use to log in to ssl.com |
| `SSL_COM_PASSWORD` | Your SSL.com account password | Your ssl.com account password |
| `SSL_COM_TOTP_SECRET` | The TOTP secret from Step 1 | Base32-encoded secret for automated OTP generation |
| `SSL_COM_CREDENTIAL_ID` | The credential ID from Step 2 | UUID identifying your signing certificate |

### Step 4: Verify

The next time a release is published, the workflow will:

1. Build the Windows PyApp binary
2. **Sign the `.exe`** using CodeSignTool
3. Build the NSIS installer (which bundles the already-signed binary)
4. **Sign the installer `.exe`**
5. Upload both signed artefacts to the GitHub release

Check the workflow run logs for `Sign Windows binary` and `Sign Windows installer` steps to confirm signing succeeded.

## Testing Locally

You can test signing locally if you have Java 11+ installed:

```bash
# Download CodeSignTool
curl -LO https://www.ssl.com/download/codesigntool-for-windows/
unzip CodeSignTool-v*.zip -d CodeSignTool
cd CodeSignTool

# Sign a test file
./CodeSignTool.sh sign \
  -username="your-email@example.com" \
  -password="your-password" \
  -totp_secret="your-totp-secret" \
  -credential_id="your-credential-id" \
  -input_file_path="/path/to/test.exe" \
  -output_dir_path="/path/to/output/"
```

On macOS/Linux use `CodeSignTool.sh`; on Windows use `CodeSignTool.bat`.

## Verifying a Signed Binary

On Windows, right-click the `.exe` → **Properties → Digital Signatures** tab. You should see your SSL.com certificate listed with a valid signature.

Alternatively, using PowerShell:

```powershell
Get-AuthenticodeSignature .\spark-windows-x86_64.exe
```

Or using `signtool` (from the Windows SDK):

```powershell
signtool verify /pa /v spark-windows-x86_64.exe
```

## Troubleshooting

| Problem | Solution |
|---|---|
| `AuthenticationError` in workflow logs | Verify `SSL_COM_USERNAME` and `SSL_COM_PASSWORD` secrets are correct |
| `Invalid TOTP` errors | Ensure `SSL_COM_TOTP_SECRET` is the eSigner TOTP secret (not your account 2FA secret) |
| `Credential not found` | Check `SSL_COM_CREDENTIAL_ID` matches the UUID in your SSL.com dashboard |
| Certificate expired | Renew the certificate in your SSL.com account and update the credential ID if it changes |
| CodeSignTool download fails | SSL.com occasionally changes download URLs — check their documentation for the latest link |

## Security Notes

- All signing credentials are stored as GitHub encrypted secrets and never appear in logs.
- The TOTP secret allows automated signing without manual OTP entry — treat it with the same care as a private key.
- CodeSignTool is downloaded fresh in each workflow run to ensure the latest version is used.
- The private key never leaves SSL.com's infrastructure — eSigner performs the signing operation server-side.

## Docker Image Signing (Future)

When Docker image signing is implemented, it will use [cosign](https://github.com/sigstore/cosign) with the SSL.com certificate via PKCS#11 integration. This will be documented separately.
