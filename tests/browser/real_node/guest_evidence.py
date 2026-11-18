#!/usr/bin/env python3
"""Public allowlisted evidence from an actual disposable Portmap guest.

Never serialize Docker Env/config, Portmap state, cloud-init data or credentials.
"""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib
import urllib.error
import urllib.request


def command(argv, check=True):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and result.returncode:
        raise RuntimeError('Evidence command failed: ' + ' '.join(argv[:3]))
    return result


def output(argv):
    return command(argv).stdout.strip()


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def host_keys():
    keys = []
    for path in sorted(Path('/etc/ssh').glob('ssh_host_*.pub')):
        fields = path.read_text().split()
        blob = base64.b64decode(fields[1], validate=True)
        digest = hashlib.sha256(blob).digest()
        keys.append({'algorithm': fields[0], 'sha256Hex': digest.hex(),
                     'sha256Base64': 'SHA256:' + base64.b64encode(digest).decode().rstrip('='),
                     'publicKey': fields[0] + ' ' + fields[1]})
    return keys


def catalog(config, address):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    responses = {}
    for path in ('/registry.json', '/api/machines', '/api/connections'):
        request = urllib.request.Request('http://' + address + path, headers={'Host': config['domain']})
        try:
            with opener.open(request, timeout=15) as response:
                responses[path] = (response.status, json.load(response))
        except urllib.error.HTTPError as error:
            responses[path] = (error.code, None)
    registry = responses['/registry.json'][1]
    if not isinstance(registry, dict):
        raise RuntimeError('Actual registry did not return a JSON object')
    services = []
    for service in registry.get('services', []):
        row = {key: service.get(key) for key in ('repo_name', 'branch', 'compose_service', 'container', 'image')}
        row['endpoints'] = [{key: endpoint.get(key) for key in
                             ('name', 'kind', 'container_port', 'host', 'url', 'preserve_host', 'upstream_host')}
                            for endpoint in service.get('endpoints', [])]
        services.append(row)
    machines = responses['/api/machines'][1]
    return {'address': address, 'registryStatus': responses['/registry.json'][0],
            'machinesStatus': responses['/api/machines'][0],
            'canManage': machines.get('can_manage_connections') if isinstance(machines, dict) else None,
            'connectionsStatus': responses['/api/connections'][0], 'services': services,
            'agentReady': registry.get('agent', {}).get('available')}


