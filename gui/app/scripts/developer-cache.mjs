import os from "node:os";
import path from "node:path";

// Developer build caches live in the per-user cache directory, not the checkout, so every
// worktree on the machine reuses one download. The name is distinct from the app's own
// "gui-for-cli" support directory, which holds user configuration.
const cacheName = "gui-for-cli-dev";

export function developerCacheDir(
  subdirectory,
  { platform = process.platform, env = process.env, homedir = os.homedir() } = {},
) {
  const join = platform === "win32" ? path.win32.join : path.posix.join;
  if (platform === "win32") {
    const localAppData = env.LOCALAPPDATA || join(homedir, "AppData", "Local");
    return join(localAppData, cacheName, "Cache", subdirectory);
  }
  if (platform === "darwin") {
    return join(homedir, "Library", "Caches", cacheName, subdirectory);
  }
  // The XDG spec says to ignore a relative XDG_CACHE_HOME.
  const xdgCacheHome = env.XDG_CACHE_HOME;
  const cacheHome =
    xdgCacheHome && path.posix.isAbsolute(xdgCacheHome) ? xdgCacheHome : join(homedir, ".cache");
  return join(cacheHome, cacheName, subdirectory);
}
