import { type Component, truncateToWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";
import { stripVTControlCharacters } from "node:util";
import { theme } from "../theme/theme.js";

export interface LoadedResourcesData {
  context?: string[];
  skills?: string[];
  prompts?: string[];
  extensions?: string[];
  agents?: string[];
}

const GROUPS: readonly [keyof LoadedResourcesData, string][] = [
  ["context", "Context"],
  ["skills", "Skills"],
  ["prompts", "Prompts"],
  ["extensions", "Extensions"],
  ["agents", "Agents"],
];

export class StartupResourcesComponent implements Component {
  private expanded = false;

  constructor(private data: LoadedResourcesData = {}) {}

  public updateData(data: LoadedResourcesData): void {
    this.data = data;
  }

  public setExpanded(expanded: boolean): void {
    this.expanded = expanded;
  }

  public getExpanded(): boolean {
    return this.expanded;
  }

  public render(width: number): string[] {
    if (width < 1) return [];
    const groups = GROUPS.flatMap(([key, label]) => {
      const items = this.data[key];
      return items?.length ? [{ label, items }] : [];
    });
    if (!groups.length) return [];

    if (!this.expanded) {
      const summary = groups.map(({ label, items }) => `${label} ${items.length}`).join(" · ");
      return [truncateToWidth(theme.fg("muted", `Resources · ${summary}`), width), ""];
    }

    return groups.flatMap(({ label, items }) => [
      truncateToWidth(theme.bold(theme.fg("text", label)), width),
      ...items.flatMap((item) => wrapTextWithAnsi(theme.fg("muted", "  " + stripVTControlCharacters(item)), width)),
      "",
    ]);
  }

  public invalidate(): void {}
}
