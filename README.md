# Dynamic Conversation Exporter

AutomationPlatform module for exporting ChatGPT conversations through the shared Chrome Debug/CDP connection on port `9222`.

## AutomationPlatform installation

Recommended target:

```text
<AutomationPlatform>\modules\dynamic_conversation_exporter
```

The module uses the platform-local Python runtime:

```text
<AutomationPlatform>\runtime\python\python.exe
```

and the shared AutomationPlatform Chrome Debug profile/launcher.

## Embedded single-window panel

The module now exposes:

```text
embedded_panel.py
DynamicConversationExporterPanel
```

for the AutomationPlatform Modular Shell.

The embedded panel is a normal Tkinter child `Frame`. It does **not** create its own `Tk()` root, `Toplevel()` window or second AutomationPlatform window.

AutomationPlatform installs/refreshed this small integration file separately from the packaged runtime and writes:

```text
<AutomationPlatform>\modules\dynamic_conversation_exporter\platform_integration.json
```

The main AutomationPlatform Shell scans this file and automatically adds **Conversation Exporter** to the left module navigation menu.

When the user switches from another page to Conversation Exporter, AutomationPlatform hides the previous view and displays the cached module Frame in the same workspace. The main window remains open and the module panel keeps its in-memory state.

## Shared platform services

The embedded panel receives the AutomationPlatform service object and uses the shared:

```text
Python runtime
Chrome Profile
Chrome Debug / CDP :9222
Platform Root
module/log/export directories
```

Module processes receive:

```text
AUTOMATION_PLATFORM_ROOT
AUTOMATION_PLATFORM_PYTHON
AUTOMATION_PLATFORM_PROFILE
AUTOMATION_PLATFORM_CDP_URL
AUTOMATION_PLATFORM_CDP_PORT
AUTOMATION_PLATFORM_EMBEDDED=1
```

## Standalone compatibility

The old standalone launcher remains available for compatibility:

```text
00_START_ALL.cmd
```

but AutomationPlatform uses `embedded_panel.py` for normal panel navigation.

## Package / updates

Repository module metadata is stored in `module.json` and `module_manifest.json`.

The packaged runtime source is stored in `packages/` as Base64 parts. AutomationPlatform reconstructs the ZIP, verifies SHA-256, installs it into the `modules` directory, installs only missing Python dependencies, preserves exports/logs/configuration during updates, and separately refreshes the embedded panel integration from GitHub even when the module runtime itself receives `SKIP`.
