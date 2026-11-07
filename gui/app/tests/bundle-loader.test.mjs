import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { createRequire, syncBuiltinESMExports } from "node:module";
import { fileURLToPath } from "node:url";

test("a failed preload that no request uses does not become an unhandled rejection", async (t) => {
  // An unhandled rejection ends the server; the app's window then only shows "Failed to fetch".
  const unhandled = [];
  const onUnhandled = (reason) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  t.after(() => process.off("unhandledRejection", onUnhandled));
  const { createOneShotBundlePreload } = await import("../dist/web/src/server/bundle-loader.js");
  const loader = createOneShotBundlePreload(async (locale) => {
    if (locale === undefined) {
      throw new Error("bootstrap failed");
    }
    return { locale };
  }, undefined, true);

  // The window's request carries Accept-Language, so it does its own load.
  assert.deepEqual(await loader.load("en", ["en-US"]), { locale: "en" });
  await new Promise((resolve) => setTimeout(resolve, 50));

  assert.deepEqual(unhandled, []);
  await assert.rejects(loader.preloaded, /bootstrap failed/);
});

test("one-shot bundle preload starts immediately and serves the first matching request", async () => {
  const calls = [];
  const { createOneShotBundlePreload } = await import("../dist/web/src/server/bundle-loader.js");
  const loader = createOneShotBundlePreload(async (locale) => {
    calls.push(locale ?? null);
    return { locale: locale ?? null, callCount: calls.length };
  }, undefined, true);

  assert.deepEqual(calls, [null]);
  assert.deepEqual(await loader.load(undefined), { locale: null, callCount: 1 });
  assert.deepEqual(calls, [null]);
  assert.deepEqual(await loader.load(undefined), { locale: null, callCount: 2 });
  assert.deepEqual(calls, [null, null]);
});

test("one-shot bundle preload is skipped when no bundle was explicitly requested", async () => {
  const calls = [];
  const { createOneShotBundlePreload } = await import("../dist/web/src/server/bundle-loader.js");
  const loader = createOneShotBundlePreload(async (locale) => {
    calls.push(locale ?? null);
    return { locale: locale ?? null };
  }, "en", false);

  assert.equal(loader.preloaded, undefined);
  assert.deepEqual(calls, []);
  assert.deepEqual(await loader.load("en"), { locale: "en" });
  assert.deepEqual(calls, ["en"]);
});

