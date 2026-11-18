#!/usr/bin/env python3
"""Run as root only in the disposable dependency-builder guest, never on the host."""
import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import tempfile
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse


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


APT_SECURITY = (
    '-o', 'Acquire::ForceHash=SHA256',
    '-o', 'Acquire::AllowInsecureRepositories=false',
    '-o', 'Acquire::AllowWeakRepositories=false',
    '-o', 'Acquire::AllowDowngradeToInsecureRepositories=false',
    '-o', 'APT::Get::AllowUnauthenticated=false',
    '-o', 'Acquire::https::Verify-Peer=true',
    '-o', 'Acquire::https::Verify-Host=true',
)


class PackageBlobError(RuntimeError):
    pass


def _url_identity(url):
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) >= 127 for char in url):
        raise PackageBlobError('Invalid pinned package URL')
    try:
        location = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(location.path, errors='strict')
    except (ValueError, UnicodeError) as error:
        raise PackageBlobError('Invalid pinned package URL') from error
    if (location.scheme != 'https' or location.netloc not in ('snapshot.ubuntu.com', 'archive.ubuntu.com') or
            location.query or location.fragment or
            re.search(r'%(?:2f|5c)', location.path, re.IGNORECASE) or '%' in path or
            not re.fullmatch(r'/[A-Za-z0-9/+._:~\-]+', path) or
            any(part in ('', '.', '..') for part in path.split('/')[1:])):
        raise PackageBlobError('Pinned package URL has an unapproved provider or path')
    if ((location.netloc == 'snapshot.ubuntu.com' and
         not re.match(r'^/ubuntu/[0-9]{8}T[0-9]{6}Z/pool/', path)) or
            (location.netloc == 'archive.ubuntu.com' and not path.startswith('/ubuntu/pool/'))):
        raise PackageBlobError('Pinned package URL is outside its approved blob pool')
    return location.scheme, location.netloc, path


def _package_row(primary, filename, size, digest, snapshot):
    if not isinstance(snapshot, str) or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', snapshot):
        raise PackageBlobError('Invalid pinned Ubuntu snapshot identity')
    if type(size) is not int or size <= 0 or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise PackageBlobError('Pinned package requires signed-index SHA256 and positive byte size')
    if not isinstance(filename, str) or '/' in filename or '\\' in filename:
        raise PackageBlobError('Invalid package cache filename')
    decoded = urllib.parse.unquote(filename)
    if '%' in decoded or not decoded.endswith('.deb'):
        raise PackageBlobError('Invalid package cache filename')
    fields = decoded[:-4].split('_')
    if len(fields) != 3:
        raise PackageBlobError('Package cache filename lacks an exact package/version/architecture')
    package, version, architecture = fields
    if (not re.fullmatch(r'[a-z0-9][a-z0-9+.\-]+', package) or
            not re.fullmatch(r'(?:[0-9]+:)?[0-9][A-Za-z0-9.+:~\-]*', version) or
            architecture not in ('amd64', 'all')):
        raise PackageBlobError('Invalid pinned Debian package identity')
    _, host, path = _url_identity(primary)
    prefix = '/ubuntu/' + snapshot + '/pool/'
    if host != 'snapshot.ubuntu.com' or not path.startswith(prefix):
        raise PackageBlobError('Package metadata must come from the exact pinned snapshot provider')
    pool = path[len(prefix):].split('/')
    remote_names = {package + '_' + value + '_' + architecture + '.deb'
                    for value in (version, version.split(':', 1)[-1])}
    if len(pool) != 4 or pool[0] not in ('main', 'restricted', 'universe', 'multiverse') or pool[-1] not in remote_names:
        raise PackageBlobError('Signed package metadata has an unexpected pool path or filename')
    return {'package': package, 'version': version, 'architecture': architecture, 'filename': filename,
            'primary': primary, 'sha256': digest, 'bytes': size}


def package_plan(text, snapshot):
    """Read SHA256-bearing APT transaction URIs, never select package versions ourselves."""
    rows, names, packages = [], set(), set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("'"):
            continue
        try:
            fields = shlex.split(line)
            if len(fields) != 4 or not fields[2].isascii() or not fields[2].isdecimal() or not fields[3].startswith('SHA256:'):
                raise ValueError('Missing strong package metadata')
            row = _package_row(fields[0], fields[1], int(fields[2]), fields[3][7:], snapshot)
        except (ValueError, UnicodeError) as error:
            raise PackageBlobError('APT transaction lacks exact signed-index SHA256/size/identity metadata') from error
        decoded_name = urllib.parse.unquote(row['filename'])
        if decoded_name in names or row['package'] in packages:
            raise PackageBlobError('Conflicting or duplicate pinned package transaction entries')
        names.add(decoded_name)
        packages.add(row['package'])
        rows.append(row)
    return rows


