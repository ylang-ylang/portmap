#!/usr/bin/env bash
# Spike 2: docker compose v2 CLI against the Podman compat socket.
# Verifies: up/ps/down lifecycle, !reset override semantics, label merge,
# external network attachment, direct TCP port mapping.
# Requires: docker CLI + compose plugin on host, rootful podman.socket.
set -u
export DOCKER_HOST="unix:///run/podman/podman.sock"
PASS=0; FAIL=0
ok()   { echo "PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

WORK=/tmp/spike2-compose
EXTNET=spike2_ext

cleanup() {
  (cd "$WORK" 2>/dev/null && sudo -E docker compose -f base.yml -f override.yml down -v >/dev/null 2>&1)
  sudo podman network rm "$EXTNET" >/dev/null 2>&1
  rm -rf "$WORK"
}
cleanup

mkdir -p "$WORK"
sudo podman network create "$EXTNET" >/dev/null || { bad "external network create"; exit 1; }

cat > "$WORK/base.yml" <<'EOF'
services:
  web:
    image: docker.io/traefik/whoami:latest
    ports:
      - "18090:80"
    labels:
      - spike.base=true
EOF

cat > "$WORK/override.yml" <<EOF
services:
  web:
    ports: !reset []
    labels:
      - spike.override=true
    networks:
      - $EXTNET

networks:
  $EXTNET:
    external: true
    name: $EXTNET
EOF

echo "== compose config renders against podman socket =="
if (cd "$WORK" && sudo -E docker compose -f base.yml -f override.yml config --format json > /tmp/spike2-config.json 2>/tmp/spike2-config.err); then
  ok "compose config renders"
else
  bad "compose config failed:"; cat /tmp/spike2-config.err | head -5
  echo "== rendered config is required for the rest; aborting =="; exit 1
fi

echo "== !reset semantics in rendered config =="
# base published 18090; override !reset must remove it from the merged result
if python3 -c "
import json
cfg = json.load(open('/tmp/spike2-config.json'))
ports = cfg['services']['web'].get('ports', [])
print('  merged ports:', ports)
assert all(p.get('published') != '18090' for p in ports), '18090 still published'
"; then ok "!reset removed base port mapping (client-side merge)"; else bad "!reset did not remove base mapping"; fi

echo "== compose up against podman =="
if (cd "$WORK" && sudo -E docker compose -f base.yml -f override.yml up -d 2>/tmp/spike2-up.err); then
  ok "compose up -d succeeded"
else
  bad "compose up failed:"; head -8 /tmp/spike2-up.err
fi

CID=$(sudo podman ps -q --filter label=spike.override=true | head -1)
echo "  container: ${CID:-none}"

echo "== label merge (base + override both present) =="
LABELS=$(sudo podman inspect "$CID" --format '{{json .Config.Labels}}' 2>/dev/null)
echo "  labels: $LABELS"
echo "$LABELS" | grep -q '"spike.base":"true"' && echo "$LABELS" | grep -q '"spike.override":"true"' \
  && ok "labels merged" || bad "label merge broken"

echo "== compose project labels present (portmap discovery depends on labels) =="
echo "$LABELS" | grep -q 'com.docker.compose.project' && ok "compose project label present" || bad "compose project label missing"

echo "== external network attached =="
NETS=$(sudo podman inspect "$CID" --format '{{json .NetworkSettings.Networks}}')
echo "$NETS" | grep -q "$EXTNET" && ok "external network attached" || bad "external network missing: $NETS"

echo "== port 18090 NOT published (!reset applied at runtime) =="
if curl -s -m 2 -o /dev/null http://127.0.0.1:18090/; then
  bad "18090 still answers"
else
  ok "18090 not published"
fi

echo "== service reachable on container ip =="
IP=$(sudo podman inspect "$CID" --format "{{.NetworkSettings.Networks.$EXTNET.IPAddress}}")
BODY=$(curl -s -m 3 "http://$IP/" | head -c 40)
echo "  http://$IP/ -> $BODY"
echo "$BODY" | grep -qi "Hostname:" && ok "service reachable on $EXTNET" || bad "service unreachable on $EXTNET"

echo "== direct TCP mapping still works when declared =="
cat > "$WORK/raw.yml" <<'EOF'
services:
  raw:
    image: docker.io/traefik/whoami:latest
    ports:
      - "18091:80/tcp"
EOF
(cd "$WORK" && sudo -E docker compose -f raw.yml up -d >/dev/null 2>&1)
BODY=$(curl -s -m 3 http://127.0.0.1:18091/ | head -c 40)
echo "  127.0.0.1:18091 -> $BODY"
echo "$BODY" | grep -qi "Hostname:" && ok "direct TCP port mapping works" || bad "direct TCP port mapping failed"
(cd "$WORK" && sudo -E docker compose -f raw.yml down -v >/dev/null 2>&1)

echo "== compose ps / down lifecycle =="
(cd "$WORK" && sudo -E docker compose -f base.yml -f override.yml ps --format json 2>/dev/null | head -c 120; echo)
cleanup
sudo podman ps -q --filter label=spike.override=true | grep -q . && bad "down left containers" || ok "compose down clean"

echo "== result: $PASS passed, $FAIL failed =="
exit $FAIL
