export function normalizeIconSet(value: unknown): "emoji" | "platform" {
    return value === "emoji" ? "emoji" : "platform";
}
export function normalizeColorTheme(value: unknown): "light" | "dark" | "system" {
    return value === "light" || value === "dark" ? value : "system";
}
