// Exercise real rendering with scripted events; no API key or Python backend.
import { KernelBridge } from "../my-pi-tui/dist/bridge/kernel-bridge.js";
import { InteractiveMode } from "../my-pi-tui/dist/interactive/interactive-mode.js";

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const bridge = new KernelBridge({ request: async () => ({}), on() {}, off() {} });
const mode = new InteractiveMode(bridge, {
  workspace: process.cwd(),
  model: "offline-ui-check",
});
try {
  await mode.init();
  mode.start();
  mode.handleAgentEvent({ type: "agent_start" });
  mode.handleAgentEvent({ type: "turn_start" });
  await pause(1800);
  mode.handleAgentEvent({ type: "message_start", message: { role: "assistant", content: "" } });
  const answer = "离线界面检查：这段回答应在同一处逐步增长，Working 动画也应停留在输入框边框中，不应不断新增行。";
  let content = "";
  for (const character of answer) {
    content += character;
    mode.handleAgentEvent({ type: "message_update", message: { role: "assistant", content } });
    await pause(45);
  }
  mode.handleAgentEvent({ type: "message_end", message: { role: "assistant", content } });
  mode.handleAgentEvent({ type: "agent_end" });
  await pause(2500);
} finally {
  mode.stop();
}
console.log("Offline UI check finished. No model request was sent.");
