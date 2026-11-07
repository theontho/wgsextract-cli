let renderApp = () => { };
let renderPending = false;

export function setRender(render: () => void) {
    renderApp = render;
}

/** Coalesces render requests into one full re-render per animation frame. */
export function scheduleRender() {
    if (renderPending) {
        return;
    }
    renderPending = true;
    nextFrame(() => {
        renderPending = false;
        renderApp();
    });
}

export function nextFrame(callback: () => void) {
    if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(callback);
    }
    else {
        setTimeout(callback, 0);
    }
}
