//! egui frontend for Splinter Net.

use crate::apps::{discover_apps, DesktopApp};
use crate::backend::{Dependencies, InterfaceBinder, InterfaceInfo, PlatformBinder};
use crate::config::{Config, Profile};
use eframe::egui;
use std::collections::HashMap;

#[derive(PartialEq, Eq, Clone, Copy)]
enum Tab {
    Profiles,
    Applications,
    Script,
    Settings,
}

pub struct SplinterApp {
    config: Config,
    interfaces: Vec<InterfaceInfo>,
    deps: Dependencies,
    status: String,
    tab: Tab,

    // discovered applications
    discovered: Vec<DesktopApp>,
    app_search: String,
    // per-app selected profile (key: app name)
    app_profile: HashMap<String, String>,

    // new profile form state
    new_profile_name: String,
    new_profile_iface: String,

    // script tab state
    script_text: String,
    script_profile: String,

    // settings
    accent_input: String,
}

fn parse_hex(hex: &str) -> Option<egui::Color32> {
    let h = hex.trim().trim_start_matches('#');
    if h.len() != 6 {
        return None;
    }
    let r = u8::from_str_radix(&h[0..2], 16).ok()?;
    let g = u8::from_str_radix(&h[2..4], 16).ok()?;
    let b = u8::from_str_radix(&h[4..6], 16).ok()?;
    Some(egui::Color32::from_rgb(r, g, b))
}

impl SplinterApp {
    pub fn new() -> Self {
        let config = Config::load().unwrap_or_default();
        let interfaces = PlatformBinder::list_interfaces();
        let deps = PlatformBinder::check_dependencies();
        let discovered = discover_apps();
        let status = if deps.all_ok() {
            format!("Ready. {} apps found.", discovered.len())
        } else {
            format!("Missing dependencies: {}", deps.missing().join(", "))
        };
        let accent_input = config.accent.clone();
        Self {
            config,
            interfaces,
            deps,
            status,
            tab: Tab::Profiles,
            discovered,
            app_search: String::new(),
            app_profile: HashMap::new(),
            new_profile_name: String::new(),
            new_profile_iface: String::new(),
            script_text: String::new(),
            script_profile: String::new(),
            accent_input,
        }
    }

    fn accent(&self) -> egui::Color32 {
        parse_hex(&self.config.accent).unwrap_or(egui::Color32::from_rgb(124, 58, 237))
    }

    /// Apply a cohesive dark theme tinted by the accent color.
    fn apply_theme(&self, ctx: &egui::Context) {
        let accent = self.accent();
        let mut visuals = egui::Visuals::dark();
        let bg = egui::Color32::from_rgb(24, 24, 30);
        let panel = egui::Color32::from_rgb(30, 30, 38);
        visuals.panel_fill = bg;
        visuals.window_fill = bg;
        visuals.extreme_bg_color = egui::Color32::from_rgb(18, 18, 22);
        visuals.faint_bg_color = panel;
        visuals.selection.bg_fill = accent.linear_multiply(0.6);
        visuals.selection.stroke = egui::Stroke::new(1.0, accent);
        visuals.hyperlink_color = accent;
        visuals.widgets.hovered.bg_fill = accent.linear_multiply(0.5);
        visuals.widgets.active.bg_fill = accent;
        visuals.widgets.active.weak_bg_fill = accent;
        visuals.widgets.inactive.weak_bg_fill = panel;
        let rounding = egui::Rounding::same(8.0);
        visuals.widgets.inactive.rounding = rounding;
        visuals.widgets.hovered.rounding = rounding;
        visuals.widgets.active.rounding = rounding;
        visuals.window_rounding = egui::Rounding::same(12.0);
        ctx.set_visuals(visuals);

        let mut style = (*ctx.style()).clone();
        style.spacing.item_spacing = egui::vec2(8.0, 8.0);
        style.spacing.button_padding = egui::vec2(10.0, 6.0);
        ctx.set_style(style);
    }

    fn refresh_interfaces(&mut self) {
        self.interfaces = PlatformBinder::list_interfaces();
        self.status = format!("Refreshed: {} interfaces found.", self.interfaces.len());
    }

    fn refresh_apps(&mut self) {
        self.discovered = discover_apps();
        self.status = format!("Refreshed: {} apps found.", self.discovered.len());
    }

    fn save_config(&mut self) {
        if let Err(e) = self.config.save() {
            self.status = format!("Failed to save config: {e}");
        }
    }

