import { type Component, visibleWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";
import pkg from "../../package.json" with { type: "json" };
import { theme } from "../theme/theme.js";
import { renderStartupLogo, STARTUP_LOGO_WIDTH } from "./startup-logo.js";

const LOGO_GAP = "   ";

export class HeaderComponent implements Component {
  constructor(private readonly version = pkg.version) {}

  public invalidate(): void {}

  public render(width: number): string[] {
    if (width <= 0) return [];
    const padding = width > 1 ? " " : "";
    const available = width - padding.length;
    const title = theme.bold(theme.fg("text", "minPi")) + theme.fg("dim", ` v${this.version}`);
    const hints = theme.fg("muted", "/ commands · Ctrl+O details · Esc stop");
    const textWidth = available - STARTUP_LOGO_WIDTH - LOGO_GAP.length;

    if (textWidth >= Math.max(visibleWidth(title), visibleWidth("Ctrl+O details"))) {
      const logo = renderStartupLogo();
      const labels = [title, ...wrapTextWithAnsi(hints, textWidth)];
      const top = Math.max(0, Math.floor((logo.length - labels.length) / 2));
      const blankLogoRow = " ".repeat(STARTUP_LOGO_WIDTH);
      const rows = Array.from({ length: Math.max(logo.length, top + labels.length) }, (_, row) => {
        const label = labels[row - top];
        return padding + (logo[row] ?? blankLogoRow) + (label ? LOGO_GAP + label : "");
      });
      return ["", ...rows];
    }

    const logo = available >= STARTUP_LOGO_WIDTH ? renderStartupLogo() : [];
    const labels = [...wrapTextWithAnsi(title, available), ...wrapTextWithAnsi(hints, available)];
    return ["", ...[...logo, ...labels].map((line) => padding + line)];
  }
}
