#!/usr/bin/env python3
import os
import signal
import subprocess
import time
import sys

upstream = sys.argv[1]
os.chdir("/root/gptimage")
subprocess.run(["pkill", "-f", "_tmp_http_upstream_forwarder.py"], check=False)
time.sleep(1)
with open("/tmp/chain_forwarder_18443.log", "w", encoding="utf-8") as log:
    subprocess.Popen(
        ["python3", "scripts/_tmp_http_upstream_forwarder.py", "127.0.0.1", "18443", upstream],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
time.sleep(2)
from curl_cffi import requests

proxies = {"http": "http://127.0.0.1:18443", "https": "http://127.0.0.1:18443"}
r = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=25, impersonate="chrome")
print("panda_chain", r.status_code, r.text)
r2 = requests.get("https://chatgpt.com/api/auth/csrf", proxies=proxies, timeout=30, impersonate="chrome")
print("csrf", r2.status_code, r2.text[:80])
