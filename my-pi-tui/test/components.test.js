import test from "node:test";
import assert from "node:assert/strict";
import { stripVTControlCharacters } from "node:util";
import { visibleWidth } from "@earendil-works/pi-tui";
import { UserMessageComponent } from "../dist/components/user-message.js";
import { AssistantMessageComponent } from "../dist/components/assistant-message.js";
import { ToolExecutionComponent } from "../dist/components/tool-execution.js";
import { FileReviewComponent } from "../dist/components/file-review.js";
import { CustomEditor } from "../dist/components/custom-editor.js";
import {
  WorkingStatusIndicator,
  CompactionStatusIndicator,
} from "../dist/components/status-indicator.js";

test("UserMessageComponent renders message inside styled box", () => {
  const comp = new UserMessageComponent("Hello from user");
  const lines = comp.render(80);
  assert.ok(lines.length > 0);
  assert.ok(lines.some((l) => l.includes("Hello from user")));
});

test("AssistantMessageComponent handles thinking deltas and text deltas", () => {
  const comp = new AssistantMessageComponent();
  comp.appendReasoningDelta("I am thinking deeply.");
  comp.appendTextDelta("Here is the final answer.");
  comp.finalize();

  const lines = comp.render(80);
  assert.ok(lines.length > 0);
  assert.ok(lines.some((l) => l.includes("Here is the final answer.")));
  assert.ok(lines.some((l) => l.includes("思考过程")));
});

test("Tool execution stays compact until expanded and preserves streamed output", () => {
  const tool = new ToolExecutionComponent("read", "call_1", {
    path: "src/main.ts",
  });
  let lines = tool.render(80);
  assert.ok(lines.some((l) => l.includes("read")));
  assert.ok(lines.some((l) => l.includes("src/main.ts")));

  // 验证运行中流式增量输出展示且未提前终止
  tool.updatePartialResult("Step 1: processing...\nStep 2: analyzing...");
  assert.strictEqual(tool.finished, false);
  lines = tool.render(80);
  assert.ok(lines.some((l) => l.includes("Step 2: analyzing...")));

  tool.updateResult("const x = 1;", false, 1.2);
  assert.strictEqual(tool.finished, true);
  lines = tool.render(80);
  assert.deepEqual(lines.map(stripVTControlCharacters), ["✓ read · src/main.ts · 1.2s"]);
  tool.setExpanded(true);
  assert.ok(tool.render(80).some((line) => line.includes("const x = 1;")));
});

test("Tool failure preview finds the actionable line and expands the returned log", () => {
  const tool = new ToolExecutionComponent("bash", "call_2", { command: "pytest -q" });
  tool.updateResult({
    error: "Command returned exit code 1",
    data: "setup\nprogress one\nprogress two\nERROR missing configuration\ncleanup",
  }, true, 1.8);
  const compact = stripVTControlCharacters(tool.render(100).join("\n"));
  assert.match(compact, /✗ bash · pytest -q · 1\.8s/);
  assert.match(compact, /Command returned exit code 1/);
  assert.match(compact, /ERROR missing configuration/);
  assert.doesNotMatch(compact, /progress one/);
  tool.setExpanded(true);
  assert.match(stripVTControlCharacters(tool.render(100).join("\n")), /progress one/);
  assert.ok(tool.render(16).every((row) => visibleWidth(row) <= 16));
});

test("Diff review counts hunk lines, keeps the relative path and approval controls visible when scrolling", () => {
  let rows = 9;
  const review = new FileReviewComponent({
    request_id: "preview",
    path: process.cwd() + "/src/settings.py",
    action: "edit",
    create: false,
    diff: "--- a/src/settings.py\n+++ b/src/settings.py\n@@ -1,2 +1,2 @@\n-old\n+new\n+\x1b[31madded\n context",
  }, () => rows, () => {}, process.cwd());
  const first = stripVTControlCharacters(review.render(42).join("\n"));
  assert.match(first, /Waiting for approval\s+\+2 −1/);
  assert.match(first, /Edit · src\/settings\.py/);
  assert.match(first, /Y approve · N\/Esc reject/);
  assert.doesNotMatch(first, /--- a\/src/);

  review.handleInput("\x1b[F");
  rows = 7;
  const last = review.render(30);
  assert.ok(last.length <= rows);
  assert.ok(last.every((line) => visibleWidth(line) <= 30));
  const text = stripVTControlCharacters(last.join("\n"));
  assert.match(text, /Y approve · N\/Esc reject/);
  assert.match(text, /context/);
  rows = 14;
  const full = stripVTControlCharacters(review.render(42).join("\n"));
  assert.match(full, /\\u\{1b\}\[31madded/);
});

test("CustomEditor renders embedded WorkingStatusIndicator and CompactionStatusIndicator in top border", () => {
  const fakeTui = { requestRender: () => {} };
  const editor = new CustomEditor(fakeTui, {
    borderColor: (s) => s,
  });

  // 1. 无 indicator 时渲染普通边框
  const normalBorder = editor.renderTopBorder(80, 0);
  assert.ok(normalBorder.includes("─"));
  assert.ok(!normalBorder.includes("Working"));

  // 2. 嵌入 WorkingStatusIndicator
  const working = new WorkingStatusIndicator(fakeTui, "Working");
  editor.setWorkingStatusIndicator(working);
  const workingBorder = editor.renderTopBorder(80, 0);
  assert.ok(workingBorder.includes("Working"));
  working.dispose();

  // 3. 嵌入 CompactionStatusIndicator (验证不会报 renderInBorder is not a function)
  const compacting = new CompactionStatusIndicator(fakeTui, "manual");
  editor.setWorkingStatusIndicator(compacting);
  const compactBorder = editor.renderTopBorder(80, 0);
  assert.ok(compactBorder.includes("Compacting"));
  compacting.dispose();

  // 4. 清除后恢复普通边框
  editor.setWorkingStatusIndicator(undefined);
  const clearedBorder = editor.renderTopBorder(80, 0);
  assert.ok(!clearedBorder.includes("Compacting"));
});
