"""Behavioral release-receipt contracts using explicitly synthetic unit-test data.

No fixture claims to execute a node or browser. Native parsers consume actual
strace-format records and Ethernet PCAP bytes so summary-only proofs cannot pass.
"""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import socket
import struct
import tarfile
import zipfile
from types import SimpleNamespace

import pytest

from tests.browser.real_node import evidence
from tests.browser.real_node import native_gateway_markers as native_gateway
from tests.browser.real_node.runtime import SOURCE_FILES

ROOT = Path(__file__).resolve().parents[1]
UBUNTU_SNAPSHOT = '20261118T000000Z'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def package_transport_fixture(*, package='docker.io', version='29.1.3', architecture='amd64'):
    filename = package + '_' + version.replace(':', '%3a') + '_' + architecture + '.deb'
    remote = package + '_' + version.split(':', 1)[-1] + '_' + architecture + '.deb'
    primary = ('https://snapshot.ubuntu.com/ubuntu/' + UBUNTU_SNAPSHOT + '/pool/main/' +
               package[0] + '/' + package + '/' + remote)
    return {'package': package, 'version': version, 'architecture': architecture, 'filename': filename,
            'primary': primary, 'sha256': 'a' * 64, 'bytes': 1024, 'provider': 'snapshot.ubuntu.com',
            'url': primary, 'primaryHttpStatus': 200, 'alternateAttempts': 0}


def archive_package_transport(row, status=502):
    row.update(provider='archive.ubuntu.com',
               url=row['primary'].replace('snapshot.ubuntu.com/ubuntu/' + UBUNTU_SNAPSHOT,
                                          'archive.ubuntu.com/ubuntu'),
               primaryHttpStatus=status, alternateAttempts=1)
    return row


def public_host_key(mode):
    algorithm = b'ssh-ed25519'
    blob = struct.pack('!I', len(algorithm)) + algorithm + struct.pack('!I', 32) + hashlib.sha256(mode.encode()).digest()
    fingerprint = hashlib.sha256(blob).digest()
    return {'algorithm': algorithm.decode(), 'publicKey': algorithm.decode() + ' ' + base64.b64encode(blob).decode(),
            'sha256Hex': fingerprint.hex(), 'sha256Base64': 'SHA256:' + base64.b64encode(fingerprint).decode().rstrip('=')}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def runtime_archive_fixture(directory, version, commit):
    directory.mkdir(parents=True, exist_ok=True)
    binary = b'\x7fELF explicitly synthetic unit-test CLI payload'
    path = directory / f'portmap-{version}-linux-amd64.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        internal = tarfile.TarInfo('_internal')
        internal.type, internal.mode = tarfile.DIRTYPE, 0o755
        archive.addfile(internal)
        dependency = b'explicitly synthetic unit-test bundled runtime dependency'
        internal_file = tarfile.TarInfo('_internal/runtime-unit-test.so')
        internal_file.size = len(dependency)
        archive.addfile(internal_file, io.BytesIO(dependency))
        member = tarfile.TarInfo('portmap')
        member.mode, member.size = 0o755, len(binary)
        archive.addfile(member, io.BytesIO(binary))
    release = {'version': version, 'source': {
        'repository': 'ylang-ylang/portmap', 'tag': 'v' + version, 'commit': commit,
    }, 'assets': [{'os': 'linux', 'arch': 'amd64', 'filename': path.name,
                   'sha256': evidence.file_sha256(path), 'size': path.stat().st_size,
                   'source_url': 'https://github.com/ylang-ylang/portmap/releases/download/v' + version + '/' + path.name}]}
    (directory / (path.name + '.sha256')).write_text(evidence.file_sha256(path) + '  ' + path.name + '\n')
    return release, digest(binary)


def reference(root, path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {'path': path.relative_to(root).as_posix(), 'sha256': digest(content), 'bytes': len(content)}


def stages(names, start=1010, end=1011):
    return [{'name': name, 'kind': 'unit-test', 'status': 'passed',
             'startedAt': start, 'finishedAt': end, 'elapsedSeconds': end - start} for name in names]


def native_pcap(destinations):
    result = struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
    for host, port in destinations:
        ethernet = b'\0' * 12 + b'\x08\x00'
        ipv4 = b'\x45\0\0\x28' + b'\0' * 5 + b'\x06' + b'\0' * 2 + socket.inet_aton('10.0.2.15') + socket.inet_aton(host)
        tcp = struct.pack('!HH', 45000, port) + b'\0' * 16
        packet = ethernet + ipv4 + tcp
        result += struct.pack('<IIII', 1, 0, len(packet), len(packet)) + packet
    return result


def gateway_request(nonce, host, *, target='/api/echo', body=None, extra_headers=()):
    body = json.dumps({'nonce': nonce} if body is None else body, separators=(',', ':')).encode()
    headers = [f'POST {target} HTTP/1.1', f'Host: {host}', 'Content-Type: application/json',
               f'Content-Length: {len(body)}', *extra_headers]
    return ('\r\n'.join(headers) + '\r\n\r\n').encode(), body


def gateway_packet(sequence, payload=b'', *, link_type=276, source='10.247.32.2', source_port=45000,
                   destination='10.0.2.15', destination_port=80, flags=0x18):
    tcp = struct.pack('!HHIIHHHH', source_port, destination_port, sequence & 0xffffffff, 0,
                      (5 << 12) | flags, 65535, 0, 0)
    ipv4 = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp) + len(payload), 0, 0x4000, 64, 6, 0,
                       socket.inet_aton(source), socket.inet_aton(destination))
    if link_type == 1:
        link = b'\0' * 12 + b'\x08\x00'
    elif link_type == 113:
        link = struct.pack('!HHH8sH', 0, 1, 6, b'\0' * 8, 0x0800)
    else:
        link = struct.pack('!HHIHBB8s', 0x0800, 0, 7, 1, 0, 6, b'\0' * 8)
    return link + ipv4 + tcp + payload


def gateway_capture(packets, *, link_type=276):
    result = struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 262144, link_type)
    for index, packet in enumerate(packets):
        result += struct.pack('<IIII', 1730000000 + index, 0, len(packet), len(packet)) + packet
    return result


def gateway_capture_rows(packets, *, link_type=276, observer=None):
    rows = []
    native_gateway.observe(io.BytesIO(gateway_capture(packets, link_type=link_type)), rows.append, observer=observer)
    return rows


def persist_fixture(path, proof, *, index=True):
    root = path.parent
    runtime = proof['runtime']['evidence']
    dump(root / 'runtime/cleanup.json', proof['runtime']['cleanup'])
    for node in runtime['nodes']:
        dump(root / ('runtime/node-' + node['mode'] + '.json'), node)
    if proof['platforms']:
        dump(root / 'runtime/public-manifest.json', next(iter(proof['platforms'].values()))['node_manifest'])
    for platform, receipt in proof['platforms'].items():
        output = root / platform
        for scenario in receipt['scenarios']:
            app = scenario['application']
            dump(output / ('captures/application-' + scenario['mode'] + '.json'), app)
            dump(output / ('captures/interaction-' + scenario['mode'] + '.json'), {'nonce': app['nonce'], 'response': app['interaction']})
            dump(output / ('captures/catalog-' + scenario['mode'] + '.json'),
                 {key: value for key, value in scenario['catalog'].items() if key not in ('screenshot', 'projectLinkClicked')})
        dump(output / 'captures/network-audit.json', receipt['network'])
        if index:
            receipt['artifacts'] = evidence.artifacts(output)
        dump(output / 'receipt.json', receipt)
    runtime['artifacts'] = [
        {'path': 'node-' + node['mode'] + '.json',
         'sha256': evidence.file_sha256(root / ('runtime/node-' + node['mode'] + '.json')),
         'bytes': (root / ('runtime/node-' + node['mode'] + '.json')).stat().st_size}
        for node in runtime['nodes']]
    proof['artifacts'] = evidence.public_artifact_hashes(root)
    dump(path, proof)


