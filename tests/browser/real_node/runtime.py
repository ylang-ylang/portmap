"""Two real Portmap nodes in disposable KVM guests, with dependency-only immutable caching.

Test nodes run in unprivileged QEMU wrappers and own their guest Docker engines.
Cold toolchain preparation uses temporary capability-limited ordinary containers
on owned egress networks, never a host-network/privileged builder or socket mount.
Tested CLI/app source enters fresh overlays; Portmap state/results never enter cache.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hmac
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

SOURCE_DIR = Path(__file__).resolve().parent
_base_spec = importlib.util.spec_from_file_location(__name__ + '.base_cache', SOURCE_DIR / 'base_cache.py')
if _base_spec is None or _base_spec.loader is None:
    raise RuntimeError('Cannot load the explicit dependency construction module')
base = importlib.util.module_from_spec(_base_spec)
_base_spec.loader.exec_module(base)
SOURCE_FILES = (
    'runtime.py', 'base_cache.py', 'runtime-inputs.json', 'Runtime.Dockerfile', 'base_bootstrap.py',
    'image_builder.py', 'runtime-image.json', 'runtime-setup.sh',
    'node_bootstrap.py', 'guest_evidence.py', 'native_gateway_markers.py',
    'deps/Dockerfile', 'deps/package.json', 'deps/package-lock.json',
    'app/Dockerfile', 'app/compose.yaml', 'app/package.json', 'app/vite.config.js',
    'app/index.html', 'app/src/main.js', 'app/src/style.css',
    'app/public/badge.svg', 'app/public/proof.txt',
)
DOCKER = ('docker', '--host', 'unix:///var/run/docker.sock')


def _source_identities():
    return {'tests/browser/real_node/' + name: base._sha256(SOURCE_DIR / name)
            for name in SOURCE_FILES}


def cache_key() -> str:
    """Return only the immutable dependency construction identity, offline."""
    return base.cache_key()


def _remote_failure(stderr):
    """Keep traceback code locations and exception types, never exception values."""
    exceptions = re.findall(r'(?m)^([A-Za-z][A-Za-z0-9]*(?:Error|Exception)):', stderr)
    frames = re.findall(r'File "/(?:opt/portmap-real-node|mnt/inputs)/(node_bootstrap|guest_evidence)\.py", line (\d+), in ([A-Za-z_<>]+)', stderr)
    return {'exceptionType': exceptions[-1] if exceptions else 'unknown',
            'frames': [{'file': filename + '.py', 'line': int(line), 'function': function}
                       for filename, line, function in frames]}


class Runtime(base.DependencyBase):
    def __init__(self, *, cache_dir: Path, state_dir: Path, out_dir: Path,
                 cli_archive: Path, cli_sha256: str, version: str,
                 source_commit: str, addon_sha256: str) -> None:
        self.cache_dir = base._safe_directory(cache_dir)
        self.state_root = base._safe_directory(state_dir)
        self.out_dir = base._safe_directory(out_dir)
        paths = (self.cache_dir, self.state_root, self.out_dir)
        if any(a.is_relative_to(b) or b.is_relative_to(a)
               for index, a in enumerate(paths) for b in paths[index + 1:]):
            raise base.RuntimeFailure('Cache, private state and public output must be disjoint directories')
        for value, length in ((cli_sha256, 64), (addon_sha256, 64), (source_commit, 40)):
            if not re.fullmatch('[0-9a-f]{' + str(length) + '}', value):
                raise base.RuntimeFailure('Invalid immutable source/archive identity')
        if not re.fullmatch('[0-9][0-9A-Za-z.+-]*', version):
            raise base.RuntimeFailure('Version must be the normalized CLI/addon version')
        self.cli_archive = Path(cli_archive).absolute()
        self.cli_sha256 = cli_sha256
        self.addon_sha256 = addon_sha256
        self.version = version
        self.source_commit = source_commit
        self.run_id = uuid.uuid4().hex
        self.prefix = 'portmap-real-node-' + self.run_id[:12]
        self.state = self.state_root / self.prefix
        self.state.mkdir(mode=0o700)
        self.key = cache_key()
        self.source_files = _source_identities()
        self.spec = base._json(SOURCE_DIR / 'runtime-inputs.json')
        self.entry = self.cache_dir / self.key
        self.wrapper_tag = base.WRAPPER_TAG_PREFIX + self.key
        self.resources = {'containers': [], 'networks': []}
        self.removed = {'containers': [], 'networks': []}
        self.nodes = []
        self.timings = {}
        self.prepared = None
        self.started = None
        self.start_phase = 'not-started'
        self.baseline = None
        self._network_diagnostic_key = os.urandom(32)
        self.stopped = None
        self.docker = DOCKER
        self._checkpoint()

    def _checkpoint(self):
        base._atomic_json(self.state / 'owner.json', {'runId': self.run_id, 'resources': self.resources}, private=True)

    def _run(self, argv, *, timeout=120, check=True, input=None):
        try:
            result = subprocess.run([str(item) for item in argv], input=input,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise base.RuntimeFailure('Runtime command could not complete: ' + str(argv[0])) from error
        if check and result.returncode:
            base._atomic_json(self.state / ('command-failure-' + uuid.uuid4().hex + '.json'),
                         {'argv': [str(item) for item in argv], 'exitCode': result.returncode,
                          'stdout': result.stdout, 'stderr': result.stderr}, private=True)
            raise base.RuntimeFailure('Runtime command failed (exit ' + str(result.returncode) + '): ' +
                                 ' '.join(str(item) for item in argv[:3]))
        return result

    def _docker(self, argv, **kwargs):
        return self._run([*self.docker, *argv], **kwargs)

    def _prerequisites(self):
        if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64') or os.getuid() == 0:
            raise base.RuntimeFailure('Run as an unprivileged Linux amd64 operator with an existing local Docker engine')
        for executable in ('docker', 'ip', 'ssh', 'ssh-keygen', 'openssl'):
            if not shutil.which(executable):
                raise base.RuntimeFailure('Missing host prerequisite: ' + executable)
        if not Path('/dev/kvm').exists() or not stat.S_ISCHR(Path('/dev/kvm').stat().st_mode):
            raise base.RuntimeFailure('An existing /dev/kvm is required; do not relax host permissions for this gate')
        environment_host = os.environ.get('DOCKER_HOST')
        if environment_host and not environment_host.startswith('unix:///'):
            raise base.RuntimeFailure('Remote DOCKER_HOST is forbidden; select an existing local Unix engine')
        context = os.environ.get('DOCKER_CONTEXT') or self._run(['docker', 'context', 'show']).stdout.strip()
        description = json.loads(self._run(['docker', 'context', 'inspect', context]).stdout)[0]
        context_host = description.get('Endpoints', {}).get('docker', {}).get('Host', '')
        if not context_host.startswith('unix:///'):
            raise base.RuntimeFailure('Remote Docker contexts are forbidden; no fixture resources were created')
        endpoint = context_host if os.environ.get('DOCKER_CONTEXT') else environment_host or context_host
        socket_path = Path(endpoint[len('unix://'):])
        if not socket_path.is_absolute() or not socket_path.exists() or not stat.S_ISSOCK(socket_path.stat().st_mode):
            raise base.RuntimeFailure('The selected local Docker Unix socket is unavailable')
        self.docker = ('docker', '--host', endpoint)
        self._docker(['info', '--format', '{{.ServerVersion}}'])
        if self.baseline is None:
            self.baseline = self._host_snapshot()
            base._atomic_json(self.state / 'host-baseline-private.json', self.baseline, private=True)

    def _host_snapshot(self):
        files = {}
        protected = [Path('/etc/resolv.conf'), Path('/etc/hosts'), Path('/etc/nsswitch.conf'),
                     Path('/etc/subuid'), Path('/etc/subgid'), Path('/etc/systemd/resolved.conf')]
        for directory in (Path('/etc/systemd/resolved.conf.d'), Path.home() / '.config/portmap', Path.home() / '.ssh'):
            if directory.exists():
                protected.extend(path for path in directory.rglob('*') if path.is_file())
        roots = {Path.home() / '.local/state/portmap',
                 Path(os.environ.get('XDG_STATE_HOME', '~/.local/state')).expanduser() / 'portmap'}
        for variable in ('PORTMAP_STATE_DIR', 'PORTMAP_CLIENT_STATE_DIR'):
            if os.environ.get(variable):
                roots.add(Path(os.environ[variable]).expanduser())
        for state in roots:
            for directory in (state, state / 'client'):
                if directory.is_dir():
                    protected.extend(path for path in directory.iterdir() if path.is_file())
            for directory in (state / 'profiles', state / 'client/profiles'):
                if directory.is_dir():
                    protected.extend(path for path in directory.rglob('*') if path.is_file())
        for path in sorted(set(protected)):
            if path.is_file():
                files[str(path)] = {'sha256': base._sha256(path), 'mode': stat.S_IMODE(path.stat().st_mode),
                                    'link': os.readlink(path) if path.is_symlink() else None}
        routes = []
        for family in ('-4', '-6'):
            for row in json.loads(self._run(['ip', family, '-j', 'route', 'show', 'table', 'all']).stdout):
                routes.append({'family': family, **{key: value for key, value in row.items()
                                                   if key not in ('expires', 'cache', 'lastuse')}})
        addresses = json.loads(self._run(['ip', '-j', 'address', 'show']).stdout)
        interfaces = [{'name': row['ifname'], 'address': row.get('address'),
                       'addresses': sorted((entry.get('family'), entry.get('local'), entry.get('prefixlen'))
                                           for entry in row.get('addr_info', []))}
                      for row in addresses]
        container_ids = self._docker(['ps', '-aq', '--no-trunc']).stdout.split()
        containers = {}
        if container_ids:
            for item in json.loads(self._docker(['inspect', *container_ids]).stdout):
                containers[item['Id']] = {'image': item['Image'], 'running': item['State']['Running']}
        resolver = self._run(['resolvectl', 'status', '--no-pager'], check=False).stdout if shutil.which('resolvectl') else None
        sysctl = {name: Path('/proc/sys/' + name.replace('.', '/')).read_text().strip()
                  for name in ('net.ipv4.ip_forward', 'net.ipv4.conf.all.route_localnet', 'net.ipv4.ip_unprivileged_port_start')}
        tailscale = None
        if shutil.which('tailscale'):
            result = self._run(['tailscale', 'status', '--json'], check=False)
            if result.returncode == 0:
                value = json.loads(result.stdout)
                peer = value.get('Self') or {}
                tailscale = {'BackendState': value.get('BackendState'), 'TailscaleIPs': value.get('TailscaleIPs'),
                             'CurrentTailnet': value.get('CurrentTailnet'),
                             'Self': {key: peer.get(key) for key in ('ID', 'PublicKey', 'TailscaleIPs', 'AllowedIPs', 'PrimaryRoutes', 'ExitNode', 'ExitNodeOption')}}
            else:
                tailscale = {'exitCode': result.returncode}
        return {'files': files, 'routes': sorted(routes, key=lambda value: json.dumps(value, sort_keys=True)),
                'interfaces': sorted(interfaces, key=lambda value: value['name']), 'containers': containers,
                'resolver': resolver, 'sysctl': sysctl, 'tailscale': tailscale}




    def _validate_entry(self):
        if (self.entry.is_symlink() or not self.entry.is_dir() or
                self.entry.stat().st_uid != os.getuid() or self.entry.stat().st_mode & 0o022):
            raise base.RuntimeFailure('Dependency cache entry must be an operator-owned protected directory')
        manifest_path = self.entry / 'manifest.json'
        if (manifest_path.is_symlink() or not manifest_path.is_file() or
                manifest_path.stat().st_uid != os.getuid() or manifest_path.stat().st_mode & 0o222):
            raise base.RuntimeFailure('Dependency manifest must be immutable and operator-owned')
        manifest = base._json(self.entry / 'manifest.json')
        expected_names = {'os.qcow2', 'wrapper-image.tar', 'dependencies.qcow2',
                          *('image-' + image['name'] + '.tar' for image in self.spec['images'])}
        if (manifest.get('schemaVersion') != 2 or manifest.get('cacheKey') != self.key or
                manifest.get('constructionFiles') != base.construction_files()):
            raise base.RuntimeFailure('Dependency cache source identity mismatch')
        if {row['name'] for row in manifest.get('artifacts', [])} != expected_names:
            raise base.RuntimeFailure('Dependency cache has missing or unexpected artifacts')
        if {path.name for path in self.entry.iterdir()} != expected_names | {'manifest.json'}:
            raise base.RuntimeFailure('Dependency cache contains unexpected files or mutable state')
        for artifact in manifest['artifacts']:
            path = self.entry / artifact['name']
            if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or
                    path.stat().st_mode & 0o222):
                raise base.RuntimeFailure('Dependency artifacts must be immutable operator-owned ordinary files')
            if path.stat().st_size != artifact['bytes'] or base._sha256(path) != artifact['sha256']:
                raise base.RuntimeFailure('Dependency cache checksum mismatch: ' + artifact['name'])
        if next(row['sha256'] for row in manifest['artifacts'] if row['name'] == 'os.qcow2') != self.spec['os']['sha256']:
            raise base.RuntimeFailure('Dependency cache OS image differs from the pinned source')
        return manifest

    def _load_wrapper(self, manifest):
        result = self._docker(['image', 'inspect', self.wrapper_tag], check=False)
        if result.returncode:
            self._docker(['load', '--input', str(self.entry / 'wrapper-image.tar')], timeout=600)
            result = self._docker(['image', 'inspect', self.wrapper_tag])
        actual = json.loads(result.stdout)[0]
        if actual['RootFS']['Layers'] != manifest['wrapper']['rootfsLayers']:
            raise base.RuntimeFailure('Loaded QEMU toolchain differs from the immutable cache')
        self.wrapper_id = actual['Id']

    def prepare(self) -> dict:
        if self.prepared is not None:
            return self.prepared
        began = time.monotonic()
        if _source_identities() != self.source_files:
            raise base.RuntimeFailure('Harness source changed before dependency preparation')
        self._prerequisites()
        locks = base._safe_directory(self.cache_dir / 'locks')
        lock_path = locks / (self.key + '.lock')
        if lock_path.is_symlink():
            raise base.RuntimeFailure('Symlinked dependency cache locks are not accepted')
        if lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_uid != os.getuid() or lock_path.stat().st_mode & 0o022):
            raise base.RuntimeFailure('Dependency locks must be protected operator-owned ordinary files')
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            hit = self.entry.exists()
            if hit:
                if _source_identities() != self.source_files:
                    raise base.RuntimeFailure('Harness source changed before dependency cache reuse')
                manifest = self._validate_entry()
            else:
                temporary = Path(tempfile.mkdtemp(prefix='.' + self.key + '-', dir=self.cache_dir))
                try:
                    seal, wrapper = self._cache_build(temporary)
                    artifacts = [{'name': path.name, 'sha256': base._sha256(path), 'bytes': path.stat().st_size}
                                 for path in sorted(temporary.iterdir())]
                    if _source_identities() != self.source_files:
                        raise base.RuntimeFailure('Harness source changed during dependency construction')
                    manifest = {'schemaVersion': 2, 'cacheKey': self.key,
                                'constructionFiles': base.construction_files(),
                                'artifacts': artifacts, 'golden': seal, 'wrapper': wrapper,
                                'sources': {**self.spec, 'constructionFiles': base.construction_files()}}
                    base._atomic_json(temporary / 'manifest.json', manifest)
                    for path in temporary.iterdir():
                        path.chmod(0o444)
                    temporary.chmod(0o555)
                    os.replace(temporary, self.entry)
                finally:
                    if temporary.exists():
                        temporary.chmod(0o700)
                        shutil.rmtree(temporary)
            self._load_wrapper(manifest)
        self.cache_manifest = manifest
        self.prepared = {'cache_key': self.key, 'cache_hit': hit,
                         'manifest_sha256': base._sha256(self.entry / 'manifest.json'),
                         'artifacts': manifest['artifacts'],
                         'sources': {**manifest['sources'], 'sourceFiles': self.source_files},
                         'golden': manifest['golden'], 'elapsed_seconds': round(time.monotonic() - began, 3)}
        self.timings['prepare'] = self.prepared['elapsed_seconds']
        return self.prepared

    def _ssh(self, node, command, *, timeout=120, check=True):
        return self._run(['ssh', '-F', '/dev/null', '-i', self.state / 'infra_ed25519',
                          '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                          '-o', 'StrictHostKeyChecking=accept-new',
                          '-o', 'UserKnownHostsFile=' + str(self.state / 'known_hosts'),
                          '-o', 'ConnectTimeout=3', node['username'] + '@' + node['address'], command],
                         timeout=timeout, check=check)

    def _startup_node_diagnostic(self, node):
        record = {'node': node['node'], 'mode': node['mode'], 'phase': node.get('phase', 'launch'),
                  'outerContainerId': node['resource']['id']}
        if 'consumerExitCode' in node:
            record['consumerExitCode'] = node['consumerExitCode']
        if 'guestFailure' in node:
            record['guestFailure'] = node['guestFailure']
        try:
            diagnostic = self._ssh(node, 'python3 /mnt/inputs/node_bootstrap.py diagnostics', timeout=20, check=False)
            record['diagnosticSshExitCode'] = diagnostic.returncode
            if diagnostic.returncode == 0:
                guest = json.loads(diagnostic.stdout)
                record['guest'] = {key: guest[key] for key in (
                    'node', 'readyMarker', 'bootStage', 'bootFailureType', 'consumerStage',
                    'versionMatches', 'commands', 'failureMarkers', 'dockerInventoryExitCode',
                    'dockerDiagnosticTimeout', 'containers')
                    if key in guest}
            else:
                record['diagnosticFailure'] = _remote_failure(diagnostic.stderr)
        except Exception as error:
            record['diagnosticErrorType'] = type(error).__name__
        try:
            logs = self._docker(['logs', node['resource']['id']], timeout=20, check=False)
            for line in (logs.stdout + logs.stderr).splitlines():
                marker = 'PORTMAP_REAL_NODE_BOOT_DIAGNOSTIC '
                if marker in line:
                    try:
                        candidate = json.loads(line.partition(marker)[2])
                    except json.JSONDecodeError:
                        continue
                    record['serialBoot'] = {key: candidate[key] for key in ('node', 'stage', 'failureType') if key in candidate}
        except Exception as error:
            record['serialDiagnosticErrorType'] = type(error).__name__
        path = self.out_dir / ('startup-' + node['mode'] + '.json')
        base._atomic_json(path, record)
        return str(path)

    def start(self) -> dict:
        began = time.monotonic()
        try:
            return self._start()
        except Exception as error:
            receipt = {'schemaVersion': 1, 'stage': self.start_phase, 'errorType': type(error).__name__,
                       'cacheKey': self.key, 'cacheHit': self.prepared['cache_hit'] if self.prepared else None,
                       'elapsedSeconds': round(time.monotonic() - began, 3), 'nodes': []}
            for node in self.nodes:
                try:
                    receipt['nodes'].append(self._startup_node_diagnostic(node))
                except Exception as diagnostic_error:
                    receipt['nodes'].append({'node': node['node'], 'diagnosticErrorType': type(diagnostic_error).__name__})
            try:
                base._atomic_json(self.out_dir / 'startup-failure.json', receipt)
            except Exception as diagnostic_error:
                error.add_note('Public startup diagnostics could not be persisted: ' + type(diagnostic_error).__name__)
            raise

    def _provision(self, node):
        began = time.monotonic()
        node['phase'] = 'cloud-init'
        deadline = began + 300
        while True:
            result = self._ssh(node, 'test -e /var/tmp/portmap-real-node-ready && cloud-init status --wait',
                               timeout=30, check=False)
            if result.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise base.RuntimeFailure('Fresh actual guest did not complete cloud-init: ' + node['mode'])
            time.sleep(2)
        boot_seconds = time.monotonic() - began
        node['phase'] = 'consumer'
        result = self._ssh(node, 'python3 /opt/portmap-real-node/node_bootstrap.py consume', timeout=600, check=False)
        node['consumerExitCode'] = result.returncode
        if result.returncode:
            node['guestFailure'] = _remote_failure(result.stderr)
            raise base.RuntimeFailure('Actual normal consumer setup failed for ' + node['mode'] +
                                      ' (exit ' + str(result.returncode) + '); see startup-' + node['mode'] + '.json')
        receipt = json.loads(result.stdout)
        node['phase'] = 'evidence'
        result = self._ssh(node, 'python3 /opt/portmap-real-node/guest_evidence.py', timeout=120, check=False)
        if result.returncode:
            node['guestFailure'] = _remote_failure(result.stderr)
            raise base.RuntimeFailure('Actual guest evidence failed for ' + node['mode'] +
                                      ' (exit ' + str(result.returncode) + '); see startup-' + node['mode'] + '.json')
        evidence = json.loads(result.stdout)
        check = evidence['statusCheck']
        if (check['exitCode'] != 0 or not all(check.get(key) is True for key in
                ('gatewayRunning', 'routesLoaded', 'dnsRunning', 'agentRunning')) or
                check['hostDnsEnabled'] is not False or check['resolverInstalled'] is not False):
            raise base.RuntimeFailure('Actual published CLI did not reach its server-only ready contract')
        for scope, management in (('public', False), ('private', True)):
            catalog = evidence['catalog'][scope]
            if (catalog['registryStatus'] != 200 or catalog['machinesStatus'] != 200 or
                    catalog['canManage'] is not management or catalog['connectionsStatus'] != (200 if management else 404)):
                raise base.RuntimeFailure('Actual public/private Portmap catalog boundary is not ready')
        node['phase'] = 'ready'
        return receipt, evidence, {'boot': round(boot_seconds, 3),
                                   'consumer_setup': round(time.monotonic() - began - boot_seconds, 3)}

    def _app_contract(self, name):
        return {'readyExpression': "window.realPortmapProof?.ready === true && window.realPortmapProof?.node === " + json.dumps(name),
                'cssSelector': '#asset-proof', 'cssProperty': 'color', 'cssExpected': 'rgb(1, 2, 3)',
                'buttonSelector': '#counter', 'imageSelector': '#badge', 'nodeSelector': '#node-identity',
                'resources': [
                    {'path': '/proof.txt', 'kind': 'static', 'sha256': base._sha256(self.state / 'inputs/app/public/proof.txt'), 'contentType': 'text/plain'},
                    {'path': '/badge.svg', 'kind': 'image', 'sha256': base._sha256(self.state / 'inputs/app/public/badge.svg'), 'contentType': 'image/svg+xml'},
                    {'path': '/src/main.js', 'kind': 'js', 'contains': 'portmap-real-vite-js-8.0.16', 'contentType': 'javascript'},
                    {'path': '/src/style.css', 'kind': 'css', 'contains': 'portmap-real-vite-css-8.0.16', 'contentType': 'javascript'}]}

    def _start(self) -> dict:
        if self.started is not None:
            return self.started
        self.start_phase = 'prepare'
        self.prepare()
        if _source_identities() != self.source_files:
            raise base.RuntimeFailure('Harness source changed after immutable dependency preparation')
        self.start_phase = 'verify-cli'
        began = time.monotonic()
        if self.cli_archive.is_symlink() or not self.cli_archive.is_file() or base._sha256(self.cli_archive) != self.cli_sha256:
            raise base.RuntimeFailure('Tested CLI archive checksum mismatch before injection')
        self.start_phase = 'inject-inputs'
        inputs = self.state / 'inputs'
        inputs.mkdir()
        for filename in SOURCE_FILES:
            if filename.startswith('app/') or filename in ('node_bootstrap.py', 'guest_evidence.py', 'native_gateway_markers.py'):
                base._copy_source(filename, inputs / filename)
        shutil.copyfile(self.cli_archive, inputs / 'portmap-runtime.tar.gz')
        base._atomic_json(inputs / 'golden-seal.json', self.cache_manifest['golden'])
        self.start_phase = 'generate-management-key'
        self._run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', self.state / 'infra_ed25519'])
        public_key = (self.state / 'infra_ed25519.pub').read_text().strip()
        self.start_phase = 'create-internal-network'
        network = self._new_network('internal', internal=True)
        subnet = ipaddress.ip_network(network['subnet'])
        passwords = {}
        self.start_phase = 'launch-nodes'
        for offset, mode in enumerate(('direct', 'ssh'), start=11):
            name = 'pm-' + mode + '-' + self.run_id[:8]
            username = 'pm' + self.run_id[:8] + mode[0]
            password = uuid.uuid4().hex + uuid.uuid4().hex
            password_hash = self._run(['openssl', 'passwd', '-6', '-stdin'], input=password + '\n').stdout.strip()
            config = {'node': name, 'mode': mode, 'domain': name + '.portmap', 'username': username,
                      'cliSha256': self.cli_sha256, 'version': self.version, 'sourceCommit': self.source_commit,
                      'dependencyImage': self.cache_manifest['golden']['dependencyImage']}
            work = self.state / mode
            work.mkdir(mode=0o700)
            cloud = {'users': [{'name': username, 'uid': 1000, 'groups': ['sudo', 'docker'],
                                'shell': '/bin/bash', 'sudo': 'ALL=(ALL) NOPASSWD:ALL',
                                'lock_passwd': False, 'passwd': password_hash, 'ssh_authorized_keys': [public_key]}],
                     'disable_root': True, 'ssh_pwauth': True, 'ssh_deletekeys': True,
                     'ssh_genkeytypes': ['rsa', 'ecdsa', 'ed25519'], 'ssh_publish_hostkeys': {'enabled': False},
                     'package_update': False, 'package_upgrade': False,
                     'write_files': [{'path': '/etc/portmap-real-node.json', 'permissions': '0644', 'content': json.dumps(config)}],
                     'runcmd': [['mkdir', '-p', '/mnt/inputs'],
                                ['mount', '-t', '9p', '-o', 'trans=virtio,version=9p2000.L,ro', 'portmapinputs', '/mnt/inputs'],
                                ['python3', '/mnt/inputs/node_bootstrap.py', 'boot', '/mnt/inputs']]}
            self._seed(work / 'seed', cloud, name)
            self._tool('/usr/bin/qemu-img', ['create', '-f', 'qcow2', '-F', 'qcow2', '-b', '/base/dependencies.qcow2',
                                             '/work/disk.qcow2', '16G'], [(self.entry, '/base', True), (work, '/work', False)])
            node = {**config, 'address': str(subnet.network_address + offset), 'work': work}
            node['resource'] = self._launch(self.prefix + '-' + mode, network, node['address'], work, inputs, self.entry)
            self.nodes.append(node)
            passwords[mode] = password
        cases = []
        self.start_phase = 'provision-nodes'
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self._provision, node) for node in self.nodes]
            for node, future in zip(self.nodes, futures):
                receipt, evidence, timing = future.result()
                node['setup'] = receipt
                node['initialEvidence'] = evidence
                self.timings[node['mode']] = timing
                keys = evidence['guest']['openssh']['hostKeys']
                fingerprint = next((key['sha256Hex'] for key in keys if key['algorithm'] == 'ecdsa-sha2-nistp256'), None)
                if not fingerprint:
                    raise base.RuntimeFailure('Ordinary guest OpenSSH did not provide its independently derived ECDSA key')
                cases.append({'mode': node['mode'], 'domain': node['domain'],
                              'catalogUrl': 'http://' + node['domain'] + '/', 'projectUrl': receipt['projectUrl'],
                              'projectName': 'vite-app', 'expectedNode': node['node'], 'host': node['address'],
                              'httpPort': 80, 'sshPort': 22, 'sshUsername': node['username'],
                              'hostKeySha256Hex': fingerprint, 'hostKeys': keys,
                              'expectedBackendHost': '127.0.0.1:5173', 'expectedForwardedHost': receipt['projectHost'],
                              'endpointPreserveHost': False, 'appContract': self._app_contract(node['node'])})
        self.start_phase = 'publish-manifest'
        self.timings['start'] = round(time.monotonic() - began, 3)
        manifest = {'schemaVersion': 1, 'runId': self.run_id, 'cacheKey': self.key,
                    'ready': True, 'frozen': True, 'frozenAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    'network': network['name'], 'publishedVersion': self.version, 'sourceCommit': self.source_commit,
                    'publishedCliSha256': self.cli_sha256, 'addonSha256': self.addon_sha256, 'cases': cases,
                    'transport': 'Unprivileged QEMU user-network transparent TCP forwarding: internal wrapper LAN:80/22 to independent guest 10.0.2.15:80/22; no host published ports'}
        public = self.out_dir / 'public-manifest.json'
        private = self.state / 'private-manifest.json'
        base._atomic_json(public, manifest)
        base._atomic_json(private, {platform_name: {'ssh': {'password': passwords['ssh']}}
                               for platform_name in ('desktop', 'android')}, private=True)
        self.started = {'public_manifest': str(public), 'private_manifest': str(private),
                        'network': network['name'], 'timings': self.timings, 'cache_hit': self.prepared['cache_hit']}
        self.start_phase = 'ready'
        return self.started

    def _outer_evidence(self, node):
        item = json.loads(self._docker(['inspect', node['resource']['id']]).stdout)[0]
        host = item['HostConfig']
        return {'id': item['Id'], 'name': item['Name'].lstrip('/'), 'image': item['Image'],
                'user': item['Config']['User'], 'privileged': host['Privileged'],
                'networkMode': host['NetworkMode'], 'capDrop': host.get('CapDrop') or [],
                'securityOpt': host.get('SecurityOpt') or [],
                'devices': [device['PathOnHost'] for device in host.get('Devices') or []],
                'mountTargets': sorted(mount['Destination'] for mount in item['Mounts']),
                'portBindings': host.get('PortBindings') or {}}

    def collect(self) -> dict:
        if self.started is None:
            raise base.RuntimeFailure('Actual nodes must be ready before collecting public evidence')
        began = time.monotonic()
        nodes = []
        artifacts = []
        for node in self.nodes:
            evidence = json.loads(self._ssh(node, 'python3 /opt/portmap-real-node/guest_evidence.py', timeout=120).stdout)
            evidence['physicalHost'] = node['address']
            evidence['outerContainer'] = self._outer_evidence(node)
            path = self.out_dir / ('node-' + node['mode'] + '.json')
            base._atomic_json(path, evidence)
            artifacts.append({'path': path.name, 'sha256': base._sha256(path), 'bytes': path.stat().st_size})
            nodes.append(evidence)
        self.timings['collect'] = round(time.monotonic() - began, 3)
        return {'schemaVersion': 1, 'runId': self.run_id, 'cacheKey': self.key,
                'publishedVersion': self.version, 'sourceCommit': self.source_commit,
                'publishedCliSha256': self.cli_sha256, 'addonSha256': self.addon_sha256,
                'publicManifest': 'public-manifest.json', 'nodes': nodes, 'artifacts': artifacts,
                'timings': self.timings}

    def _network_diff(self, before, after):
        """Bounded structural diagnostics; never change the baseline comparison."""
        limit, field_limit, address_limit = 16, 24, 8
        fields = {'family', 'dst', 'src', 'prefsrc', 'gateway', 'via', 'dev', 'iif', 'oif',
                  'table', 'protocol', 'scope', 'type', 'metric', 'flags', 'pref', 'tos',
                  'uid', 'nhid', 'multipath', 'nexthops', 'encap', 'mtu', 'hoplimit'}
        enums = {
            'family': {'-4', '-6', 'inet', 'inet6'},
            'table': {'main', 'local', 'default', 'unspec'},
            'protocol': {'kernel', 'boot', 'static', 'ra', 'dhcp', 'redirect', 'bird', 'babel',
                         'zebra', 'bgp', 'ospf', 'rip', 'eigrp', 'isis', 'pim', 'bpf'},
            'scope': {'global', 'link', 'host', 'site', 'nowhere'},
            'type': {'unicast', 'local', 'broadcast', 'multicast', 'throw', 'unreachable',
                     'blackhole', 'prohibit', 'nat', 'anycast'},
            'pref': {'high', 'medium', 'low'},
        }
        flags = {'onlink', 'linkdown', 'pervasive', 'dead', 'offload', 'trap', 'notify',
                 'cloned', 'rt_offload', 'rt_trap', 'rt_offload_failed'}
        numbers = {'table', 'protocol', 'metric', 'tos', 'uid', 'nhid', 'mtu', 'hoplimit', 'prefixlen'}

        def encoded(value):
            return json.dumps(value, sort_keys=True, separators=(',', ':'))

        def digest(value):
            return hmac.new(self._network_diagnostic_key, encoded(value).encode(), 'sha256').hexdigest()

        def public_name(name):
            return name if name in fields else 'field-hmac:' + digest(name)

        def public_value(field, value):
            if value is None or type(value) is bool:
                return value
            if field in {'name', 'dev', 'iif', 'oif'} and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:@-]{1,32}', value):
                return value
            if field in enums and isinstance(value, str) and value in enums[field]:
                return value
            if field in numbers and type(value) is int and 0 <= value <= 2 ** 32:
                return value
            if field == 'flags' and isinstance(value, list):
                return bounded(value, lambda flag: flag if isinstance(flag, str) and flag in flags else {'hmacSha256': digest(flag)})
            if field == 'dst' and value == 'default':
                return value
            result = {'hmacSha256': digest(value)}
            if field == 'dst' and isinstance(value, str):
                try:
                    network = ipaddress.ip_network(value, strict=False)
                    result.update(version=network.version, prefixLength=network.prefixlen)
                except ValueError:
                    pass
            return result

        def bounded(rows, render, maximum=limit):
            return {'count': len(rows), 'items': [render(row) for row in rows[:maximum]],
                    'omitted': max(0, len(rows) - maximum)}

        def delta(old, new):
            old_counts, new_counts = Counter(map(encoded, old)), Counter(map(encoded, new))
            values = {encoded(row): row for row in [*old, *new]}
            removed = [values[key] for key, count in sorted((old_counts - new_counts).items()) for _ in range(count)]
            added = [values[key] for key, count in sorted((new_counts - old_counts).items()) for _ in range(count)]
            return removed, added

        def public_address(row):
            return {'family': public_value('family', row[0]), 'local': public_value('local', row[1]),
                    'prefixlen': public_value('prefixlen', row[2])}

        def public_interface(row):
            return {'name': public_value('name', row['name']), 'address': public_value('address', row.get('address')),
                    'addresses': bounded(row.get('addresses', []), public_address, address_limit)}

        def changed_interface(pair):
            old, new = pair
            result = {'name': public_value('name', old['name']),
                      'changedFields': [key for key in ('address', 'addresses') if old.get(key) != new.get(key)]}
            if old.get('address') != new.get('address'):
                result['address'] = {'before': public_value('address', old.get('address')),
                                     'after': public_value('address', new.get('address'))}
            if old.get('addresses') != new.get('addresses'):
                removed, added = delta(old.get('addresses', []), new.get('addresses', []))
                result['addresses'] = {'added': bounded(added, public_address, address_limit),
                                       'removed': bounded(removed, public_address, address_limit)}
            return result

        def public_route(row):
            keys = sorted(row, key=lambda key: (key not in fields, key))
            return {'fields': {public_name(key): public_value(key, row[key]) for key in keys[:field_limit]},
                    'omittedFields': max(0, len(keys) - field_limit)}

        def changed_route(pair):
            old, new = pair
            changed = [key for key in sorted(old.keys() | new.keys(), key=lambda key: (key not in fields, key))
                       if (key in old) != (key in new) or old.get(key) != new.get(key)]
            return {'changedFields': [public_name(key) for key in changed[:field_limit]],
                    'omittedChangedFields': max(0, len(changed) - field_limit),
                    'before': public_route(old), 'after': public_route(new)}

        old_interfaces = {row['name']: row for row in before['interfaces']}
        new_interfaces = {row['name']: row for row in after['interfaces']}
        removed_routes, added_routes = delta(before['routes'], after['routes'])
        groups = {}
        for side, rows in (('before', removed_routes), ('after', added_routes)):
            for row in rows:
                identity = encoded({key: row.get(key) for key in ('family', 'table', 'dst', 'type', 'tos')})
                groups.setdefault(identity, {'before': [], 'after': []})[side].append(row)
        changed_routes, removed_routes, added_routes = [], [], []
        for _, group in sorted(groups.items()):
            if len(group['before']) == len(group['after']) == 1:
                changed_routes.append((group['before'][0], group['after'][0]))
            else:
                removed_routes.extend(group['before'])
                added_routes.extend(group['after'])
        return {
            'schemaVersion': 1,
            'valueEncoding': 'Sensitive values and unknown field names use per-run HMAC-SHA256; the key is not exported.',
            'limits': {'recordsPerCategory': limit, 'fieldsPerRoute': field_limit, 'addressesPerInterface': address_limit},
            'routeMatching': 'Only unambiguous family/table/destination/type/tos pairs are changed; other differences remain added/removed.',
            'interfaces': {
                'beforeCount': len(old_interfaces), 'afterCount': len(new_interfaces),
                'added': bounded([new_interfaces[name] for name in sorted(new_interfaces.keys() - old_interfaces.keys())], public_interface),
                'removed': bounded([old_interfaces[name] for name in sorted(old_interfaces.keys() - new_interfaces.keys())], public_interface),
                'changed': bounded([(old_interfaces[name], new_interfaces[name]) for name in sorted(old_interfaces.keys() & new_interfaces.keys())
                                    if old_interfaces[name] != new_interfaces[name]], changed_interface),
            },
            'routes': {'beforeCount': len(before['routes']), 'afterCount': len(after['routes']),
                       'added': bounded(added_routes, public_route), 'removed': bounded(removed_routes, public_route),
                       'changed': bounded(changed_routes, changed_route)},
        }


    def stop(self) -> dict:
        if self.stopped is not None:
            return self.stopped
        began = time.monotonic()
        errors = []
        # Include a resource whose create command was interrupted after the daemon
        # accepted it. Discovery is restricted to our unpredictable ownership label.
        if self.baseline is not None:
            try:
                self._discover_owned()
            except (base.RuntimeFailure, OSError, ValueError) as error:
                errors.append(str(error))
        for resource in list(self.resources['containers']):
            try:
                self._remove_container(resource)
            except (base.RuntimeFailure, OSError, ValueError) as error:
                errors.append(str(error))
        for resource in list(self.resources['networks']):
            try:
                self._remove_network(resource)
            except (base.RuntimeFailure, OSError, ValueError) as error:
                errors.append(str(error))
        baseline = {'unchanged': True, 'checks': {'noHostResourcesCreated': True}}
        if self.baseline is not None:
            try:
                after = self._host_snapshot()
                checks = {key: self.baseline[key] == after[key] for key in self.baseline}
                baseline = {'unchanged': all(checks.values()), 'checks': checks}
                baseline['defaultBridge'] = {}
                for phase, snapshot in (('before', self.baseline), ('after', after)):
                    bridge = next((item for item in snapshot['interfaces'] if item['name'] == 'docker0'), None)
                    baseline['defaultBridge'][phase] = {
                        'present': bridge is not None,
                        'ipv6AddressCount': sum(address[0] == 'inet6' for address in bridge['addresses'])
                        if bridge is not None else 0,
                    }
                if not baseline['unchanged']:
                    errors.append('Host baseline changed: ' + ', '.join(key for key, unchanged in checks.items() if not unchanged))
                    if not checks.get('routes', True) or not checks.get('interfaces', True):
                        try:
                            baseline['networkDiff'] = self._network_diff(self.baseline, after)
                        except Exception as diagnostic_error:
                            baseline['networkDiffErrorType'] = type(diagnostic_error).__name__
            except (base.RuntimeFailure, OSError, ValueError) as error:
                baseline = {'unchanged': False, 'checks': {}}
                errors.append(str(error))
        private_removed = False
        try:
            marker = base._json(self.state / 'owner.json')
            if marker['runId'] != self.run_id or self.state.is_symlink():
                raise base.RuntimeFailure('Refusing to remove private state owned by a different run')
            shutil.rmtree(self.state)
            private_removed = not self.state.exists()
        except (base.RuntimeFailure, OSError, ValueError) as error:
            errors.append(str(error))
        receipt = {'status': 'failed' if errors else 'passed', 'runId': self.run_id,
                   'containersRemoved': self.removed['containers'], 'networksRemoved': self.removed['networks'],
                   'privateStateRemoved': private_removed, 'hostBaseline': baseline, 'errors': errors,
                   'remainingResources': self.resources,
                   'retainedDependencyCache': True, 'elapsedSeconds': round(time.monotonic() - began, 3)}
        base._atomic_json(self.out_dir / 'cleanup.json', receipt)
        self.stopped = receipt
        return receipt
