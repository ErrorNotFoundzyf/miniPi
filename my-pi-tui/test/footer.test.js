import test from "node:test";
import assert from "node:assert/strict";
import { stripVTControlCharacters } from "node:util";
import { visibleWidth } from "@earendil-works/pi-tui";
import { FooterComponent } from "../dist/components/footer.js";

test("Footer shows workspace, model, permission, and current context on one row", () => {
  const footer = new FooterComponent({
    workspace: "D:/code/my-project",
    gitBranch: "main",
    sessionName: "refactor-auth",
    modelName: "deepseek-flash",
    permissionMode: "strict",
    totalTokens: 98000,
    contextTokens: 12500,
    contextWindow: 128000,
  });
  assert.deepEqual(footer.render(100).map(stripVTControlCharacters), [
    "my-project · deepseek-flash · strict · ctx 9.8%/128k",
  ]);
});

test("Footer reveals session, provider, token metrics, and cost in details", () => {
  const footer = new FooterComponent({
    workspace: "D:/code/my-project",
    gitBranch: "main",
    sessionName: "refactor-auth",
    providerName: "anthropic",
    modelName: "claude-sonnet",
    thinkingLevel: "high",
    inputTokens: 3200,
    outputTokens: 450,
    cacheReadTokens: 1200,
    costUsd: 0.006,
    autoCompactEnabled: false,
  });
  const compact = footer.render(120);
  footer.setExpanded(true);
  const expanded = footer.render(120).map(stripVTControlCharacters);
  assert.equal(expanded[0], stripVTControlCharacters(compact[0]));
  assert.match(expanded[1], /my-project · main · refactor-auth/);
  assert.match(expanded[2], /↑3.2k · ↓450 · R1.2k · CH27.3% · \$0.006/);
  assert.match(expanded[2], /anthropic · thinking high · auto-compact off/);
  footer.setExpanded(false);
  assert.deepEqual(footer.render(120), compact);
});

test("Footer stacks the model and status as necessary and stays within narrow widths", () => {
  const footer = new FooterComponent({
    workspace: "D:/code/中文项目",
    modelName: "super-long-model-version-name-extra",
    permissionMode: "autonomous",
    contextTokens: 95000,
    contextWindow: 100000,
  });
  const lines = footer.render(32).map(stripVTControlCharacters);
  assert.equal(lines.length, 2);
  assert.match(lines[1], /autonomous · ctx 95.0%\/100k/);
  for (const width of [100, 40, 32, 20, 9, 1]) {
    assert.ok(footer.render(width).every((line) => visibleWidth(line) <= width), `width ${width}`);
  }
  footer.update({ modelName: "updated", contextTokens: 75000 });
  assert.match(stripVTControlCharacters(footer.render(80).join("\n")), /updated · autonomous · ctx 75.0%/);
});
