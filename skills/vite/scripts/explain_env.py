# /// script
# requires-python = ">=3.14"
# ///
"""Show where each import.meta.env value comes from for one Vite command and mode.

Resolves the project's config with its installed Vite (resolveConfig), so envDir,
envPrefix, the mode and NODE_ENV handling match a real run. Lists the env files Vite reads
for the mode, the source of every client variable, variables the client never sees, and
values cut short by "#" or changed by "$" expansion. --demo runs four commands against a
throwaway project (Vite 8.3.4) with .env, .env.development, .env.production and
.env.staging. Needs node and npm on PATH.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NODE_RESOLVE = r"""
const fs = await import('node:fs')
const path = await import('node:path')
const { parseEnv } = await import('node:util')
const vite = await import('vite')
const { command, mode } = JSON.parse(process.env.ENV_TRACE)
const shell = { ...process.env }
const defaults = command === 'build' ? ['production', 'production'] : ['development', 'development']
const config = await vite.resolveConfig({ mode: mode || undefined, logLevel: 'silent' }, command, ...defaults)
const prefixes = vite.resolveEnvPrefix({ envPrefix: config.envPrefix })
const names = ['.env', '.env.local', `.env.${config.mode}`, `.env.${config.mode}.local`]
const files = config.envDir === false ? [] : names.map((name) => {
  const file = path.join(config.envDir, name)
  const text = fs.existsSync(file) ? fs.readFileSync(file, 'utf-8') : null
  return { name, text, values: text === null ? {} : parseEnv(text) }
})
console.log(JSON.stringify({
  mode: config.mode,
  envDir: config.envDir,
  prefixes,
  shellNodeEnv: shell.NODE_ENV ?? null,
  nodeEnv: process.env.NODE_ENV,
  clientEnv: config.env,
  shellKeys: Object.keys(shell).filter(
    (k) => prefixes.some((p) => k.startsWith(p)) || files.some((f) => k in f.values),
  ),
  files,
  expanded: config.envDir === false ? {} : vite.loadEnv(config.mode, config.envDir, ''),
}))
"""

LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][\w.-]*)\s*=\s*(.*)$")
BUILT_IN = {"BASE_URL", "MODE", "DEV", "PROD", "SSR"}

DEMO_FILES = {
    "index.html": '<!doctype html>\n<script type="module" src="/src/main.js"></script>\n',
    "src/main.js": "console.log(import.meta.env.VITE_API_URL)\n",
    ".env": (
        "VITE_API_URL=https://api.example.com\n"
        "VITE_COLOR=#ff0000\n"
        "VITE_TOKEN=abc$def\n"
        "DB_PASSWORD=secret\n"
    ),
    ".env.development": "VITE_API_URL=http://localhost:3000\n",
    ".env.production": "VITE_API_URL=https://prod.example.com\n",
    ".env.staging": "VITE_API_URL=https://staging.example.com\nNODE_ENV=development\n",
}

DEMO_RUNS = [
    ("serve", "", {}),
    ("build", "", {}),
    ("build", "staging", {}),
    ("build", "", {"VITE_API_URL": "https://shell.example.com"}),
]


def resolve(root: Path, command: str, mode: str, extra_env: dict[str, str]) -> dict:
    env = {**os.environ, **extra_env}
    env["ENV_TRACE"] = json.dumps({"command": command, "mode": mode})
    out = subprocess.run(
        ["node", "--input-type=module", "-e", NODE_RESOLVE],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


def raw_lines(text: str) -> dict[str, tuple[int, str]]:
    """Map each key to its last line number and raw value text in one env file."""
    found = {}
    for number, line in enumerate(text.splitlines(), 1):
        if m := LINE_RE.match(line):
            found[m[1]] = (number, m[2])
    return found


def report(data: dict, command: str, mode: str, extra_env: dict[str, str]) -> None:
    prefixes = data["prefixes"]
    files = [f for f in data["files"] if f["text"] is not None]
    shown = " ".join(
        [*(f"{k}={v}" for k, v in extra_env.items()), "vite"]
        + (["build"] if command == "build" else [])
        + ([f"--mode {mode}"] if mode else [])
    )
    print(f"$ {shown}\n  mode {data['mode']!r}, envDir {data['envDir']}")

    node_env = data["nodeEnv"]
    file_node_env = [f["name"] for f in files if "NODE_ENV" in f["values"]]
    if data["shellNodeEnv"]:
        source = "from the shell"
    elif file_node_env and node_env == "development":
        source = f"from {file_node_env[-1]}"
    elif file_node_env:
        source = (
            f"set by Vite; only NODE_ENV=development is honoured in {file_node_env[-1]}"
        )
    else:
        source = "set by Vite for this command"
    env = data["clientEnv"]
    print(
        f"  NODE_ENV={node_env} ({source})"
        f" -> DEV={json.dumps(env['DEV'])}, PROD={json.dumps(env['PROD'])}"
    )

    print("\n  Env files for this mode, lowest to highest priority (shell beats all):")
    for f in data["files"]:
        print(f"    {f['name']:<28} {'read' if f['text'] is not None else 'not found'}")

    print("\n  import.meta.env:")
    width = max(map(len, env)) + 2
    for key in sorted(env, key=lambda k: (k not in BUILT_IN, k)):
        defined_in = [f["name"] for f in files if key in f["values"]]
        if key in BUILT_IN:
            origin = "Vite"
        elif key == "VITE_USER_NODE_ENV" and file_node_env:
            origin = f"Vite, copied from NODE_ENV in {file_node_env[-1]}"
        elif key in data["shellKeys"]:
            origin = "shell" + (
                f" (beats {', '.join(defined_in)})" if defined_in else ""
            )
        else:
            origin = defined_in[-1] if defined_in else "?"
            if len(defined_in) > 1:
                origin += f" (beats {', '.join(defined_in[:-1])})"
        print(f"    {key:<{width}} {json.dumps(env[key]):<32} {origin}")

    hidden = sorted(
        {k for f in files for k in f["values"]}
        - {"NODE_ENV"}
        - {k for k in env if any(k.startswith(p) for p in prefixes)}
    )
    if hidden:
        print(f"\n  Not exposed to the client (no {' or '.join(prefixes)} prefix):")
        for key in hidden:
            where = ", ".join(f["name"] for f in files if key in f["values"])
            print(f"    {key:<{width}} {where}")

    warnings = []
    for f in files:
        for key, (number, raw) in raw_lines(f["text"]).items():
            parsed = f["values"].get(key, "")
            if not raw.startswith(("'", '"', "`")) and re.search(r"(^|\S)#", raw):
                warnings.append(
                    f"{f['name']}:{number} {key}={raw} reads as {json.dumps(parsed)}:"
                    f' an unquoted # starts a comment; write {key}="{raw}"'
                )
            final = data["expanded"].get(key)
            winner = [g["name"] for g in files if key in g["values"]][-1]
            from_file = winner == f["name"] and key not in data["shellKeys"]
            if "$" in parsed and final != parsed and from_file:
                warnings.append(
                    f"{f['name']}:{number} {key}={raw} reads as {json.dumps(final)}:"
                    " $ expands variables, even inside quotes; escape it as \\$"
                )
    if warnings:
        print("\n  Check these values:")
        for line in warnings:
            print(f"    {line}")
    print()


def make_demo(root: Path) -> None:
    package = {"private": True, "type": "module", "devDependencies": {"vite": "8.3.4"}}
    (root / "package.json").write_text(json.dumps(package, indent=2))
    for name, text in DEMO_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if name.startswith(".env"):
            print(f"# {name}\n{text}")
    print(f"Installing vite@8.3.4 into {root} ...\n", file=sys.stderr)
    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", default=".", help="Vite root, where vite.config.* lives (default: .)"
    )
    parser.add_argument(
        "--command",
        choices=["serve", "build"],
        default="serve",
        help="serve is `vite` / `vite dev`; build is `vite build` (default: serve)",
    )
    parser.add_argument("--mode", default="", help="the --mode value, if any")
    parser.add_argument("--demo", action="store_true", help="use a throwaway project")
    args = parser.parse_args()

    for tool in ("node", "npm"):
        if shutil.which(tool) is None:
            parser.error(f"{tool} is not on PATH")

    if not args.demo:
        root = Path(args.root).resolve()
        report(resolve(root, args.command, args.mode, {}), args.command, args.mode, {})
        return 0

    with tempfile.TemporaryDirectory(prefix="vite-env-") as tmp:
        root = Path(tmp)
        make_demo(root)
        for command, mode, extra_env in DEMO_RUNS:
            report(resolve(root, command, mode, extra_env), command, mode, extra_env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
