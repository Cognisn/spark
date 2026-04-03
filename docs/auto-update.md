# Auto-Update

Spark checks for updates on startup and can apply them automatically depending on the installation method.

## Update Check

After the web server is ready, Spark checks the [GitHub Releases](https://github.com/Cognisn/spark/releases) page for newer versions. If an update is available, a notification appears in the web UI.

- **Stable releases:** Checked by default
- **Pre-releases:** Also checked if the current version is a pre-release (alpha/beta)

The update check is non-blocking and does not prevent Spark from starting.

## Installation Methods

Spark detects how it was installed and offers the appropriate update path:

### PyApp Binary

If Spark was installed as a pre-built binary (via PyApp), the updater:

1. Locates the PyApp binary (macOS .app bundle, Windows .exe, or Linux binary)
2. Runs `<binary> self update` to download and install the new version
3. Prompts for a restart

PyApp bundles a Python runtime, so the update includes both the application and its dependencies.

### pip Install

If Spark was installed via pip, the updater:

1. Runs `pip install --upgrade cognisn-spark`
2. Prompts for a restart

## Manual Update

### pip

```bash
pip install --upgrade cognisn-spark
```

### PyApp Binary

Download the latest binary from the [Releases page](https://github.com/Cognisn/spark/releases) and replace the existing binary.

## Version Comparison

The updater compares semantic versions including pre-release suffixes:

- `0.2.0` is newer than `0.1.0`
- `0.2.0` is newer than `0.2.0rc1`
- `0.2.0rc1` is newer than `0.2.0b2`
- `0.2.0b2` is newer than `0.2.0a1`

The ordering is: alpha (a) < beta (b) < release candidate (rc) < release.

## Update Notification

When an update is available, the web UI displays:

- The current and latest version numbers
- A link to the release notes
- An **Update** button (if the installation method supports it)
- Whether the update is a pre-release

## Disabling Update Checks

Update checks can be disabled by not connecting to the internet. There is no explicit configuration to disable them -- the check fails silently if GitHub is unreachable.
