import subprocess

def ssh(cmd: str) -> str:
    return subprocess.check_output(["ssh", "panda", cmd], text=True, errors="replace").strip()

checks = [
    "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8012/accounts",
    "curl -sS http://127.0.0.1:8012/accounts 2>/dev/null | python3 -c \"import sys; d=sys.stdin.read(200); print(repr(d))\"",
    "docker exec chatgpt2api-local curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:80/accounts",
    "docker exec chatgpt2api-local ls -la /app/web_dist/accounts/index.html 2>&1",
    "docker exec chatgpt2api-local ls -la /app/web_dist/index.html 2>&1",
    "grep -n web_dist /root/gptimage/docker-compose.panda.yml",
]
for cmd in checks:
    print("---", cmd)
    try:
        print(ssh(cmd))
    except subprocess.CalledProcessError as exc:
        print("ERR", exc.output if hasattr(exc, 'output') else exc)