test("bundle source resolver accepts bundle directories and manifest files", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-source-root-"));
  try {
    const manifestPath = path.join(directory, "manifest.json");
    await writeFile(
      manifestPath,
      JSON.stringify({
        id: "source-root",
        displayName: "Source Root",
        summary: "Tests source resolution.",
        pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
      }),
    );

    const { resolveBundleSourceRoot } = await import("../dist/web/src/server/bundle-loader.js");
    assert.equal(await resolveBundleSourceRoot(directory), path.resolve(directory));
    assert.equal(await resolveBundleSourceRoot(manifestPath), path.resolve(directory));
  }
  finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("bundle source resolver rejects non-manifest files", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-source-root-invalid-"));
  try {
    const filePath = path.join(directory, "bundle.txt");
    await writeFile(filePath, "not a bundle");
    const { resolveBundleSourceRoot } = await import("../dist/web/src/server/bundle-loader.js");
    await assert.rejects(
      () => resolveBundleSourceRoot(filePath),
      /Choose a bundle folder, manifest\.json file, or bundle archive/,
    );
  }
  finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("bundle source resolver normalizes missing path errors", async () => {
  const { resolveBundleSourceRoot } = await import("../dist/web/src/server/bundle-loader.js");

  await assert.rejects(
    () => resolveBundleSourceRoot(path.join(tmpdir(), "gui-for-cli-missing-bundle")),
    /Choose a bundle folder, manifest\.json file, or bundle archive/,
  );
});

test("bundle loader accepts comments in bundle JSON files", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-json-comments-"));
  try {
    await mkdir(path.join(directory, "pages"), { recursive: true });
    await writeFile(
      path.join(directory, "manifest.json"),
      `{
        // Bundle authors can leave versioning notes here.
        "id": "json-comments",
        "version": "1.2.3",
        "displayName": "JSON Comments",
        "summary": "https://example.com/not-a-comment",
        "pages": [
          "main.json" /* page files can be annotated too */
        ]
      }`,
    );
    await writeFile(
      path.join(directory, "pages", "main.json"),
      `{
        "id": "main",
        "title": "Main",
        "summary": "literal /* not a comment */ text",
        // The rest of the page stays normal JSON.
        "sections": [{ "id": "main" }]
      }`,
    );

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    const manifest = await loadManifestFromRoot(directory);

    assert.equal(manifest.version, "1.2.3");
    assert.equal(manifest.summary, "https://example.com/not-a-comment");
    assert.equal(manifest.pages[0].summary, "literal /* not a comment */ text");
  }
  finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("icon map TOML parses source-specific aliases", async () => {
  const { parseIconMapToml } = await import("../dist/shared/icon-map.js");
  const iconMap = parseIconMapToml(`
[sf-symbols]
"fasta" = "point.3.connected.trianglepath.dotted"

[windows]
"download" = "\\uE896"
"refresh" = " \\uE72C"

[bootstrap]
"warning" = "exclamation-triangle-fill"

[emoji]
"warning" = "⚠️"
`);

  assert.equal(iconMap["sf-symbols"].fasta, "point.3.connected.trianglepath.dotted");
  assert.equal(iconMap.windows.download, "\uE896");
  assert.equal(iconMap.windows.refresh, " \uE72C");
  assert.equal(iconMap.bootstrap.warning, "exclamation-triangle-fill");
  assert.equal(iconMap.emoji.warning, "⚠️");
});

test("icon map TOML rejects malformed content", async () => {
  const { parseIconMapToml } = await import("../dist/shared/icon-map.js");
  assert.throws(() => parseIconMapToml(`[emoji]\n"warning" = "\\uZZZZ"`), /Invalid icon map TOML at line 2/);
  assert.throws(() => parseIconMapToml(`[emoji]\n"warning" "⚠️"`), /Invalid icon map TOML at line 2/);
  assert.throws(() => parseIconMapToml(`[emoji]\n"warning" = "⚠️" trailing`), /Invalid icon map TOML at line 2/);
});

test("bundle loader merges built-in and bundle icon maps", async () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-icon-map-"));
  try {
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "icon-map-bundle",
        version: "2.3.4",
        displayName: "Icon Map Bundle",
        summary: "Tests bundle icon maps.",
        iconName: "fasta",
        pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
      })
    );
    await writeFile(
      path.join(directory, "iconmap.toml"),
      `
[sf-symbols]
"fasta" = "point.3.connected.trianglepath.dotted"

[bootstrap]
"fasta" = "diagram-3"
`
    );

    const { loadLocalizedBundle } = await import("../dist/web/src/server/bundle-loader.js");
    const bundle = await loadLocalizedBundle(undefined, repoRoot, directory, directory);

    assert.equal(bundle.iconMap["sf-symbols"].fasta, "point.3.connected.trianglepath.dotted");
    assert.equal(bundle.manifest.version, "2.3.4");
    assert.equal(bundle.iconMap.bootstrap.fasta, "diagram-3");
    assert.equal(bundle.iconMap.bootstrap.terminal, "terminal");
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("bundle loader reports setup tool version file context", async () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-tool-version-"));
  try {
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "tool-version-context",
        version: "1.0.0",
        displayName: "Tool Version Context",
        summary: "Tests tool version file errors.",
        setup: { steps: [] },
        uninstall: {
          steps: [
            {
              id: "remove-cli",
              kind: "setupScript",
              label: "Remove CLI",
              value: "scripts/remove-cli.sh",
              toolVersionFile: "missing-version.txt",
            },
          ],
        },
        pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
      }),
    );

    const { loadLocalizedBundle } = await import("../dist/web/src/server/bundle-loader.js");
    await assert.rejects(
      () => loadLocalizedBundle(undefined, repoRoot, directory, directory),
      /Could not read uninstall\.steps\.remove-cli \(Remove CLI\)\.toolVersionFile at missing-version\.txt:/,
    );
  }
  finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("bundle loader surfaces invalid bundle icon map errors", async () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-icon-map-invalid-"));
  try {
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "icon-map-bundle-invalid",
        displayName: "Invalid Icon Map Bundle",
        summary: "Tests invalid icon maps.",
        iconName: "fasta",
        pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
      })
    );
    await writeFile(path.join(directory, "iconmap.toml"), `[emoji]\n"play" = "\\uZZZZ"\n`);

    const { loadLocalizedBundle } = await import("../dist/web/src/server/bundle-loader.js");
    await assert.rejects(
      () => loadLocalizedBundle(undefined, repoRoot, directory, directory),
      /Invalid icon map TOML at line 2/
    );
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("bundle loader rejects incomplete platform script folders", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-script-platforms-"));
  try {
    await mkdir(path.join(directory, "scripts", "windows"), { recursive: true });
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "script-platforms",
        displayName: "Script Platforms",
        summary: "Tests platform script validation.",
        pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
        setup: {
          steps: [
            { id: "install", kind: "setupScript", label: "Install", value: "scripts/install.sh" },
            { id: "verify", kind: "setupScript", label: "Verify", value: "scripts/verify.sh" },
          ],
        },
      })
    );
    await writeFile(path.join(directory, "scripts", "windows", "install.ps1"), "Write-Output install\n");

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    await assert.rejects(
      () => loadManifestFromRoot(directory),
      /Platform script folder .*scripts.*windows.*missing required scripts: verify/
    );
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("bundle loader accepts platform-specific setup scripts", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-script-platforms-conditional-"));
  try {
    await mkdir(path.join(directory, "scripts", "windows"), { recursive: true });
    await mkdir(path.join(directory, "scripts", "posix"), { recursive: true });
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "conditional-script-platforms",
        displayName: "Conditional Script Platforms",
        summary: "Tests conditional platform script validation.",
        pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
        setup: {
          steps: [
            {
              id: "macos-tools",
              kind: "setupScript",
              label: "macOS Tools",
              value: "scripts/macos-tools.sh",
              platforms: ["macos"],
            },
          ],
        },
      })
    );
    await writeFile(path.join(directory, "scripts", "posix", "macos-tools.sh"), "#!/bin/sh\n");

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    const manifest = await loadManifestFromRoot(directory);

    assert.equal(manifest.setup.steps[0].id, "macos-tools");
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("bundle loader validates macOS setup scripts in nested POSIX script folders", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-script-platforms-posix-missing-"));
  try {
    await mkdir(path.join(directory, "scripts", "posix", "tools"), { recursive: true });
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "conditional-script-platforms-missing",
        displayName: "Conditional Script Platforms Missing",
        summary: "Tests conditional platform script validation.",
        pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
        setup: {
          steps: [
            {
              id: "macos-tools",
              kind: "setupScript",
              label: "macOS Tools",
              value: "scripts/macos-tools.sh",
              platforms: ["macos"],
            },
          ],
        },
      })
    );

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    await assert.rejects(
      () => loadManifestFromRoot(directory),
      /Platform script folder .*scripts.*posix.*tools.*missing required scripts: macos-tools/
    );
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("bundle loader rejects unsupported setup platforms", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-script-platforms-invalid-"));
  try {
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "invalid-script-platform",
        displayName: "Invalid Script Platform",
        summary: "Tests setup platform validation.",
        pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
        setup: {
          steps: [
            {
              id: "bad-platform",
              kind: "pathTool",
              label: "Bad Platform",
              value: "tool",
              platforms: ["beos"],
            },
          ],
        },
      })
    );

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    await assert.rejects(
      () => loadManifestFromRoot(directory),
      /Unsupported setup platform at setup\.steps\.0\.platforms\.0: beos/
    );
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("bundle loader rejects non-boolean setup admin flags", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-setup-admin-invalid-"));
  try {
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "invalid-admin-flag",
        displayName: "Invalid Admin Flag",
        summary: "Tests setup admin flag validation.",
        pages: [{ id: "main", title: "Main", summary: "Main page.", sections: [{ id: "main" }] }],
        setup: {
          steps: [
            {
              id: "admin",
              kind: "setupScript",
              label: "Admin",
              value: "scripts/admin.sh",
              requiresAdmin: "true",
            },
          ],
        },
      })
    );

    const { loadManifestFromRoot } = await import("../dist/web/src/server/bundle-loader.js");
    await assert.rejects(
      () => loadManifestFromRoot(directory),
      /Invalid setup\.steps\.0\.requiresAdmin: expected a boolean/
    );
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("bundle loader localizes language options and marks AI-translated bundle strings", async () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-locales-"));
  try {
    await mkdir(path.join(directory, "strings"), { recursive: true });
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "localized-options",
        displayName: "Localized Options",
        summary: "Tests localized language names.",
        pages: [{ id: "main", title: "Main", summary: "Main.", sections: [{ id: "main" }] }],
      })
    );
    await writeFile(path.join(directory, "strings", "strings.en.toml"), `"language.name" = "English"\n`);
    await writeFile(
      path.join(directory, "strings", "strings.es.toml"),
      `"language.aiTranslated" = "true"\n"language.name" = "Español"\n`
    );

    const { loadLocalizedBundle } = await import("../dist/web/src/server/bundle-loader.js");
    const bundle = await loadLocalizedBundle("es", repoRoot, directory, directory);
    const english = bundle.localizationOptions.find((option) => option.code === "en");
    const spanish = bundle.localizationOptions.find((option) => option.code === "es");

    assert.equal(english.displayName, "English (Inglés)");
    assert.equal(spanish.displayName, "Español (Español)");
    assert.equal(spanish.isAITranslated, true);
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("bundle loader bootstraps first-run config defaults before returning state", async () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-config-bootstrap-"));
  try {
    await mkdir(path.join(directory, "scripts"), { recursive: true });
    await writeFile(
      path.join(directory, "scripts", "bootstrap-config.sh"),
      `#!/bin/sh
set -eu
escaped_workspace="$(printf '%s' "$GUI_FOR_CLI_BUNDLE_WORKSPACE" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g')"
count_file="$GUI_FOR_CLI_BUNDLE_WORKSPACE/bootstrap.count"
count=0
if [ -f "$count_file" ]; then count="$(cat "$count_file")"; fi
printf '%s\n' "$((count + 1))" > "$count_file"
printf '{"values":{"output_directory":"%s/output","reference_library":"%s/reference"}}\\n' "$escaped_workspace" "$escaped_workspace"
`
    );
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "config-bootstrap",
        displayName: "Config Bootstrap",
        summary: "Tests config bootstrapping.",
        pages: [
          {
            id: "settings",
            title: "Settings",
            summary: "Settings.",
            sections: [
              {
                id: "paths",
                controls: [
                  {
                    id: "tool-settings",
                    label: "Tool Settings",
                    kind: "configEditor",
                    configFile: {
                      path: "{{bundleWorkspace}}/settings/config.toml",
                      format: "toml",
                      bootstrap: {
                        mode: "mergeMissing",
                        script: { path: "scripts/bootstrap-config.sh" },
                      },
                    },
                    settings: [
                      { id: "out_dir", key: "output_directory", label: "Output Directory", kind: "path" },
                      { id: "ref_path", key: "reference_library", label: "Reference Library", kind: "path" },
                    ],
                  },
                ],
              },
            ],
          },
        ],
      })
    );

    const { loadLocalizedBundle } = await import("../dist/web/src/server/bundle-loader.js");
    const bundle = await loadLocalizedBundle(undefined, repoRoot, directory, directory);

    assert.equal(bundle.configValues["tool-settings.out_dir"], `${directory}/output`);
    assert.equal(bundle.configValues["tool-settings.ref_path"], `${directory}/reference`);
    assert.equal((await readFile(path.join(directory, "bootstrap.count"), "utf8")).trim(), "1");

    await loadLocalizedBundle(undefined, repoRoot, directory, directory);
    assert.equal((await readFile(path.join(directory, "bootstrap.count"), "utf8")).trim(), "1");
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});

