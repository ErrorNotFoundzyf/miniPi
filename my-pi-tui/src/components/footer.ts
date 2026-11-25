import * as os from "node:os";
import * as path from "node:path";
import {
  type Component,
  truncateToWidth,
  visibleWidth,
} from "@earendil-works/pi-tui";
import { stripVTControlCharacters } from "node:util";
import { theme } from "../theme/theme.js";

export type PermissionMode = "review" | "strict" | "autonomous";

export interface FooterData {
  workspace: string;
  gitBranch?: string;
  sessionName?: string;
  providerName?: string;
  modelName?: string;
  permissionMode?: PermissionMode;
  thinkingLevel?: string;
  inputTokens?: number;
  outputTokens?: number;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
  cacheHitRate?: number;
  totalTokens?: number;
  contextTokens?: number;
  contextWindow?: number;
  autoCompactEnabled?: boolean;
  costUsd?: number;
  debugMode?: boolean;
}

export function formatTokens(count: number): string {
  if (count < 1000) return count.toString();
  if (count < 10000) return `${(count / 1000).toFixed(1)}k`;
  if (count < 1000000) return `${Math.round(count / 1000)}k`;
  if (count < 10000000) return `${(count / 1000000).toFixed(1)}M`;
  return `${Math.round(count / 1000000)}M`;
}

function safeLabel(value: string): string {
  return stripVTControlCharacters(value).replace(/[\p{Cc}\u2028\u2029]/gu, " ");
}

export class FooterComponent implements Component {
  private data: FooterData;
  private expanded = false;

  constructor(initialData: Partial<FooterData> = {}) {
    this.data = { workspace: process.cwd(), ...initialData };
  }

  public update(partial: Partial<FooterData>): void {
    this.data = { ...this.data, ...partial };
  }

  public setExpanded(expanded: boolean): void {
    this.expanded = expanded;
  }

  public getContextWindow(): number {
    return this.data.contextWindow || 128000;
  }

  public getSessionName(): string | undefined {
    return this.data.sessionName;
  }

  public invalidate(): void {}

  public render(width: number): string[] {
    if (width <= 0) return [];
    const divider = theme.fg("dim", " · ");
    const model = theme.fg("text", safeLabel(this.data.modelName || "default"));
    const mode = this.data.permissionMode || "review";
    const permission = theme.fg(mode === "autonomous" ? "warning" : "muted", mode);
    const context = this.formatContext();
    const state = permission + divider + context;
    const primary = model + divider + state;
    const workspace = this.data.workspace ? path.basename(path.resolve(this.data.workspace)) : "";
    const label = workspace && theme.fg("dim", safeLabel(workspace)) + divider;
    const debug = this.data.debugMode ? divider + theme.fg("warning", "DEBUG") : "";

    const lines = visibleWidth(primary) <= width
      ? [visibleWidth(label + primary + debug) <= width ? label + primary + debug
        : visibleWidth(primary + debug) <= width ? primary + debug : primary]
      : [truncateToWidth(model + debug, width), truncateToWidth(state, width)];
    if (!this.expanded) return lines;

    const location = [
      this.formatCwd(this.data.workspace),
      this.data.gitBranch,
      this.data.sessionName,
    ].filter((field): field is string => Boolean(field)).map(safeLabel);
    if (location.length) {
      lines.push(truncateToWidth(theme.fg("dim", location.join(" · ")), width));
    }
    const stats = this.formatDetails();
    if (stats.length) lines.push(truncateToWidth(theme.fg("dim", stats.join(" · ")), width));
    return lines;
  }

  private formatContext(): string {
    const size = this.getContextWindow();
    const tokens = this.data.contextTokens ?? this.data.totalTokens ?? 0;
    const percent = size > 0 ? Math.max(0, tokens / size * 100) : 0;
    const used = percent > 0 && percent < 0.05 ? "<0.1" : percent.toFixed(1);
    const tone = percent > 90 ? "error" : percent > 70 ? "warning" : "muted";
    return theme.fg(tone, `ctx ${used}%/${formatTokens(size)}`);
  }

  private formatDetails(): string[] {
    const {
      inputTokens = 0, outputTokens = 0, totalTokens = 0,
      cacheReadTokens = 0, cacheWriteTokens = 0, cacheHitRate, costUsd = 0,
      providerName, thinkingLevel,
    } = this.data;
    const details: string[] = [];
    if (inputTokens > 0) details.push(`↑${formatTokens(inputTokens)}`);
    if (outputTokens > 0) details.push(`↓${formatTokens(outputTokens)}`);
    if (!inputTokens && !outputTokens && totalTokens > 0) details.push(`${formatTokens(totalTokens)} tokens`);
    if (cacheReadTokens > 0) details.push(`R${formatTokens(cacheReadTokens)}`);
    if (cacheWriteTokens > 0) details.push(`W${formatTokens(cacheWriteTokens)}`);
    if (cacheReadTokens > 0 || cacheWriteTokens > 0) {
      const rate = cacheHitRate ?? (cacheReadTokens / (inputTokens + cacheReadTokens + cacheWriteTokens) * 100);
      details.push(`CH${rate.toFixed(1)}%`);
    }
    if (costUsd > 0) details.push(`$${costUsd.toFixed(3)}`);
    if (providerName) details.push(safeLabel(providerName));
    if (thinkingLevel && thinkingLevel !== "off") details.push(`thinking ${safeLabel(thinkingLevel)}`);
    if (this.data.autoCompactEnabled === false) details.push("auto-compact off");
    return details;
  }

  private formatCwd(dir: string): string {
    if (!dir) return "";
    const relative = path.relative(os.homedir(), path.resolve(dir));
    if (!relative) return "~";
    if (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(".." + path.sep)) {
      return "~/" + relative.replace(/\\/g, "/");
    }
    return path.resolve(dir).replace(/\\/g, "/");
  }
}
