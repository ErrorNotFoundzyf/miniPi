import test from "node:test";
import assert from "node:assert/strict";
import { stripVTControlCharacters } from "node:util";
import { visibleWidth } from "@earendil-works/pi-tui";
import { StartupResourcesComponent } from "../dist/components/startup-resources.js";

test("Startup resources show counts until details are expanded", () => {
  const component = new StartupResourcesComponent({
    context: ["global/AGENTS.md", "AGENTS.md"],
    skills: ["brainstorming", "systematic-debugging"],
    prompts: ["/review-loop"],
    extensions: ["custom-tools"],
    agents: ["code-reviewer"],
  });
  const compact = component.render(120).map(stripVTControlCharacters);
  assert.equal(compact[0], "Resources · Context 2 · Skills 2 · Prompts 1 · Extensions 1 · Agents 1");
  assert.equal(compact.length, 2);
  component.setExpanded(true);
  const expanded = component.render(120).map(stripVTControlCharacters);
  assert.ok(expanded.includes("Context"));
  assert.ok(expanded.includes("  global/AGENTS.md"));
  assert.ok(expanded.includes("  systematic-debugging"));
  assert.ok(expanded.includes("  /review-loop"));
  assert.ok(expanded.includes("  custom-tools"));
  assert.ok(expanded.includes("  code-reviewer"));
  assert.ok(component.render(16).every((line) => visibleWidth(line) <= 16));
  component.setExpanded(false);
  assert.deepEqual(component.render(120).map(stripVTControlCharacters), compact);
  component.updateData({});
  assert.deepEqual(component.render(120), []);
});