def collect():
    config = json.loads(Path('/etc/portmap-real-node.json').read_text())
    home = Path.home()
    app = home / 'vite-app'
    receipt = json.loads((home / 'node-setup-receipt.json').read_text())
    version = output(['portmap', '--version'])
    status_result = command(['portmap', 'status', '--check'], check=False)
    if not status_result.stdout.strip():
        raise RuntimeError('Actual status --check produced no structured evidence')
    status = json.loads(status_result.stdout)
    ingress_fields = ('name', 'backend', 'bind_address', 'port', 'container', 'container_id', 'container_state', 'running', 'target_port')
    ingresses = [{key: entry.get(key) for key in ingress_fields}
                 for entry in (status.get('gateway', {}).get('ingress', {}), status.get('server', {}).get('lan_ingress', {}))]
    agent = status.get('server', {}).get('agent', {})
    agent_executable = Path('/proc') / str(agent.get('pid', 0)) / 'exe'
    check = {'exitCode': status_result.returncode,
             'gatewayRunning': status.get('gateway', {}).get('running'),
             'routesLoaded': status.get('gateway', {}).get('routes_loaded'),
             'dnsRunning': status.get('dns', {}).get('running'),
             'hostDnsEnabled': status.get('dns', {}).get('host_enabled'),
             'resolverInstalled': status.get('resolver', {}).get('installed'),
             'agentRunning': agent.get('running'), 'agentPid': agent.get('pid'),
             'agentExecutableSha256': sha256(agent_executable) if agent_executable.exists() else None,
             'localHttpAddress': status.get('local', {}).get('http_address'),
             'localHttpPort': status.get('local', {}).get('http_port'), 'ingresses': ingresses}
    ids = output(['docker', 'ps', '-aq', '--no-trunc']).splitlines()
    actual_containers = json.loads(output(['docker', 'inspect', *ids])) if ids else []
    containers = []
    app_container = None
    route_label = re.compile(r'^traefik\.http\.(?:routers\.[^.]+\.(?:rule|entrypoints|service|middlewares)|services\.[^.]+\.loadbalancer\.(?:server\.port|passhostheader)|middlewares\.[^.]+\.headers\.customrequestheaders\.Host)$', re.I)
    for item in actual_containers:
        labels = item.get('Config', {}).get('Labels') or {}
        service = labels.get('com.docker.compose.service')
        row = {'id': item['Id'], 'name': item['Name'].lstrip('/'), 'image': item['Image'],
               'imageReference': item['Config']['Image'], 'state': item['State']['Status'],
               'composeService': service, 'composeProject': labels.get('com.docker.compose.project'),
               'routeLabels': {key: value for key, value in labels.items() if route_label.fullmatch(key)},
               'ports': item['NetworkSettings'].get('Ports') or {},
               'networks': {name: {'address': network.get('IPAddress'), 'networkId': network.get('NetworkID')}
                            for name, network in item['NetworkSettings'].get('Networks', {}).items()}}
        containers.append(row)
        if service == 'frontend':
            if app_container:
                raise RuntimeError('Fresh guest unexpectedly contains multiple frontend containers')
            app_container = item['Id']
    if not app_container:
        raise RuntimeError('Actual Docker does not contain the discovered Vite frontend')
    image_ids = sorted({item['Image'] for item in actual_containers})
    images = []
    for identity in json.loads(output(['docker', 'image', 'inspect', *image_ids])):
        images.append({'id': identity['Id'], 'references': identity.get('RepoTags') or [],
                       'rootfsLayers': identity.get('RootFS', {}).get('Layers', [])})
    markers = []
    logs = command(['docker', 'logs', app_container]).stdout
    allowed_marker_fields = ('type', 'node', 'at', 'method', 'host', 'path', 'peer', 'forwardedFor', 'forwardedHost', 'nonce')
    allowed_types = {'vite-http-request', 'vite-http-post-nonce', 'vite-websocket-upgrade', 'vite-websocket-nonce'}
    for line in logs.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('type') in allowed_types:
            markers.append({key: row[key] for key in allowed_marker_fields if key in row})
    gateway_markers = []
    for line in output(['sudo', '-n', 'cat', '/var/log/portmap-real-node-gateway.jsonl']).splitlines():
        row = json.loads(line)
        if row.get('type') in ('native-gateway-http-request', 'native-gateway-flow-invalid'):
            gateway_markers.append({key: row[key] for key in
                                    ('type', 'schemaVersion', 'at', 'flow', 'request', 'method', 'path', 'host', 'nonce', 'reason') if key in row})
    effective = dict(line.split(' ', 1) for line in output(['sudo', '-n', '/usr/sbin/sshd', '-T']).splitlines() if ' ' in line)
    journal = output(['sudo', '-n', 'journalctl', '--no-pager', 'SYSLOG_IDENTIFIER=sshd', '-o', 'cat'])
    forwarding = re.compile(r'.*server_request_direct_tcpip: originator [0-9a-fA-F:.]+ port [0-9]+, target 127\.0\.0\.1 port 80.*')
    forwardings = [line for line in journal.splitlines() if forwarding.fullmatch(line)]
    processes = []
    for line in output(['ps', '-eo', 'pid=,ppid=,uid=,comm=']).splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4:
            processes.append({'pid': int(fields[0]), 'ppid': int(fields[1]), 'uid': int(fields[2]), 'name': fields[3]})
    engine = json.loads(output(['docker', 'info', '--format', '{{json .}}']))
    declaration = app / '.portmap/endpoints.toml'
    endpoint = tomllib.loads(declaration.read_text())['endpoints']['frontend']
    return {'mode': config['mode'], 'name': config['node'], 'domain': config['domain'],
            'guest': {'machineId': Path('/etc/machine-id').read_text().strip(),
                      'bootId': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                      'cli': {'version': version.removeprefix('portmap '), 'versionOutput': version,
                              'archiveSha256': sha256(home / 'portmap-runtime.tar.gz'),
                              'executableSha256': sha256(home / 'portmap-dist/portmap')},
                      'engine': {'version': engine['ServerVersion'], 'id': engine['ID']},
                      'processes': processes, 'listeners': output(['sudo', '-n', 'ss', '-lntp']),
                      'interfaces': json.loads(output(['ip', '-j', 'address', 'show'])),
                      'openssh': {'hostKeys': host_keys(), 'effective': {key: effective.get(key) for key in
                                   ('passwordauthentication', 'allowtcpforwarding', 'permitrootlogin', 'loglevel')},
                                  'packageVersion': output(['dpkg-query', '-W', '-f=${Version}', 'openssh-server']),
                                  'forwardings': forwardings}},
            'statusCheck': check, 'containers': containers, 'images': images,
            'catalog': {'public': catalog(config, '10.0.2.15'), 'private': catalog(config, '127.0.0.1')},
            'project': {'repoName': 'vite-app', 'branch': 'development', 'endpointPreserveHost': bool(endpoint.get('preserve_host', False)),
                        'endpointTomlSha256': sha256(declaration), 'composeSha256': sha256(app / 'compose.yaml'),
                        'url': receipt['projectUrl'], 'commands': receipt['commands']},
            'appMarkers': markers, 'gatewayMarkers': gateway_markers}


if __name__ == '__main__':
    print(json.dumps(collect(), separators=(',', ':')))