    fn create_profile(&mut self) {
        let name = self.new_profile_name.trim().to_string();
        let iface = self.new_profile_iface.trim().to_string();
        if name.is_empty() || iface.is_empty() {
            self.status = "Profile name and interface are required.".to_string();
            return;
        }
        if self.config.find(&name).is_some() {
            self.status = format!("A profile named '{name}' already exists.");
            return;
        }
        match PlatformBinder::create_profile(&name, &iface) {
            Ok(()) => {
                self.config.profiles.push(Profile {
                    name: name.clone(),
                    interface: iface,
                    apps: Vec::new(),
                });
                self.save_config();
                self.new_profile_name.clear();
                self.new_profile_iface.clear();
                self.status = format!("Profile '{name}' created.");
            }
            Err(e) => {
                self.status = format!("Failed to create profile: {e}");
            }
        }
    }

    fn delete_profile(&mut self, idx: usize) {
        let name = self.config.profiles[idx].name.clone();
        match PlatformBinder::delete_profile(&name) {
            Ok(()) => {
                self.config.remove(&name);
                self.save_config();
                self.status = format!("Profile '{name}' deleted.");
            }
            Err(e) => {
                self.status = format!("Failed to delete profile: {e}");
            }
        }
    }

    fn launch(&mut self, profile: &str, command: &str, args: &[String]) {
        if profile.is_empty() {
            self.status = "Select a profile first.".to_string();
            return;
        }
        match PlatformBinder::launch_in_profile(profile, command, args) {
            Ok(_child) => {
                self.status = format!("Launched '{command}' in profile '{profile}'.");
            }
            Err(e) => {
                self.status = format!("Failed to launch: {e}");
            }
        }
    }

    fn profile_names(&self) -> Vec<String> {
        self.config.profiles.iter().map(|p| p.name.clone()).collect()
    }

    // ---- in-app header strip (branding only, no window controls) -----
    //
    // Window decorations (title bar, close/min/max buttons, drag-to-move,
    // resize edges) are handled by the OS compositor so the app works on
    // every desktop environment: GNOME/Wayland, KDE, XFCE, i3, etc.

    // fn title_bar(&self, _ctx: &egui::Context, ui: &mut egui::Ui) {
    //     let accent = self.accent();
    //     ui.horizontal(|ui| {
    //         ui.add_space(4.0);
    //         // ui.colored_label(accent, "\u{2756}");
    //         ui.add_space(2.0);
    //         ui.label(
    //             egui::RichText::new("Splinter Net")
    //                 .size(15.0)
    //                 .color(egui::Color32::from_gray(230)),
    //         );
    //     });
    // }

    // ---- tabs ---------------------------------------------------------