def aggregate_fixture(directory, *, release, xpi, source, executable_sha256, runtime_archive):
    directory.mkdir(parents=True)
    dump(directory / 'inputs/release.json', release)
    (directory / 'inputs/addon.xpi').write_bytes(xpi.read_bytes())
    (directory / 'inputs/runtime.tar.gz').write_bytes(runtime_archive.read_bytes())
    version, commit = release['version'], release['source']['commit']
    inputs = {'mode': 'candidate', 'version': version, 'source_commit': commit,
              'xpi_sha256': evidence.file_sha256(xpi), 'runtime_archive_sha256': release['assets'][0]['sha256'],
              'runtime_manifest_sha256': evidence.file_sha256(directory / 'inputs/release.json')}
    network = 'portmap-real-node-unit-test'
    cases = []
    contents = {'js': b'export const genuineApp = true;', 'css': b'#app { color: red; }',
                'image': b'<svg>synthetic test image</svg>', 'static': b'synthetic static source'}
    resources = [{'kind': kind, 'path': '/src/' + kind, 'contentType': 'text/' + kind,
                  **({'contains': 'genuineApp'} if kind == 'js' else {'sha256': digest(content)})}
                 for kind, content in contents.items()]
    contract = {'resources': resources, 'cssSelector': '#app', 'cssProperty': 'color', 'cssExpected': 'rgb(255, 0, 0)'}
    for index, mode in enumerate(('direct', 'ssh')):
        domain = mode + '-unit.portmap'
        cases.append({'mode': mode, 'domain': domain, 'host': '172.18.0.' + str(index + 2),
                      'httpPort': 80, 'sshPort': 22, 'sshUsername': 'disposable', 'hostKeySha256Hex': public_host_key(mode)['sha256Hex'],
                      'catalogUrl': 'http://' + domain + ':80/', 'projectUrl': 'http://frontend.' + domain + ':80',
                      'projectName': 'vite-app', 'expectedNode': 'node-' + mode, 'appContract': copy.deepcopy(contract),
                      'endpointPreserveHost': False, 'expectedBackendHost': '127.0.0.1:5173'})
    browser_sources = {name: source['harness_sha256'][name] for name in evidence.BROWSER_SOURCE_INPUTS}
    runtime_sources = {'tests/browser/real_node/' + name: source['harness_sha256']['tests/browser/real_node/' + name]
                       for name in SOURCE_FILES}
    construction_sources = {name: runtime_sources[name] for name in evidence.CONSTRUCTION_SOURCE_INPUTS}
    construction_key = hashlib.sha256(json.dumps({'schema': 2, 'constructionFiles': construction_sources},
                                                 sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    manifest = {'ready': True, 'frozen': True, 'runId': 'fresh-runtime-unit-test', 'network': network,
                'cacheKey': construction_key, 'publishedVersion': version, 'sourceCommit': commit,
                'publishedCliSha256': inputs['runtime_archive_sha256'], 'addonSha256': inputs['xpi_sha256'], 'cases': cases}
    platforms = {}
    for platform in ('desktop', 'android'):
        output = directory / platform
        (output / 'raw').mkdir(parents=True)
        (output / 'raw/container-hosts.txt').write_text('127.0.0.1 localhost\n')
        (output / 'raw/container-resolv.conf').write_text('nameserver 127.0.0.11\n')
        if platform == 'desktop':
            (output / 'raw/desktop-network.123').write_text('\n'.join(
                'connect(4, {sa_family=AF_INET, sin_port=htons(' + str(port) + '), sin_addr=inet_addr("' + host + '")}, 16) = 0'
                for host, port in ((cases[0]['host'], 80), (cases[1]['host'], 22))))
        else:
            (output / 'raw/android-network.pcap').write_bytes(native_pcap(((cases[0]['host'], 80), (cases[1]['host'], 22))))
        (output / 'launcher.log').write_text('synthetic unit-test launcher log\n')
        dump(output / 'container-cleanup.json', {'status': 'passed', 'name': 'owned-' + platform, 'removed': True})
        scenarios = []
        for case in cases:
            mode = case['mode']
            node = case['expectedNode']
            url = evidence._canonical_url(case['projectUrl'])
            original_host = url.split('/')[2]
            nonce = 'portmap-realnode-' + platform + '-' + mode + '-' + digest((platform + mode).encode())[:32]
            screenshot = reference(output, output / ('screens/' + mode + '.png'), PNG)
            screenshot['kind'] = 'native Firefox full-page'
            if platform == 'android':
                screenshot['nativeScreen'] = reference(output, output / ('screens/' + mode + '-native.png'), PNG)
            responses = []
            for resource in resources:
                kind = resource['kind']
                artifact = reference(output, output / ('captures/' + mode + '-' + kind + '.bin'), contents[kind])
                responses.append({'sourcePath': resource['path'], 'kind': kind, 'status': 200,
                                  'url': 'http://' + original_host + resource['path'] + '?browserProofNonce=' + nonce,
                                  'contentType': resource['contentType'], 'artifact': artifact})
            interaction = {'nonce': nonce, 'node': node, 'cssLoaded': True, 'jsBehavior': True, 'staticLoaded': True}
            for channel in ('post', 'websocket'):
                interaction[channel] = {'nonce': nonce, 'node': node, 'host': '127.0.0.1:5173', 'forwardedHost': original_host}
            document = {'url': url, 'host': original_host, 'proof': {'ready': True, 'node': node},
                        'images': [{'complete': True, 'naturalWidth': 1}]}
            app = {'nonce': nonce, 'node': node, 'before': document, 'after': copy.deepcopy(document),
                   'originalProjectUrl': case['projectUrl'], 'observedSourceHost': original_host,
                   'originalForwardedHost': original_host, 'defaultBackendHost': '127.0.0.1:5173', 'endpointPreserveHost': False,
                   'interaction': interaction, 'console': [], 'screenshot': screenshot,
                   'resources': {'responses': responses, 'computedCss': {'selector': '#app', 'property': 'color',
                                                                       'expected': contract['cssExpected'], 'observed': contract['cssExpected']}}}
            saved = {'mode': mode, 'httpPort': 80}
            onboarding = {'actualOptionsUi': True, 'saved': saved}
            if mode == 'ssh':
                saved['ssh'] = {'credentialFree': True, 'authType': 'managed', 'hostKeySha256Hex': case['hostKeySha256Hex']}
                onboarding.update(fingerprint=case['hostKeySha256Hex'], explicitFingerprintComparison=True,
                                  oneTimePasswordBootstrap=True, passwordCleared=True,
                                  managedIdentity={'present': True, 'rsaBits': 3072, 'localPrivateCopy': False})
            catalog = {'url': evidence._canonical_url(case['catalogUrl']), 'title': 'Portmap', 'text': 'vite-app',
                       'links': [{'href': url}], 'projectHref': case['projectUrl'], 'projectLinkClicked': True,
                       'controlRole': 'private-ssh' if mode == 'ssh' else 'public-lan', 'screenshot': screenshot,
                       'ingressBoundary': {'connectionsStatus': 200 if mode == 'ssh' else 404, 'machinesStatus': 200,
                                           'canManageConnections': mode == 'ssh'}}
            scenarios.append({'id': platform + '-' + mode, 'mode': mode, 'status': 'passed', 'domain': case['domain'],
                              'projectUrl': case['projectUrl'], 'onboarding': onboarding, 'catalog': catalog, 'application': app,
                              'startedAt': 1020, 'finishedAt': 1040, 'elapsedSeconds': 20,
                              'stages': stages(('onboarding', 'catalog', 'application', 'resources', 'screenshot'), 1021, 1022)})
        toolchain = {'seleniumVersion': '4.29', 'geckodriver': '0.36', 'browserVersion': '148.0a1', 'platform': platform,
                     'buildId': '20261118000000', 'addon': {'addon_state': {'active': True, 'appDisabled': False, 'temporary': False},
                     'signature_required': False, 'experiments_preference': True, 'browser_version': '148.0a1',
                     'buildID': '20261118000000', 'processID': 123}}
        names = ['inputs', 'environment', 'browser-start', 'addon-install', 'browser-close', 'native-network-audit',
                 'profile-discard', 'artifact-index']
        if platform == 'android':
            names += ['android-boot', 'android-stop']
            toolchain['android'] = {'apk_sha256': 'a' * 64, 'freshAvd': True}
        platforms[platform] = {'schemaVersion': 1, 'platform': platform, 'status': 'passed', 'failures': [],
            'startedAt': 1000, 'finishedAt': 1100, 'stages': stages(names), 'timings': {'totalSeconds': 100},
            'xpi_sha256': inputs['xpi_sha256'], 'addon_version': version, 'source_commit': commit,
            'runtime_archive_sha256': inputs['runtime_archive_sha256'], 'node_manifest': copy.deepcopy(manifest),
            'harnessFiles': browser_sources, 'harness_sha256': digest(json.dumps(browser_sources, sort_keys=True, separators=(',', ':')).encode()),
            'profile': {'id': 'fresh-' + platform, 'fresh': True, 'discarded': True,
                        'nativeProfileIdSha256': digest(platform.encode()), 'initialConnectionsEmpty': True,
                        'initialManagedIdentity': {'present': False, 'reference_present': False}},
            'toolchain': toolchain, 'network': evidence.network_audit(platform, output / 'raw', cases), 'scenarios': scenarios}
    nodes = []
    for case in cases:
        mode, node = case['mode'], case['expectedNode']
        service = {'repo_name': 'vite-app', 'branch': 'development', 'compose_service': 'frontend',
                   'endpoints': [{'url': case['projectUrl']}]}
        catalog = {scope: {'registryStatus': 200, 'machinesStatus': 200, 'connectionsStatus': 200 if private else 404,
                           'canManage': private, 'services': [copy.deepcopy(service)]}
                   for scope, private in (('public', False), ('private', True))}
        markers, gateway = [], []
        for platform, receipt in platforms.items():
            scenario = next(row for row in receipt['scenarios'] if row['mode'] == mode)
            nonce, original_host = scenario['application']['nonce'], scenario['application']['observedSourceHost']
            for kind in ('vite-http-post-nonce', 'vite-websocket-nonce'):
                markers.append({'type': kind, 'node': node, 'nonce': nonce, 'host': '127.0.0.1:5173', 'forwardedHost': original_host})
            header, body = gateway_request(nonce, original_host)
            port = 45000 + len(gateway)
            gateway.extend(gateway_capture_rows([
                gateway_packet(1000, source_port=port, flags=2),
                gateway_packet(1001, header, source_port=port),
                gateway_packet(1001 + len(header), body, source_port=port),
            ]))
        nodes.append({'mode': mode, 'name': node, 'domain': case['domain'], 'physicalHost': case['host'],
            'outerContainer': {'id': 'outer-' + mode, 'image': 'sha256:' + 'b' * 64, 'user': '1000:1000',
                'privileged': False, 'networkMode': network, 'capDrop': ['ALL'], 'securityOpt': ['no-new-privileges:true'],
                'devices': ['/dev/kvm'], 'mountTargets': ['/base', '/work', '/inputs'], 'portBindings': {}},
            'guest': {'machineId': 'machine-' + mode, 'bootId': 'boot-' + mode,
                'cli': {'version': version, 'archiveSha256': inputs['runtime_archive_sha256'], 'executableSha256': executable_sha256},
                'engine': {'version': '29.1.3', 'id': 'engine-' + mode},
                'processes': [{'pid': 1, 'name': 'init'}, {'pid': 123, 'name': 'portmap'}],
                'listeners': 'LISTEN 0 128 0.0.0.0:22',
                'openssh': {'hostKeys': [public_host_key(mode)], 'effective': {'allowtcpforwarding': 'yes'},
                            'forwardings': ['server_request_direct_tcpip: target 127.0.0.1 port 80']}},
            'statusCheck': {'exitCode': 0, 'gatewayRunning': True, 'routesLoaded': True, 'dnsRunning': True,
                            'hostDnsEnabled': False, 'agentRunning': True, 'agentPid': 123,
                            'agentExecutableSha256': executable_sha256, 'ingresses': [{'port': 80}]},
            'containers': [{'id': 'vite-' + mode, 'image': 'sha256:' + 'e' * 64, 'state': 'running', 'composeService': 'frontend'},
                           {'id': 'gateway-' + mode, 'image': 'sha256:' + 'f' * 64, 'state': 'running', 'imageReference': 'traefik:v3.6'}],
            'images': [{'id': 'sha256:' + value * 64, 'references': [name], 'rootfsLayers': ['sha256:' + 'a' * 64]}
                       for value, name in (('e', 'real-node-vite:unit-test'), ('f', 'traefik:v3.6'))], 'catalog': catalog,
            'project': {'repoName': 'vite-app', 'branch': 'development', 'endpointPreserveHost': False, 'url': case['projectUrl'],
                        'endpointTomlSha256': 'e' * 64, 'composeSha256': 'f' * 64}, 'appMarkers': markers, 'gatewayMarkers': gateway})
    proof = {'schemaVersion': 1, 'kind': 'portmap-real-node-browser', 'status': 'passed', 'error': None, 'failures': [],
             'inputs': inputs, 'harness': copy.deepcopy(source), 'platforms': platforms,
             'browser_images': {platform: {'platform': platform, 'cache_key': 'c' * 64, 'cache_hit': True,
                                          'image_id': 'sha256:' + 'b' * 64, 'archive_sha256': 'd' * 64} for platform in platforms},
             'runtime': {'cache': {'cache_key': construction_key, 'cache_hit': True, 'elapsed_seconds': 1,
                                  'manifest_sha256': 'd' * 64, 'artifacts': [{'name': 'base.qcow2', 'sha256': 'e' * 64, 'bytes': 100}],
                                  'sources': {'os': {'sha256': 'e' * 64}, 'images': ['sha256:' + 'f' * 64], 'ubuntuSnapshot': UBUNTU_SNAPSHOT,
                                              'dockerVersion': '29.1.3', 'nodeVersion': '22.22.3', 'viteVersion': '8.0.16',
                                              'constructionFiles': construction_sources, 'sourceFiles': runtime_sources},
                                  'golden': {'construction': 'dependencies-only-no-portmap-no-app-no-consumer-credentials',
                                      'dockerVersion': '29.1.3', 'nodeVersion': '22.22.3', 'viteVersion': '8.0.16',
                                      'consumerAccounts': 0, 'containers': 0, 'sshHostKeys': 0,
                                      'portmapInstalled': False, 'appInstalled': False, 'packagesSha256': 'a' * 64,
                                      'packages': ['docker.io=29.1.3'], 'packageTransports': [package_transport_fixture()],
                                      'images': [{'id': 'sha256:' + 'b' * 64}],
                                      'dependencyImage': 'deps:unit-test', 'dependencyImageId': 'sha256:' + 'c' * 64,
                                      'freeBlockSanitization': '/: unit-test fstrim receipt',
                                      'sshKeyGenerationPrevented': 'no build-time host keys',
                                      'diskSanitization': 'offline flattened disk without backing file'}},
                         'start': {'network': network, 'cache_hit': True, 'public_manifest': str(directory / 'runtime/public-manifest.json')},
                         'evidence': {'schemaVersion': 1, 'runId': manifest['runId'], 'cacheKey': construction_key,
                                      'publishedVersion': version, 'sourceCommit': commit, 'publishedCliSha256': inputs['runtime_archive_sha256'],
                                      'addonSha256': inputs['xpi_sha256'], 'publicManifest': 'public-manifest.json',
                                      'nodes': nodes, 'artifacts': []},
                         'cleanup': {'status': 'passed', 'runId': manifest['runId'], 'privateStateRemoved': True,
                                     'containersRemoved': ['outer-direct', 'outer-ssh'], 'networksRemoved': [network], 'errors': [],
                                     'hostBaseline': {'unchanged': True, 'checks': {key: True for key in
                                         ('files', 'routes', 'interfaces', 'containers', 'resolver', 'sysctl', 'tailscale')}}}},
             'timings_seconds': {name: 1 for name in ('inputs', 'runtime_prepare', 'browser_prepare', 'nodes_start', 'desktop', 'android', 'collect_cleanup', 'total')}}
    path = directory / 'result.json'
    persist_fixture(path, proof)
    return path, proof


@pytest.fixture
def real_proof(tmp_path):
    version, commit = '0.26.1', '1' * 40
    release, executable_sha256 = runtime_archive_fixture(tmp_path / 'assets', version, commit)
    xpi = tmp_path / 'addon.xpi'
    with zipfile.ZipFile(xpi, 'w') as archive:
        archive.writestr('manifest.json', json.dumps({'version': version, 'experiment_apis': {'portmap': {}},
            'browser_specific_settings': {'gecko': {'id': 'firefox@portmap.ylang-ylang.github.io'}}}))
    source = {'commit': commit, 'dirty': False, 'payload_sha256': {'manifest.json': 'a' * 64},
              'harness_sha256': {name: evidence.file_sha256(ROOT / name) for name in
                                 (*evidence.BROWSER_SOURCE_INPUTS, *('tests/browser/real_node/' + name for name in SOURCE_FILES))}}
    path, proof = aggregate_fixture(tmp_path / 'proof', release=release, xpi=xpi, source=source,
                                    executable_sha256=executable_sha256,
                                    runtime_archive=tmp_path / 'assets' / release['assets'][0]['filename'])
    return path, proof, {'release': release, 'xpi_sha256': evidence.file_sha256(xpi), 'version': version,
                          'commit': commit, 'source': source, 'require_candidate': True}


@pytest.fixture
def golden_packages():
    return {'packages': ['docker.io=29.1.3'], 'packageTransports': [package_transport_fixture()]}


def test_complete_candidate_binds_four_cases_two_full_nodes_and_native_raw_records(real_proof):
    path, proof, required = real_proof
    assert evidence.validate_aggregate(path, **required) == proof
    assert len(proof['runtime']['evidence']['nodes']) == 2
    assert sum(len(receipt['scenarios']) for receipt in proof['platforms'].values()) == 4
    sources = proof['runtime']['cache']['sources']
    assert len(sources['sourceFiles']) == len(SOURCE_FILES) == 23
    assert sources['sourceFiles'] == {'tests/browser/real_node/' + name:
                                      evidence.file_sha256(ROOT / 'tests/browser/real_node' / name) for name in SOURCE_FILES}
    assert len(sources['constructionFiles']) == len(evidence.CONSTRUCTION_SOURCE_INPUTS) == 10
    assert sources['constructionFiles'] == {name: sources['sourceFiles'][name]
                                             for name in evidence.CONSTRUCTION_SOURCE_INPUTS}


def test_aggregate_accepts_exact_archive_fallback_without_replacing_snapshot_provenance(real_proof):
    path, proof, required = real_proof
    row = archive_package_transport(proof['runtime']['cache']['golden']['packageTransports'][0])
    persist_fixture(path, proof)
    assert evidence.validate_aggregate(path, **required) == proof
    assert row['primaryHttpStatus'] == 502 and row['alternateAttempts'] == 1
    assert row['provider'] == 'archive.ubuntu.com'


@pytest.mark.parametrize('field', ['primary', 'url'])
@pytest.mark.parametrize('value', [None, '', 123, ['https://snapshot.ubuntu.com/']])
def test_package_transports_reject_non_url_provenance(golden_packages, field, value):
    golden_packages['packageTransports'][0][field] = value
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('archive', [False, True], ids=['snapshot-labelled-archive', 'archive-labelled-snapshot'])
def test_package_transports_cannot_swap_provider_labels(golden_packages, archive):
    row = golden_packages['packageTransports'][0]
    if archive:
        archive_package_transport(row)
        row['provider'] = 'snapshot.ubuntu.com'
    else:
        row['provider'] = 'archive.ubuntu.com'
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('missing', [True, False], ids=['missing', 'empty'])
def test_aggregate_requires_new_golden_package_transport_provenance(real_proof, missing):
    path, proof, required = real_proof
    golden = proof['runtime']['cache']['golden']
    if missing:
        del golden['packageTransports']
    else:
        golden['packageTransports'] = []
    persist_fixture(path, proof)
    with pytest.raises(ValueError, match='package transport provenance'):
        evidence.validate_aggregate(path, **required)


@pytest.mark.parametrize('architecture,suffix', [('amd64', ''), ('amd64', ':amd64'), ('all', ''), ('all', ':all')])
def test_package_transports_bind_actual_binary_package_architecture(architecture, suffix):
    row = package_transport_fixture(architecture=architecture)
    golden = {'packages': ['base-files=13ubuntu10.3', row['package'] + suffix + '=' + row['version']],
              'packageTransports': [row]}
    assert evidence._package_transports(golden, UBUNTU_SNAPSHOT) == [row]


@pytest.mark.parametrize('encoding', [':', '%3a', '%3A'])
@pytest.mark.parametrize('remote_epoch', [False, True], ids=['epoch-omitted-in-pool', 'epoch-retained-in-pool'])
def test_package_transports_accept_exact_epoch_version_without_equating_newer_packages(encoding, remote_epoch):
    row = package_transport_fixture(version='2:29.1.3-0ubuntu1~24.04.1+test')
    row['filename'] = row['filename'].replace('%3a', encoding)
    if remote_epoch:
        row['primary'] = row['primary'].replace('docker.io_29', 'docker.io_2%3a29')
        row['url'] = row['primary']
    golden = {'packages': ['docker.io:amd64=' + row['version']], 'packageTransports': [row]}
    assert evidence._package_transports(golden, UBUNTU_SNAPSHOT) == [row]


@pytest.mark.parametrize('status', [404, 410, 500, 502, 599])
def test_package_transports_accept_exactly_one_approved_availability_alternate(golden_packages, status):
    row = archive_package_transport(golden_packages['packageTransports'][0], status)
    assert evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT) == [row]
    assert row['primary'].startswith('https://snapshot.ubuntu.com/ubuntu/' + UBUNTU_SNAPSHOT + '/pool/')
    assert row['url'].startswith('https://archive.ubuntu.com/ubuntu/pool/')
    assert row['primaryHttpStatus'] == status and row['alternateAttempts'] == 1


def test_package_transports_accept_multiple_distinct_pinned_packages(golden_packages):
    row = package_transport_fixture(package='runc', version='1.2.8-0ubuntu1~24.04.1')
    golden_packages['packages'].append(row['package'] + '=' + row['version'])
    golden_packages['packageTransports'].append(row)
    assert evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT) == golden_packages['packageTransports']


