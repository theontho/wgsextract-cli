export type FocusSnapshot = {
    selector: string;
    value?: string;
    selectionStart?: number | null;
    selectionEnd?: number | null;
};

/**
 * Records which control has focus so a full re-render (which replaces the DOM) can put focus, an
 * uncommitted text value, and the caret back. Controls are matched by their tag and data attributes.
 */
export function captureFocus(root: HTMLElement): FocusSnapshot | null {
    const active = globalThis.document?.activeElement;
    if (!(active instanceof HTMLElement) || active === root || !root.contains(active)) {
        return null;
    }
    const attributes = [...active.attributes]
        .filter((attribute) => attribute.name.startsWith("data-") || attribute.name === "id" || attribute.name === "name")
        .map((attribute) => `[${attribute.name}="${CSS.escape(attribute.value)}"]`);
    if (!attributes.length) {
        return null;
    }
    const snapshot: FocusSnapshot = { selector: `${active.tagName.toLowerCase()}${attributes.join("")}` };
    if (isTextEntry(active)) {
        snapshot.value = active.value;
        snapshot.selectionStart = active.selectionStart;
        snapshot.selectionEnd = active.selectionEnd;
    }
    return snapshot;
}

export function restoreFocus(root: HTMLElement, snapshot: FocusSnapshot | null) {
    if (!snapshot) {
        return;
    }
    const element = root.querySelector<HTMLElement>(snapshot.selector);
    if (!element) {
        return;
    }
    // Restore the uncommitted value before focusing, so the browser does not treat it as an edit.
    if (isTextEntry(element) && snapshot.value != null) {
        element.value = snapshot.value;
    }
    element.focus({ preventScroll: true });
    if (isTextEntry(element) && snapshot.selectionStart != null && snapshot.selectionEnd != null) {
        element.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd);
    }
}

function isTextEntry(element: Element): element is HTMLInputElement | HTMLTextAreaElement {
    if (element instanceof HTMLTextAreaElement) {
        return true;
    }
    return element instanceof HTMLInputElement && ["text", "search", "url", "email", "tel", "password", "number", ""].includes(element.type);
}