def _archive_url(row):
    _, host, path = _url_identity(row['primary'])
    if host != 'snapshot.ubuntu.com' or '/pool/' not in path:
        raise PackageBlobError('Alternate package provider requires exact snapshot metadata')
    return 'https://archive.ubuntu.com/ubuntu/pool/' + urllib.parse.quote(path.split('/pool/', 1)[1], safe='/+:._~-')


@contextlib.contextmanager
def _open_package(url, maximum_size):
    """Use the pinned guest's existing curl without ambient credentials or retries."""
    identity = _url_identity(url)
    if type(maximum_size) is not int or maximum_size <= 0:
        raise PackageBlobError('Pinned package transport requires a positive byte bound')
    environment = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'FTP_PROXY', 'NO_PROXY', 'ALL_PROXY'):
        environment[name] = environment[name.lower()] = ''
    deadline = time.monotonic() + 120
    visited = {url}
    with tempfile.TemporaryDirectory(prefix='portmap-real-node-package-') as temporary:
        body, headers = Path(temporary) / 'body', Path(temporary) / 'headers'
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PackageBlobError('Pinned package transport exceeded its deadline')
            command = [
                'curl', '--disable', '--proxy', '', '--noproxy', '*', '--proto', '=https',
                '--connect-timeout', f'{min(30, remaining):.3f}', '--max-time', f'{remaining:.3f}',
                '--max-filesize', str(maximum_size), '--silent', '--show-error',
                '--dump-header', str(headers), '--output', str(body),
                '--write-out', '%{http_code}\\n%{url_effective}\\n%{redirect_url}', url,
            ]
            try:
                result = subprocess.run(command, capture_output=True, text=True, env=environment,
                                        timeout=remaining + 1)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise PackageBlobError('Pinned package curl transport failed: ' + url) from error
            if result.returncode:
                raise PackageBlobError(f'Pinned package curl transport failed (exit {result.returncode}): {url}')
            fields = result.stdout.split('\n')
            if (len(fields) != 3 or not re.fullmatch(r'[1-5][0-9]{2}', fields[0]) or
                    _url_identity(fields[1]) != identity):
                raise PackageBlobError('Pinned package curl response changed its status/provider/path')
            status, effective, redirect = int(fields[0]), fields[1], fields[2]
            if status in (301, 302, 303, 307, 308):
                if not redirect or _url_identity(redirect) != identity or redirect in visited or len(visited) >= 5:
                    raise PackageBlobError('Pinned package redirect changed its provider/path or repeated')
                visited.add(redirect)
                url = redirect
                continue
            if redirect:
                raise PackageBlobError('Pinned package response exposed an unexpected redirect')
            if status >= 400:
                raise urllib.error.HTTPError(effective, status, 'Pinned package HTTP response', None, None)
            with headers.open('rb') as source:
                raw = source.read(65537)
            if len(raw) > 65536:
                raise PackageBlobError('Pinned package response headers exceed their bound')
            blocks = [block for block in raw.split(b'\r\n\r\n') if block]
            if not blocks:
                raise PackageBlobError('Pinned package response lacks HTTP headers')
            first, _, values = blocks[-1].partition(b'\r\n')
            if not re.fullmatch(rb'HTTP/(?:1\.[01]|2|3) ' +
                                str(status).encode() + rb'(?: .*)?', first):
                raise PackageBlobError('Pinned package header status differs from curl response')
            parsed = http.client.parse_headers(io.BytesIO(values + b'\r\n\r\n'))
            with body.open('rb') as source:
                yield SimpleNamespace(status=status, headers=parsed, geturl=lambda: effective, read=source.read)
            return


