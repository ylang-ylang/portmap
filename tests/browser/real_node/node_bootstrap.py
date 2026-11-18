#!/usr/bin/env python3
"""Fresh guest setup and normal consumer commands; never a Portmap service substitute."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import re
import subprocess
import socket
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request

CONFIG = Path('/etc/portmap-real-node.json')
BOOT_DIAGNOSTIC = Path('/var/tmp/portmap-real-node-boot.json')
OBSERVER_READY = Path('/var/tmp/portmap-real-node-observer-ready')
COMMAND_STEPS = {
    ('portmap', 'up'): 'portmap-up', ('portmap', 'status'): 'status-check',
    ('portmap', 'demo'): 'demo-up', ('portmap', 'init'): 'project-init',
    ('portmap', 'docker-compose'): 'project-up', ('git', 'init'): 'project-git-init',
    ('git', 'config'): 'project-git-config', ('git', 'add'): 'project-git-add',
    ('git', 'commit'): 'project-git-commit',
}
BOOT_STEPS = {'install-cli', 'copy-app', 'install-observer', 'openssh',
              'observer', 'verify-dependencies', 'ready'}
FAILURE_MARKERS = (
    'pull access denied', 'failed to resolve source metadata', 'failed to solve',
    'network is unreachable', 'i/o timeout', 'connection refused', 'no such host',
    'manifest unknown', 'no such image', 'permission denied', 'address already in use',
    'dubious ownership', 'invalid interpolation', 'required variable', 'cannot connect to the docker daemon',
    'failed to load', 'invalid endpoint', 'no configuration file provided',
)


def wait_for_observer(timeout=30):
    """Require live capture readiness before any fresh consumer/browser requests."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if OBSERVER_READY.is_file() and not OBSERVER_READY.is_symlink():
            return
        try:
            # A payload-free guest-loopback handshake flushes tcpdump's first record.
            with socket.create_connection(('127.0.0.1', 80), timeout=0.1):
                pass
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError('Native ingress observer did not establish live packet capture')


def boot_phase(config, stage, failure_type=None):
    record = {'node': config['node'], 'stage': stage}
    if failure_type:
        record['failureType'] = failure_type
    BOOT_DIAGNOSTIC.write_text(json.dumps(record))
    print('PORTMAP_REAL_NODE_BOOT_DIAGNOSTIC ' + json.dumps(record), flush=True)


