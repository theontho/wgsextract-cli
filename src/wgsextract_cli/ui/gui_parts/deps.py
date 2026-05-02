"""Frame for checking and displaying external tool dependencies."""

import threading
from typing import Any

import customtkinter as ctk

from wgsextract_cli.core.dependencies import check_all_dependencies
from wgsextract_cli.ui.gui_parts.common import ScrollableBaseFrame, ToolTip


class DepsFrame(ScrollableBaseFrame):
    """Frame for checking and displaying external tool dependencies."""

    def setup_ui(self) -> None:
        """Set up the dependencies UI elements."""
        super().setup_ui()

        # Content frame for better alignment
        self.content_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.content_frame.pack(fill="both", expand=True, padx=20, pady=10)

        self.refresh()

    def refresh(self) -> None:
        """Clear the content and run a new check."""
        # Clear existing
        for widget in self.content_frame.winfo_children():
            widget.destroy()

        # Loading screen
        self.show_loading()

        # Run check in a thread
        threading.Thread(target=self.run_check, daemon=True).start()

    def show_loading(self) -> None:
        """Show a loading message."""
        self.loading_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.loading_frame.pack(expand=True, pady=50)

        ctk.CTkLabel(
            self.loading_frame,
            text="Checking Dependencies...",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=10)

        ctk.CTkLabel(
            self.loading_frame,
            text="This takes a while as we perform functional tests on each tool.",
            font=ctk.CTkFont(size=12, slant="italic"),
        ).pack(pady=5)

        # Optional: Add a progress bar
        self.progress = ctk.CTkProgressBar(self.loading_frame)
        self.progress.pack(pady=20, padx=20)
        self.progress.configure(mode="indeterminate")
        self.progress.start()

    def run_check(self) -> None:
        """Run the dependency check and update the UI."""
        results = check_all_dependencies()
        # Schedule UI update on main thread
        if self.winfo_exists():
            self.after(0, lambda: self.display_results(results))

    def display_results(self, results: dict[str, list[dict[str, Any]]]) -> None:
        """Display the check results."""
        if not self.winfo_exists():
            return

        if hasattr(self, "loading_frame") and self.loading_frame.winfo_exists():
            self.loading_frame.destroy()

        # Clear existing (just in case)
        for widget in self.content_frame.winfo_children():
            widget.destroy()

        # Mandatory Section
        self.create_section_title(
            self.content_frame,
            "Mandatory Tools",
            "Essential tools required for core functionality (Alignment, Sorting, Indexing).",
        )
        self._render_group(results["mandatory"])

        # Optional Section
        self.create_section_title(
            self.content_frame,
            "Optional Tools",
            "Tools required for specific features (Variant Calling, Ancestry, VEP).",
        )
        self._render_group(results["optional"])

        # Refresh Button
        refresh_btn_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        refresh_btn_frame.pack(fill="x", pady=20)
        ctk.CTkButton(
            refresh_btn_frame,
            text="Refresh Dependencies",
            command=self.refresh,
            font=("Courier", 11, "bold"),
            width=200,
        ).pack(side="right", padx=30)

    def _render_group(self, tools: list[dict[str, Any]]) -> None:
        """Render a group of tools in a grid."""
        grid_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        grid_frame.pack(fill="x", pady=5)

        row = 0
        col = 0
        max_cols = 4

        for tool in tools:
            name = tool["name"]
            path = tool["path"]
            version = tool["version"]

            is_valid = path is not None and version and not version.startswith("Error:")
            color = "#4CAF50" if is_valid else "#F44336"
            symbol = "✓" if is_valid else "✗"

            tool_frame = ctk.CTkFrame(grid_frame, fg_color="transparent")
            tool_frame.grid(row=row, column=col, padx=10, pady=5, sticky="w")

            symbol_lbl = ctk.CTkLabel(
                tool_frame, text=symbol, text_color=color, font=("Courier", 14, "bold")
            )
            symbol_lbl.pack(side="left", padx=(0, 5))

            display_text = name
            if (
                version
                and not version.startswith("Available")
                and not version.startswith("Error:")
            ):
                # Extract short version if possible
                short_v = version.splitlines()[0]
                if len(short_v) > 20:
                    short_v = short_v[:17] + "..."
                display_text += f" ({short_v})"

            name_lbl = ctk.CTkLabel(tool_frame, text=display_text)
            name_lbl.pack(side="left")

            # Tooltips
            details = []
            if path:
                details.append(f"Path: {path}")
            if version:
                details.append(f"Status: {version}")
            if not path:
                details.append("Error: Tool not found in PATH or pixi environments.")

            tooltip_text = "\n".join(details)
            ToolTip(name_lbl, tooltip_text)
            ToolTip(symbol_lbl, tooltip_text)

            col += 1
            if col >= max_cols:
                col = 0
                row += 1
