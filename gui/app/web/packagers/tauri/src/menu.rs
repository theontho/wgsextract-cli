use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::Manager;

pub(crate) const CHECK_FOR_UPDATES_MENU_ID: &str = "check-for-updates";
pub(crate) const LOAD_BUNDLE_MENU_ID: &str = "load-bundle";
pub(crate) const ABOUT_MENU_ID: &str = "about-gui-for-cli";

pub(crate) fn app_menu<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> tauri::Result<Menu<R>> {
    let menu = Menu::default(app)?;
    replace_about_menu_item(app, &menu)?;
    let load_bundle = MenuItem::with_id(
        app,
        LOAD_BUNDLE_MENU_ID,
        "Load Bundle...",
        true,
        Some("CmdOrCtrl+O"),
    )?;
    let check_for_updates = MenuItem::with_id(
        app,
        CHECK_FOR_UPDATES_MENU_ID,
        "Check for Updates...",
        true,
        None::<&str>,
    )?;
    add_platform_menu_items(app, &menu, &load_bundle, &check_for_updates)?;
    Ok(menu)
}

#[cfg(target_os = "macos")]
fn replace_about_menu_item<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    menu: &Menu<R>,
) -> tauri::Result<()> {
    let menu_items = menu.items()?;
    let Some(app_menu) = menu_items.first().and_then(|item| item.as_submenu()) else {
        return Ok(());
    };
    let _ = app_menu.remove_at(0)?;
    let about_label = format!("About {}", app_name(app));
    let about = MenuItem::with_id(app, ABOUT_MENU_ID, about_label, true, None::<&str>)?;
    app_menu.insert(&about, 0)?;
    Ok(())
}

#[cfg(not(target_os = "macos"))]
fn replace_about_menu_item<R: tauri::Runtime>(
    _app: &tauri::AppHandle<R>,
    _menu: &Menu<R>,
) -> tauri::Result<()> {
    Ok(())
}

fn add_platform_menu_items<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    menu: &Menu<R>,
    load_bundle: &MenuItem<R>,
    check_for_updates: &MenuItem<R>,
) -> tauri::Result<()> {
    #[cfg(target_os = "macos")]
    add_check_for_updates_to_app_menu(app, menu, check_for_updates)?;

    #[cfg(target_os = "macos")]
    add_items_to_file_menu(app, menu, &[load_bundle])?;

    #[cfg(not(target_os = "macos"))]
    add_items_to_file_menu(app, menu, &[load_bundle, check_for_updates])?;

    #[cfg(not(windows))]
    add_uninstall_to_file_menu(app, menu)?;

    Ok(())
}

/// Neither the Trash nor a Linux package manager removes what setup put in the user's home; the
/// app's Uninstall does. Windows uses its NSIS uninstaller.
#[cfg(not(windows))]
fn add_uninstall_to_file_menu<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    menu: &Menu<R>,
) -> tauri::Result<()> {
    let uninstall = MenuItem::with_id(
        app,
        crate::app_uninstall::UNINSTALL_MENU_ID,
        format!("Uninstall {}...", app_name(app)),
        true,
        None::<&str>,
    )?;
    if let Some(file_menu) = find_submenu_by_text(menu, "File")? {
        // After Load Bundle and Check for Updates, before the separator.
        file_menu.insert(&uninstall, 2)?;
    }

    Ok(())
}

#[cfg(target_os = "macos")]
fn add_check_for_updates_to_app_menu<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    menu: &Menu<R>,
    check_for_updates: &MenuItem<R>,
) -> tauri::Result<()> {
    let app_menu_title = app_menu_title(app);
    if let Some(app_menu) = find_submenu_by_text(menu, &app_menu_title)? {
        app_menu.insert_items(&[check_for_updates], 1)?;
    } else {
        let app_menu = Submenu::with_items(app, app_menu_title, true, &[check_for_updates])?;
        menu.insert(&app_menu, 0)?;
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn app_menu_title<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> String {
    app.config()
        .product_name
        .clone()
        .unwrap_or_else(|| app.package_info().name.clone())
}

fn add_items_to_file_menu<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    menu: &Menu<R>,
    leading_items: &[&MenuItem<R>],
) -> tauri::Result<()> {
    let items: Vec<&dyn tauri::menu::IsMenuItem<R>> = leading_items
        .iter()
        .map(|item| *item as &dyn tauri::menu::IsMenuItem<R>)
        .collect();
    if let Some(file_menu) = find_submenu_by_text(menu, "File")? {
        let separator = PredefinedMenuItem::separator(app)?;
        let mut file_items = items;
        file_items.push(&separator);
        file_menu.insert_items(&file_items, 0)?;
        return Ok(());
    }

    let file_menu = Submenu::with_items(app, "File", true, &items)?;
    #[cfg(target_os = "macos")]
    menu.insert(&file_menu, 1)?;
    #[cfg(not(target_os = "macos"))]
    menu.insert(&file_menu, 0)?;
    Ok(())
}

fn find_submenu_by_text<R: tauri::Runtime>(
    menu: &Menu<R>,
    text: &str,
) -> tauri::Result<Option<Submenu<R>>> {
    for item in menu.items()? {
        let Some(submenu) = item.as_submenu() else {
            continue;
        };
        if submenu.text()? == text {
            return Ok(Some(submenu.clone()));
        }
    }
    Ok(None)
}

pub(crate) fn request_about<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        if let Err(error) = window.eval("window.dispatchEvent(new Event('gui-for-cli-show-about'))")
        {
            eprintln!("Failed to dispatch about event: {error}");
        }
    }
}

pub(crate) fn request_load_bundle<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        if let Err(error) =
            window.eval("window.dispatchEvent(new Event('gui-for-cli-load-bundle'))")
        {
            eprintln!("Failed to dispatch load bundle event: {error}");
        }
    }
}

pub(crate) fn app_name<R: tauri::Runtime, M: Manager<R>>(app: &M) -> String {
    app.config()
        .product_name
        .clone()
        .unwrap_or_else(|| "GUI for CLI".to_string())
}
