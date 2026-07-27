# Dynamic Conversation Exporter

AutomationPlatform module for exporting ChatGPT conversations through an existing Chrome Debug/CDP connection on port `9222`.

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

Start the module with:

```text
00_START_ALL.cmd
```

Repository module metadata is stored in `module.json` and `module_manifest.json`.

The packaged runtime source is stored in `packages/` as Base64 parts. AutomationPlatform reconstructs the ZIP, verifies SHA-256, installs it into the `modules` directory, installs only missing Python dependencies, and preserves module exports/logs/configuration during updates.