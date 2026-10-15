"""Command-line interface for Gardener Proxy."""

import argparse
import sys
import uvicorn

from gardener.config import load_config
from gardener.proxy import create_app


def print_banner(config):
    banner = f"""
🌿 Gardener Proxy v0.1.0
────────────────────────────────────────────────────────────
  Proxy URL:       http://{config.server.host}:{config.server.port}
  Upstream Target: {config.upstream.base_url}
  Gardener Model:  {config.gardener.model or "(mirrors main agent model)"}
  Schedule:        Garden every {config.gardener.turns} turn(s)
  Protected Tail:  Last {config.gardener.protected_turns} assistant turns kept verbatim
  State Mode:      {config.gardener.state_injection_mode}
────────────────────────────────────────────────────────────
Point your frontend / harness OPENAI_BASE_URL to:
  http://{config.server.host}:{config.server.port}/v1
────────────────────────────────────────────────────────────
"""
    print(banner, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="🌿 Gardener: Turn-based context compaction proxy for LLM APIs"
    )
    parser.add_argument(
        "-c", "--config",
        type=str,
        default=None,
        help="Path to YAML configuration file (default: config.yaml)"
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=None,
        help="Port to listen on (default: 8000)"
    )
    parser.add_argument(
        "-H", "--host",
        type=str,
        default=None,
        help="Host address to bind to (default: 127.0.0.1)"
    )
    parser.add_argument(
        "-u", "--upstream",
        type=str,
        default=None,
        help="Upstream OpenAI-compatible base URL"
    )
    parser.add_argument(
        "-m", "--model",
        type=str,
        default=None,
        help="Model to use for gardener summarization (default: mirror request model)"
    )
    parser.add_argument(
        "-n", "--turns",
        type=int,
        default=None,
        help="Garden every N turns (default: 3)"
    )
    parser.add_argument(
        "-k", "--protected-turns",
        type=int,
        default=None,
        help="Number of recent assistant turns to keep verbatim (default: 5)"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn hot reloading for development"
    )

    args = parser.parse_args()

    config = load_config(args.config)

    # CLI flag overrides
    if args.port is not None:
        config.server.port = args.port
    if args.host is not None:
        config.server.host = args.host
    if args.upstream is not None:
        config.upstream.base_url = args.upstream
    if args.model is not None:
        config.gardener.model = args.model
    if args.turns is not None:
        config.gardener.turns = args.turns
    if args.protected_turns is not None:
        config.gardener.protected_turns = args.protected_turns

    print_banner(config)

    app = create_app(config)
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
