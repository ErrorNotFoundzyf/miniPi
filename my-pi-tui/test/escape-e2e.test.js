import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { KernelBridge } from "../dist/bridge/kernel-bridge.js";
import { PythonKernelClient } from "../dist/client.js";
import { InteractiveMode } from "../dist/interactive/interactive-mode.js";

const python = process.env.MINIPI_E2E_PYTHON;
const fixture = fileURLToPath(new URL("./fixtures/escape_kernel.py", import.meta.url));

async function waitFor(check, message) {
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    const result = await check();
    if (result) return result;
    await delay(20);
  }
  assert.fail(message);
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error.code === "ESRCH") return false;
    throw error;
  }
}

class EscapeTestClient extends PythonKernelClient {
  resolvePythonCommand(workspace) {
    return { cmd: python, args: [fixture, workspace] };
  }
}

test("Escape cancels a live Bash child over stdio and restores the TUI", {
  skip: !python && "Set MINIPI_E2E_PYTHON to the project's Python executable",
  timeout: 30_000,
}, async () => {
  const workspace = await mkdtemp(join(tmpdir(), "minipi-escape-"));
  const client = new EscapeTestClient({ workspace, mode: "autonomous" });
  const mode = new InteractiveMode(new KernelBridge(client), { workspace });
  const events = [];
  client.on("event", (event) => events.push(event));
  const release = join(workspace, "release.txt");
  const finished = join(workspace, "finished.txt");
  try {
    await writeFile(join(workspace, "wait_for_release.py"), [
      "from os import getpid",
      "from pathlib import Path",
      "from time import monotonic, sleep",
      "Path('ready.tmp').write_text(str(getpid()), encoding='ascii')",
      "Path('ready.tmp').replace('ready.txt')",
      "deadline = monotonic() + 15",
      "while not Path('release.txt').is_file() and monotonic() < deadline:",
      "    sleep(0.02)",
      "Path('finished.txt').touch()",
      "",
    ].join("\n"));
    await client.start();
    await mode.init();
    const prompt = mode.handleUserInput("Run the cancellation fixture.");
    const pid = await waitFor(async () => {
      const value = await readFile(join(workspace, "ready.txt"), "ascii").catch((error) => {
        if (error.code === "ENOENT") return "";
        throw error;
      });
      return /^\d+$/.test(value) ? Number(value) : 0;
    }, "Bash child did not reach its readiness marker");
    assert.ok(isAlive(pid));
    assert.ok(events.some((event) => event.type === "tool_execution_start" && event.toolCallId === "escape-bash"));
    mode.ui.handleTerminalInput("\x1b");
    await prompt;

    const { report } = await client.sendRequest("last_run");
    assert.equal(report.stop_reason, "cancelled");
    await waitFor(() => !isAlive(pid), "Bash child survived Escape");
    assert.equal(mode.isStreaming, false);
    assert.equal(mode.isWorking, false);
    assert.equal(mode.isSubmitting, false);
    assert.match(mode.ui.render(100).join("\n"), /执行已中断/);

    mode.defaultEditor.setText("Confirm the editor has recovered.");
    mode.ui.handleTerminalInput("\r");
    await waitFor(() => events.some((event) => event.type === "agent_end" && event.stop_reason === "end_turn"),
      "Follow-up prompt did not complete");
    assert.match(mode.ui.render(100).join("\n"), /FOLLOWUP_OK/);
    await delay(100);
    await assert.rejects(access(finished), { code: "ENOENT" });
  } finally {
    await writeFile(release, "");
    mode.stop();
    const exited = client.child ? once(client, "exit") : Promise.resolve();
    await client.shutdown();
    await exited;
    await rm(workspace, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
  }
});