def diagnostics():
    """Inspect native state and completed logs; never re-run a failed CLI check."""
    config = json.loads(CONFIG.read_text())
    result = {'node': config['node'], 'readyMarker': Path('/var/tmp/portmap-real-node-ready').exists()}
    if BOOT_DIAGNOSTIC.exists():
        boot_record = json.loads(BOOT_DIAGNOSTIC.read_text())
        result['bootStage'] = boot_record.get('stage') if boot_record.get('stage') in BOOT_STEPS else 'unknown'
        failure_type = boot_record.get('failureType', '')
        if re.fullmatch('[A-Za-z][A-Za-z0-9]{0,63}', failure_type):
            result['bootFailureType'] = failure_type
    receipt_path = Path.home() / 'node-setup-receipt.json'
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        stages = set(COMMAND_STEPS.values()) | {'version', 'discovery', 'ready'}
        result['consumerStage'] = receipt.get('stage') if receipt.get('stage') in stages else 'unknown'
        result['versionMatches'] = receipt.get('versionMatches') is True
        result['commands'] = [{'step': COMMAND_STEPS.get(tuple(row.get('argv', [])[:2]), 'unknown'),
                               'exitCode': row.get('exitCode') if isinstance(row.get('exitCode'), int) else None,
                               'elapsedSeconds': row.get('elapsedSeconds') if isinstance(row.get('elapsedSeconds'), (int, float)) else None}
                              for row in receipt.get('commands', [])]
    log = Path.home() / 'consumer-setup-private.log'
    if log.exists():
        text = log.read_text(errors='replace').lower()
        # Explicit static classifiers, not raw command output or token redaction.
        result['failureMarkers'] = [marker for marker in FAILURE_MARKERS if marker in text]
    try:
        inventory = subprocess.run(['docker', 'ps', '-aq', '--no-trunc'], capture_output=True, text=True, timeout=5)
        result['dockerInventoryExitCode'] = inventory.returncode
        if inventory.returncode == 0 and inventory.stdout.split():
            inspection = subprocess.run(['docker', 'inspect', *inventory.stdout.split()], capture_output=True, text=True, timeout=5)
            if inspection.returncode == 0:
                result['containers'] = [{'id': row['Id'], 'name': row['Name'].lstrip('/'),
                                         'image': row['Image'], 'state': row['State']['Status'],
                                         'exitCode': row['State'].get('ExitCode')}
                                        for row in json.loads(inspection.stdout)]
    except subprocess.TimeoutExpired:
        result['dockerDiagnosticTimeout'] = True
    print(json.dumps(result), flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def output(argv):
    return subprocess.check_output(argv, text=True).strip()


def declare_project_endpoint(app):
    declaration = app / '.portmap/endpoints.toml'
    payload = tomllib.loads(declaration.read_text())
    if not payload.get('endpoints'):
        # init installs an empty starter; declare only this real Compose service.
        declaration.write_text('[endpoints.frontend]\nkind = "http"\nservice = "frontend"\ncontainer_port = 5173\n')
        payload = tomllib.loads(declaration.read_text())
    endpoint = payload['endpoints']['frontend']
    if (endpoint.get('preserve_host', False) or endpoint.get('upstream_host') or
            endpoint['kind'] != 'http' or endpoint['service'] != 'frontend' or endpoint['container_port'] != 5173):
        raise RuntimeError('The real default endpoint declaration changed; do not mask a default Host regression')
    return declaration


def boot(inputs):
    if os.geteuid() != 0:
        raise RuntimeError('boot is root only inside the disposable guest')
    config = json.loads(CONFIG.read_text())
    boot_phase(config, 'install-cli')
    account = pwd.getpwnam(config['username'])
    home = Path(account.pw_dir)
    archive = home / 'portmap-runtime.tar.gz'
    shutil.copyfile(inputs / 'portmap-runtime.tar.gz', archive)
    if sha256(archive) != config['cliSha256']:
        raise RuntimeError('Injected tested CLI archive differs from the verified input')
    distribution = home / 'portmap-dist'
    distribution.mkdir()
    with tarfile.open(archive) as package:
        package.extractall(distribution, filter='data')
    binary = distribution / 'portmap'
    if not binary.is_file():
        raise RuntimeError('Tested CLI distribution is missing its real executable')
    Path('/usr/local/bin/portmap').symlink_to(binary)
    boot_phase(config, 'copy-app')
    app = home / 'vite-app'
    shutil.copytree(inputs / 'app', app)
    (app / '.env').write_text('PORTMAP_PROOF_NODE=' + config['node'] + '\nVITE_DEPS_IMAGE=' + config['dependencyImage'] + '\n')
    settings = home / '.config/portmap'
    settings.mkdir(parents=True)
    (settings / 'portmap.toml').write_text(
        '[gateway]\nhttp_bind = "10.0.2.15"\nhttp_port = 80\ndns_domain = "' + config['domain'] +
        '"\n[access]\ningress = "container"\n')
    for directory, subdirs, files in os.walk(home):
        os.chown(directory, account.pw_uid, account.pw_gid)
        for filename in subdirs + files:
            os.chown(Path(directory) / filename, account.pw_uid, account.pw_gid, follow_symlinks=False)
    boot_phase(config, 'install-observer')
    observer = Path('/opt/portmap-real-node')
    observer.mkdir(parents=True)
    for filename in ('node_bootstrap.py', 'guest_evidence.py', 'native_gateway_markers.py'):
        shutil.copyfile(inputs / filename, observer / filename)
    boot_phase(config, 'openssh')
    Path('/etc/ssh/sshd_config.d/00-real-node.conf').write_text(
        'PasswordAuthentication yes\nKbdInteractiveAuthentication no\nAllowTcpForwarding yes\n'
        'PermitRootLogin no\nLogLevel DEBUG\n')
    subprocess.run(['sshd', '-t'], check=True)
    subprocess.run(['systemctl', 'restart', 'ssh.service'], check=True)
    boot_phase(config, 'observer')
    unit = Path('/etc/systemd/system/portmap-real-node-observer.service')
    unit.write_text('[Unit]\nDescription=Read-only native routing proof\nAfter=network.target\n'
                    '[Service]\nExecStart=/usr/bin/python3 /opt/portmap-real-node/native_gateway_markers.py\n'
                    'UMask=0077\nStandardOutput=append:/var/log/portmap-real-node-gateway.jsonl\n'
                    '[Install]\nWantedBy=multi-user.target\n')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    OBSERVER_READY.unlink(missing_ok=True)
    subprocess.run(['systemctl', 'enable', '--now', unit.name], check=True)
    wait_for_observer()
    boot_phase(config, 'verify-dependencies')
    seal = json.loads((inputs / 'golden-seal.json').read_text())
    if output(['docker', 'version', '--format', '{{.Server.Version}}']) != seal['dockerVersion']:
        raise RuntimeError('Fresh overlay changed the pinned Docker version')
    if output(['docker', 'info', '--format', '{{.ID}}']) == seal['builderEngineId']:
        raise RuntimeError('Fresh overlay reused the dependency builder engine identity')
    for image in seal['images']:
        actual = json.loads(output(['docker', 'image', 'inspect', image['reference']]))[0]
        if actual['Id'] != image['id'] or actual['RootFS']['Layers'] != image['rootfsLayers']:
            raise RuntimeError('Fresh overlay changed a pinned dependency image')
    dependency = json.loads(output(['docker', 'image', 'inspect', seal['dependencyImage']]))[0]
    if dependency['Id'] != seal['dependencyImageId'] or dependency['RootFS']['Layers'] != seal['dependencyRootfsLayers']:
        raise RuntimeError('Fresh overlay changed its dependency-only Vite image')
    Path('/var/tmp/portmap-real-node-ready').touch()
    boot_phase(config, 'ready')


def consume():
    config = json.loads(CONFIG.read_text())
    if pwd.getpwuid(os.geteuid()).pw_name != config['username']:
        raise RuntimeError('Consumer setup must run as the fresh ordinary guest account')
    home = Path.home()
    app = home / 'vite-app'
    receipt = {'commands': [], 'node': config['node'], 'stage': 'version'}
    receipt_path = home / 'node-setup-receipt.json'
    receipt_path.write_text(json.dumps(receipt))
    log = home / 'consumer-setup-private.log'
    log.touch(mode=0o600)

    def command(argv, cwd=None):
        receipt['stage'] = COMMAND_STEPS.get(tuple(argv[:2]), 'unknown')
        receipt_path.write_text(json.dumps(receipt))
        started = time.monotonic()
        with log.open('a') as destination:
            result = subprocess.run(argv, cwd=cwd, stdout=destination, stderr=subprocess.STDOUT)
        receipt['commands'].append({'argv': argv, 'exitCode': result.returncode,
                                    'elapsedSeconds': round(time.monotonic() - started, 3)})
        (home / 'node-setup-receipt.json').write_text(json.dumps(receipt))
        if result.returncode:
            raise RuntimeError('Normal consumer command failed: ' + ' '.join(argv))

    version_output = output(['portmap', '--version'])
    receipt['versionMatches'] = version_output == 'portmap ' + config['version']
    receipt_path.write_text(json.dumps(receipt))
    if version_output != 'portmap ' + config['version']:
        raise RuntimeError('Injected CLI version differs from the requested version')
    command(['portmap', 'up'])
    command(['portmap', 'status', '--check'])
    command(['portmap', 'demo', 'up'])
    command(['git', 'init', '-q', '-b', 'development'], app)
    command(['git', 'config', 'user.email', 'real-node-gate@portmap.invalid'], app)
    command(['git', 'config', 'user.name', 'Portmap real node gate'], app)
    command(['git', 'add', '-A'], app)
    command(['git', 'commit', '-qm', 'Genuine Vite development workload'], app)
    command(['portmap', 'init'], app)
    declaration = declare_project_endpoint(app)
    command(['portmap', 'docker-compose', '--', 'up', '-d', '--build'], app)
    command(['portmap', 'status', '--check'])
    receipt['stage'] = 'discovery'
    receipt_path.write_text(json.dumps(receipt))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 90
    while True:
        endpoints = []
        try:
            request = urllib.request.Request('http://10.0.2.15/registry.json', headers={'Host': config['domain']})
            with opener.open(request, timeout=10) as response:
                registry = json.load(response)
            endpoints = [endpoint for service in registry.get('services', [])
                         if service.get('repo_name') == 'vite-app' and service.get('branch') == 'development'
                         and service.get('compose_service') == 'frontend' for endpoint in service.get('endpoints', [])
                         if endpoint.get('name') == 'frontend' and endpoint.get('kind') == 'http']
            if endpoints:
                endpoint = endpoints[0]
                request = urllib.request.Request('http://10.0.2.15/', headers={'Host': endpoint['host']})
                with opener.open(request, timeout=10) as response:
                    page = response.read().decode()
        except (urllib.error.URLError, TimeoutError):
            endpoints = []
        if endpoints:
            if 'Actual Portmap node' not in page or '/src/main.js' not in page:
                raise RuntimeError('Actual discovered project did not return the Vite development page')
            receipt['stage'] = 'ready'
            receipt.update(projectUrl=endpoint['url'], projectHost=endpoint['host'],
                           endpointTomlSha256=sha256(declaration), composeSha256=sha256(app / 'compose.yaml'),
                           version=version_output, cliArchiveSha256=sha256(home / 'portmap-runtime.tar.gz'),
                           cliExecutableSha256=sha256(home / 'portmap-dist/portmap'))
            (home / 'node-setup-receipt.json').write_text(json.dumps(receipt))
            print(json.dumps(receipt), flush=True)
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Actual Portmap agent/catalog never discovered the real Vite project')
        time.sleep(1)


if __name__ == '__main__':
    if sys.argv[1] == 'boot':
        try:
            boot(Path(sys.argv[2]))
        except BaseException as error:
            config = json.loads(CONFIG.read_text())
            previous = json.loads(BOOT_DIAGNOSTIC.read_text()) if BOOT_DIAGNOSTIC.exists() else {}
            boot_phase(config, previous.get('stage', 'install-cli'), type(error).__name__)
            raise
    elif sys.argv[1] == 'consume':
        consume()
    elif sys.argv[1] == 'diagnostics':
        diagnostics()
    else:
        raise SystemExit('Expected boot, consume or diagnostics')
