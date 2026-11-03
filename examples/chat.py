"""Run a prompt against a configured Chat Completions endpoint."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from aster_harness import create_harness


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aster Harness: run a prompt with optional coding tools.")
    parser.add_argument("prompt", help="Task or message to send to the model")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Working directory for coding tools")
    parser.add_argument("--tools", action="store_true", help="Enable read/write/edit/bash tools")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    required = ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_ID", "MODEL_CONTEXT_WINDOW", "MODEL_MAX_TOKENS")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}. See README.md.", file=sys.stderr)
        return 2
    if not args.cwd.is_dir():
        print(f"Working directory does not exist: {args.cwd}", file=sys.stderr)
        return 2
    try:
        context_window = int(os.environ["MODEL_CONTEXT_WINDOW"])
        max_tokens = int(os.environ["MODEL_MAX_TOKENS"])
        if context_window <= 0 or max_tokens <= 0:
            raise ValueError
    except ValueError:
        print("MODEL_CONTEXT_WINDOW and MODEL_MAX_TOKENS must be positive integers.", file=sys.stderr)
        return 2

    harness = create_harness(
        api_key=os.environ["MODEL_API_KEY"],
        base_url=os.environ["MODEL_BASE_URL"],
        model_id=os.environ["MODEL_ID"],
        context_window=context_window,
        max_tokens=max_tokens,
        cwd=args.cwd,
        builtin_tools=args.tools,
        system_prompt="You are a concise assistant. Use tool results to support your answers.",
    )

    def on_event(event: dict[str, Any]) -> None:
        if event["type"] == "message_update":
            update = event["assistantMessageEvent"]
            if update["type"] == "text_delta":
                print(update["delta"], end="", flush=True)
        elif event["type"] == "tool_execution_start":
            print(f"\n[tool] {event['toolName']}", file=sys.stderr)

    unsubscribe = harness.subscribe(on_event)
    try:
        result = await harness.prompt(args.prompt)
    finally:
        unsubscribe()
    print()
    reason = result.get("stopReason")
    if reason != "stop":
        print(f"[{reason}] {result.get('errorMessage', 'The model did not complete normally.')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
