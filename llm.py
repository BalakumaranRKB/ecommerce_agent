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
from pydantic import BaseModel, ValidationError

from cost_ledger import record_llm_call
from tools_schema import _to_anthropic_tools, _to_bedrock_tools, _to_groq_tools

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
    usage: dict = field(default_factory=dict)


class StructuredOutputError(RuntimeError):
    """Raised by Provider.create_structured() when a typed decision does not
    come back in the shape asked for: no tool call, the wrong tool name, or a
    tool input that fails the target Pydantic model's validation.

    This IS the "fails loudly at the boundary" behaviour Phase 4 requires
    (docs/phase_4_implementation_plan.md, "The core idea"). A missing or
    malformed amount raises HERE -- at the call site, immediately -- instead
    of silently becoming `None`, a wrong type, or a wrong refund three call
    frames downstream.
    """


class Provider:
    """Common interface both backends implement."""

    def create(
        self,
        system: str,
        messages: list,
        tools: list | None = None,
        tool_choice: str | None = None,
    ) -> ModelResponse:
        raise NotImplementedError

    def append_assistant_turn(self, messages: list, response: ModelResponse) -> None:
        raise NotImplementedError

    def append_tool_results(self, messages: list, response: ModelResponse, results: dict) -> None:
        raise NotImplementedError

    # -- Structured output (Phase 4 / spec §2.7) ---------------------------
    #
    # Shared here, ONCE, across every backend -- rather than three bespoke
    # implementations on AnthropicProvider/GroqProvider/BedrockProvider --
    # because it reuses the exact ToolCall/tool-dispatch seam each provider's
    # create() already implements, plus one small additive `tool_choice`
    # param. We never ask the model for JSON in the prompt and hope, and we
    # never regex a value out of `response.text`: the provider's tool-use
    # support enforces the shape of what comes back, and Pydantic enforces it
    # again on receipt.
    def create_structured(self, system: str, messages: list, response_model: type[BaseModel]):
        """Force the model to return ONE typed tool call shaped like
        `response_model`, then parse + validate its input into that Pydantic
        model and return the model instance.

        Raises StructuredOutputError if the model doesn't call the tool, calls
        the wrong one, or the tool's input fails `response_model`'s
        validation -- i.e. exactly the cases where a prose-based approach
        would otherwise have silently produced a missing or wrong value.
        """
        tool_name = f"submit_{response_model.__name__.lower()}"
        tool_spec = {
            "name": tool_name,
            "description": (
                f"Submit the decision as a {response_model.__name__}. This is "
                f"the ONLY way to report the decision -- do not describe it in "
                f"plain text instead."
            ),
            "parameters": response_model.model_json_schema(),
        }
        rendered_tool = self._render_structured_tool(tool_spec)

        response = self.create(
            system=system,
            messages=messages,
            tools=[rendered_tool],
            tool_choice=tool_name,
        )

        if response.stop_reason != "tool_use" or not response.tool_calls:
            raise StructuredOutputError(
                f"Expected a {tool_name!r} tool call, got stop_reason="
                f"{response.stop_reason!r} with no tool calls. Raw text: {response.text!r}"
            )

        call = response.tool_calls[0]
        if call.name != tool_name:
            raise StructuredOutputError(
                f"Expected tool call {tool_name!r}, got {call.name!r} instead."
            )

        try:
            return response_model(**call.input)
        except ValidationError as e:
            raise StructuredOutputError(
                f"Model's {tool_name} call did not match {response_model.__name__}'s "
                f"schema: {e}"
            ) from e

    def _render_structured_tool(self, tool_spec: dict) -> dict:
        """Render one generic {name, description, parameters} tool spec into
        this provider's wire format. Delegates to tools_schema's renderers --
        the SAME functions TOOLS_BY_PROVIDER is built from -- so a structured-
        output tool call is rendered identically to every other tool call
        this agent makes, with no second rendering path to drift out of sync.
        """
        raise NotImplementedError


