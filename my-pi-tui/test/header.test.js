import test from "node:test";
import assert from "node:assert/strict";
import { stripVTControlCharacters } from "node:util";
import chalk from "chalk";
import { visibleWidth } from "@earendil-works/pi-tui";
import pkg from "../package.json" with { type: "json" };
import { HeaderComponent } from "../dist/components/header.js";
import { renderStartupLogo } from "../dist/components/startup-logo.js";
import { theme } from "../dist/theme/theme.js";

test("HeaderComponent renders the seven-row minPi logo alongside the name and shortcuts", () => {
  const header = new HeaderComponent("1.2.3");
  const lines = header.render(80);
  const logoRows = renderStartupLogo();
  assert.equal(lines.length, 8);
  assert.equal(logoRows.length, 7);
  for (const [index, line] of logoRows.entries()) {
    assert.ok(lines[index + 1].startsWith(" " + line));
    assert.equal(visibleWidth(line), 16);
  }
  const text = stripVTControlCharacters(lines.join("\n"));
  assert.match(text, /minPi v1\.2\.3/);
  assert.match(text, /\/ commands · Ctrl\+O details · Esc stop/);
});

test("HeaderComponent reads its package version and fits narrow windows", () => {
  const header = new HeaderComponent();
  assert.ok(stripVTControlCharacters(header.render(80).join("\n")).includes(`minPi v${pkg.version}`));
  for (const width of [80, 34, 32, 24, 18, 16, 7, 1]) {
    assert.ok(header.render(width).every((row) => visibleWidth(row) <= width), `width ${width}`);
  }
  assert.deepEqual(header.render(0), []);
});

test("The startup logo takes both theme palettes and keeps the prompt shape in monochrome", () => {
  const previousTheme = theme.currentThemeName;
  const previousLevel = chalk.level;
  try {
    chalk.level = 3;
    theme.setTheme("light");
    const light = renderStartupLogo();
    assert.ok(light[0].includes("38;2;230;166;73"));
    assert.ok(light[3].includes("38;2;134;234;196"));
    theme.setTheme("dark");
    const dark = renderStartupLogo();
    assert.ok(dark[0].includes("38;2;246;188;103"));
    assert.ok(dark[3].includes("38;2;166;243;207"));
    assert.deepEqual(dark.map(stripVTControlCharacters), light.map(stripVTControlCharacters));

    chalk.level = 0;
    const monochrome = renderStartupLogo();
    assert.equal(monochrome[3].slice(5, 7), "▀▄");
    assert.equal(monochrome[4].slice(8, 11), "▀▀▀");
  } finally {
    chalk.level = previousLevel;
    theme.setTheme(previousTheme);
  }
});
