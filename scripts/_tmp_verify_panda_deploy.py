import json
import subprocess

def ssh(cmd: str) -> str:
    return subprocess.check_output(["ssh", "panda", cmd], text=True, errors="replace")

print("web_dist listing:")
try:
    print(ssh("ls -la /root/gptimage/web_dist 2>&1 | head -20"))
except Exception as e:
    print(e)

print("\nbackups:")
print(ssh("ls -dt /root/gptimage/web_dist.bak.* 2>/dev/null | head -3"))

print("\nhealth:")
d = json.loads(ssh("curl -fsS 'http://127.0.0.1:8012/health?format=json'"))
print("healthy", d.get("healthy"), "limit", (d.get("accounts") or {}).get("image_account_concurrency_limit"))
