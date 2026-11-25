import { type Component, truncateToWidth, visibleWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";
import { stripVTControlCharacters } from "node:util";
import { theme } from "../theme/theme.js";

const FAILURE_MARKER = /\b(?:error|failed|failure|exception|denied|not found|traceback|timed out|panic|fatal|assertion\w*)\b/i;

function cleanLines(text: string): string[] {
  return stripVTControlCharacters(text)
    .replace(/\r\n?/g, "\n")
    .replace(/\t/g, "    ")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/g, "")
    .trimEnd()
    .split("\n");
}

function resultText(result: unknown, isError: boolean): string {
  if (typeof result === "string") return result;
  if (Array.isArray(result) && result.every((item) => item && (item.type === "text" || item.type === "image"))) {
    return result.map((item) => item.type === "text" ? item.text : "[image]").join("\n");
  }
  if (result && typeof result === "object") {
    const { data, error } = result as { data?: unknown; error?: unknown };
    const output = data === undefined || data === null ? "" : typeof data === "string" ? data : JSON.stringify(data);
    if (isError && typeof error === "string" && error && !output.includes(error)) {
      return output ? error + "\n" + output : error;
    }
    return output || (typeof error === "string" ? error : JSON.stringify(result));
  }
  return String(result ?? "");
}

function failurePreview(lines: string[]): string[] {
  const nonempty = lines.filter((line) => line.trim());
  if (nonempty.length <= 3) return nonempty;
  const matches = nonempty.slice(1).filter((line) => FAILURE_MARKER.test(line));
  return [nonempty[0], ...(matches.length ? matches.slice(-2) : nonempty.slice(-2))];
}

export class ToolExecutionComponent implements Component {
  private finishedAt?: number;
  private isError = false;
  private expanded = false;
  private lines: string[] = [];
  private preview: string[] = [];
  private readonly startTime = Date.now();

  constructor(
    public readonly toolName: string,
    public readonly toolCallId: string,
    public args: Record<string, unknown> = {},
  ) {}

  public get finished(): boolean {
    return this.finishedAt !== undefined;
  }

  public get elapsed(): number {
    return this.finishedAt ?? Math.max(0, (Date.now() - this.startTime) / 1000);
  }

  public updateArgs(args: Record<string, unknown>): void {
    this.args = args;
  }

  public updatePartialResult(partial: unknown): void {
    if (this.finished) return;
    this.lines = cleanLines(resultText(partial, false));
  }

  public updateResult(result: unknown, isError: boolean, elapsedSeconds?: number): void {
    this.finishedAt = elapsedSeconds !== undefined && elapsedSeconds >= 0 ? elapsedSeconds : this.elapsed;
    this.isError = isError;
    this.lines = cleanLines(resultText(result, isError));
    this.preview = isError ? failurePreview(this.lines) : [];
  }

  public setExpanded(expanded: boolean): void {
    this.expanded = expanded;
  }

  public invalidate(): void {}

  public render(width: number): string[] {
    if (width <= 0) return [];
    const { heading, appendage } = this.renderHeading(width);
    const output = this.expanded ? this.lines
      : !this.finished ? this.lines.slice(-2)
      : this.preview;
    const contentWidth = Math.max(1, width - 2);
    const prefix = width > 2 ? "  " : "";
    const outputRows = this.expanded
      ? output.flatMap((line) => wrapTextWithAnsi(line, contentWidth))
      : output.map((line) => truncateToWidth(line, contentWidth));
    const hasOutput = output.some((line) => line.length > 0);
    const rows = [heading, ...appendage];
    if (hasOutput) rows.push(...outputRows.map((line) => prefix + theme.fg("toolOutput", line)));
    if (!this.expanded && this.isError && this.lines.length > this.preview.length) {
      rows.push(truncateToWidth(prefix + theme.fg("dim", "Ctrl+O for full result"), width));
    }
    return rows;
  }

  private renderHeading(width: number): { heading: string; appendage: string[] } {
    const tone = this.finished ? this.isError ? "error" : "success" : "muted";
    const mark = this.finished ? this.isError ? "✗" : "✓" : "…";
    const name = cleanLines(this.toolName).join(" ");
    const label = theme.fg(tone, mark) + " " + theme.bold(theme.fg("toolTitle", name));
    const timing = this.finished && this.elapsed > 0 ? theme.fg("dim", ` · ${this.elapsed.toFixed(1)}s`) : "";
    const summary = this.argumentSummary();
    const detail = summary ? theme.fg("muted", ` · ${summary}`) : "";
    const spaceForDetails = width - visibleWidth(label) - visibleWidth(timing);
    const heading = spaceForDetails < 0
      ? truncateToWidth(label, width)
      : label + truncateToWidth(detail, spaceForDetails) + timing;
    const appendage = this.expanded && visibleWidth(detail) > spaceForDetails
      ? wrapTextWithAnsi(theme.fg("muted", summary), width)
      : [];
    return { heading, appendage };
  }

  private argumentSummary(): string {
    const concise = ["command", "path", "pattern", "query", "name"]
      .map((key) => this.args[key])
      .find((value): value is string => typeof value === "string" && value.length > 0);
    if (concise) return cleanLines(concise).join(" ");
    return Object.entries(this.args)
      .filter(([, value]) => typeof value === "string" || typeof value === "number")
      .map(([key, value]) => cleanLines(`${key}=${value}`).join(" "))
      .join(", ");
  }
}
