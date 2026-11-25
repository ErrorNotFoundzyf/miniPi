import {
  type Component,
  matchesKey,
  truncateToWidth,
  visibleWidth,
  wrapTextWithAnsi,
} from "@earendil-works/pi-tui";
import path from "node:path";
import type { FileReviewRequest } from "../protocol.js";
import { theme } from "../theme/theme.js";

function displayText(text: string): string {
  return text.replace(/[\p{Cc}\p{Cf}\u2028\u2029]/gu, (character) => {
    if (character === "\t") return "\\t";
    if (character === "\r") return "\\r";
    return `\\u{${character.codePointAt(0)?.toString(16)}}`;
  });
}

export class FileReviewComponent implements Component {
  private top = 0;
  private pageSize = 1;
  private submitting = false;
  private readonly label: string;
  private readonly lines: string[];
  private readonly additions: number;
  private readonly deletions: number;

  constructor(
    request: FileReviewRequest,
    private readonly availableRows: () => number,
    private readonly reply: (approved: boolean) => void,
    workspace: string,
  ) {
    const action = request.create ? "Create" : request.action === "write" ? "Overwrite" : "Edit";
    const relative = path.isAbsolute(request.path) ? path.relative(workspace, request.path) : request.path;
    const inWorkspace = !path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(".." + path.sep);
    const projectPath = path.sep === "\\" ? relative.replace(/\\/g, "/") : relative;
    this.label = `${action} · ${displayText(inWorkspace ? projectPath : request.path)}`;
    const lines = request.diff ? request.diff.replace(/\n$/, "").split("\n") : [];
    const firstHunk = lines.findIndex((line) => line.startsWith("@@"));
    this.lines = firstHunk >= 0 ? lines.slice(firstHunk)
      : lines[0]?.startsWith("--- ") && lines[1]?.startsWith("+++ ") ? lines.slice(2) : lines;
    this.additions = this.lines.filter((line) => line.startsWith("+")).length;
    this.deletions = this.lines.filter((line) => line.startsWith("-")).length;
  }

  public render(width: number): string[] {
    if (width <= 0) return [];
    const count = theme.fg("success", `+${this.additions}`) + " " + theme.fg("error", `−${this.deletions}`);
    const title = theme.bold(theme.fg("text", "Waiting for approval"));
    const spare = width - visibleWidth(title) - visibleWidth(count);
    const header = spare > 1 ? title + " ".repeat(spare) + count
      : truncateToWidth(theme.bold("Review") + "  " + count, width);
    const [file, ...pathContinuation] = wrapTextWithAnsi(theme.fg("text", this.label), width);
    const diffRows = (this.lines.length ? this.lines : ["(No text difference)"]).flatMap((line) => {
      const tone = line.startsWith("+") ? "success" : line.startsWith("-") ? "error"
        : line.startsWith("@@") ? "borderAccent" : "muted";
      return wrapTextWithAnsi(theme.fg(tone, displayText(line)), width);
    });
    const rows = [...pathContinuation, ...diffRows];
    const actions = this.submitting ? "Sending decision…" : "Y approve · N/Esc reject";
    const keys = wrapTextWithAnsi(theme.fg(this.submitting ? "muted" : "text", actions), width);
    this.pageSize = Math.max(1, Math.min(12, this.availableRows() - keys.length - 4));
    this.top = Math.max(0, Math.min(this.top, rows.length - this.pageSize));
    const end = Math.min(this.top + this.pageSize, rows.length);
    const paging = rows.length > this.pageSize
      ? [truncateToWidth(theme.fg("muted", `${this.top + 1}–${end}/${rows.length} · ↑↓ scroll`), width)]
      : [];
    return [
      theme.fg("borderAccent", "─".repeat(width)),
      header,
      file ?? "",
      ...rows.slice(this.top, end),
      ...paging,
      ...keys,
    ];
  }

  public handleInput(data: string): void {
    if (this.submitting) return;
    if (data.toLowerCase() === "y" || data.toLowerCase() === "n" || matchesKey(data, "escape")) {
      this.submitting = true;
      this.reply(data.toLowerCase() === "y");
    } else if (matchesKey(data, "up")) {
      this.top--;
    } else if (matchesKey(data, "down")) {
      this.top++;
    } else if (matchesKey(data, "pageUp")) {
      this.top -= this.pageSize;
    } else if (matchesKey(data, "pageDown")) {
      this.top += this.pageSize;
    } else if (matchesKey(data, "home")) {
      this.top = 0;
    } else if (matchesKey(data, "end")) {
      this.top = Number.MAX_SAFE_INTEGER;
    }
  }

  public invalidate(): void {}
}