@pytest.mark.parametrize('rows', [None, {}, 'receipt', [], [None], ['receipt'], [{}]])
def test_package_transports_reject_missing_or_malformed_rows(golden_packages, rows):
    golden_packages['packageTransports'] = rows
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('field', tuple(package_transport_fixture()))
def test_package_transports_require_every_public_provenance_field(golden_packages, field):
    del golden_packages['packageTransports'][0][field]
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


def test_package_transports_reject_extra_private_or_unapproved_provenance(golden_packages):
    golden_packages['packageTransports'][0]['cachePath'] = '/var/cache/private-package-cache'
    with pytest.raises(ValueError, match='exact public provenance fields'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('field,value', [
    ('package', 'docker'), ('package', 'docker.io:amd64'), ('package', None),
    ('version', '29.1.4'), ('version', '29.1.3\n'), ('version', None),
    ('architecture', 'arm64'), ('architecture', 'i386'), ('architecture', 'all'), ('architecture', True),
    ('sha256', 'a' * 32), ('sha256', 'a' * 63), ('sha256', 'g' * 64), ('sha256', 'A' * 64),
    ('sha256', 'SHA256:' + 'a' * 64), ('sha256', None),
    ('bytes', 0), ('bytes', -1), ('bytes', True), ('bytes', 1024.0), ('bytes', '1024'),
    ('provider', 'snapshot'), ('provider', 'archive'), ('provider', 'evil.example'), ('provider', None),
])
def test_package_transports_reject_unbound_identity_hash_size_or_provider(golden_packages, field, value):
    golden_packages['packageTransports'][0][field] = value
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('filename', [
    '../docker.io_29.1.3_amd64.deb', '/docker.io_29.1.3_amd64.deb',
    'dir/docker.io_29.1.3_amd64.deb', 'dir\\docker.io_29.1.3_amd64.deb',
    '%2e%2e%2fdocker.io_29.1.3_amd64.deb', 'docker.io_29%2f1.3_amd64.deb',
    'docker.io_29%5c1.3_amd64.deb', 'docker.io_29%001.3_amd64.deb',
    'docker.io_29%ff1.3_amd64.deb', 'docker.io_29%zz1.3_amd64.deb',
    'docker.io_2%253a29.1.3_amd64.deb', 'docker.io_29.1.3_amd64.deb\n',
    'docker.io_29.1.3_arm64.deb', 'docker.io_29.1.3_amd64.rpm',
    'docker.io_29.1.4_amd64.deb', 'other_29.1.3_amd64.deb', 'Docker.io_29.1.3_amd64.deb',
])
def test_package_transports_reject_unsafe_or_conflicting_destination_filenames(golden_packages, filename):
    golden_packages['packageTransports'][0]['filename'] = filename
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('packages', [
    None, [], {}, 'docker.io=29.1.3', [None], ['docker=29.1.3'], ['docker.io=29.1.4'],
    ['docker.io:arm64=29.1.3'], ['docker.io:all=29.1.3'], ['docker.io:amd64junk=29.1.3'],
    ['docker.io:amd64:extra=29.1.3'], ['docker.io=29.1.3=extra'], ['docker.io=29.1.3\n'],
    ['docker.io=29.1.3', 'docker.io=29.1.3'], ['docker.io=29.1.3', 'docker.io:amd64=29.1.3'],
    ['docker.io=29.1.3', 'docker.io=29.1.2'],
])
def test_package_transports_require_one_exact_actual_installed_identity(golden_packages, packages):
    golden_packages['packages'] = packages
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('version', ['29.1.4', '1:29.1.3'])
def test_package_transports_cannot_relabel_installed_version_with_self_consistent_blob_identity(golden_packages, version):
    golden_packages['packageTransports'] = [package_transport_fixture(version=version)]
    with pytest.raises(ValueError, match='actual installed package'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('collision', ['identical', 'encoded-filename-alias', 'newer-version', 'other-architecture',
                                       'conflicting-destination'])
def test_package_transports_reject_duplicate_or_conflicting_identities_and_filenames(golden_packages, collision):
    duplicate = copy.deepcopy(golden_packages['packageTransports'][0])
    if collision == 'encoded-filename-alias':
        duplicate['filename'] = duplicate['filename'].replace('docker.io', 'docker%2eio')
    elif collision == 'newer-version':
        duplicate = package_transport_fixture(version='29.1.4')
    elif collision == 'other-architecture':
        duplicate = package_transport_fixture(architecture='all')
    elif collision == 'conflicting-destination':
        duplicate['package'] = 'runc'
    golden_packages['packageTransports'].append(duplicate)
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('snapshot', [None, 20261118, '20261118', '2026-11-18T00:00:00Z',
                                      '20261118T000000Z/..', '20261118T000000Z?'])
def test_package_transports_require_exact_pinned_snapshot_identity(golden_packages, snapshot):
    with pytest.raises(ValueError, match='exact pinned Ubuntu snapshot'):
        evidence._package_transports(golden_packages, snapshot)


@pytest.mark.parametrize('old,new', [
    ('https://', 'http://'), ('https://', 'HTTPS://'),
    ('snapshot.ubuntu.com', 'archive.ubuntu.com'), ('snapshot.ubuntu.com', 'snapshot.ubuntu.com.evil'),
    ('snapshot.ubuntu.com', 'user@snapshot.ubuntu.com'), ('snapshot.ubuntu.com', 'user:password@snapshot.ubuntu.com'),
    ('snapshot.ubuntu.com', 'snapshot.ubuntu.com:443'), ('snapshot.ubuntu.com', '[snapshot.ubuntu.com'),
    ('/ubuntu/', '/debian/'), (UBUNTU_SNAPSHOT, '20261117T000000Z'), ('/pool/', '/dists/'),
    ('/main/', '/../'), ('/main/', '/./'), ('/main/', '/%2e%2e/'), ('/main/', '//'),
    ('/main/', '/ma%2fin/'), ('/main/', '/ma%5cin/'), ('/main/', '/ma%252fin/'),
    ('/main/', '/ma%zzin/'), ('/main/', '/ma%ffin/'), ('/main/', '/ma%00in/'), ('/main/', '/ma\tin/'),
    ('/main/', '/ma\\in/'), ('docker.io_29.1.3', 'runc_29.1.3'), ('29.1.3', '29.1.4'),
    ('_amd64.deb', '_all.deb'), ('.deb', '.rpm'),
])
def test_package_transports_reject_unapproved_primary_provider_or_exact_pool_path(golden_packages, old, new):
    row = golden_packages['packageTransports'][0]
    row['primary'] = row['primary'].replace(old, new)
    row['url'] = row['primary']
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('suffix', ['?token=secret', '#fragment', '?', '#'])
def test_package_transports_reject_even_empty_query_or_fragment_delimiters(golden_packages, suffix):
    row = golden_packages['packageTransports'][0]
    row['primary'] += suffix
    row['url'] = row['primary']
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('prefix', [' ', '\t', '\n'])
def test_package_transports_reject_url_parser_whitespace_normalization(golden_packages, prefix):
    row = golden_packages['packageTransports'][0]
    row['primary'] = prefix + row['primary']
    row['url'] = row['primary']
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


def test_package_transports_reject_different_remote_epoch_even_with_matching_destination(golden_packages):
    row = package_transport_fixture(version='2:29.1.3')
    row['primary'] = row['primary'].replace('docker.io_29', 'docker.io_3%3a29')
    row['url'] = row['primary']
    golden_packages.update(packages=['docker.io=2:29.1.3'], packageTransports=[row])
    with pytest.raises(ValueError, match='pool filename differs'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('old,new', [
    ('https://', 'http://'), ('archive.ubuntu.com', 'snapshot.ubuntu.com'),
    ('archive.ubuntu.com', 'archive.ubuntu.com.evil'), ('archive.ubuntu.com', 'user@archive.ubuntu.com'),
    ('archive.ubuntu.com', 'archive.ubuntu.com:443'), ('/ubuntu/pool/', '/ubuntu/' + UBUNTU_SNAPSHOT + '/pool/'),
    ('/main/', '/universe/'), ('/main/', '/../'), ('29.1.3', '29.1.4'), ('.deb', '.deb?'),
])
def test_package_transports_reject_alternate_provider_or_path_changes(golden_packages, old, new):
    row = archive_package_transport(golden_packages['packageTransports'][0])
    row['url'] = row['url'].replace(old, new)
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


def test_package_transports_reject_relabelled_snapshot_url(golden_packages):
    row = golden_packages['packageTransports'][0]
    row['url'] = row['url'].replace('/main/', '/universe/')
    with pytest.raises(ValueError, match='snapshot provenance'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('provider,status,attempts', [
    ('snapshot.ubuntu.com', 404, 0), ('snapshot.ubuntu.com', 410, 0), ('snapshot.ubuntu.com', 502, 0),
    ('snapshot.ubuntu.com', 200, 1), ('snapshot.ubuntu.com', '200', 0), ('snapshot.ubuntu.com', True, 0),
    ('snapshot.ubuntu.com', 200.0, 0), ('snapshot.ubuntu.com', 200, False),
    ('snapshot.ubuntu.com', 200, '0'), ('snapshot.ubuntu.com', 200, 0.0),
    ('archive.ubuntu.com', 200, 1), ('archive.ubuntu.com', 301, 1), ('archive.ubuntu.com', 400, 1),
    ('archive.ubuntu.com', 401, 1), ('archive.ubuntu.com', 403, 1), ('archive.ubuntu.com', 409, 1),
    ('archive.ubuntu.com', 411, 1), ('archive.ubuntu.com', 429, 1), ('archive.ubuntu.com', 499, 1),
    ('archive.ubuntu.com', 600, 1), ('archive.ubuntu.com', -1, 1), ('archive.ubuntu.com', '502', 1),
    ('archive.ubuntu.com', 502.0, 1), ('archive.ubuntu.com', 502, 0), ('archive.ubuntu.com', 502, 2),
    ('archive.ubuntu.com', 502, True), ('archive.ubuntu.com', 502, 1.0), ('archive.ubuntu.com', 502, '1'),
])
def test_package_transports_reject_other_statuses_and_any_extra_or_unproved_attempt(golden_packages, provider, status, attempts):
    row = golden_packages['packageTransports'][0]
    if provider == 'archive.ubuntu.com':
        archive_package_transport(row)
    row.update(primaryHttpStatus=status, alternateAttempts=attempts)
    with pytest.raises(ValueError, match='package transport'):
        evidence._package_transports(golden_packages, UBUNTU_SNAPSHOT)


@pytest.mark.parametrize('name', evidence.CONSTRUCTION_SOURCE_INPUTS)
@pytest.mark.parametrize('component', ['sourceFiles', 'constructionFiles'])
def test_aggregate_rejects_each_missing_cold_construction_source(real_proof, name, component):
    path, proof, required = real_proof
    del proof['runtime']['cache']['sources'][component][name]
    persist_fixture(path, proof)
    message = 'incomplete current harness/app source inputs' if component == 'sourceFiles' else 'construction inputs differ'
    with pytest.raises(ValueError, match=message):
        evidence.validate_aggregate(path, **required)


@pytest.mark.parametrize('identity', ['old-key', 'old-construction', 'relabelled-old-construction'])
def test_aggregate_rejects_old_or_relabelled_schema2_construction_identity(real_proof, identity):
    path, proof, required = real_proof
    cache = proof['runtime']['cache']
    current = cache['sources']['constructionFiles']
    new_inputs = {'tests/browser/real_node/' + name for name in
                  ('image_builder.py', 'runtime-image.json', 'runtime-setup.sh')}
    legacy = {name: value for name, value in current.items() if name not in new_inputs}
    old_key = hashlib.sha256(json.dumps({'schema': 2, 'constructionFiles': legacy},
                                      sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert len(legacy) == 7 and len(current) == 10 and old_key != cache['cache_key']
    if identity != 'old-key':
        cache['sources']['constructionFiles'] = legacy
    if identity != 'relabelled-old-construction':
        cache['cache_key'] = old_key
    persist_fixture(path, proof)
    message = 'schema-2 construction inputs' if identity == 'old-key' else 'construction inputs differ'
    with pytest.raises(ValueError, match=message):
        evidence.validate_aggregate(path, **required)


@pytest.mark.parametrize('components,value', [
    (('failures',), [{'stage': 'runtime_cleanup', 'message': 'cleanup failed'}]),
    (('inputs', 'runtime_archive_sha256'), 'f' * 64),
    (('runtime', 'evidence', 'nodes'), []),
    (('platforms', 'android', 'scenarios'), []),
    (('platforms', 'desktop', 'scenarios', 0, 'status'), 'failed'),
    (('platforms', 'desktop', 'scenarios', 0, 'application', 'interaction', 'websocket', 'nonce'), 'stale-nonce'),
    (('platforms', 'android', 'scenarios', 1, 'application', 'defaultBackendHost'), 'frontend.ssh-unit.portmap'),
    (('platforms', 'android', 'scenarios', 1, 'application', 'endpointPreserveHost'), True),
    (('platforms', 'desktop', 'scenarios', 0, 'catalog', 'ingressBoundary', 'connectionsStatus'), 200),
    (('platforms', 'android', 'profile', 'discarded'), False),
    (('platforms', 'desktop', 'profile', 'initialConnectionsEmpty'), False),
    (('platforms', 'desktop', 'harness_sha256'), 'f' * 64),
    (('platforms', 'desktop', 'runtime_archive_sha256'), 'f' * 64),
    (('runtime', 'evidence', 'nodes', 0, 'guest', 'cli', 'archiveSha256'), 'f' * 64),
    (('runtime', 'evidence', 'nodes', 0, 'guest', 'processes'), []),
    (('runtime', 'evidence', 'nodes', 0, 'statusCheck', 'agentExecutableSha256'), 'f' * 64),
    (('runtime', 'evidence', 'nodes', 0, 'catalog', 'public', 'services'), []),
    (('runtime', 'evidence', 'nodes', 0, 'appMarkers'), []),
    (('runtime', 'evidence', 'nodes', 0, 'gatewayMarkers'), []),
    (('runtime', 'evidence', 'nodes', 0, 'gatewayMarkers', 0, 'request', 'complete'), False),
    (('runtime', 'evidence', 'nodes', 0, 'gatewayMarkers', 0, 'request', 'body_bytes'), 1),
    (('runtime', 'evidence', 'nodes', 0, 'gatewayMarkers', 0, 'flow', 'syn_observed'), False),
    (('runtime', 'evidence', 'nodes', 0, 'gatewayMarkers', 0, 'flow', 'destination_port'), 5173),
    (('runtime', 'evidence', 'nodes', 0, 'gatewayMarkers', 0, 'path'), '/api/echo?private=secret'),
    (('runtime', 'evidence', 'nodes', 1, 'guest', 'openssh', 'forwardings'), []),
    (('runtime', 'cleanup', 'status'), 'failed'),
    (('runtime', 'cleanup', 'privateStateRemoved'), False),
    (('runtime', 'cleanup', 'containersRemoved'), []),
    (('runtime', 'cleanup', 'hostBaseline', 'unchanged'), False),
    (('runtime', 'cleanup', 'hostBaseline', 'checks', 'routes'), False),
    (('runtime', 'cache', 'cache_key'), 'f' * 64),
    (('runtime', 'cache', 'sources', 'sourceFiles'), {}),
    (('runtime', 'cache', 'sources', 'constructionFiles'), {}),
    (('runtime', 'cache', 'sources', 'constructionFiles', 'tests/browser/real_node/base_cache.py'), 'f' * 64),
    (('runtime', 'cache', 'sources', 'constructionFiles', 'tests/browser/real_node/app/src/main.js'), 'f' * 64),
    (('runtime', 'cache', 'golden', 'portmapInstalled'), True),
    (('runtime', 'cache', 'golden', 'appInstalled'), True),
    (('runtime', 'cache', 'golden', 'sshHostKeys'), 1),
    (('runtime', 'cache', 'golden', 'consumerAccounts'), 1),
    (('harness', 'commit'), 'f' * 40),
    (('harness', 'dirty'), True),
    (('inputs', 'mode'), 'release'),
])
def test_aggregate_rejects_semantic_failures_even_with_consistent_artifact_hashes(real_proof, components, value):
    path, proof, required = real_proof
    target = proof
    for component in components[:-1]:
        target = target[component]
    target[components[-1]] = value
    persist_fixture(path, proof)
    with pytest.raises(ValueError):
        evidence.validate_aggregate(path, **required)


def test_partial_platform_is_local_debug_only_and_never_publication_evidence(real_proof):
    path, proof, required = real_proof
    del proof['platforms']['android']
    persist_fixture(path, proof)
    with pytest.raises(ValueError, match='every required'):
        evidence.validate_aggregate(path, **required)
    local = {**required, 'source': None, 'require_candidate': False, 'required_platforms': ('desktop',)}
    assert evidence.validate_aggregate(path, **local)['status'] == 'passed'
    with pytest.raises(ValueError, match='both platforms'):
        evidence.validate_aggregate(path, **{**required, 'required_platforms': ('desktop',)})


def test_retained_pre_attachment_manifest_need_not_equal_post_attachment_manifest_bytes(real_proof):
    path, proof, required = real_proof
    after_attachment = copy.deepcopy(required['release'])
    after_attachment['firefox_experimental'] = {'xpi': {'sha256': required['xpi_sha256']}}
    assert evidence.validate_aggregate(path, **{**required, 'release': after_attachment})['status'] == 'passed'
    after_attachment['assets'][0]['sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='CLI hash'):
        evidence.validate_aggregate(path, **{**required, 'release': after_attachment})


def test_missing_or_modified_raw_artifact_invalidates_complete_root_inventory(real_proof):
    path, proof, required = real_proof
    raw = path.parent / 'desktop/raw/desktop-network.123'
    raw.write_text('changed after receipt')
    with pytest.raises(ValueError, match='artifact hashes/inventory'):
        evidence.validate_aggregate(path, **required)
    raw.unlink()
    with pytest.raises(ValueError, match='artifact hashes/inventory'):
        evidence.validate_aggregate(path, **required)


def test_summary_only_native_network_proof_is_rejected_after_rehashing(real_proof):
    path, proof, required = real_proof
    (path.parent / 'desktop/raw/desktop-network.123').write_text('connect summary says passed')
    persist_fixture(path, proof)
    with pytest.raises(ValueError, match='native browser recording'):
        evidence.validate_aggregate(path, **required)


def test_incomplete_child_artifact_inventory_is_rejected(real_proof):
    path, proof, required = real_proof
    proof['platforms']['desktop']['artifacts'].pop()
    persist_fixture(path, proof, index=False)
    with pytest.raises(ValueError, match='raw evidence inventory'):
        evidence.validate_aggregate(path, **required)


@pytest.mark.parametrize('escape', ['../external', '/absolute', 'desktop/../external', './external', 'desktop\\external'])
def test_artifact_path_cannot_escape_or_alias_public_root(real_proof, escape):
    path, proof, required = real_proof
    proof['artifacts'][escape] = 'f' * 64
    dump(path, proof)
    with pytest.raises(ValueError, match='artifact path'):
        evidence.validate_aggregate(path, **required)


def test_symlinked_public_artifacts_are_never_followed(real_proof, tmp_path):
    path, proof, required = real_proof
    private = tmp_path / 'private'
    private.write_text('never publish')
    (path.parent / 'private-link').symlink_to(private)
    with pytest.raises(ValueError, match='symlink'):
        evidence.validate_aggregate(path, **required)


def test_reused_profile_identity_is_rejected_even_with_fresh_case_nonces(real_proof):
    path, proof, required = real_proof
    proof['platforms']['android']['profile']['id'] = proof['platforms']['desktop']['profile']['id']
    persist_fixture(path, proof)
    with pytest.raises(ValueError, match='profiles or scenario nonces were reused'):
        evidence.validate_aggregate(path, **required)


def test_browser_container_cleanup_is_not_implied_by_passed_browser_receipt(real_proof):
    path, proof, required = real_proof
    dump(path.parent / 'desktop/container-cleanup.json', {'status': 'failed', 'removed': False, 'name': 'owned-desktop'})
    persist_fixture(path, proof)
    with pytest.raises(ValueError, match='container cleanup failed'):
        evidence.validate_aggregate(path, **required)


@pytest.mark.parametrize('index', [0, 1])
def test_guest_and_agent_self_consistent_fake_executable_hash_cannot_replace_actual_archive(real_proof, index):
    path, proof, required = real_proof
    node = proof['runtime']['evidence']['nodes'][index]
    node['guest']['cli']['executableSha256'] = 'f' * 64
    node['statusCheck']['agentExecutableSha256'] = 'f' * 64
    persist_fixture(path, proof)
    with pytest.raises(ValueError, match='guest executable differs'):
        evidence.validate_aggregate(path, **required)


@pytest.mark.parametrize('change', ['missing', 'bytes'])
def test_retained_runtime_archive_is_required_and_hash_bound_even_with_reindexed_artifacts(real_proof, change):
    path, proof, required = real_proof
    archive = path.parent / 'inputs/runtime.tar.gz'
    if change == 'missing':
        archive.unlink()
    else:
        archive.write_bytes(archive.read_bytes() + b'changed retained archive')
    persist_fixture(path, proof)
    with pytest.raises(ValueError):
        evidence.validate_aggregate(path, **required)


def test_archive_executable_digest_supports_internal_dependencies_without_filesystem_extraction(tmp_path, monkeypatch):
    release, executable_sha256 = runtime_archive_fixture(tmp_path, '0.26.1', '1' * 40)
    def forbidden(*args, **kwargs):
        pytest.fail('archive validation must never extract filesystem paths')
    monkeypatch.setattr(tarfile.TarFile, 'extract', forbidden)
    monkeypatch.setattr(tarfile.TarFile, 'extractall', forbidden)
    assert evidence._archive_executable_sha256(tmp_path / release['assets'][0]['filename']) == executable_sha256
    assert not (tmp_path / 'portmap').exists()
    assert not (tmp_path / '_internal').exists()


@pytest.mark.parametrize('shape', ['symlink', 'hardlink', 'directory', 'fifo', 'duplicate', 'noncanonical', 'empty', 'missing'])
def test_archive_executable_must_be_a_unique_exact_nonempty_regular_member(tmp_path, shape):
    path = tmp_path / 'runtime.tar.gz'
    payload = b'explicitly synthetic unit-test executable'
    with tarfile.open(path, 'w:gz') as archive:
        name = './portmap' if shape == 'noncanonical' else 'not-portmap' if shape == 'missing' else 'portmap'
        member = tarfile.TarInfo(name)
        member.mode = 0o755
        types = {'symlink': tarfile.SYMTYPE, 'hardlink': tarfile.LNKTYPE,
                 'directory': tarfile.DIRTYPE, 'fifo': tarfile.FIFOTYPE}
        if shape in types:
            member.type = types[shape]
            member.linkname = '../never-extract'
            archive.addfile(member)
        else:
            content = b'' if shape == 'empty' else payload
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
            if shape == 'duplicate':
                archive.addfile(member, io.BytesIO(content))
    with pytest.raises(ValueError):
        evidence._archive_executable_sha256(path)
    assert not (tmp_path / 'portmap').exists()


def test_unreadable_retained_archive_reports_validation_failure(tmp_path):
    path = tmp_path / 'runtime.tar.gz'
    path.write_bytes(b'not a gzip tar archive')
    with pytest.raises(ValueError):
        evidence._archive_executable_sha256(path)


@pytest.mark.parametrize('link_type', [113, 276, 1])
def test_native_gateway_reconstructs_split_post_and_redacts_every_payload_header_and_query(link_type):
    nonce, host = 'portmap-realnode-unit-split', 'frontend.unit.portmap'
    header, body = gateway_request(nonce, host, target='/api/echo?private=query-secret',
        body={'nonce': nonce, 'private': 'body-secret'},
        extra_headers=('Authorization: Basic auth-secret', 'Cookie: private=cookie-secret'))
    rows = gateway_capture_rows([
        gateway_packet(1000, flags=2, link_type=link_type),
        gateway_packet(1000, flags=2, link_type=link_type),
        gateway_packet(1001, header, link_type=link_type),
        gateway_packet(1001, header, link_type=link_type),
        gateway_packet(1001 + len(header), body, link_type=link_type),
        gateway_packet(1001 + len(header), body, link_type=link_type),
    ], link_type=link_type)
    assert evidence._gateway_requests(rows) == rows and len(rows) == 1
    row = rows[0]
    assert (row['nonce'], row['host'], row['method'], row['path']) == (nonce, host, 'POST', '/api/echo')
    assert row['flow']['initial_sequence'] == 1000 and row['flow']['syn_observed'] is True
    assert row['request'] == {'ordinal': 1, 'start_offset': 0, 'header_bytes': len(header),
        'body_bytes': len(body), 'end_offset': len(header) + len(body), 'complete': True, 'framing': 'content-length'}
    serialized = json.dumps(rows)
    assert all(secret not in serialized for secret in ('query-secret', 'body-secret', 'auth-secret', 'cookie-secret',
                                                      'Authorization', 'Cookie', 'payload', '?private'))


@pytest.mark.parametrize('initial_sequence', [1000, 0xfffffffe])
def test_native_gateway_reorders_segments_and_deduplicates_retransmissions(initial_sequence):
    header, body = gateway_request('portmap-realnode-unit-reordered', 'frontend.unit.portmap')
    start = initial_sequence + 1
    rows = gateway_capture_rows([
        gateway_packet(initial_sequence, flags=2),
        gateway_packet(start + len(header), body),
        gateway_packet(start + 31, header[31:]),
        gateway_packet(start, header[:31]),
        gateway_packet(start + 31, header[31:]),
        gateway_packet(start + len(header), body),
    ])
    assert len(evidence._gateway_requests(rows)) == 1
    assert rows[0]['request']['end_offset'] == len(header) + len(body)


@pytest.mark.parametrize('when', ['incomplete', 'already-complete'])
def test_native_gateway_conflicting_retransmission_invalidates_the_connection(when):
    header, body = gateway_request('portmap-realnode-unit-conflicting', 'frontend.unit.portmap')
    packets = [gateway_packet(1000, flags=2), gateway_packet(1001, header)]
    if when == 'already-complete':
        packets += [gateway_packet(1001 + len(header), body), gateway_packet(1001, b'X' + header[1:])]
    else:
        packets += [gateway_packet(1001 + len(header), body[:5]),
                    gateway_packet(1001 + len(header), b'X' + body[1:])]
    rows = gateway_capture_rows(packets)
    assert rows[-1]['type'] == 'native-gateway-flow-invalid'
    assert rows[-1]['reason'] == 'conflicting-tcp-overlap'
    assert evidence._gateway_requests(rows) == []


@pytest.mark.parametrize('mismatch', ['keepalive-request', 'different-flow'])
def test_aggregate_cannot_join_host_and_nonce_across_requests_or_flows(real_proof, mismatch):
    path, proof, required = real_proof
    node = proof['runtime']['evidence']['nodes'][0]
    original = node['gatewayMarkers'][0]
    header, body = gateway_request(original['nonce'], 'wrong-host.unit.portmap')
    other_header, other_body = gateway_request(original['nonce'], original['host'], body={'other': 'not-a-nonce'})
    if mismatch == 'keepalive-request':
        first = other_header + other_body
        packets = [gateway_packet(1000, flags=2), gateway_packet(1001, first),
                   gateway_packet(1001 + len(first), header), gateway_packet(1001 + len(first) + len(header), body)]
    else:
        packets = [gateway_packet(1000, flags=2), gateway_packet(1001, other_header),
                   gateway_packet(2000, flags=2, source_port=45001),
                   gateway_packet(2001, header, source_port=45001),
                   gateway_packet(2001 + len(header), body, source_port=45001)]
    observed = gateway_capture_rows(packets)
    assert len(evidence._gateway_requests(observed)) == 1
    assert observed[0]['host'] == 'wrong-host.unit.portmap'
    node['gatewayMarkers'] = observed + node['gatewayMarkers'][1:]
    persist_fixture(path, proof)
    with pytest.raises(ValueError):
        evidence.validate_aggregate(path, **required)


def test_native_gateway_needs_observed_syn_and_a_complete_request_body():
    header, body = gateway_request('portmap-realnode-unit-incomplete', 'frontend.unit.portmap')
    assert gateway_capture_rows([gateway_packet(1001, header + body)]) == []
    assert gateway_capture_rows([gateway_packet(1000, flags=2), gateway_packet(1001, header + body[:-1])]) == []


@pytest.mark.parametrize('headers', [('Content-Length: 1',), ('Transfer-Encoding: chunked',)])
def test_native_gateway_ambiguous_http_framing_fails_closed(headers):
    header, body = gateway_request('portmap-realnode-unit-framing', 'frontend.unit.portmap', extra_headers=headers)
    rows = gateway_capture_rows([gateway_packet(1000, flags=2), gateway_packet(1001, header + body)])
    assert rows[0]['reason'] == 'request-framing'
    assert evidence._gateway_requests(rows) == []


@pytest.mark.parametrize('limits,reason', [
    ({'max_header_bytes': 32}, 'header-limit'),
    ({'max_body_bytes': 1}, 'body-limit'),
    ({'max_stream_bytes': 64}, 'stream-limit'),
])
def test_native_gateway_request_memory_limits_fail_closed(limits, reason):
    observer = native_gateway.RequestObserver(**limits)
    header, body = gateway_request('portmap-realnode-unit-limits', 'frontend.unit.portmap')
    rows = gateway_capture_rows([gateway_packet(1000, flags=2), gateway_packet(1001, header + body)], observer=observer)
    assert rows[0]['reason'] == reason and evidence._gateway_requests(rows) == []
    assert not observer.flows


def test_native_gateway_flow_count_and_expiration_are_bounded():
    now = [0]
    observer = native_gateway.RequestObserver(max_flows=2, idle_seconds=1, clock=lambda: now[0])
    for port in (45000, 45001, 45002):
        gateway_capture_rows([gateway_packet(1000, source_port=port, flags=2)], observer=observer)
    assert len(observer.flows) == 2
    now[0] = 2
    gateway_capture_rows([gateway_packet(1001, b'not-a-request', source_port=45002)], observer=observer)
    assert not observer.flows


def test_native_gateway_live_pcap_short_reads_and_ready_boundary():
    header, body = gateway_request('portmap-realnode-unit-short-read', 'frontend.unit.portmap')
    packets = [gateway_packet(1000, flags=2), gateway_packet(1001, header), gateway_packet(1001 + len(header), body)]
    class ShortReads(io.BytesIO):
        def read(self, size):
            return super().read(min(size, 7))
    events = []
    native_gateway.observe(ShortReads(gateway_capture(packets)), lambda row: events.append(row),
                           ready=lambda: events.append('capture-ready'))
    assert events[0] == 'capture-ready'
    assert len(evidence._gateway_requests(events[1:])) == 1


@pytest.mark.parametrize('capture', [b'not-pcap', gateway_capture([])[:-1],
    struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 262144, 999)])
def test_native_gateway_never_announces_ready_for_invalid_capture(capture):
    ready = []
    with pytest.raises(native_gateway.CaptureError):
        native_gateway.observe(io.BytesIO(capture), lambda row: pytest.fail('invalid capture cannot emit proof'),
                               ready=lambda: ready.append(True))
    assert ready == []


def test_guest_waits_for_capture_readiness_without_sending_payload(tmp_path, monkeypatch):
    from tests.browser.real_node import node_bootstrap
    ready = tmp_path / 'observer-ready'
    monkeypatch.setattr(node_bootstrap, 'OBSERVER_READY', ready)
    clock = SimpleNamespace(monotonic=native_gateway.time.monotonic, sleep=lambda seconds: None)
    monkeypatch.setattr(node_bootstrap, 'time', clock)
    calls = []
    class Probe:
        def __enter__(self):
            ready.touch()
            return self
        def __exit__(self, *args):
            pass
    def connection(address, timeout):
        calls.append((address, timeout))
        return Probe()
    monkeypatch.setattr(node_bootstrap.socket, 'create_connection', connection)
    node_bootstrap.wait_for_observer(timeout=1)
    assert calls == [(('127.0.0.1', 80), 0.1)]


def test_guest_refuses_to_continue_without_live_observer(tmp_path, monkeypatch):
    from tests.browser.real_node import node_bootstrap
    monkeypatch.setattr(node_bootstrap, 'OBSERVER_READY', tmp_path / 'never-ready')
    ticks = iter((0, 0, 1))
    monkeypatch.setattr(node_bootstrap, 'time', SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda seconds: None))
    calls = []
    def refused(address, timeout):
        calls.append(address)
        raise ConnectionRefusedError()
    monkeypatch.setattr(node_bootstrap.socket, 'create_connection', refused)
    with pytest.raises(RuntimeError):
        node_bootstrap.wait_for_observer(timeout=0.5)
    assert calls == [('127.0.0.1', 80)]
