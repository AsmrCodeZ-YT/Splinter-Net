#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod apps;
mod backend;
mod config;
mod gui;

use eframe::egui;

fn main() -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([420.0, 650.0])
            .with_min_inner_size([420.0, 650.0])
            .with_resizable(false)
            .with_title("Splinter Net"),
        ..Default::default()
    };

    eframe::run_native(
        "Splinter Net",
        options,
        Box::new(|_cc| Box::new(gui::SplinterApp::new())),
    )
}
