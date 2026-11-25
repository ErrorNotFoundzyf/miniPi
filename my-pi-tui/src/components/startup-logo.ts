import chalk from "chalk";
import { theme } from "../theme/theme.js";

const PIXELS = [
  "............aa..",
  "............aa..",
  ".............s..",
  "...ssssssssss...",
  "..sbbbbbbbbbbs..",
  ".ssddddddddddss.",
  ".ssddpdddddddss.",
  "ssbdddpdddddbbss",
  "ssbddpddpppdbbss",
  ".ssddddddddddss.",
  ".ssddddddddddss.",
  "..sbbbbbbbbbbs..",
  "...ssssssssss...",
  "....ss....ss....",
] as const;

const INK: Readonly<Record<string, string>> = {
  a: "logoIndicator",
  s: "logoOutline",
  b: "logoBody",
  d: "logoScreen",
  p: "logoPrompt",
};

export const STARTUP_LOGO_WIDTH = PIXELS[0].length;

function drawCell(top: string, bottom: string): string {
  if (chalk.level === 0) {
    // A transparent screen keeps the prompt visible on terminals with color disabled.
    const upper = top !== "." && top !== "d";
    const lower = bottom !== "." && bottom !== "d";
    if (upper && lower) return "█";
    if (upper) return "▀";
    return lower ? "▄" : " ";
  }
  if (top === ".") return bottom === "." ? " " : theme.fg(INK[bottom], "▄");
  if (bottom === ".") return theme.fg(INK[top], "▀");
  if (top === bottom) return theme.fg(INK[top], "█");
  return theme.bg(INK[bottom], theme.fg(INK[top], "▀"));
}

export function renderStartupLogo(): string[] {
  const rows: string[] = [];
  for (let y = 0; y < PIXELS.length; y += 2) {
    let row = "";
    for (let x = 0; x < STARTUP_LOGO_WIDTH; x++) {
      row += drawCell(PIXELS[y][x], PIXELS[y + 1][x]);
    }
    rows.push(row);
  }
  return rows;
}