test("concurrent first-run loads bootstrap the config file once instead of racing to rename it", async (t) => {
  // The startup preload and the window's first request load the bundle at the same time. Both used to
  // find no config file and rename a temp file onto it at once, which Windows refuses (EPERM), so the
  // app's first launch failed with "Could not load Web UI".
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-config-bootstrap-concurrent-"));
  const require = createRequire(import.meta.url);
  const fsPromises = require("node:fs/promises");
  const originalRename = fsPromises.rename;
  const renamesOntoConfig = [];
  fsPromises.rename = async (from, to) => {
    if (String(to).endsWith("config.toml")) {
      renamesOntoConfig.push(String(from));
    }
    return originalRename(from, to);
  };
  syncBuiltinESMExports();
  t.after(async () => {
    fsPromises.rename = originalRename;
    syncBuiltinESMExports();
    await rm(directory, { force: true, recursive: true });
  });
  await mkdir(path.join(directory, "scripts"), { recursive: true });
  await writeFile(
    path.join(directory, "scripts", "bootstrap-config.sh"),
    "#!/bin/sh\nprintf '{\"values\":{\"output_directory\":\"out\"}}\\n'\n",
  );
  await writeFile(
    path.join(directory, "manifest.json"),
    JSON.stringify({
      id: "config-bootstrap-concurrent",
      displayName: "Config Bootstrap Concurrent",
      summary: "Tests concurrent config bootstrapping.",
      pages: [{
        id: "settings",
        title: "Settings",
        summary: "Settings.",
        sections: [{
          id: "paths",
          controls: [{
            id: "tool-settings",
            label: "Tool Settings",
            kind: "configEditor",
            configFile: {
              path: "{{bundleWorkspace}}/settings/config.toml",
              format: "toml",
              bootstrap: { mode: "createIfMissing", script: { path: "scripts/bootstrap-config.sh" } },
            },
            settings: [{ id: "out_dir", key: "output_directory", label: "Output Directory", kind: "path" }],
          }],
        }],
      }],
    }),
  );

  const { loadLocalizedBundle } = await import("../dist/web/src/server/bundle-loader.js");
  const [first, second] = await Promise.all([
    loadLocalizedBundle(undefined, repoRoot, directory, directory),
    loadLocalizedBundle(undefined, repoRoot, directory, directory, ["en-US"]),
  ]);

  assert.equal(renamesOntoConfig.length, 1, `config.toml written ${renamesOntoConfig.length} times`);
  assert.equal(first.configValues["tool-settings.out_dir"], "out");
  assert.equal(second.configValues["tool-settings.out_dir"], "out");
});

