export interface NavigatorPlatformInfo {
  platform?: string;
  userAgent?: string;
  maxTouchPoints?: number;
  userAgentData?: {
    platform?: string;
  };
}

export interface TauriGlobalInfo {
  __GUI_FOR_CLI_TAURI__?: unknown;
  __TAURI__?: unknown;
  __TAURI_INTERNALS__?: unknown;
}

export function isAppleOperatingSystem(navigatorInfo: NavigatorPlatformInfo = globalThis.navigator) {
  const platform = navigatorInfo.userAgentData?.platform ?? navigatorInfo.platform ?? "";
  const userAgent = navigatorInfo.userAgent ?? "";
  return /Mac|iPhone|iPad|iPod/i.test(platform) || /Macintosh|iPhone|iPad|iPod/i.test(userAgent);
}

export function effectiveWebUIFont(
  preference: string,
  navigatorInfo: NavigatorPlatformInfo = globalThis.navigator
) {
  return preference === "sfPro" || isAppleOperatingSystem(navigatorInfo) ? "sf-pro" : "system";
}

export function isTauriRuntime(globalInfo: TauriGlobalInfo = globalThis as TauriGlobalInfo) {
  return Boolean(globalInfo.__GUI_FOR_CLI_TAURI__ || globalInfo.__TAURI_INTERNALS__ || globalInfo.__TAURI__);
}

export function shouldRenderInPageBundleLoader(globalInfo: TauriGlobalInfo = globalThis as TauriGlobalInfo) {
  return !isTauriRuntime(globalInfo);
}
