#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
CLI_NAME="cli-anything-dolphinscheduler"
SKILL_ID="cli-anything-dolphinscheduler"
INSTALL_MODE="editable"
ENV_MODE="venv"
VENV_DIR="${VENV_DIR:-.venv}"
WITH_DEV=0
RUN_VERIFY=0
RUN_FORCE_INSTALLED_TESTS=0
INSTALL_SKILL=0
INSTALL_BIN=0
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SKILL_DIRS=()

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install cli-anything-dolphinscheduler from this repository root.

Options:
  --venv DIR               Create/use a virtualenv at DIR (default: .venv).
  --system                 Install into the current Python environment.
  --user                   Install with pip --user.
  --no-editable            Install as a regular local package instead of editable.
  --dev                    Install test dependencies.
  --verify                 Verify the installed command and run smoke checks.
  --force-installed-tests  Run subprocess tests against the installed command.
  --install-skill          Install the AI skill file into default skill dirs.
  --skill-dir DIR          Install the skill file into DIR (repeatable).
  --install-bin            Install a stable launcher into ~/.local/bin.
  --bin-dir DIR            Install the launcher into DIR (default: ~/.local/bin).
  -h, --help               Show this help.

Examples:
  ./install.sh --verify
  ./install.sh --dev --verify --install-skill --install-bin --force-installed-tests
  ./install.sh --install-skill --skill-dir ~/.codex/skills --verify
  ./install.sh --install-skill --skill-dir ~/.claude/skills --verify
  ./install.sh --install-skill --skill-dir ~/.openclaw/skills --verify
  PYTHON=/path/to/python ./install.sh --venv .venv --verify
  ./install.sh --system --user --verify
EOF
}

expand_path() {
  case "$1" in
    "~")
      printf '%s\n' "$HOME"
      ;;
    "~/"*)
      printf '%s/%s\n' "$HOME" "${1#~/}"
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

resolve_cli_bin() {
  "$PYTHON_BIN" -c 'import shutil, sys, sysconfig; from pathlib import Path; name = sys.argv[1]; path = shutil.which(name) or str(Path(sysconfig.get_path("scripts")) / name); print(path); sys.exit(0 if Path(path).exists() else 1)' "$CLI_NAME"
}

abs_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$ROOT_DIR" "$1" ;;
  esac
}

install_skill_file() {
  local source_skill="$ROOT_DIR/skills/$SKILL_ID/SKILL.md"
  if [[ ! -f "$source_skill" ]]; then
    echo "Skill file not found: $source_skill" >&2
    exit 1
  fi

  local target_roots=()
  if [[ "${#SKILL_DIRS[@]}" -gt 0 ]]; then
    target_roots=("${SKILL_DIRS[@]}")
  else
    target_roots+=("${CODEX_HOME:-$HOME/.codex}/skills")
    target_roots+=("${AGENTS_HOME:-$HOME/.agents}/skills")
  fi

  local installed=()
  local root target_root target_dir
  for root in "${target_roots[@]}"; do
    target_root="$(expand_path "$root")"
    target_dir="$target_root/$SKILL_ID"
    mkdir -p "$target_dir"
    cp "$source_skill" "$target_dir/SKILL.md"
    installed+=("$target_dir/SKILL.md")
  done

  echo "Installed AI skill:"
  printf '  %s\n' "${installed[@]}"
}

install_user_bin() {
  local cli_bin target_bin
  if [[ "$ENV_MODE" == "venv" ]]; then
    cli_bin="$(abs_path "$VENV_DIR")/bin/$CLI_NAME"
  else
    cli_bin="$(resolve_cli_bin)"
  fi

  if [[ ! -x "$cli_bin" ]]; then
    echo "CLI executable not found: $cli_bin" >&2
    exit 1
  fi

  BIN_DIR="$(expand_path "$BIN_DIR")"
  target_bin="$BIN_DIR/$CLI_NAME"
  mkdir -p "$BIN_DIR"
  cat > "$target_bin" <<EOF
#!/usr/bin/env bash
exec "$cli_bin" "\$@"
EOF
  chmod +x "$target_bin"
  echo "Installed launcher: $target_bin"
}

