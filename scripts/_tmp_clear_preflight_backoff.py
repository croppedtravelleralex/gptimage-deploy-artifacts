#!/usr/bin/env python3
import base64, subprocess
py = """
from services.account_service import account_service
account_service.reload_from_storage()
n=len(account_service._image_preflight_failed_until)
account_service._image_preflight_failed_until.clear()
print('cleared_preflight_backoff', n)
"""
b64=base64.b64encode(py.encode()).decode()
print(subprocess.check_output(["ssh","-o","ConnectTimeout=20","panda",f"docker exec chatgpt2api-local uv run python3 -c \"import base64; exec(base64.b64decode('{b64}').decode())\""], text=True))
