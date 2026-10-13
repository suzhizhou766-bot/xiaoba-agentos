# Native adapter boundary

Web MVP uses FileReader, Blob downloads and browser file selection. Future native implementations should expose `openProject()`, `saveProject()`, `readAsset()` and `writeImage()` here, with runtime detection of Tauri. Core, store, scene and compiler must stay independent of native APIs.

The `src-tauri` scaffold is intentionally not enabled by package scripts. See root README before installing Tauri CLI and Rust prerequisites.
