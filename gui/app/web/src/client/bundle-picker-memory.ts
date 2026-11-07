const bundlePickerLastDirectoryKey = "guiForCLI.bundlePicker.lastDirectory";
type BundlePickerStorage = Pick<Storage, "getItem" | "setItem"> | undefined;

export function bundlePickerDefaultPath(fallbackPath = "", storage: BundlePickerStorage = globalThis.localStorage) {
    return storage?.getItem(bundlePickerLastDirectoryKey) || fallbackPath;
}

/** Remembers the folder holding the chosen bundle: for a bundle's manifest.json, that is the bundle folder's parent. */
export function rememberBundlePickerPath(selectedPath: string, storage: BundlePickerStorage = globalThis.localStorage) {
    const chosenBundle = /[\\/]manifest\.json$/i.test(selectedPath.trim()) ? parentDirectory(selectedPath) : selectedPath;
    const directory = parentDirectory(chosenBundle);
    if (directory) {
        storage?.setItem(bundlePickerLastDirectoryKey, directory);
    }
}

export function parentDirectory(rawPath: string) {
    const value = rawPath.trim().replace(/[\\/]+$/, "");
    if (!value) {
        return "";
    }
    const separator = Math.max(value.lastIndexOf("/"), value.lastIndexOf("\\"));
    if (separator < 0) {
        return "";
    }
    if (separator === 0) {
        return value.slice(0, 1);
    }
    if (/^[A-Za-z]:[\\/]?$/.test(value.slice(0, separator + 1))) {
        return value.slice(0, separator + 1);
    }
    return value.slice(0, separator);
}
