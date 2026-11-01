#!/usr/bin/env bash
# Run the whole of Interlock with one command, for any project: build its
# database, print the project status, then start the review interface and open
# it in the browser. Ctrl+C stops the server.
#
#   ./run.sh                       the demonstration assembly: demo, status, web interface
#   ./run.sh --project so-arm100   a real product: built on first use, then status and web
#   ./run.sh --serve               skip the build and serve the existing database
#   ./run.sh --rebuild             rebuild a real product's database from its sources
#   ./run.sh --tests               run the test suite first
#   ./run.sh --list                list the projects
#   ./run.sh --stop                stop a review server left running
#
# Linux / macOS. Expects the project venv at .venv/ (see README, Setup).

set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
PY=.venv/bin/python
HOST=127.0.0.1
PORT=8000
URL="http://$HOST:$PORT"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
cli() { $PY -m interlock.cli --project "$project" "$@"; }

if [[ ! -x $PY ]]; then
    echo "no virtual environment at .venv/ -- see README, Setup" >&2
    exit 1
fi

project=winch
serve_only=0
rebuild=0
run_tests=0
while (( $# )); do
    case $1 in
        --project) project=${2:?--project needs a name}; shift ;;
        --serve) serve_only=1 ;;
        --rebuild) rebuild=1 ;;
        --tests) run_tests=1 ;;
        --list) exec $PY -m interlock.cli projects ;;
        --stop)
            if pkill -f '^[^ ]*python[0-9.]* -m interlock\.cli( .*)? serve( |$)'; then echo "review server stopped"
            else echo "no review server was running"; fi
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

if ss -ltn 2>/dev/null | grep -q ":$PORT\b"; then
    echo "port $PORT is already in use: a review server is still running (stop it with --stop)" >&2
    exit 1
fi

if (( run_tests )); then
    say "Tests"
    $PY -m pytest tests -q
fi

if (( ! serve_only )); then
    if [[ $project == winch ]]; then
        say "Building the demonstration database and running all five stages"
        cli demo
    elif (( rebuild )) || [[ ! -f data/projects/$project/interlock.db ]]; then
        say "Building project $project from its published CAD"
        $PY -m interlock.cli build-project "$project"
        say "Interference check (advisory)"
        cli interference
    fi
fi

say "Project status: $project"
cli status --fast

say "Review interface for $project at $URL"
cli serve --host "$HOST" --port "$PORT" &
server=$!
trap 'kill $server 2>/dev/null; wait $server 2>/dev/null; echo; echo "server stopped"' EXIT INT TERM

for _ in $(seq 50); do
    curl -fs "$URL/healthz" >/dev/null 2>&1 && break
    sleep 0.2
done
if command -v xdg-open >/dev/null; then
    xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null; then
    open "$URL"
fi
echo "open $URL in a browser if it did not open by itself. Ctrl+C to stop."
wait $server
