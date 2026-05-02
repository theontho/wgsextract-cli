"""Settings tab frame for the Web GUI."""

from nicegui import ui

from wgsextract_cli.core.config import save_config

from .common import ui_row_dir, ui_row_input
from .state import state


async def handle_save_config():
    """Save current state to config.toml."""
    updates = {
        "output_directory": state.out_dir,
        "reference_library": state.ref_path,
        "yleaf_executable": state.yleaf_path,
        "haplogrep_executable": state.haplogrep_path,
    }
    try:
        save_config(updates)
        ui.notify("Settings saved to config.toml", type="positive")
    except Exception as e:
        ui.notify(f"Failed to save settings: {e}", type="negative")


def frame_settings():
    ui.label("Settings").classes("text-h5 mb-2")
    ui.markdown("Configure global paths and application settings.").classes(
        "text-slate-400 mb-4"
    )

    with ui.card().classes("w-full p-4 gap-4"):
        ui_row_dir("Output Directory", "out_dir")
        ui_row_dir("Reference Library", "ref_path")
        ui_row_input("Yleaf Execution Path", "yleaf_path")
        ui_row_input("Haplogrep JAR Path", "haplogrep_path")

        ui.separator()
        with ui.row().classes("w-full justify-end"):
            ui.button("Save Settings", on_click=handle_save_config).props("primary")
