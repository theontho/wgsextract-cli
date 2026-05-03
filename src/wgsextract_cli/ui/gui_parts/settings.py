"""Settings frame for configuring application paths, cache, and tool dependencies."""


import customtkinter as ctk

from wgsextract_cli.ui.gui_parts.common import ScrollableBaseFrame


class SettingsFrame(ScrollableBaseFrame):
    """Settings frame for configuring application paths, cache, and tool dependencies."""

    def setup_ui(self) -> None:
        """Set up the settings UI elements."""
        super().setup_ui()

        # Content frame for better alignment
        content_frame = ctk.CTkFrame(self, fg_color="transparent")
        content_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # Section: Application Paths
        self.create_section_title(
            content_frame,
            "Application Paths",
            "Configure default paths used across the application.",
        )

        self.create_dir_selector(
            content_frame,
            label_text="Reference Library:",
            variable=self.main_app.ref_path_var,
            info_text="Directory where reference genome files (e.g. FASTA) are stored.",
        )

        self.create_dir_selector(
            content_frame,
            label_text="Output Directory:",
            variable=self.main_app.out_dir_var,
            info_text="Default directory for saving outputs.",
        )

        self.create_file_selector(
            content_frame,
            label_text="Yleaf Path:",
            variable=self.main_app.yleaf_path_var,
            info_text="Path to the Yleaf executable for Y-haplogroup prediction.",
        )

        self.create_file_selector(
            content_frame,
            label_text="Haplogrep Path:",
            variable=self.main_app.haplogrep_path_var,
            info_text="Path to the Haplogrep jar or executable for mitochondrial lineage.",
        )

        self.create_file_selector(
            content_frame,
            label_text="Input VCF:",
            variable=self.main_app.vcf_path_var,
            info_text="Default primary VCF file path.",
        )

        self.create_file_selector(
            content_frame,
            label_text="Mother VCF:",
            variable=self.main_app.vcf_mother_var,
            info_text="Default mother VCF file path for trio analysis.",
        )

        self.create_file_selector(
            content_frame,
            label_text="Father VCF:",
            variable=self.main_app.vcf_father_var,
            info_text="Default father VCF file path for trio analysis.",
        )

        # Section: Configuration Cache
        self.create_section_title(
            content_frame,
            "Configuration File",
            "Persistent settings are stored in this TOML file.",
        )

        self.create_read_only_entry_with_info(
            content_frame,
            label_text="Config File:",
            variable=self.main_app.config_path_var,
            info_text="Path to the config.toml file where your settings are persisted.",
        )

        # Save Button
        btn_frame = ctk.CTkFrame(content_frame, fg_color="transparent")
        btn_frame.pack(fill="x", pady=20)

        ctk.CTkButton(
            btn_frame,
            text="Save Configuration",
            command=self.save_settings,
            font=("Courier", 13, "bold"),
            fg_color="#3a7ebf",
            hover_color="#325882",
        ).pack(side="right", padx=30)

        # Status Label
        self.status_label = ctk.CTkLabel(btn_frame, text="", text_color="green")
        self.status_label.pack(side="right", padx=10)

    def save_settings(self) -> None:
        """Save current paths to config.toml."""
        from wgsextract_cli.core.config import save_config

        updates = {
            "reference_library": self.main_app.ref_path_var.get(),
            "output_directory": self.main_app.out_dir_var.get(),
            "yleaf_executable": self.main_app.yleaf_path_var.get(),
            "haplogrep_executable": self.main_app.haplogrep_path_var.get(),
            "default_input_vcf": self.main_app.vcf_path_var.get(),
            "mother_vcf_path": self.main_app.vcf_mother_var.get(),
            "father_vcf_path": self.main_app.vcf_father_var.get(),
        }

        try:
            save_config(updates)
            self.status_label.configure(
                text="Settings saved to config.toml!", text_color="green"
            )
            # Clear status after 3 seconds
            self.after(3000, lambda: self.status_label.configure(text=""))
            self.main_app.log("Saved configuration to config.toml")
        except Exception as e:
            self.status_label.configure(text=f"Error saving: {e}", text_color="red")
            self.main_app.log(f"Error saving configuration: {e}")