def _fetch_package(url, row, destination):
    if url not in (row['primary'], _archive_url(row)):
        raise PackageBlobError('Unapproved pinned package download provider or path')
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise PackageBlobError('Package cache destination already exists before verified publication')
    temporary = None
    try:
        with _open_package(url, row['bytes']) as response:
            if response.status != 200 or _url_identity(response.geturl()) != _url_identity(url):
                raise PackageBlobError('Pinned package response changed its approved status/provider/path')
            length = response.headers.get('Content-Length')
            encoding = response.headers.get('Content-Encoding')
            if ((length is not None and (not isinstance(length, str) or not length.isascii() or not length.isdecimal() or int(length) != row['bytes'])) or
                    encoding not in (None, 'identity')):
                raise PackageBlobError('Pinned package response has an unexpected byte length or encoding')
            digest, count = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + '.', suffix='.partial', delete=False) as target:
                temporary = Path(target.name)
                while chunk := response.read(min(1024 * 1024, row['bytes'] - count + 1)):
                    count += len(chunk)
                    if count > row['bytes']:
                        raise PackageBlobError('Pinned package exceeds its signed-index byte size')
                    digest.update(chunk)
                    target.write(chunk)
                if count != row['bytes'] or digest.hexdigest() != row['sha256']:
                    raise PackageBlobError('Pinned package bytes differ from the signed-index SHA256/size')
                target.flush()
                os.fsync(target.fileno())
            try:
                fields = output(['dpkg-deb', '--field', str(temporary), 'Package', 'Version', 'Architecture'])
                identity = dict(line.split(': ', 1) for line in fields.splitlines())
            except (subprocess.CalledProcessError, ValueError) as error:
                raise PackageBlobError('Verified archive does not expose an exact Debian package identity') from error
            if identity != {'Package': row['package'], 'Version': row['version'], 'Architecture': row['architecture']}:
                raise PackageBlobError('Verified archive has a different pinned package/version/architecture')
            temporary.chmod(0o644)
            os.replace(temporary, destination)
    except urllib.error.HTTPError:
        raise
    except (OSError, urllib.error.URLError) as error:
        raise PackageBlobError('Pinned package transport or bytes could not be verified') from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prefetch_packages(plan, archive_dir):
    records = []
    archive_dir = Path(archive_dir)
    for row in plan:
        match = re.match(r'^https://snapshot\.ubuntu\.com/ubuntu/([0-9]{8}T[0-9]{6}Z)/', row['primary'])
        if match is None or row != _package_row(row['primary'], row['filename'], row['bytes'], row['sha256'], match[1]):
            raise PackageBlobError('Pinned package plan no longer matches signed metadata')
        url, status, attempts = row['primary'], 200, 0
        try:
            _fetch_package(url, row, archive_dir / row['filename'])
        except urllib.error.HTTPError as error:
            if error.code not in (404, 410) and not 500 <= error.code <= 599:
                raise
            status, attempts, url = error.code, 1, _archive_url(row)
            _fetch_package(url, row, archive_dir / row['filename'])
        record = {**row, 'provider': urllib.parse.urlsplit(url).netloc, 'url': url,
                  'primaryHttpStatus': status, 'alternateAttempts': attempts}
        records.append(record)
        if attempts:
            print('PORTMAP_REAL_NODE_PACKAGE_TRANSPORT ' + json.dumps(record, sort_keys=True), flush=True)
    return records


def install_guest_packages(specification):
    environment = dict(os.environ, DEBIAN_FRONTEND='noninteractive', LC_ALL='C')
    # A fresh private archive cache makes the URI plan include every needed blob;
    # APT cannot silently skip an existing unverified .deb based only on its size.
    with tempfile.TemporaryDirectory(prefix='portmap-real-node-apt-', dir='/var/cache') as temporary:
        archives = Path(temporary)
        archives.chmod(0o755)
        options = [*APT_SECURITY, '-o', 'Dir::Cache::Archives=' + str(archives) + '/']
        requested = list(specification['guestPackages'])
        result = run(['apt-get', *options, '--print-uris', '--download-only', '-y',
                      '--no-install-recommends', 'install', *requested],
                     env=environment, stdout=subprocess.PIPE)
        plan = package_plan(result.stdout, specification['ubuntuSnapshot'])
        exact = {row['package']: row['version'] for row in plan}
        for request in requested:
            if '=' in request:
                package, version = request.split('=', 1)
                if package in exact and exact[package] != version:
                    raise PackageBlobError('APT plan changed an explicitly pinned package version')
        records = prefetch_packages(plan, archives)
        pins = [row['package'] + '=' + row['version'] for row in plan]
        run(['apt-get', *options, '-y', '--no-install-recommends', '--no-download', 'install', *requested, *pins],
            env=environment)
        return records


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
    run(['apt-get', *APT_SECURITY, 'update'])
    package_transports = install_guest_packages(specification)
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
            'packageTransports': package_transports,
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