test("script bootstrap skip checks generated defaults and caches the script result", async () => {
  const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
  const directory = await mkdtemp(path.join(tmpdir(), "gui-for-cli-config-bootstrap-generated-"));
  try {
    await mkdir(path.join(directory, "scripts"), { recursive: true });
    await mkdir(path.join(directory, "settings"), { recursive: true });
    await writeFile(path.join(directory, "settings", "config.toml"), 'primary = "present"\n');
    await writeFile(
      path.join(directory, "scripts", "bootstrap-config.sh"),
      `#!/bin/sh
set -eu
count_file="$GUI_FOR_CLI_BUNDLE_WORKSPACE/bootstrap.count"
count=0
if [ -f "$count_file" ]; then count="$(cat "$count_file")"; fi
printf '%s\n' "$((count + 1))" > "$count_file"
printf '{"values":{"secondary":"script"}}\\n'
`
    );
    await writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        id: "config-bootstrap-generated",
        displayName: "Config Bootstrap Generated",
        summary: "Tests generated config bootstrapping.",
        pages: [
          {
            id: "settings",
            title: "Settings",
            summary: "Settings.",
            sections: [
              {
                id: "paths",
                controls: [
                  {
                    id: "tool-settings",
                    label: "Tool Settings",
                    kind: "configEditor",
                    configFile: {
                      path: "{{bundleWorkspace}}/settings/config.toml",
                      format: "toml",
                      bootstrap: {
                        mode: "mergeMissing",
                        script: { path: "scripts/bootstrap-config.sh" },
                      },
                    },
                    settings: [
                      { id: "primary", key: "primary", label: "Primary", kind: "text" },
                    ],
                  },
                ],
              },
            ],
          },
        ],
      })
    );

    const { loadLocalizedBundle } = await import("../dist/web/src/server/bundle-loader.js");
    const { parseFlatToml } = await import("../dist/shared/rendering.js");
    await loadLocalizedBundle(undefined, repoRoot, directory, directory);

    const config = parseFlatToml(await readFile(path.join(directory, "settings", "config.toml"), "utf8"));
    assert.equal(config.primary, "present");
    assert.equal(config.secondary, "script");
    assert.equal((await readFile(path.join(directory, "bootstrap.count"), "utf8")).trim(), "1");

    await loadLocalizedBundle(undefined, repoRoot, directory, directory);
    assert.equal((await readFile(path.join(directory, "bootstrap.count"), "utf8")).trim(), "1");
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});
