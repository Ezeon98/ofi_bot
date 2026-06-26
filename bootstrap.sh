#!/bin/bash

echo "===== Starting WhatsApp Bot ====="

if [ ! -z "$DEBUG" ] && [ "$DEBUG" != "false" ]; then
    echo "===== Debug mode — starting with debugpy on port 3002 ====="
    exec python -m debugpy --listen 0.0.0.0:3002 \
        -m uvicorn src.presentation.api.main:app --host 0.0.0.0 --port 8000 --reload
else
    echo "===== Production mode ====="
    exec uvicorn src.presentation.api.main:app --host 0.0.0.0 --port 8000 --reload
fi
