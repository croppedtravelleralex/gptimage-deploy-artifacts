import subprocess
import time

def ssh(cmd: str) -> str:
    return subprocess.check_output(["ssh", "panda", cmd], text=True, errors="replace").strip()

print("force-recreate...")
print(ssh("cd /root/gptimage && docker compose -f docker-compose.panda.yml up -d --force-recreate"))
time.sleep(10)
print("container accounts html:")
print(ssh("docker exec chatgpt2api-local ls -la /app/web_dist/accounts/index.html"))
print("http /accounts:")
print(ssh("curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8012/accounts"))
print("health:")
print(ssh("curl -fsS 'http://127.0.0.1:8012/health?format=json' | python3 -c \"import json,sys; d=json.load(sys.stdin); print('healthy', d.get('healthy'))\""))
