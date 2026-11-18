#!/usr/bin/env python3
"""Run as root only in the disposable dependency-builder guest, never on the host."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tomllib


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)


def output(argv):
    return subprocess.check_output(argv, text=True).strip()


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build(inputs):
    specification = json.loads((inputs / 'runtime-inputs.json').read_text())
    artifacts = json.loads((inputs / 'image-inputs.json').read_text())
    if os.geteuid() != 0:
        raise RuntimeError('Dependency construction requires root INSIDE the disposable guest')
    if any(1000 <= account.pw_uid < 60000 for account in pwd.getpwall()):
        raise RuntimeError('Dependency base must be constructed without consumer accounts')
    for unit in ('apt-daily.timer', 'apt-daily-upgrade.timer', 'unattended-upgrades.service'):
        run(['systemctl', 'disable', '--now', unit], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    run(['systemctl', 'mask', '--now', 'ssh.service', 'ssh.socket'])
    ssh_configuration = Path('/etc/ssh/sshd_config')
    original_ssh_configuration = ssh_configuration.read_text()
    # openssh-server.postinst only creates its default keys when they are listed
    # by HostKey. No SSH login or host private key is needed in this builder.
    ssh_configuration.write_text(original_ssh_configuration +
                                 '\nHostKey /run/real-node-dependency-builder-no-key\n')
    sources = Path('/etc/apt/sources.list.d')
    for path in sources.iterdir():
        if path.suffix in ('.sources', '.list'):
            path.unlink()
    Path('/etc/apt/sources.list').write_text('')
    (sources / 'real-node-snapshot.sources').write_text(
        'Types: deb\nURIs: https://snapshot.ubuntu.com/ubuntu/' + specification['ubuntuSnapshot'] +
        '\nSuites: noble noble-updates noble-security\nComponents: main restricted universe multiverse\n'
        'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\nCheck-Valid-Until: no\n')
    run(['apt-get', 'update'])
    environment = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
    run(['apt-get', 'install', '-y', '--no-install-recommends', *specification['guestPackages']],
        env=environment)
    if list(Path('/etc/ssh').glob('ssh_host_*')):
        raise RuntimeError('Dependency installation unexpectedly generated SSH host keys')
    ssh_configuration.write_text(original_ssh_configuration)
    run(['systemctl', 'unmask', 'ssh.service', 'ssh.socket'])
    run(['systemctl', 'enable', 'ssh.service'])
    run(['systemctl', 'enable', '--now', 'docker'])
    version = output(['docker', 'version', '--format', '{{.Server.Version}}'])
    if version != specification['dockerVersion']:
        raise RuntimeError('Pinned guest Docker version mismatch: ' + version)
    images = []
    for artifact in artifacts['images']:
        archive = inputs / artifact['archive']
        if sha256(archive) != artifact['sha256']:
            raise RuntimeError('OCI archive digest mismatch: ' + artifact['name'])
        run(['docker', 'load', '--input', str(archive)])
        run(['docker', 'tag', artifact['cacheTag'], artifact['tag']])
        identity = json.loads(output(['docker', 'image', 'inspect', artifact['tag']]))[0]
        if identity['RootFS']['Layers'] != artifact['rootfsLayers']:
            raise RuntimeError('Loaded OCI layer identity mismatch: ' + artifact['name'])
        images.append({'name': artifact['name'], 'reference': artifact['tag'],
                       'source': artifact['ref'], 'archiveSha256': artifact['sha256'],
                       'id': identity['Id'], 'rootfsLayers': identity['RootFS']['Layers']})
    dependency_image = artifacts['dependencyImage']
    run(['docker', 'build', '--pull=false', '--tag', dependency_image, str(inputs / 'deps')])
    dependency = json.loads(output(['docker', 'image', 'inspect', dependency_image]))[0]
    vite_version = output(['docker', 'run', '--rm', '--network', 'none', dependency_image,
                           'node', '-p', "require('/opt/real-node-deps/node_modules/vite/package.json').version"])
    node_version = output(['docker', 'run', '--rm', '--network', 'none', dependency_image,
                           'node', '-p', 'process.versions.node'])
    if vite_version != specification['viteVersion'] or node_version != specification['nodeVersion']:
        raise RuntimeError('Pinned Node/Vite dependency versions differ')
    if output(['docker', 'ps', '-aq']):
        raise RuntimeError('Dependency base contains containers')
    if shutil.which('portmap') or Path('/root/.config/portmap').exists() or Path('/root/.local/state/portmap').exists():
        raise RuntimeError('Dependency base must never contain Portmap binaries, configuration or state')
    engine = json.loads(output(['docker', 'info', '--format', '{{json .}}']))
    docker_root = Path(engine['DockerRootDir'])
    containerd_root = Path(tomllib.loads(output(['containerd', 'config', 'dump']))['root'])
    if (docker_root != Path('/var/lib/docker') or containerd_root != Path('/var/lib/containerd') or
            ['driver-type', 'io.containerd.snapshotter.v1'] not in engine.get('DriverStatus', []) or
            engine.get('Swarm', {}).get('LocalNodeState') != 'inactive'):
        raise RuntimeError('Pinned external containerd image-store layout changed; do not guess cleanup paths')
    if (docker_root / 'engine-id').read_text().strip() != engine['ID']:
        raise RuntimeError('Actual engine-id file differs from source-verified Moby identity storage')
    if output(['docker', 'volume', 'ls', '-q']):
        raise RuntimeError('Dependency base unexpectedly contains volumes')
    run(['docker', 'builder', 'prune', '--all', '--force'])
    packages = output(['dpkg-query', '-W', '-f=${binary:Package}=${Version}\n'])
    seal = {'construction': 'dependencies-only-no-portmap-no-app-no-consumer-credentials',
            'dockerVersion': version, 'nodeVersion': node_version, 'viteVersion': vite_version,
            'images': images, 'dependencyImage': dependency_image,
            'dependencyImageId': dependency['Id'], 'dependencyRootfsLayers': dependency['RootFS']['Layers'],
            'packagesSha256': hashlib.sha256(packages.encode()).hexdigest(),
            'packages': packages.splitlines(), 'consumerAccounts': 0, 'containers': 0,
            'portmapInstalled': False, 'appInstalled': False, 'sshHostKeys': 0,
            'builderEngineId': engine['ID'], 'dockerRootDir': str(docker_root),
            'containerdRoot': str(containerd_root)}
    run(['systemctl', 'stop', 'docker.service', 'docker.socket', 'containerd.service', 'ssh.service', 'ssh.socket'])
    # Moby 29.1.3 daemon/id.go stores engine-id under DockerRootDir. Its
    # containerd image service stores images in the external containerd store.
    # Keep only those dependency images, never daemon/network/swarm/volume state.
    shutil.rmtree(docker_root)
    retained = {'io.containerd.content.v1.content', 'io.containerd.metadata.v1.bolt',
                'io.containerd.snapshotter.v1.overlayfs'}
    for path in containerd_root.iterdir():
        if path.name not in retained:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    seal['retainedImageStoreDirectories'] = sorted(path.name for path in containerd_root.iterdir())
    seal['dockerRuntimeMetadataRemoved'] = not docker_root.exists()
    Path('/etc/docker/key.json').unlink(missing_ok=True)
    Path('/etc/machine-id').unlink(missing_ok=True)
    Path('/etc/machine-id').touch(mode=0o444)
    Path('/var/lib/dbus/machine-id').unlink(missing_ok=True)
    Path('/var/lib/systemd/random-seed').unlink(missing_ok=True)
    for path in Path('/etc/ssh').glob('ssh_host_*'):
        path.unlink()
    run(['cloud-init', 'clean', '--logs', '--seed', '--machine-id'])
    for path in (Path('/root/.ssh'), Path('/var/lib/dhcp')):
        if path.exists():
            shutil.rmtree(path)
    for name in ('/var/log/wtmp', '/var/log/btmp', '/var/log/lastlog'):
        path = Path(name)
        if path.exists():
            path.write_bytes(b'')
    for path in Path('/var/log').glob('cloud-init*'):
        if path.is_file():
            path.write_bytes(b'')
    if list(Path('/etc/ssh').glob('ssh_host_*')):
        raise RuntimeError('Unsealed SSH host keys')
    os.sync()
    seal['freeBlockSanitization'] = output(['fstrim', '--verbose', '/'])
    seal['sshKeyGenerationPrevented'] = 'cloud-init ssh_genkeytypes=[] plus nondefault HostKey during package configuration'
    print('PORTMAP_REAL_NODE_BASE_SEAL ' + json.dumps(seal, separators=(',', ':')), flush=True)


if __name__ == '__main__':
    try:
        build(Path(sys.argv[1]))
    except BaseException as error:
        print('PORTMAP_REAL_NODE_BASE_FAILED ' + type(error).__name__ + ': ' + str(error), flush=True)
        raise
    finally:
        os.sync()
        subprocess.run(['systemctl', 'poweroff'], check=False)
