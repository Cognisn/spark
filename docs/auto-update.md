# Auto-Update

Spark checks for updates on startup and notifies you when a newer version is available.

## Update Check

After the web server is ready, Spark checks the [GitHub Releases](https://github.com/Cognisn/spark/releases) page for newer versions. If an update is available, a notification appears in the web UI.

- **Stable releases:** Checked by default
- **Pre-releases:** Also checked if the current version is a pre-release (alpha/beta)

The update check is non-blocking and does not prevent Spark from starting.

## Update Notification

When an update is available:

1. A badge appears on the **Help** menu item in the navigation bar
2. On the dashboard, a modal displays showing the current and latest version with rendered release notes
3. Click **Download Update** to open the GitHub releases page where you can download the new installer

The notification modal only appears once per session. You can re-open it from the Help menu at any time.

## Updating

### macOS (DMG)

1. Download the new DMG from the releases page.
2. Open the DMG and drag Spark to Applications (replacing the old version).
3. On next launch, the app detects the binary has changed and clears the PyApp cache automatically.

### Windows (NSIS Installer)

1. Download and run the new setup executable.
2. The installer clears the old installation and PyApp cache automatically.
3. Launch Spark from the desktop shortcut or Start Menu.

### pip

```bash
pip install --upgrade cognisn-spark
```

## Version Comparison

The updater compares semantic versions including pre-release suffixes:

- `0.2.0` is newer than `0.1.0`
- `0.2.0` is newer than `0.2.0rc1`
- `0.2.0rc1` is newer than `0.2.0b2`
- `0.2.0b2` is newer than `0.2.0a1`

The ordering is: alpha (a) < beta (b) < release candidate (rc) < release.

## Disabling Update Checks

Update checks can be disabled by not connecting to the internet. There is no explicit configuration to disable them -- the check fails silently if GitHub is unreachable.
