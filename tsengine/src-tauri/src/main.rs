// LightCode⁴ Tauri shell.
// Starts the Bun engine (bundled as the "lc4-engine" sidecar), waits for it to print
// its local URL, then opens the native window pointed at the engine — the same model
// the Python app used with pywebview, but with a tiny system-webview window.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::{process::CommandEvent, ShellExt};

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let handle = app.handle().clone();

            // The UI (the existing static files) is bundled as a resource; the engine serves it.
            let ui_dir = app
                .path()
                .resolve("ui", tauri::path::BaseDirectory::Resource)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default();

            let (mut rx, child) = app
                .shell()
                .sidecar("lc4-engine")?
                .env("LC4_STATIC", ui_dir)
                .spawn()?;

            // Own the child in the task so the engine lives as long as the app.
            tauri::async_runtime::spawn(async move {
                let _child = child;
                let mut opened = false;
                while let Some(event) = rx.recv().await {
                    if let CommandEvent::Stdout(line) = event {
                        let text = String::from_utf8_lossy(&line);
                        if !opened {
                            if let Some(url) = text.trim().strip_prefix("LC4_URL ") {
                                if let Ok(parsed) = url.trim().parse() {
                                    let _ = WebviewWindowBuilder::new(
                                        &handle, "main", WebviewUrl::External(parsed),
                                    )
                                    .title("LightCode\u{2074}")
                                    .inner_size(1200.0, 820.0)
                                    .min_inner_size(900.0, 600.0)
                                    .build();
                                    opened = true;
                                }
                            }
                        }
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running LightCode\u{2074}");
}
