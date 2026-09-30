#!/bin/sh
# Локальный сервер модели; веса хранятся в data/ и не попадают в репозиторий.
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export OLLAMA_MODELS="$project_dir/data/ollama/models"
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_NO_CLOUD=1
export OLLAMA_CONTEXT_LENGTH="${AI_CONTEXT_TOKENS:-16384}"
export OLLAMA_KEEP_ALIVE="${AI_KEEP_ALIVE:-30m}"
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_MAX_LOADED_MODELS=1
mkdir -p "$OLLAMA_MODELS"

exec ollama serve
