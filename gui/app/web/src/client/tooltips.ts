import { elements } from "./model.js";
type ActiveTooltip = { target: HTMLElement; tooltip: HTMLDivElement; previousDescribedBy: string | null };
let activeTooltip: ActiveTooltip | null = null;
let tooltipCleanup = () => { };
let tooltipIDCounter = 0;
export function bindTooltipEvents() {
    elements("[data-tooltip]").forEach((target) => {
        target.addEventListener("mouseenter", () => showFloatingTooltip(target));
        target.addEventListener("mouseleave", hideFloatingTooltip);
        target.addEventListener("focus", () => showFloatingTooltip(target));
        target.addEventListener("blur", hideFloatingTooltip);
        target.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                hideFloatingTooltip();
                target.blur();
            }
        });
    });
}
export function showFloatingTooltip(target: HTMLElement) {
    const text = target.dataset.tooltip?.trim();
    if (!text) {
        return;
    }
    hideFloatingTooltip();
    const tooltip = document.createElement("div");
    const previousDescribedBy = target.getAttribute("aria-describedby");
    tooltip.className = "floating-tooltip";
    tooltip.id = `floating-tooltip-${++tooltipIDCounter}`;
    tooltip.setAttribute("role", "tooltip");
    tooltip.textContent = text;
    document.body.append(tooltip);
    target.setAttribute("aria-describedby", previousDescribedBy ? `${previousDescribedBy} ${tooltip.id}` : tooltip.id);
    activeTooltip = { target, tooltip, previousDescribedBy };
    const update = () => positionFloatingTooltip(target, tooltip);
    const raf = requestAnimationFrame(update);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    tooltipCleanup = () => {
        cancelAnimationFrame(raf);
        window.removeEventListener("resize", update);
        window.removeEventListener("scroll", update, true);
    };
}
export function hideFloatingTooltip() {
    tooltipCleanup();
    tooltipCleanup = () => { };
    if (activeTooltip) {
        if (activeTooltip.previousDescribedBy) {
            activeTooltip.target.setAttribute("aria-describedby", activeTooltip.previousDescribedBy);
        }
        else {
            activeTooltip.target.removeAttribute("aria-describedby");
        }
    }
    activeTooltip?.tooltip.remove();
    activeTooltip = null;
}
export function positionFloatingTooltip(target: HTMLElement, tooltip: HTMLElement) {
    if (!document.body.contains(target)) {
        hideFloatingTooltip();
        return;
    }
    const margin = 12;
    const gap = 8;
    tooltip.style.maxWidth = `${Math.min(420, Math.max(260, window.innerWidth - margin * 2))}px`;
    tooltip.style.left = "0px";
    tooltip.style.top = "0px";
    const targetRect = target.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const preferredLeft = targetRect.left + targetRect.width / 2 - tooltipRect.width / 2;
    const left = Math.min(Math.max(margin, preferredLeft), window.innerWidth - tooltipRect.width - margin);
    const belowTop = targetRect.bottom + gap;
    const aboveTop = targetRect.top - tooltipRect.height - gap;
    const top = belowTop + tooltipRect.height + margin <= window.innerHeight ? belowTop : Math.max(margin, aboveTop);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
}
