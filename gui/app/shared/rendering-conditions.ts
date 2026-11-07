import { contextValue } from "./rendering-context.js";
import { interpolate, placeholdersIn } from "./rendering-placeholders.js";
import type { ActionSpec, CommandContext, ConditionSpec, LooseRecord } from "./types.js";

export function isPrecheckReady(precheck: LooseRecord | null | undefined, context: CommandContext): boolean {
    if (!precheck?.diskSpaceGB) {
        return false;
    }
    return placeholdersIn([precheck.diskSpaceGB])
        .filter((placeholder) => placeholder.endsWith(".fileSizeGB") || placeholder.endsWith(".fileSize"))
        .every((placeholder) => {
        const fieldID = placeholder.slice(0, placeholder.lastIndexOf("."));
        return String(context.fieldValues?.[fieldID] ?? context.configValues?.[fieldID] ?? "").trim().length > 0;
    });
}

export function isActionVisible(action: ActionSpec, context: CommandContext): boolean {
    return (action.visibleWhen ?? []).every((condition) => conditionMatches(condition, context));
}
export function disabledReason(action: ActionSpec, context: CommandContext, fallback = "This action is not available."): string | undefined {
    if (!(action.disabledWhen ?? []).some((condition) => conditionMatches(condition, context))) {
        return undefined;
    }
    return action.disabledTooltip ? interpolate(action.disabledTooltip, context) : fallback;
}
export function conditionMatches(condition: ConditionSpec, context: CommandContext): boolean {
    const rawValue = contextValue(context, condition.placeholder) ?? "";
    const value = String(rawValue).trim();
    if (condition.exists != null && condition.exists !== value.length > 0) {
        return false;
    }
    if (condition.equals != null && value !== interpolate(condition.equals, context)) {
        return false;
    }
    if (condition.notEquals != null && value === interpolate(condition.notEquals, context)) {
        return false;
    }
    const includeValues = condition.in ?? [];
    if (includeValues.length > 0 && !includeValues.map((item) => interpolate(item, context)).includes(value)) {
        return false;
    }
    if ((condition.notIn ?? []).map((item) => interpolate(item, context)).includes(value)) {
        return false;
    }
    if (condition.lessThan != null && !compareNumeric(value, interpolate(condition.lessThan, context), (left, right) => left < right)) {
        return false;
    }
    if (condition.lessThanOrEqual != null &&
        !compareNumeric(value, interpolate(condition.lessThanOrEqual, context), (left, right) => left <= right)) {
        return false;
    }
    if (condition.greaterThan != null &&
        !compareNumeric(value, interpolate(condition.greaterThan, context), (left, right) => left > right)) {
        return false;
    }
    if (condition.greaterThanOrEqual != null &&
        !compareNumeric(value, interpolate(condition.greaterThanOrEqual, context), (left, right) => left >= right)) {
        return false;
    }
    return true;
}

function compareNumeric(left: unknown, right: unknown, op: (left: number, right: number) => boolean): boolean {
    const leftValue = evaluateNumeric(left);
    const rightValue = evaluateNumeric(right);
    return Number.isFinite(leftValue) && Number.isFinite(rightValue) && op(leftValue, rightValue);
}
export function evaluateNumeric(expression: unknown): number {
    const parser = new NumericParser(String(expression ?? ""));
    return parser.parse();
}
class NumericParser {
    text: string;
    index: number;
    constructor(text: string) {
        this.text = text;
        this.index = 0;
    }
    parse(): number {
        const value = this.expression();
        this.skipWhitespace();
        return this.index === this.text.length ? value : Number.NaN;
    }
    expression(): number {
        let value = this.term();
        while (true) {
            this.skipWhitespace();
            if (this.consume("+")) {
                value += this.term();
            }
            else if (this.consume("-")) {
                value -= this.term();
            }
            else {
                return value;
            }
        }
    }
    term(): number {
        let value = this.factor();
        while (true) {
            this.skipWhitespace();
            if (this.consume("*")) {
                value *= this.factor();
            }
            else if (this.consume("/")) {
                value /= this.factor();
            }
            else {
                return value;
            }
        }
    }
    factor(): number {
        this.skipWhitespace();
        if (this.consume("+")) {
            return this.factor();
        }
        if (this.consume("-")) {
            return -this.factor();
        }
        if (this.consume("(")) {
            const value = this.expression();
            return this.consume(")") ? value : Number.NaN;
        }
        return this.number();
    }
    number(): number {
        this.skipWhitespace();
        const start = this.index;
        while (/[0-9.]/.test(this.text[this.index] ?? "")) {
            this.index += 1;
        }
        return start === this.index ? Number.NaN : Number(this.text.slice(start, this.index));
    }
    consume(token: string): boolean {
        if (this.text[this.index] === token) {
            this.index += 1;
            return true;
        }
        return false;
    }
    skipWhitespace(): void {
        while (/\s/.test(this.text[this.index] ?? "")) {
            this.index += 1;
        }
    }
}
