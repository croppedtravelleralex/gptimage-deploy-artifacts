import json
import sqlite3

conn = sqlite3.connect("/root/gptimage/data/image_tasks.db")
rows = conn.execute(
    "SELECT task_id, data FROM image_tasks WHERE status='error' ORDER BY updated_ts DESC LIMIT 8"
).fetchall()
for task_id, data in rows:
    t = json.loads(data or "{}")
    print("---", task_id)
    print("error:", t.get("error"))
    print("progress:", t.get("progress"))
conn.close()
