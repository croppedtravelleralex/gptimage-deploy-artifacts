#!/usr/bin/env python3
import subprocess
cmd = [
    "ssh",
    "panda",
    "docker exec chatgpt2api-local /app/.venv/bin/python -c \"import curl_cffi,PIL; print('ok', curl_cffi.__version__, PIL.__version__)\"",
]
raise SystemExit(subprocess.call(cmd))