    fn profiles_tab(&mut self, ui: &mut egui::Ui) {
        // New profile form (full width), interface chosen from a dropdown.
        ui.group(|ui| {
            ui.set_width(ui.available_width());
            ui.label(egui::RichText::new("Create profile").strong());
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                ui.label("Name");
                ui.add(
                    egui::TextEdit::singleline(&mut self.new_profile_name)
                        .hint_text("e.g. YouTube")
                        .desired_width(ui.available_width()),
                );
            });
            ui.add_space(4.0);
            ui.horizontal(|ui| {
                ui.label("Iface");
                egui::ComboBox::from_id_source("iface_combo")
                    .selected_text(if self.new_profile_iface.is_empty() {
                        "select interface...".to_string()
                    } else {
                        self.new_profile_iface.clone()
                    })
                    .width(ui.available_width() - 40.0)
                    .show_ui(ui, |ui| {
                        for iface in &self.interfaces {
                            let label = format!(
                                "{} ({}{})",
                                iface.name,
                                iface.state,
                                iface
                                    .ip
                                    .as_ref()
                                    .map(|i| format!(", {i}"))
                                    .unwrap_or_default()
                            );
                            ui.selectable_value(
                                &mut self.new_profile_iface,
                                iface.name.clone(),
                                label,
                            );
                        }
                    });
                if ui
                    .button("\u{21bb}")
                    .on_hover_text("Refresh interfaces")
                    .clicked()
                {
                    self.refresh_interfaces();
                }
            });
            ui.add_space(6.0);
            if ui
                .add_sized(
                    [ui.available_width(), 28.0],
                    egui::Button::new("\u{002b}  Create profile"),
                )
                .clicked()
            {
                self.create_profile();
            }
        });

        ui.add_space(8.0);
        ui.label(egui::RichText::new("Profiles").strong());
        ui.add_space(4.0);

        let mut launch_action: Option<(String, String, Vec<String>)> = None;
        let mut add_app_to: Option<usize> = None;
        let mut remove_app: Option<(usize, usize)> = None;
        let mut delete_idx: Option<usize> = None;

        egui::ScrollArea::vertical()
            .auto_shrink([false, false])
            .show(ui, |ui| {
                if self.config.profiles.is_empty() {
                    ui.weak("No profiles yet. Create one above.");
                }
                for (idx, profile) in self.config.profiles.iter().enumerate() {
                    ui.group(|ui| {
                        ui.horizontal(|ui| {
                            ui.label(egui::RichText::new(&profile.name).strong());
                            ui.weak(format!("\u{2192} {}", profile.interface));
                            ui.with_layout(
                                egui::Layout::right_to_left(egui::Align::Center),
                                |ui| {
                                    if ui.small_button("Delete").clicked() {
                                        delete_idx = Some(idx);
                                    }
                                },
                            );
                        });
                        if profile.apps.is_empty() {
                            ui.weak("No applications.");
                        }
                        for (app_idx, app) in profile.apps.iter().enumerate() {
                            ui.horizontal(|ui| {
                                if ui.button("\u{25b6}").on_hover_text("Run").clicked() {
                                    launch_action =
                                        Some((profile.name.clone(), app.clone(), vec![]));
                                }
                                ui.label(app);
                                ui.with_layout(
                                    egui::Layout::right_to_left(egui::Align::Center),
                                    |ui| {
                                        if ui.small_button("\u{2715}").clicked() {
                                            remove_app = Some((idx, app_idx));
                                        }
                                    },
                                );
                            });
                        }
                        if ui.button("\u{002b} Add application").clicked() {
                            add_app_to = Some(idx);
                        }
                    });
                    ui.add_space(4.0);
                }
            });

        if let Some((profile, cmd, args)) = launch_action {
            self.launch(&profile, &cmd, &args);
        }
        if let Some(idx) = add_app_to {
            if let Some(path) = rfd::FileDialog::new().pick_file() {
                if let Some(p) = path.to_str() {
                    self.config.profiles[idx].apps.push(p.to_string());
                    self.save_config();
                    self.status = format!("Added '{p}'.");
                }
            }
        }
        if let Some((p_idx, a_idx)) = remove_app {
            self.config.profiles[p_idx].apps.remove(a_idx);
            self.save_config();
            self.status = "Application removed.".to_string();
        }
        if let Some(idx) = delete_idx {
            self.delete_profile(idx);
        }
    }

    fn applications_tab(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label("\u{1f50d}");
            ui.add(
                egui::TextEdit::singleline(&mut self.app_search)
                    .hint_text("Search applications...")
                    .desired_width(ui.available_width() - 40.0),
            );
            if ui.button("\u{21bb}").on_hover_text("Rescan apps").clicked() {
                self.refresh_apps();
            }
        });
        ui.add_space(4.0);

        let profiles = self.profile_names();
        let query = self.app_search.to_lowercase();

        // Snapshot filtered apps to avoid borrowing self while mutating maps.
        let filtered: Vec<DesktopApp> = self
            .discovered
            .iter()
            .filter(|a| query.is_empty() || a.name.to_lowercase().contains(&query))
            .cloned()
            .collect();

        let mut launch: Option<(String, String, Vec<String>)> = None;

        egui::ScrollArea::vertical()
            .auto_shrink([false, false])
            .show(ui, |ui| {
                if filtered.is_empty() {
                    ui.weak("No matching applications.");
                }
                for app in &filtered {
                    ui.horizontal(|ui| {
                        let sel = self
                            .app_profile
                            .entry(app.name.clone())
                            .or_insert_with(|| {
                                profiles.first().cloned().unwrap_or_default()
                            });
                        egui::ComboBox::from_id_source(format!("prof_{}", app.name))
                            .selected_text(if sel.is_empty() {
                                "profile".to_string()
                            } else {
                                sel.clone()
                            })
                            .width(110.0)
                            .show_ui(ui, |ui| {
                                for p in &profiles {
                                    ui.selectable_value(sel, p.clone(), p);
                                }
                            });
                        if ui.button("\u{25b6} Launch").clicked() {
                            let (cmd, args) = app.command_and_args();
                            launch = Some((sel.clone(), cmd, args));
                        }
                        ui.label(&app.name);
                    });
                }
            });

        if let Some((profile, cmd, args)) = launch {
            self.launch(&profile, &cmd, &args);
        }
    }

    fn script_tab(&mut self, ui: &mut egui::Ui) {
        ui.label(egui::RichText::new("Run a custom command or script").strong());
        ui.add_space(4.0);
        let profiles = self.profile_names();
        ui.horizontal(|ui| {
            ui.label("Profile");
            egui::ComboBox::from_id_source("script_profile")
                .selected_text(if self.script_profile.is_empty() {
                    "select...".to_string()
                } else {
                    self.script_profile.clone()
                })
                .width(160.0)
                .show_ui(ui, |ui| {
                    for p in &profiles {
                        ui.selectable_value(&mut self.script_profile, p.clone(), p);
                    }
                });
        });
        ui.add_space(4.0);
        ui.add(
            egui::TextEdit::multiline(&mut self.script_text)
                .hint_text("e.g. curl https://ifconfig.me")
                .desired_rows(8)
                .desired_width(f32::INFINITY)
                .code_editor(),
        );
        ui.add_space(4.0);
        if ui.button("\u{25b6} Run script").clicked() {
            let profile = self.script_profile.clone();
            let script = self.script_text.clone();
            if script.trim().is_empty() {
                self.status = "Script is empty.".to_string();
            } else {
                self.launch(&profile, "sh", &["-c".to_string(), script]);
            }
        }
    }

    fn settings_tab(&mut self, ui: &mut egui::Ui) {
        ui.label(egui::RichText::new("Appearance").strong());
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            ui.label("Accent color");
            let mut color = self.accent();
            if ui.color_edit_button_srgba(&mut color).changed() {
                self.accent_input = format!(
                    "#{:02X}{:02X}{:02X}",
                    color.r(),
                    color.g(),
                    color.b()
                );
                self.config.accent = self.accent_input.clone();
                self.save_config();
            }
        });
        ui.horizontal(|ui| {
            ui.label("Hex");
            ui.add(
                egui::TextEdit::singleline(&mut self.accent_input)
                    .hint_text("#7C3AED")
                    .desired_width(120.0),
            );
            if ui.button("Apply").clicked() {
                if parse_hex(&self.accent_input).is_some() {
                    self.config.accent = self.accent_input.trim().to_string();
                    self.save_config();
                    self.status = "Accent color updated.".to_string();
                } else {
                    self.status = "Invalid hex color (use #RRGGBB).".to_string();
                }
            }
        });

        ui.add_space(12.0);
        ui.label(egui::RichText::new("Dependencies").strong());
        ui.add_space(6.0);
        let mark = |ok: bool| if ok { "\u{2714}" } else { "\u{2715}" };
        ui.label(format!("{} ip (iproute2)", mark(self.deps.ip)));
        ui.label(format!("{} dhclient", mark(self.deps.dhclient)));
        ui.label(format!("{} pkexec (PolicyKit)", mark(self.deps.pkexec)));
    }
}