PIP_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      ENV_MODE="system"
      PIP_ARGS+=("--user")
      ;;
    --system)
      ENV_MODE="system"
      ;;
    --venv)
      ENV_MODE="venv"
      shift
      if [[ $# -eq 0 ]]; then
        echo "--venv requires a directory" >&2
        exit 2
      fi
      VENV_DIR="$1"
      ;;
    --no-editable)
      INSTALL_MODE="regular"
      ;;
    --dev)
      WITH_DEV=1
      ;;
    --verify)
      RUN_VERIFY=1
      ;;
    --force-installed-tests)
      RUN_FORCE_INSTALLED_TESTS=1
      WITH_DEV=1
      RUN_VERIFY=1
      ;;
    --install-skill)
      INSTALL_SKILL=1
      ;;
    --skill-dir)
      INSTALL_SKILL=1
      shift
      if [[ $# -eq 0 ]]; then
        echo "--skill-dir requires a directory" >&2
        exit 2
      fi
      SKILL_DIRS+=("$1")
      ;;
    --install-bin)
      INSTALL_BIN=1
      ;;
    --bin-dir)
      INSTALL_BIN=1
      shift
      if [[ $# -eq 0 ]]; then
        echo "--bin-dir requires a directory" >&2
        exit 2
      fi
      BIN_DIR="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

cd "$ROOT_DIR"

if [[ "$ENV_MODE" == "venv" ]]; then
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  PYTHON_BIN="$VENV_DIR/bin/python"
  export PATH="$ROOT_DIR/$VENV_DIR/bin:$PATH"
fi

"$PYTHON_BIN" -m ensurepip --upgrade >/dev/null 2>&1 || true
"$PYTHON_BIN" -m pip --version >/dev/null

TARGET="."
if [[ "$WITH_DEV" -eq 1 ]]; then
  TARGET=".[dev]"
fi

if [[ "$INSTALL_MODE" == "editable" ]]; then
  "$PYTHON_BIN" -m pip install -e "$TARGET" "${PIP_ARGS[@]}"
else
  "$PYTHON_BIN" -m pip install "$TARGET" "${PIP_ARGS[@]}"
fi

if [[ "$INSTALL_SKILL" -eq 1 ]]; then
  install_skill_file
fi

if [[ "$INSTALL_BIN" -eq 1 ]]; then
  install_user_bin
fi

if [[ "$RUN_VERIFY" -eq 1 ]]; then
  CLI_BIN="$(resolve_cli_bin)"
  "$CLI_BIN" --version
  "$CLI_BIN" --help >/dev/null
  "$CLI_BIN" task --help >/dev/null
  "$CLI_BIN" resource --help >/dev/null
  "$CLI_BIN" --json task build-shell \
    --name install_smoke \
    --script "echo install_smoke" \
    --code 1001 >/dev/null
fi

if [[ "$RUN_FORCE_INSTALLED_TESTS" -eq 1 ]]; then
  CLI_ANYTHING_FORCE_INSTALLED=1 "$PYTHON_BIN" -m pytest \
    cli_anything/dolphinscheduler/tests/test_subprocess.py -v
fi

CLI_BIN="$(resolve_cli_bin || printf '%s' "$CLI_NAME")"

echo "Installed $CLI_NAME"
echo "CLI command: $CLI_BIN"
if [[ "$ENV_MODE" == "venv" ]]; then
  echo "Activate with: source $ROOT_DIR/$VENV_DIR/bin/activate"
fi
if [[ "$INSTALL_BIN" -eq 1 ]]; then
  echo "User launcher: $BIN_DIR/$CLI_NAME"
fi
