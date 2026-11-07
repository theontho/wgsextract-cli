const htmlEscapes: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export function escapeHTML(value: unknown): string {
    return String(value ?? "").replace(/[&<>"']/g, (character) => htmlEscapes[character] ?? character);
}
export function escapeAttribute(value: unknown): string {
    return escapeHTML(value);
}
export function clamp(value: number, minimum: number, maximum: number): number {
    return Math.min(Math.max(value, minimum), maximum);
}
