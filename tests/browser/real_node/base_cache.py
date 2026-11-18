"""Dependency-only base construction. Every construction command/input is cache-keyed here.

Runtime, app and tested Portmap binaries are per-run inputs, never golden-base
contents. This module also owns the shared unprivileged QEMU launch primitives.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import urllib.request
import uuid

SOURCE_DIR = Path(__file__).resolve().parent
CONSTRUCTION_FILES = (
    'base_cache.py', 'runtime-inputs.json', 'Runtime.Dockerfile', 'base_bootstrap.py',
    'deps/Dockerfile', 'deps/package.json', 'deps/package-lock.json',
)
LABEL = 'portmap.real-node.run'
WRAPPER_TAG_PREFIX = 'portmap-real-node-runtime:cache-'


def construction_files() -> dict:
    return {'tests/browser/real_node/' + name: _sha256(SOURCE_DIR / name)
            for name in CONSTRUCTION_FILES}


def cache_key() -> str:
    material = {'schema': 2, 'constructionFiles': construction_files()}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class RuntimeFailure(RuntimeError):
    pass

def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

def _json(path):
    return json.loads(Path(path).read_text())

def _atomic_json(path, value, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        os.fchmod(target.fileno(), 0o600 if private else 0o644)
        json.dump(value, target, indent=2, sort_keys=True)
        target.write('\n')
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)

def _safe_directory(path):
    path = Path(os.path.abspath(path))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeFailure('Symlinked runtime/cache/output paths are not accepted')
    for parent in path.parents:
        if not parent.exists():
            continue
        metadata = parent.stat()
        sticky_root = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
        if metadata.st_uid not in (0, os.getuid()) or (metadata.st_mode & 0o022 and not sticky_root):
            raise RuntimeFailure('Runtime/cache ancestors must not be writable by other operators')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
        raise RuntimeFailure('Runtime directories must be operator-owned and not group/world writable')
    return path

def _copy_source(relative, destination):
    source = SOURCE_DIR / relative
    if source.is_symlink() or not source.is_file():
        raise RuntimeFailure('Source inputs must be ordinary files: ' + relative)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


class DependencyBase:
    def _new_network(self, suffix, internal):
        # Reject overlap BEFORE creating a bridge, including policy-routing tables
        # used by Tailscale. Docker's default pool selection alone is insufficient.
        occupied = []
        for route in json.loads(self._run(['ip', '-j', 'route', 'show', 'table', 'all']).stdout):
            destination = route.get('dst')
            if destination and destination != 'default':
                network = ipaddress.ip_network(destination, strict=False)
                if network.version == 4 and network.prefixlen:
                    occupied.append(network)
        ids = self._docker(['network', 'ls', '-q']).stdout.split()
        if ids:
            for network in json.loads(self._docker(['network', 'inspect', *ids]).stdout):
                for configuration in network.get('IPAM', {}).get('Config') or []:
                    subnet = configuration.get('Subnet')
                    if subnet and ':' not in subnet:
                        occupied.append(ipaddress.ip_network(subnet))
        candidates = [ipaddress.ip_network(prefix + str(index) + '.0/24')
                      for prefix in ('10.247.', '172.30.', '192.168.', '198.18.')
                      for index in range(32, 240)]
        subnet = next((candidate for candidate in candidates if not any(candidate.overlaps(other) for other in occupied)), None)
        if subnet is None:
            raise RuntimeFailure('No non-overlapping disposable IPv4 subnet; do not change host or Tailscale routes')
        name = self.prefix + '-' + suffix
        arguments = ['network', 'create', '--label', LABEL + '=' + self.run_id,
                     '--subnet', str(subnet)]
        if internal:
            arguments.append('--internal')
        identifier = self._docker([*arguments, name]).stdout.strip()
        resource = {'id': identifier, 'name': name, 'subnet': str(subnet)}
        self.resources['networks'].append(resource)
        self._checkpoint()
        return resource

    def _tool(self, executable, arguments, mounts):
        command = ['run', '--rm', '--network', 'none', '--user', f'{os.getuid()}:{os.getgid()}',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--read-only',
                   '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m', '--entrypoint', executable]
        for source, destination, readonly in mounts:
            command.extend(['--mount', f'type=bind,src={source},dst={destination}' + (',readonly' if readonly else '')])
        return self._docker([*command, self.wrapper_tag, *arguments], timeout=1800)

    def _seed(self, directory, configuration, hostname):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, value in (('user-data', '#cloud-config\n' + json.dumps(configuration)),
                            ('meta-data', json.dumps({'instance-id': hostname + '-' + self.run_id, 'local-hostname': hostname}))):
            path = directory / name
            path.write_text(value + '\n')
            path.chmod(0o600)
        self._tool('/usr/bin/genisoimage', ['-quiet', '-output', '/work/seed.iso', '-volid', 'cidata',
                                           '-joliet', '-rock', '/work/user-data', '/work/meta-data'],
                   [(directory, '/work', False)])

    def _launch(self, name, network, address, work, inputs, base, expose_services=True):
        user_network = 'user,id=n0'
        if expose_services:
            user_network += f',hostfwd=tcp:{address}:80-:80,hostfwd=tcp:{address}:22-:22'
        command = ['run', '-d', '--name', name, '--label', LABEL + '=' + self.run_id,
                   '--network', 'name=' + network['name'] + ',ip=' + address,
                   '--user', f'{os.getuid()}:{os.getgid()}', '--group-add', str(Path('/dev/kvm').stat().st_gid),
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--device', '/dev/kvm',
                   '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m', '--pids-limit', '256',
                   '--memory', '4g', '--mount', f'type=bind,src={base},dst=/base,readonly',
                   '--mount', f'type=bind,src={work},dst=/work',
                   '--mount', f'type=bind,src={inputs},dst=/inputs,readonly', self.wrapper_tag,
                   '-enable-kvm', '-cpu', 'host', '-smp', '2', '-m', '2560', '-display', 'none',
                   '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
                   '-uuid', str(uuid.uuid4()),
                   '-object', 'rng-random,filename=/dev/urandom,id=rng0', '-device', 'virtio-rng-pci,rng=rng0',
                   '-drive', 'file=/work/disk.qcow2,if=virtio,format=qcow2,cache=none,discard=unmap',
                   '-drive', 'file=/work/seed/seed.iso,format=raw,media=cdrom,readonly=on',
                   '-fsdev', 'local,id=inputs,path=/inputs,security_model=none,readonly=on',
                   '-device', 'virtio-9p-pci,fsdev=inputs,mount_tag=portmapinputs',
                   '-netdev', user_network,
                   '-device', 'virtio-net-pci,netdev=n0']
        identifier = self._docker(command).stdout.strip()
        resource = {'id': identifier, 'name': name}
        self.resources['containers'].append(resource)
        self._checkpoint()
        return resource

    def _download_os(self, destination):
        _safe_directory(self.cache_dir / 'blobs')
        blob_dir = _safe_directory(self.cache_dir / 'blobs' / 'sha256')
        blob = blob_dir / self.spec['os']['sha256']
        if blob.is_symlink():
            raise RuntimeFailure('Symlinked dependency blobs are not accepted')
        if not blob.exists():
            temporary = blob_dir / ('.download-' + self.run_id)
            try:
                with urllib.request.urlopen(self.spec['os']['url'], timeout=180) as response, temporary.open('wb') as target:
                    shutil.copyfileobj(response, target)
                if _sha256(temporary) != self.spec['os']['sha256']:
                    raise RuntimeFailure('Immutable Ubuntu image checksum mismatch')
                temporary.chmod(0o444)
                os.replace(temporary, blob)
            finally:
                temporary.unlink(missing_ok=True)
        if not blob.is_file() or blob.stat().st_uid != os.getuid() or blob.stat().st_mode & 0o222:
            raise RuntimeFailure('Cached OS blobs must be immutable operator-owned ordinary files')
        if _sha256(blob) != self.spec['os']['sha256']:
            raise RuntimeFailure('Cached Ubuntu image checksum mismatch')
        os.link(blob, destination)

    def _cache_build(self, temporary):
        work = self.state / 'dependency-builder'
        work.mkdir(mode=0o700)
        wrapper = work / 'wrapper'
        wrapper.mkdir()
        _copy_source('Runtime.Dockerfile', wrapper / 'Dockerfile')
        self._docker(['build', '--pull=false', '--tag', self.wrapper_tag, str(wrapper)], timeout=1800)
        wrapper_identity = json.loads(self._docker(['image', 'inspect', self.wrapper_tag]).stdout)[0]
        self._docker(['save', '--output', str(temporary / 'wrapper-image.tar'), self.wrapper_tag], timeout=600)
        self._download_os(temporary / 'os.qcow2')
        inputs = work / 'inputs'
        inputs.mkdir()
        for filename in ('base_bootstrap.py', 'runtime-inputs.json', 'deps/Dockerfile', 'deps/package.json', 'deps/package-lock.json'):
            _copy_source(filename, inputs / filename)
        image_inputs = {'images': [], 'dependencyImage': 'portmap-real-node-vite-deps:' + self.key}
        for image in self.spec['images']:
            self._docker(['pull', '--platform', self.spec['platform'], image['ref']], timeout=900)
            actual = json.loads(self._docker(['image', 'inspect', image['ref']]).stdout)[0]
            cache_tag = 'portmap-real-node-dependency:' + self.key[:16] + '-' + image['name']
            self._docker(['tag', image['ref'], cache_tag])
            filename = 'image-' + image['name'] + '.tar'
            self._docker(['save', '--output', str(temporary / filename), cache_tag], timeout=600)
            try:
                os.link(temporary / filename, inputs / filename)
            except OSError:
                shutil.copyfile(temporary / filename, inputs / filename)
            image_inputs['images'].append({**image, 'cacheTag': cache_tag, 'archive': filename,
                                            'sha256': _sha256(temporary / filename),
                                            'rootfsLayers': actual['RootFS']['Layers']})
        _atomic_json(inputs / 'image-inputs.json', image_inputs)
        self._tool('/usr/bin/qemu-img', ['create', '-f', 'qcow2', '-F', 'qcow2', '-b', '/base/os.qcow2',
                                         '/work/disk.qcow2', '16G'], [(temporary, '/base', True), (work, '/work', False)])
        cloud = {'users': [], 'disable_root': True, 'ssh_pwauth': False, 'ssh_deletekeys': True,
                 'ssh_genkeytypes': [], 'ssh_publish_hostkeys': {'enabled': False},
                 'package_update': False, 'package_upgrade': False,
                 'runcmd': [['mkdir', '-p', '/mnt/inputs'],
                            ['mount', '-t', '9p', '-o', 'trans=virtio,version=9p2000.L,ro', 'portmapinputs', '/mnt/inputs'],
                            ['python3', '/mnt/inputs/base_bootstrap.py', '/mnt/inputs']]}
        self._seed(work / 'seed', cloud, 'pm-dependency-base')
        network = self._new_network('dependency-egress', internal=False)
        address = str(ipaddress.ip_network(network['subnet']).network_address + 11)
        resource = self._launch(self.prefix + '-dependency-builder', network, address, work, inputs, temporary, expose_services=False)
        try:
            self._docker(['wait', resource['id']], timeout=2700)
            logs = self._docker(['logs', resource['id']]).stdout
            # This builder has no accounts, credentials, tested CLI or app source.
            (self.out_dir / 'dependency-build.log').write_text(logs)
            found = re.search(r'PORTMAP_REAL_NODE_BASE_SEAL (\{[^\r\n]+\})', logs)
            if not found:
                raise RuntimeFailure('Dependency construction did not produce its credential-free seal; see dependency-build.log')
            seal = json.loads(found.group(1))
        finally:
            self._remove_container(resource)
            self._remove_network(network)
        self._tool('/usr/bin/qemu-img', ['convert', '-O', 'qcow2', '-S', '4k', '/work/disk.qcow2',
                                         '/base/dependencies.qcow2'], [(temporary, '/base', False), (work, '/work', True)])
        seal['diskSanitization'] = 'guest fstrim with discard=unmap; offline flattened qemu-img convert without backing file'
        information = json.loads(self._tool('/usr/bin/qemu-img', ['info', '--output=json', '/base/dependencies.qcow2'],
                                            [(temporary, '/base', True)]).stdout)
        if information.get('backing-filename'):
            raise RuntimeFailure('Sealed dependency disk must not have a backing file')
        shutil.rmtree(work)
        return seal, {'rootfsLayers': wrapper_identity['RootFS']['Layers']}
