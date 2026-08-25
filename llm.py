"""
Provider abstraction for the e-commerce order-support agent.

One seam over two backends, so the harness, tools, and RAG code downstream
never see a provider-specific message shape:

- AnthropicProvider — the default / documented stack (assignment §7).
- GroqProvider      — free-tier substitute for cheap iteration, at the cost
                      of somewhat less reliable tool-calling behaviour.

Everything downstream sees only ModelResponse / ToolCall. Swapping providers
must never change what harness_check() does or how the loop is shaped — that
is the whole point of isolating provider quirks here.

Smoke test (Stage 0 acceptance — proves the seam returns model text):
    uv run python llm.py                    # uses $LLM_PROVIDER (see .env)
    uv run python llm.py --provider groq    # force a specific provider
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # loads ANTHROPIC_API_KEY / GROQ_API_KEY from .env, once, on import


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ModelResponse:
    stop_reason: str  # "tool_use" or "end"
    text: str
    tool_calls: list = field(default_factory=list)
    raw: object = None  # provider-native response, needed to replay history


class Provider:
    """Common interface both backends implement."""

    def create(self, system: str, messages: list, tools: list | None = None) -> ModelResponse:
        raise NotImplementedError

    def append_assistant_turn(self, messages: list, response: ModelResponse) -> None:
        raise NotImplementedError

    def append_tool_results(self, messages: list, response: ModelResponse, results: dict) -> None:
        raise NotImplementedError


class AnthropicProvider(Provider):
    MODEL = "claude-sonnet-5"

    def __init__(self):
        import anthropic
        self._client = anthropic.Anthropic()

    def create(self, system, messages, tools=None):
        resp = self._client.messages.create(
            model=self.MODEL,
            max_tokens=1024,
            system=system,
            tools=tools or [],
            messages=messages,
        )
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in resp.content if b.type == "tool_use"
        ]
        text = "".join(b.text for b in resp.content if b.type == "text")
        stop_reason = "tool_use" if resp.stop_reason == "tool_use" else "end"
        return ModelResponse(stop_reason=stop_reason, text=text, tool_calls=tool_calls, raw=resp)

    def append_assistant_turn(self, messages, response):
        messages.append({"role": "assistant", "content": response.raw.content})

    def append_tool_results(self, messages, response, results):
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tc.id, "content": results[tc.id]}
                for tc in response.tool_calls
            ],
        })


class GroqProvider(Provider):
    MODEL = "openai/gpt-oss-120b"

    def __init__(self):
        from groq import Groq
        self._client = Groq()

    def create(self, system, messages, tools=None):
        full_messages = [{"role": "system", "content": system}] + messages
        resp = self._client.chat.completions.create(
            model=self.MODEL,
            messages=full_messages,
            tools=tools or [],
        )
        msg = resp.choices[0].message
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, input=json.loads(tc.function.arguments))
            for tc in (msg.tool_calls or [])
        ]
        stop_reason = "tool_use" if tool_calls else "end"
        return ModelResponse(stop_reason=stop_reason, text=msg.content or "", tool_calls=tool_calls, raw=msg)

    def append_assistant_turn(self, messages, response):
        entry = {"role": "assistant", "content": response.raw.content}
        if response.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                }
                for tc in response.tool_calls
            ]
        messages.append(entry)

    def append_tool_results(self, messages, response, results):
        for tc in response.tool_calls:
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": results[tc.id]})


class BedrockProvider(Provider):
    # Global cross-region inference profile for Claude Sonnet 4.6.
    # Swap to "global.anthropic.claude-sonnet-5" once Sonnet 5 access is enabled.
    MODEL = "global.anthropic.claude-sonnet-4-6"

    def __init__(self):
        import boto3
        self._client = boto3.client('bedrock-runtime', region_name='ap-south-1')

    def create(self, system, messages, tools=None):
        bedrock_messages = []
        for m in messages:
            if isinstance(m["content"], str):
                bedrock_messages.append({"role": m["role"], "content": [{"text": m["content"]}]})
            else:
                bedrock_messages.append(m)

        kwargs = {
            "modelId": self.MODEL,
            "system": [{"text": system}],
            "messages": bedrock_messages,
        }
        if tools:
            bedrock_tools = []
            for t in tools:
                if "toolSpec" in t:
                    bedrock_tools.append(t)
                else:
                    bedrock_tools.append({
                        "toolSpec": {
                            "name": t["name"],
                            "description": t["description"],
                            "inputSchema": {"json": t.get("input_schema") or t.get("parameters")}
                        }
                    })
            kwargs["toolConfig"] = {"tools": bedrock_tools}

        resp = self._client.converse(**kwargs)
        
        tool_calls = []
        text = ""
        for block in resp['output']['message']['content']:
            if 'text' in block:
                text += block['text']
            elif 'toolUse' in block:
                tu = block['toolUse']
                tool_calls.append(ToolCall(id=tu['toolUseId'], name=tu['name'], input=tu['input']))
                
        stop_reason = "tool_use" if resp['stopReason'] == "tool_use" else "end"
        
        return ModelResponse(stop_reason=stop_reason, text=text, tool_calls=tool_calls, raw=resp['output']['message'])

    def append_assistant_turn(self, messages, response):
        messages.append({"role": "assistant", "content": response.raw['content']})

    def append_tool_results(self, messages, response, results):
        messages.append({
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": tc.id, "content": [{"text": results[tc.id]}]}}
                for tc in response.tool_calls
            ],
        })


def get_provider(name: str | None = None) -> Provider:
    # Provider is config, not code: default comes from LLM_PROVIDER in .env,
    # so switching backends never touches the harness/tool/RAG code.
    name = name or os.getenv("LLM_PROVIDER", "anthropic")
    if name == "anthropic":
        return AnthropicProvider()
    if name == "groq":
        return GroqProvider()
    if name == "bedrock":
        return BedrockProvider()
    raise ValueError(f"Unknown provider: {name!r} (expected 'anthropic', 'groq', or 'bedrock')")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Provider seam smoke test.")
    parser.add_argument(
        "--provider",
        choices=["anthropic", "groq", "bedrock"],
        default=os.getenv("LLM_PROVIDER", "anthropic"),
    )
    args = parser.parse_args()

    provider = get_provider(args.provider)
    print(f"[llm] using provider: {args.provider}")
    response = provider.create(
        system="You are a terse assistant.",
        messages=[{"role": "user", "content": "Reply with exactly: provider seam OK"}],
        tools=[],
    )
    print(f"[{args.provider}] stop_reason={response.stop_reason!r}")
    print(f"[{args.provider}] text={response.text!r}")
