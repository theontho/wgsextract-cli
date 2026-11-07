import assert from "node:assert/strict";
import test from "node:test";

import { developerCacheDir } from "../scripts/developer-cache.mjs";

// Worktrees share one Tauri Node download only if the cache is outside the checkout.
test("developer cache is the per-user platform cache, outside any checkout", () => {
  assert.equal(
    developerCacheDir("tauri-node", {
      platform: "win32",
      env: { LOCALAPPDATA: "C:\\Users\\dev\\AppData\\Local" },
      homedir: "C:\\Users\\dev",
    }),
    "C:\\Users\\dev\\AppData\\Local\\gui-for-cli-dev\\Cache\\tauri-node",
  );
  assert.equal(
    developerCacheDir("tauri-node", { platform: "darwin", env: {}, homedir: "/Users/dev" }),
    "/Users/dev/Library/Caches/gui-for-cli-dev/tauri-node",
  );
  assert.equal(
    developerCacheDir("tauri-node", {
      platform: "linux",
      env: { XDG_CACHE_HOME: "/var/cache/dev" },
      homedir: "/home/dev",
    }),
    "/var/cache/dev/gui-for-cli-dev/tauri-node",
  );
});

test("developer cache falls back when the platform cache variable is unset or relative", () => {
  assert.equal(
    developerCacheDir("tauri-node", { platform: "win32", env: {}, homedir: "C:\\Users\\dev" }),
    "C:\\Users\\dev\\AppData\\Local\\gui-for-cli-dev\\Cache\\tauri-node",
  );
  for (const XDG_CACHE_HOME of [undefined, "", "relative/cache"]) {
    assert.equal(
      developerCacheDir("tauri-node", {
        platform: "linux",
        env: { XDG_CACHE_HOME },
        homedir: "/home/dev",
      }),
      "/home/dev/.cache/gui-for-cli-dev/tauri-node",
    );
  }
});
