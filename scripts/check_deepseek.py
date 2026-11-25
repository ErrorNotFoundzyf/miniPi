"""Run small API probes and save a credential-redacted diagnostic report."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

import httpx

from my_agent_llm.auth.manager import AuthManager
from my_agent_llm.auth.schema import ApiKeyCredential


def redact(text: str, key: str) -> str:
    text = text.replace(key, "[REDACTED]") if key else text
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text[:2000]


def main() -> int:
    credential = AuthManager().get_credential("deepseek")
    key = credential.resolve_key() if isinstance(credential, ApiKeyCredential) else ""
    key = key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("No DeepSeek credential found; use start-local-tui.ps1 -CheckApi.")
        return 1

    model = sys.argv[1] if len(sys.argv) > 1 else "deepseek-flash"
    base_url = "https://api.deepseek.com"
    report: dict = {
        "model": model,
        "base_url": base_url,
        "proxy_environment_names": [
            name for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") if os.environ.get(name)
        ],
        "probes": [],
    }
    from my_coding_agent.tools import build_coding_tools

    root = Path(__file__).resolve().parents[1]
    tools = [tool.to_openai_schema() for tool in build_coding_tools(root)]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply only OK. Do not call tools."}],
        "max_tokens": 128,
        "temperature": 0.7,
        "thinking": {"type": "disabled"},
    }
    cases = [
        ("model_list", "GET", "/models", None),
        ("minimal_chat", "POST", "/chat/completions", payload),
        ("coding_tool_schemas", "POST", "/chat/completions", {**payload, "tools": tools}),
        (
            "streaming_tools_default_thinking",
            "POST",
            "/chat/completions",
            {key: value for key, value in {**payload, "tools": tools, "stream": True}.items() if key != "thinking"},
        ),
    ]
    with httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=30) as client:
        for name, method, path, body in cases:
            item: dict = {"name": name}
            try:
                response = client.request(method, base_url + path, json=body)
                item.update(status=response.status_code, content_type=response.headers.get("content-type", ""))
                if response.is_success:
                    if name == "streaming_tools_default_thinking":
                        item["stream_completed"] = "data: [DONE]" in response.text
                    else:
                        data = response.json()
                        if name == "model_list":
                            item["available_models"] = [entry["id"] for entry in data.get("data", [])]
                        else:
                            item["returned_model"] = data.get("model")
                else:
                    item["error"] = redact(response.text, key)
            except Exception as exc:
                item["exception_type"] = type(exc).__name__
            report["probes"].append(item)
            print(json.dumps(item, ensure_ascii=False))
            if item.get("status") in (401, 402, 403, 429):
                break

    output = root / ".my_agent_core" / "interactive-home" / "deepseek-diagnostic.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Diagnostic report: {output}")
    return 0 if len(report["probes"]) == len(cases) and all(
        200 <= item.get("status", 0) < 300 and "exception_type" not in item
        and item.get("stream_completed", True) for item in report["probes"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
