"""Run the real stdio kernel with a deterministic model for the Escape test."""

import asyncio
import json
import os
import sys
from pathlib import Path

from my_agent_llm.models import StreamChunk
from my_coding_agent.rpc_server import RpcServer


class EscapeModel:
    model = "escape-fixture"

    def __init__(self) -> None:
        self.calls = 0

    async def achat_stream(self, messages, **kwargs):
        self.calls += 1
        if self.calls > 1:
            yield StreamChunk(content="FOLLOWUP_OK")
            return
        binary = Path(sys.executable).as_posix()
        yield StreamChunk(content="", tool_calls=[{
            "id": "escape-bash",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({"command": f'"{binary}" -I -B wait_for_release.py', "timeout": 20}),
            },
        }])


if __name__ == "__main__":
    os.environ["MY_AGENT_HOME"] = str(Path(sys.argv[1]) / "home")
    asyncio.run(RpcServer(llm=EscapeModel()).run_forever())
