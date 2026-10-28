from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def virtualenv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def ensure_virtualenv() -> Path:
    executable = virtualenv_python()
    if not executable.is_file():
        print("Creating virtual environment...")
        venv.EnvBuilder(with_pip=True, clear=False).create(ROOT / ".venv")
    if not executable.is_file():
        raise RuntimeError(f"Virtual environment Python was not created: {executable}")
    return executable


def install_dependencies(executable: Path) -> None:
    subprocess.run(
        [str(executable), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
        check=True,
    )


def existing_token(output_path: Path) -> str:
    if not output_path.is_file():
        return ""
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
        server_group = data.get("mcpServers", data.get("servers", {}))
        return str(server_group.get("mineru-local", {}).get("env", {}).get("MINERU_API_TOKEN", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def build_config(executable: Path, client: str, token: str) -> dict:
    command = str(executable.resolve())
    args = [str((ROOT / "server.py").resolve())]
    server = {
        "command": command,
        "args": args,
        "env": {"MINERU_API_TOKEN": token},
    }
    if client == "vscode":
        return {"servers": {"mineru-local": {"type": "stdio", **server}}}
    return {"mcpServers": {"mineru-local": server}}


def write_config(output_path: Path, config: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        output_path.chmod(0o600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install MinerU Local MCP and generate client configuration.")
    parser.add_argument("--client", choices=("claude", "vscode"), default="claude",
                        help="MCP client configuration format (default: claude).")
    parser.add_argument("--output", type=Path,
                        help="Generated configuration path; defaults to mcp-config.generated.json.")
    parser.add_argument("--skip-install", action="store_true",
                        help="Create configuration without installing dependencies.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    executable = ensure_virtualenv()
    if not args.skip_install:
        install_dependencies(executable)
    default_name = "mcp-config.vscode.generated.json" if args.client == "vscode" else "mcp-config.generated.json"
    output_path = (args.output or (ROOT / default_name)).resolve()
    token = existing_token(output_path)
    write_config(output_path, build_config(executable, args.client, token))
    print(f"Generated MCP configuration: {output_path}")
    print("Open that file, fill in MINERU_API_TOKEN, and import it into your MCP client.")
    print(f"Python: {executable.resolve()}")
    print(f"Server: {(ROOT / 'server.py').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