impl eframe::App for SplinterApp {
    fn clear_color(&self, _visuals: &egui::Visuals) -> [f32; 4] {
        // Transparent so the rounded frame shows correctly.
        egui::Color32::TRANSPARENT.to_normalized_gamma_f32()
    }

    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.apply_theme(ctx);
        let accent = self.accent();

        let frame = egui::Frame {
            fill: ctx.style().visuals.panel_fill,
            // rounding: egui::Rounding::same(12.0),
            stroke: egui::Stroke::new(1.0, egui::Color32::from_rgb(50, 50, 60)),
            ..Default::default()
        };

        egui::CentralPanel::default().frame(frame).show(ctx, |ui| {
            // Branding header strip.
            // self.title_bar(ctx, ui);
            // ui.separator();

            // Tab selector.
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.tab, Tab::Profiles, "Profiles");
                ui.selectable_value(&mut self.tab, Tab::Applications, "Applications");
                ui.selectable_value(&mut self.tab, Tab::Script, "Script");
                ui.selectable_value(&mut self.tab, Tab::Settings, "Settings");
            });
            ui.separator();

            if !self.deps.all_ok() {
                ui.colored_label(
                    egui::Color32::from_rgb(220, 140, 0),
                    format!("\u{26a0} Missing: {}", self.deps.missing().join(", ")),
                );
            }

            // Body area, leaving room for the status bar.
            let body_height = ui.available_height() - 28.0;
            egui::Frame::none().show(ui, |ui| {
                ui.set_min_height(body_height);
                ui.set_max_height(body_height);
                match self.tab {
                    Tab::Profiles => self.profiles_tab(ui),
                    Tab::Applications => self.applications_tab(ui),
                    Tab::Script => self.script_tab(ui),
                    Tab::Settings => self.settings_tab(ui),
                }
            });

            // Status bar.
            ui.separator();
            ui.horizontal(|ui| {
                ui.colored_label(accent, "\u{25cf}");
                ui.small(&self.status);
            });
        });
    }
}
