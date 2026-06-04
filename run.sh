#!/bin/bash
set -eu
cd "$(dirname "$0")"

python=python3

if [ ! -f .venv/bin/activate ]; then
    echo "Création de l'environnement virtuel…"
    $python -m venv --prompt "$($python --version | tr -d ' ')" .venv
    source .venv/bin/activate
    pip install --quiet -r requirements.txt
else
    source .venv/bin/activate
fi

exec python main.py "$@"
