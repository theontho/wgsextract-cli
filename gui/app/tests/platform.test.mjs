import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import nodePath from "node:path";
import test from "node:test";

const { effectiveWebUIFont, isAppleOperatingSystem, isTauriRuntime, shouldRenderInPageBundleLoader } = await import("../dist/web/src/client/platform.js");
const { createDevReload } = await import("../dist/web/src/server/dev-reload.js");
const { distModulePath } = await import("../dist/web/src/server/paths.js");

test("detects Apple operating systems for WebUI font selection", () => {
  assert.equal(isAppleOperatingSystem({ platform: "MacIntel", userAgent: "Mozilla/5.0" }), true);
  assert.equal(isAppleOperatingSystem({ platform: "iPhone", userAgent: "Mozilla/5.0" }), true);
  assert.equal(isAppleOperatingSystem({ platform: "Linux x86_64", userAgent: "Mozilla/5.0" }), false);
});

test("uses SF Pro automatically on Apple operating systems", () => {
  assert.equal(effectiveWebUIFont("system", { platform: "MacIntel" }), "sf-pro");
  assert.equal(effectiveWebUIFont("system", { platform: "iPad" }), "sf-pro");
  assert.equal(effectiveWebUIFont("system", { platform: "Linux x86_64" }), "system");
  assert.equal(effectiveWebUIFont("sfPro", { platform: "Linux x86_64" }), "sf-pro");
});

test("detects Tauri runtime globals for native desktop UI affordances", () => {
  assert.equal(isTauriRuntime({}), false);
  assert.equal(isTauriRuntime({ __GUI_FOR_CLI_TAURI__: true }), true);
  assert.equal(isTauriRuntime({ __TAURI_INTERNALS__: {} }), true);
  assert.equal(isTauriRuntime({ __TAURI__: {} }), true);
  assert.equal(shouldRenderInPageBundleLoader({}), true);
  assert.equal(shouldRenderInPageBundleLoader({ __GUI_FOR_CLI_TAURI__: true }), false);
  assert.equal(shouldRenderInPageBundleLoader({ __TAURI_INTERNALS__: {} }), false);
  assert.equal(shouldRenderInPageBundleLoader({ __TAURI__: {} }), false);
});

test("resolves nested compiled WebUI modules safely", () => {
  assert.equal(
    distModulePath("/client/view/actions.js", "dist-root"),
    joinedPath("dist-root", "web", "src", "client", "view", "actions.js"),
  );
  assert.equal(
    distModulePath("/shared/rendering.js", "dist-root"),
    joinedPath("dist-root", "shared", "rendering.js"),
  );
  assert.equal(distModulePath("/client/../server/main.js", "dist-root"), undefined);
});

test("dev reload watches nested compiled source directories", async () => {
  const tempRoot = await mkdtemp(nodePath.join(tmpdir(), "gui-for-cli-dev-reload-"));
  let closeWatcher = () => {};
  try {
    const distRoot = nodePath.join(tempRoot, "dist");
    const webRoot = nodePath.join(tempRoot, "web");
    const nestedClientRoot = nodePath.join(distRoot, "web", "src", "client", "view");
    await mkdir(nestedClientRoot, { recursive: true });
    await mkdir(nodePath.join(distRoot, "shared"), { recursive: true });
    await mkdir(webRoot, { recursive: true });

    const response = new MockSseResponse();
    const devReload = createDevReload({ enabled: true, distRoot, webRoot });
    devReload.addClient(response);
    closeWatcher = devReload.installWatcher();

    const reloadEvent = response.nextWriteContaining("event: reload");
    await new Promise((resolve) => setTimeout(resolve, 25));
    await writeFile(nodePath.join(nestedClientRoot, "actions.js"), "changed\n");

    assert.match(await reloadEvent, /data: changed/);
  } finally {
    closeWatcher();
    await rm(tempRoot, { recursive: true, force: true });
  }
});

