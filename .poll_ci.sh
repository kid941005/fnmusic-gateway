cd /d/dsh/esp32/fnmusic-gateway
for i in $(seq 1 80); do
  s=$(C:/Users/19198/.local/gh/gh.exe run list --limit 1 --json databaseId,status,conclusion,displayTitle 2>/dev/null)
  echo "[$(date +%H:%M:%S)] $s"
  echo "$s" | grep -q '"status":"completed"' && break
  sleep 30
done
echo DONE_POLL