class AnthropicProvider(Provider):
    MODEL = "claude-sonnet-5"

    def __init__(self):
        import anthropic
        self._client = anthropic.Anthropic()

    def create(self, system, messages, tools=None, tool_choice=None):
        kwargs = dict(
            model=self.MODEL,
            max_tokens=1024,
            system=system,
            tools=tools or [],
            messages=messages,
        )
        if tool_choice:
            # Forces the model to call exactly this tool, rather than
            # optionally reaching for it or answering in prose instead --
            # the structured-output request (Phase 4) needs the former.
            kwargs["tool_choice"] = {"type": "tool", "name": tool_choice}
        resp = self._client.messages.create(**kwargs)
        inp_tok = getattr(resp.usage, "input_tokens", 0) if hasattr(resp, "usage") else 0
        out_tok = getattr(resp.usage, "output_tokens", 0) if hasattr(resp, "usage") else 0
        record_llm_call(model=self.MODEL, input_tokens=inp_tok, output_tokens=out_tok)
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in resp.content if b.type == "tool_use"
        ]
        text = "".join(b.text for b in resp.content if b.type == "text")
        stop_reason = "tool_use" if resp.stop_reason == "tool_use" else "end"
        return ModelResponse(
            stop_reason=stop_reason,
            text=text,
            tool_calls=tool_calls,
            raw=resp,
            usage={"input_tokens": inp_tok, "output_tokens": out_tok},
        )

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

    def _render_structured_tool(self, tool_spec):
        return _to_anthropic_tools([tool_spec])[0]


class GroqProvider(Provider):
    MODEL = "openai/gpt-oss-120b"

    def __init__(self):
        from groq import Groq
        self._client = Groq()

    def create(self, system, messages, tools=None, tool_choice=None):
        full_messages = [{"role": "system", "content": system}] + messages
        kwargs = dict(model=self.MODEL, messages=full_messages, tools=tools or [])
        if tool_choice:
            kwargs["tool_choice"] = {"type": "function", "function": {"name": tool_choice}}
        resp = self._client.chat.completions.create(**kwargs)
        inp_tok = getattr(resp.usage, "prompt_tokens", 0) if hasattr(resp, "usage") and resp.usage else 0
        out_tok = getattr(resp.usage, "completion_tokens", 0) if hasattr(resp, "usage") and resp.usage else 0
        record_llm_call(model=self.MODEL, input_tokens=inp_tok, output_tokens=out_tok)
        msg = resp.choices[0].message
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, input=json.loads(tc.function.arguments))
            for tc in (msg.tool_calls or [])
        ]
        stop_reason = "tool_use" if tool_calls else "end"
        return ModelResponse(
            stop_reason=stop_reason,
            text=msg.content or "",
            tool_calls=tool_calls,
            raw=msg,
            usage={"input_tokens": inp_tok, "output_tokens": out_tok},
        )

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

    def _render_structured_tool(self, tool_spec):
        return _to_groq_tools([tool_spec])[0]


class BedrockProvider(Provider):
    # Global cross-region inference profile for Claude Sonnet 4.6.
    # Swap to "global.anthropic.claude-sonnet-5" once Sonnet 5 access is enabled.
    MODEL = "global.anthropic.claude-sonnet-4-6"

    def __init__(self):
        import boto3
        self._client = boto3.client('bedrock-runtime', region_name='ap-south-1')

    def create(self, system, messages, tools=None, tool_choice=None):
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
            tool_config = {"tools": bedrock_tools}
            if tool_choice:
                # Forces this specific tool rather than leaving it optional --
                # required for the structured-output request (Phase 4).
                tool_config["toolChoice"] = {"tool": {"name": tool_choice}}
            kwargs["toolConfig"] = tool_config

        resp = self._client.converse(**kwargs)
        u = resp.get("usage", {})
        inp_tok = u.get("inputTokens", 0)
        out_tok = u.get("outputTokens", 0)
        record_llm_call(model=self.MODEL, input_tokens=inp_tok, output_tokens=out_tok)
        
        tool_calls = []
        text = ""
        for block in resp['output']['message']['content']:
            if 'text' in block:
                text += block['text']
            elif 'toolUse' in block:
                tu = block['toolUse']
                tool_calls.append(ToolCall(id=tu['toolUseId'], name=tu['name'], input=tu['input']))
                
        stop_reason = "tool_use" if resp['stopReason'] == "tool_use" else "end"
        
        return ModelResponse(
            stop_reason=stop_reason,
            text=text,
            tool_calls=tool_calls,
            raw=resp['output']['message'],
            usage={"input_tokens": inp_tok, "output_tokens": out_tok},
        )

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

    def _render_structured_tool(self, tool_spec):
        return _to_bedrock_tools([tool_spec])[0]


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
