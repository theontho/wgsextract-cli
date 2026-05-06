# macOS App Packaging

The macOS package is a drag-and-drop `.app` distributed inside a DMG. The app bundle contains a snapshot of this project and launches the desktop GUI with Pixi from a writable runtime under `~/Library/Application Support/WGS Extract/runtime/<version>`.

Build locally from the repository root:

```bash
pixi run build-macos-app
```

The DMG is written to `out/macos/`. Users still need Pixi installed; the app validates Pixi at launch and shows a macOS dialog if it is missing.
