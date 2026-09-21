#!/usr/bin/env bash
# Spike 1: Traefik docker provider against the Podman compat socket (rootful).
# Verifies: label discovery + HTTP routing + live events (container add/remove).
# Run on the target host as root (or via sudo): bash 01-traefik-podman-socket.sh
set -u
P="sudo podman"
NET="spike_net"
PASS=0; FAIL=0

ok()   { echo "PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

cleanup() {
  $P rm -f spike-traefik spike-web-a spike-web-b >/dev/null 2>&1
  $P network rm "$NET" >/dev/null 2>&1
}
cleanup

echo "== setup: network + traefik on podman socket =="
$P network create "$NET" >/dev/null || { bad "network create"; exit 1; }

$P run -d --name spike-traefik \
  -p 18080:80 -p 18081:8080 \
  -v /run/podman/podman.sock:/var/run/docker.sock:ro \
  --network "$NET" \
  docker.io/library/traefik:v3.6 \
  --providers.docker=true \
  --providers.docker.exposedbydefault=false \
  --entrypoints.web.address=:80 \
  --api.insecure=true >/dev/null || { bad "traefik run"; exit 1; }

sleep 3
$P logs spike-traefik 2>&1 | grep -iE "error|failed" | grep -viE "gateway|entrypoint" | head -5

echo "== test 1: label discovery + routing =="
$P run -d --name spike-web-a \
  --network "$NET" \
  --label traefik.enable=true \
  --label "traefik.http.routers.web-a.rule=Host(\`web-a.spike.test\`)" \
  --label traefik.http.routers.web-a.entrypoints=web \
  --label traefik.http.services.web-a.loadbalancer.server.port=80 \
  docker.io/traefik/whoami:latest >/dev/null

sleep 3
BODY=$(curl -s -H 'Host: web-a.spike.test' http://127.0.0.1:18080/ | head -c 80)
echo "  response: $BODY"
echo "$BODY" | grep -qi "Hostname:" && ok "label discovered, routed by Host" || bad "no route for labeled container"

echo "== test 2: events - new container appears without restart =="
$P run -d --name spike-web-b \
  --network "$NET" \
  --label traefik.enable=true \
  --label "traefik.http.routers.web-b.rule=Host(\`web-b.spike.test\`)" \
  --label traefik.http.routers.web-b.entrypoints=web \
  --label traefik.http.services.web-b.loadbalancer.server.port=80 \
  docker.io/traefik/whoami:latest >/dev/null

sleep 3
BODY=$(curl -s -H 'Host: web-b.spike.test' http://127.0.0.1:18080/ | head -c 80)
echo "  response: $BODY"
echo "$BODY" | grep -qi "Hostname:" && ok "events: new container routed live" || bad "events: new container not picked up"

echo "== test 3: events - removed container deregisters =="
$P rm -f spike-web-b >/dev/null
sleep 3
CODE=$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: web-b.spike.test' http://127.0.0.1:18080/)
echo "  http code after removal: $CODE"
[ "$CODE" = "404" ] && ok "events: removed container deregistered" || bad "events: stale route after removal (code=$CODE)"

echo "== traefik provider view =="
curl -s http://127.0.0.1:18081/api/http/routers | python3 -c 'import json,sys; [print("  router:", r["name"], r["status"]) for r in json.load(sys.stdin)]' 2>/dev/null

cleanup
echo "== result: $PASS passed, $FAIL failed =="
exit $FAIL