test("loads bundle distribution metadata", async () => {
  const directory = await mkdtemp(joinedPath(tmpdir(), "gui-for-cli-bundle-metadata-"));
  try {
    await writeFile(joinedPath(directory, "manifest.json"), JSON.stringify({
      id: "metadata-bundle",
      displayName: "Metadata Bundle",
      version: "3.2.1",
    }));
    const { loadBundleMetadata, packageFileStem } = await import("../scripts/bundle-metadata.mjs");

    assert.deepEqual(await loadBundleMetadata(directory), {
      id: "metadata-bundle",
      displayName: "Metadata Bundle",
      version: "3.2.1",
    });
    assert.equal(packageFileStem("WGS Extract 0.3"), "WGS-Extract-0.3");
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("Tauri distribution defaults include Linux distro packages", async () => {
  const {
    distributionBuildPlans,
    distributionPlanEnv,
    parseBundleList,
    platformBundles,
  } = await import("../scripts/tauri-build-dist.mjs");

  assert.deepEqual(platformBundles("darwin"), ["app", "dmg"]);
  assert.deepEqual(platformBundles("linux"), ["deb", "rpm", "appimage"]);
  assert.deepEqual(platformBundles("win32"), ["nsis"]);
  assert.deepEqual(parseBundleList(" deb, rpm,appimage "), ["deb", "rpm", "appimage"]);
  assert.deepEqual(distributionBuildPlans(["deb", "rpm"], "linux"), [
    { bundles: ["deb"], env: {} },
    { bundles: ["rpm"], env: {} },
  ]);
  assert.deepEqual(distributionBuildPlans(["app", "dmg"], "darwin"), [
    { bundles: ["app", "dmg"], env: {} },
  ]);
  assert.deepEqual(distributionBuildPlans(["nsis"], "win32"), [
    { bundles: ["nsis"], env: {} },
  ]);
  assert.deepEqual(
    distributionPlanEnv(
      { TAURI_PRODUCT_SUFFIX: "Custom WebUI", TAURI_CLEAN_RELEASE_BUNDLE: "0", EXAMPLE: "caller" },
      { bundles: ["deb"], env: {} },
      0,
    ),
    { TAURI_PRODUCT_SUFFIX: "Custom WebUI", TAURI_CLEAN_RELEASE_BUNDLE: "1", EXAMPLE: "caller" },
  );
  assert.deepEqual(
    distributionPlanEnv(
      { TAURI_PRODUCT_SUFFIX: "Custom WebUI", EXAMPLE: "caller" },
      { bundles: ["app"], env: {} },
      0,
    ),
    { TAURI_PRODUCT_SUFFIX: "Custom WebUI", TAURI_CLEAN_RELEASE_BUNDLE: "1", EXAMPLE: "caller" },
  );
  assert.deepEqual(
    distributionPlanEnv(
      { TAURI_CLEAN_RELEASE_BUNDLE: "1" },
      { bundles: ["rpm"], env: {} },
      1,
    ),
    { TAURI_CLEAN_RELEASE_BUNDLE: "0" },
  );
});

test("Tauri product name keeps public apps on base name", async () => {
  const { tauriProductName } = await import("../scripts/run-tauri.mjs");

  assert.equal(tauriProductName("WGSExtract"), "WGSExtract");
  assert.equal(tauriProductName("WGSExtract macOS WebUI"), "WGSExtract");
  assert.equal(tauriProductName("WGSExtract", "Ubuntu WebUI"), "WGSExtract Ubuntu WebUI");
  assert.equal(tauriProductName(" WGSExtract ", " Ubuntu WebUI "), "WGSExtract Ubuntu WebUI");
  assert.equal(tauriProductName("WGSExtract", "none"), "WGSExtract");
  assert.equal(tauriProductName("WGSExtract", "NONE"), "WGSExtract");
  assert.equal(tauriProductName("WGSExtract", "None"), "WGSExtract");
  assert.equal(tauriProductName("WGSExtract Windows WebUI", "none"), "WGSExtract");
  assert.equal(tauriProductName("WGSExtract Windows WebUI", "   "), "WGSExtract");
  assert.equal(tauriProductName(null), null);
  assert.equal(tauriProductName("   "), null);
});

test("About dialog renders bundle and tool versions with clickable GitHub link", async () => {
  const previousWindow = globalThis.window;
  const previousLocalStorage = globalThis.localStorage;
  const localStorage = memoryStorage();
  globalThis.window = { GUI_FOR_CLI_APPLICATION_NAME: "GUI for CLI", GUI_FOR_CLI_APPLICATION_VERSION: "9.8.7" };
  globalThis.localStorage = localStorage;
  try {
    const { state } = await import("../dist/web/src/client/state.js");
    const { aboutVersionRows, githubURL, renderAboutDialog } = await import(`../dist/web/src/client/view/about.js?about=${Date.now()}`);
    state.manifest = {
      version: "0.3.5",
      setup: { steps: [{ toolName: "WGS Extract CLI", toolVersion: "v0.3.5" }] },
      uninstall: { steps: [] },
    };

    assert.deepEqual(aboutVersionRows({ guiForCliVersion: state.applicationVersion, manifest: state.manifest }), [
      { label: "GUI for CLI version", value: "9.8.7" },
      { label: "Bundle version", value: "0.3.5" },
      { label: "Tool version", value: "WGS Extract CLI v0.3.5" },
    ]);
    const html = renderAboutDialog();
    assert.match(html, /MIT License/);
    assert.match(html, /GUI for CLI version/);
    assert.match(html, /Bundle version/);
    assert.match(html, /Tool version/);
    assert.match(html, /WGS Extract CLI v0\.3\.5/);
    assert.match(html, new RegExp(`href="${githubURL}"[^>]+data-about-github`));
  } finally {
    if (previousWindow === undefined) {
      delete globalThis.window;
    } else {
      globalThis.window = previousWindow;
    }
    if (previousLocalStorage === undefined) {
      delete globalThis.localStorage;
    } else {
      globalThis.localStorage = previousLocalStorage;
    }
  }
});

test("Tauri updater config remains valid when updates are not configured", async () => {
  const { tauriUpdaterPluginConfig } = await import("../scripts/run-tauri.mjs");

  assert.deepEqual(tauriUpdaterPluginConfig({}), { pubkey: "", endpoints: [] });
  assert.deepEqual(
    tauriUpdaterPluginConfig({
      TAURI_UPDATER_PUBKEY: "public-key",
      TAURI_UPDATER_ENDPOINTS: "https://example.test/latest.json,https://example.test/beta.json",
      TAURI_UPDATER_WINDOWS_INSTALL_MODE: "quiet",
    }),
    {
      pubkey: "public-key",
      endpoints: ["https://example.test/latest.json", "https://example.test/beta.json"],
      windows: { installMode: "quiet" },
    },
  );
});

test("Tauri grants IPC only to the exact backend origin and only the commands the page uses", async () => {
  const main = await readFile(new URL("../web/packagers/tauri/src/main.rs", import.meta.url), "utf8");
  const buildScript = await readFile(new URL("../web/packagers/tauri/build.rs", import.meta.url), "utf8");
  const config = JSON.parse(await readFile(new URL("../web/packagers/tauri/tauri.conf.json", import.meta.url), "utf8"));
  const capability = main.slice(main.indexOf('CapabilityBuilder::new("backend-webui")'), main.indexOf(")?;", main.indexOf('CapabilityBuilder::new("backend-webui")')));

  assert.ok(capability, "the backend capability is built at runtime once the port is known");
  assert.match(main, /let origin = format!\("http:\/\/127\.0\.0\.1:\{port\}"\);/);
  assert.match(capability, /\.remote\(origin\.clone\(\)\)/);
  assert.deepEqual([...capability.matchAll(/\.permission\("([^"]+)"\)/g)].map((match) => match[1]), [
    "core:event:allow-listen",
    "core:event:allow-unlisten",
    "allow-gfc-report-rendered",
    "allow-gfc-update-check",
    "allow-gfc-update-download",
    "allow-gfc-update-install",
  ]);
  assert.doesNotMatch(main, /updater:default/);
  await assert.rejects(readFile(new URL("../web/packagers/tauri/capabilities/main.json", import.meta.url)), { code: "ENOENT" });
  assert.equal(config.app.security.capabilities, undefined);
  for (const command of ["gfc_report_rendered", "gfc_update_check", "gfc_update_download", "gfc_update_install"]) {
    assert.match(buildScript, new RegExp(`"${command}"`));
  }
});

test("Tauri hands the API token to Node and its own page without argv", async () => {
  const backend = await readFile(new URL("../web/packagers/tauri/src/backend.rs", import.meta.url), "utf8");
  const main = await readFile(new URL("../web/packagers/tauri/src/main.rs", import.meta.url), "utf8");

  assert.match(backend, /\.env\("GFC_API_TOKEN", launch\.api_token\)/);
  assert.match(backend, /\.args\(\["--port", "0", "--host", "127\.0\.0\.1", "--bundle"\]\)/);
  assert.doesNotMatch(backend, /\.arg\([^)]*token/i);
  assert.match(main, /if \(location\.origin !== \{origin:\?\}\)/);
});

test("Tauri hands test harnesses the token only next to the port file they asked for", async () => {
  const main = await readFile(new URL("../web/packagers/tauri/src/main.rs", import.meta.url), "utf8");
  const lifecycle = await readFile(new URL("../../scripts/validate-windows-tauri-lifecycle.ps1", import.meta.url), "utf8");
  const harness = main.slice(main.indexOf("fn write_test_harness_files"), main.indexOf("fn write_private_file"));

  assert.match(harness, /let Some\(path\) = env::var_os\(PORT_FILE_ENV\)[^;]*else \{\s*return Ok\(\(\)\);/);
  assert.ok(harness.indexOf('push(".token")') < harness.indexOf("fs::write(path"), "token file is written before the port file");
  assert.match(main, /OpenOptionsExt::mode\(&mut options, 0o600\)/);
  assert.match(lifecycle, /\$TokenFile = "\$PortFile\.token"/);
  assert.match(lifecycle, /'x-gfc-token': token/);
});

test("Tauri updater menu follows platform menu conventions", async () => {
  const menuRs = await readFile(new URL("../web/packagers/tauri/src/menu.rs", import.meta.url), "utf8");
  const mainRs = await readFile(new URL("../web/packagers/tauri/src/main.rs", import.meta.url), "utf8");

  assert.doesNotMatch(menuRs, /Submenu::with_items\(app, "Updates"/);
  assert.match(menuRs, /#\[cfg\(target_os = "macos"\)\]\s+add_check_for_updates_to_app_menu/);
  assert.match(menuRs, /let app_menu_title = app_menu_title\(app\);/);
  assert.match(menuRs, /find_submenu_by_text\(menu, &app_menu_title\)/);
  assert.match(menuRs, /#\[cfg\(not\(target_os = "macos"\)\)\]\s+add_items_to_file_menu\(app, menu, &\[load_bundle, check_for_updates\]\)/);
  // Neither the Trash nor a Linux package manager removes what setup put in the home folder; the File
  // menu has the app's Uninstall everywhere but Windows, whose NSIS uninstaller does it.
  assert.match(menuRs, /#\[cfg\(not\(windows\)\)\]\s+add_uninstall_to_file_menu\(app, menu\)/);
  assert.match(mainRs, /cfg!\(not\(windows\)\) && env_flag\(app_uninstall::AUTO_RUN_UNINSTALL_ENV\)/);
  assert.match(mainRs, /mod menu;/);
  assert.match(mainRs, /app_uninstall::UNINSTALL_MENU_ID => app_uninstall::request_uninstall\(app\)/);
});

test("Tauri child env keeps macOS signing identity aligned", async () => {
  const { effectiveMacOSSigningIdentity, tauriChildEnv } = await import("../scripts/run-tauri.mjs");
  const env = {
    APPLE_SIGNING_IDENTITY: "",
    TAURI_MACOS_SIGNING_IDENTITY: "-",
  };

  assert.equal(effectiveMacOSSigningIdentity(env, {}), "-");
  assert.deepEqual(tauriChildEnv(env, "darwin", {}), {
    APPLE_SIGNING_IDENTITY: "-",
    TAURI_MACOS_SIGNING_IDENTITY: "-",
  });
  assert.deepEqual(tauriChildEnv(env, "linux", {}), env);

  const appleOnlyEnv = { APPLE_SIGNING_IDENTITY: "Developer ID Application: Example" };
  assert.equal(effectiveMacOSSigningIdentity(appleOnlyEnv, {}), "Developer ID Application: Example");
  assert.deepEqual(tauriChildEnv(appleOnlyEnv, "darwin", {}), {
    APPLE_SIGNING_IDENTITY: "Developer ID Application: Example",
    TAURI_MACOS_SIGNING_IDENTITY: "Developer ID Application: Example",
  });

  assert.equal(effectiveMacOSSigningIdentity({}, {}), "");
  assert.deepEqual(tauriChildEnv({}, "darwin", {}), {});

  const devConfig = { apple: { signing: { signing_identity: "Developer ID Application: Local" } } };
  assert.equal(effectiveMacOSSigningIdentity({}, devConfig), "Developer ID Application: Local");
  assert.equal(effectiveMacOSSigningIdentity(appleOnlyEnv, devConfig), "Developer ID Application: Example");
});

test("render scroll state restores only matching containers", async () => {
  const { captureScrollState, restoreScrollState } = await import("../dist/web/src/client/scroll-state.js");
  const original = { scrollLeft: 13, scrollTop: 377 };
  const replacement = { scrollLeft: 0, scrollTop: 0 };
  const other = { scrollLeft: 0, scrollTop: 0 };
  const snapshot = captureScrollState(original, "settings");

  assert.deepEqual(snapshot, { key: "settings", left: 13, top: 377 });
  assert.equal(restoreScrollState(other, snapshot, "fastq"), false);
  assert.deepEqual(other, { scrollLeft: 0, scrollTop: 0 });
  assert.equal(restoreScrollState(replacement, snapshot, "settings"), true);
  assert.deepEqual(replacement, { scrollLeft: 13, scrollTop: 377 });
});

test("Tauri NSIS uninstall app data cleanup removes the backend data directory", async () => {
  const config = JSON.parse(await readFile(new URL("../web/packagers/tauri/tauri.conf.json", import.meta.url), "utf8"));
  const hooks = await readFile(new URL("../web/packagers/tauri/nsis-hooks.nsh", import.meta.url), "utf8");
  const appDataBlock = hooks.slice(hooks.indexOf("$DeleteAppDataCheckboxState = 1"));

  assert.equal(config.bundle.windows.nsis.installerHooks, "nsis-hooks.nsh");
  assert.deepEqual(appDataBlock.match(/RmDir \/r "[^"]+"/g), ['RmDir /r "$LOCALAPPDATA\\${BUNDLEID}"']);
});

test("Tauri NSIS uninstall always removes the install dir and per-user productName leftover", async () => {
  const hooks = await readFile(new URL("../web/packagers/tauri/nsis-hooks.nsh", import.meta.url), "utf8");

  assert.match(hooks, /Delete \/REBOOTOK "\$INSTDIR\\uninstall\.exe"/);
  assert.match(hooks, /RmDir \/REBOOTOK "\$INSTDIR"/);
  assert.match(hooks, /RmDir \/REBOOTOK "\$LOCALAPPDATA\\\$\{PRODUCTNAME\}"/);
  assert.match(hooks, /RmDir \/REBOOTOK "\$LOCALAPPDATA\\Programs\\\$\{PRODUCTNAME\}"/);

  const updateGuardIndex = hooks.indexOf("$UpdateMode <> 1");
  const deleteAppDataGuardIndex = hooks.indexOf("$DeleteAppDataCheckboxState = 1");
  const uninstallExeIndex = hooks.indexOf("Delete /REBOOTOK \"$INSTDIR\\uninstall.exe\"");
  assert.ok(updateGuardIndex >= 0 && uninstallExeIndex > updateGuardIndex,
    "install-dir cleanup must be guarded by $UpdateMode but not by $DeleteAppDataCheckboxState");
  assert.ok(deleteAppDataGuardIndex < 0 || deleteAppDataGuardIndex > uninstallExeIndex,
    "install-dir cleanup must run before the $DeleteAppDataCheckboxState branch");
});

test("platform script resolution rejects paths that escape the bundle root", async () => {
  const directory = await mkdtemp(joinedPath(tmpdir(), "gui-for-cli-platform-script-safe-"));
  try {
    await mkdir(joinedPath(directory, "scripts", "windows"), { recursive: true });
    await mkdir(joinedPath(directory, "scripts", "posix"), { recursive: true });
    await writeFile(joinedPath(directory, "scripts", "windows", "safe.ps1"), "Write-Output safe\n");
    await writeFile(joinedPath(directory, "scripts", "posix", "safe.sh"), "echo safe\n");
    const { resolvePlatformScriptPath } = await import("../dist/web/src/server/platform-scripts.js");

    await assert.rejects(
      () => resolvePlatformScriptPath("scripts/../../outside.sh", directory),
      /Bundle script path escapes bundle root/
    );
    const resolvedScript = await resolvePlatformScriptPath("scripts/safe.sh", directory);
    assert.equal(nodePath.basename(nodePath.dirname(resolvedScript)), process.platform === "win32" ? "windows" : "posix");
    assert.equal(nodePath.basename(resolvedScript), process.platform === "win32" ? "safe.ps1" : "safe.sh");
    assert.equal(await readFile(resolvedScript, "utf8"), process.platform === "win32" ? "Write-Output safe\n" : "echo safe\n");
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

function joinedPath(...parts) {
  return parts.join(process.platform === "win32" ? "\\" : "/");
}

function memoryStorage() {
  const values = new Map();
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    removeItem(key) {
      values.delete(key);
    },
  };
}

class MockSseResponse extends EventEmitter {
  writeHead() {}

  write(chunk) {
    this.emit("write", String(chunk));
    return true;
  }

  nextWriteContaining(fragment) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.off("write", onWrite);
        reject(new Error(`Timed out waiting for ${fragment}`));
      }, 1_000);
      const onWrite = (chunk) => {
        if (!chunk.includes(fragment)) {
          return;
        }
        clearTimeout(timer);
        this.off("write", onWrite);
        resolve(chunk);
      };
      this.on("write", onWrite);
    });
  }
}
