export type ScrollSnapshot = {
    key: string;
    left: number;
    top: number;
};

export type ScrollableElement = {
    scrollLeft: number;
    scrollTop: number;
};

export function captureScrollState(element: ScrollableElement | null | undefined, key: string | null | undefined): ScrollSnapshot | null {
    if (!element || !key) {
        return null;
    }
    return {
        key,
        left: element.scrollLeft,
        top: element.scrollTop,
    };
}

export function restoreScrollState(element: ScrollableElement | null | undefined, snapshot: ScrollSnapshot | null, key: string | null | undefined) {
    if (!element || !snapshot || snapshot.key !== key) {
        return false;
    }
    element.scrollLeft = snapshot.left;
    element.scrollTop = snapshot.top;
    return true;
}
