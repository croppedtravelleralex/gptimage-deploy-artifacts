docker logs --since 24h chatgpt2api-local 2>&1 | rg -n 'image_download|images_ready|image_result|returned_images|file_ids.: \[\"file-|generation succeeded|image_ok|status.: .success|download_ok|saved_image' | tail -n 40
echo '---TIMEOUT_COUNT---'
docker logs --since 24h chatgpt2api-local 2>&1 | rg -c 'image_poll_timeout' || true
echo '---SSE_CID_COUNT---'
docker logs --since 24h chatgpt2api-local 2>&1 | rg -c 'image_sse_conversation_id_captured' || true
echo '---CFG---'
python3 - <<'PY'
import json
d=json.load(open('/root/gptimage/config.json'))
print('image_spa_tool_path=', d.get('image_spa_tool_path'))
PY
