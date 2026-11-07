import assert from "node:assert/strict";
import test from "node:test";

const { validateManifest } = await import("../dist/web/src/server/manifest-validator.js");

function validManifest() {
  return {
    id: "fixture",
    displayName: "Fixture",
    summary: "A fixture bundle.",
    setup: { steps: [{ id: "tool", kind: "setupScript", label: "Tool", value: "scripts/tool.sh" }] },
    uninstall: { steps: [] },
    exitCodeReference: [{ code: 3, title: "Missing input", summary: "An input file was not found." }],
    pages: [
      {
        id: "main",
        title: "Main",
        summary: "Main page.",
        sections: [
          {
            id: "inputs",
            controls: [{ id: "sample", label: "Sample", kind: "text" }],
            actions: [{ id: "run", title: "Run", command: { executable: "tool" } }],
          },
        ],
      },
    ],
  };
}

function rejects(mutate, pattern) {
  const manifest = validManifest();
  mutate(manifest);
  assert.throws(() => validateManifest(manifest), pattern);
}

test("manifest validator accepts a complete manifest", () => {
  assert.doesNotThrow(() => validateManifest(validManifest()));
});

test("manifest validator requires top-level identity fields", () => {
  rejects((manifest) => { delete manifest.summary; }, /Required field is empty: summary/);
  rejects((manifest) => { manifest.displayName = "  "; }, /Required field is empty: displayName/);
});

test("manifest validator requires pages with sections", () => {
  rejects((manifest) => { manifest.pages = []; }, /at least one page/);
  rejects((manifest) => { manifest.pages[0].sections = []; }, /Page 'main' must define at least one section/);
});

test("manifest validator rejects duplicate ids", () => {
  rejects((manifest) => { manifest.pages.push({ ...manifest.pages[0] }); }, /Duplicate id 'main' at pages\./);
  rejects(
    (manifest) => { manifest.pages[0].sections[0].controls.push({ id: "sample", label: "Again", kind: "text" }); },
    /Duplicate id 'sample' at pages\.main\.sections\.inputs\.controls/,
  );
  rejects((manifest) => { manifest.exitCodeReference.push({ code: "3", title: "T", summary: "S" }); }, /Duplicate id '3' at exitCodeReference/);
});

test("manifest validator rejects paths that escape the bundle", () => {
  rejects((manifest) => { manifest.setup.steps[0].value = "../outside.sh"; }, /Invalid relative path at setup\.steps\.tool\.value/);
  rejects((manifest) => { manifest.setup.steps[0].value = "C:\\tools\\setup.ps1"; }, /Invalid relative path/);
  rejects((manifest) => { manifest.iconPath = "/etc/icon.png"; }, /Invalid relative path at iconPath/);
  rejects(
    (manifest) => { manifest.pages[0].sections[0].dataSource = { path: "{{home}}/list.py" }; },
    /Invalid relative path at pages\.main\.sections\.inputs\.dataSource\.path/,
  );
});

test("manifest validator checks config file locations", () => {
  const configFile = (path) => (manifest) => {
    manifest.pages[0].sections[0].controls.push({ id: "config", label: "Config", kind: "configEditor", configFile: { path } });
  };
  assert.doesNotThrow(() => { const manifest = validManifest(); configFile("{{bundleWorkspace}}/config.toml")(manifest); validateManifest(manifest); });
  rejects(configFile("{{tmp}}/config.toml"), /Invalid relative path at .*configFile\.path/);
});

test("manifest validator checks actions", () => {
  rejects((manifest) => { manifest.pages[0].sections[0].actions[0].command.executable = ""; }, /Action 'run' must define a command executable/);
  rejects((manifest) => { manifest.pages[0].sections[0].actions[0].textIcon = "abc"; }, /must be 1 or 2 characters/);
  rejects((manifest) => { manifest.pages[0].sections[0].actions[0].visibleWhen = [{ placeholder: "sample" }]; }, /Required field is empty: .*visibleWhen\.0/);
  rejects(
    (manifest) => { manifest.pages[0].sections[0].actions[0].confirm = { title: "Sure?", confirmButtonTitle: "Yes" }; },
    /Required field is empty: .*confirm\.cancelButtonTitle/,
  );
});

test("manifest validator counts emoji text icons as single characters", () => {
  const manifest = validManifest();
  manifest.textIcon = "👩‍🔬";
  assert.doesNotThrow(() => validateManifest(manifest));
});

test("manifest validator checks setup step platforms and admin flags", () => {
  rejects((manifest) => { manifest.setup.steps[0].platforms = ["beos"]; }, /Unsupported setup platform at setup\.steps\.0\.platforms\.0: beos/);
  rejects((manifest) => { manifest.setup.steps[0].requiresAdmin = "yes"; }, /Invalid setup\.steps\.0\.requiresAdmin/);
});
