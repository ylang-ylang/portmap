"""Native network recording and strict, source-bound full-node release receipts.

These checks enforce the complete trusted workflow contract; JSON receipts are
not an authentication mechanism for arbitrary evidence supplied by outsiders.
"""
import base64
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import socket
import struct
import tarfile
import zipfile

from urllib.parse import parse_qs, urlsplit, urlunsplit

BROWSER_SOURCE_INPUTS = (
    'tests/browser/real_node/browser.py', 'tests/browser/real_node/android_runtime.py',
    'tests/browser/real_node/evidence.py', 'tests/browser/real_node/traced-geckodriver.sh',
    'tests/browser/runner.py', 'tests/browser/android.py', 'tests/browser/managed.py',
)

CONSTRUCTION_SOURCE_INPUTS = tuple('tests/browser/real_node/' + name for name in (
    'base_cache.py', 'runtime-inputs.json', 'Runtime.Dockerfile', 'base_bootstrap.py',
    'deps/Dockerfile', 'deps/package.json', 'deps/package-lock.json',
))


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_executable_sha256(path):
    """Read the sole exact regular CLI member without extracting any archive path."""
    digest = None
    try:
        with tarfile.open(path, mode='r|gz') as bundle:
            for member in bundle:
                if PurePosixPath(member.name).as_posix() != 'portmap':
                    continue
                _require(member.name == 'portmap' and member.isreg() and member.size > 0 and digest is None,
                         'runtime archive must contain one exact regular portmap executable')
                with bundle.extractfile(member) as executable:
                    digest = hashlib.file_digest(executable, 'sha256').hexdigest()
    except (OSError, EOFError, tarfile.TarError) as error:
        raise ValueError('real-node evidence: unreadable retained CLI archive') from error
    _require(digest is not None, 'retained CLI archive lacks its exact portmap executable')
    return digest


def artifacts(output):
    result = []
    for directory in ('raw', 'captures', 'screens'):
        for path in sorted((output / directory).rglob('*')):
            _require(not path.is_symlink(), 'symlink in browser evidence artifacts')
            if path.is_file():
                result.append({'path': path.relative_to(output).as_posix(),
                               'sha256': file_sha256(path), 'bytes': path.stat().st_size})
    return result


def desktop(raw):
    traces = sorted(raw.glob('desktop-network.*'))
    if not traces:
        raise AssertionError('No native Firefox connect/execve trace was produced')
    destinations = {}
    for trace in traces:
        for line in trace.read_text(errors='replace').splitlines():
            if not line.startswith('connect('):
                continue
            address = re.search(r'sin_addr=inet_addr\("([^"]+)"\)', line)
            port = re.search(r'sin_port=htons\((\d+)\)', line)
            if address and port:
                endpoint = address.group(1) + ':' + port.group(1)
                destinations[endpoint] = destinations.get(endpoint, 0) + 1
    return {'recorder': 'native-desktop-strace-connect-execve',
            'traceFiles': len(traces), 'tcpDestinations': destinations}


def android(raw):
    destinations = {}
    packets = 0
    with (raw / 'android-network.pcap').open('rb') as capture:
        header = capture.read(24)
        if len(header) != 24:
            raise AssertionError('Missing native Android emulator PCAP header')
        endian = {b'\xd4\xc3\xb2\xa1': '<', b'\xa1\xb2\xc3\xd4': '>',
                  b'\x4d\x3c\xb2\xa1': '<', b'\xa1\xb2\x3c\x4d': '>'}.get(header[:4])
        if not endian or struct.unpack(endian + 'I', header[20:24])[0] != 1:
            raise AssertionError('Expected a native Ethernet PCAP from the Android emulator')
        while entry := capture.read(16):
            if len(entry) != 16:
                raise AssertionError('Truncated Android PCAP packet record')
            size = struct.unpack(endian + 'IIII', entry)[2]
            frame = capture.read(size)
            if len(frame) != size or size < 14:
                raise AssertionError('Truncated Android PCAP Ethernet frame')
            packets += 1
            protocol, offset = int.from_bytes(frame[12:14], 'big'), 14
            if protocol == 0x8100:
                if size < 18:
                    raise AssertionError('Truncated Android PCAP VLAN frame')
                protocol, offset = int.from_bytes(frame[16:18], 'big'), 18
            if protocol == 0x0800 and len(frame) >= offset + 20 and frame[offset + 9] == 6:
                tcp = offset + (frame[offset] & 15) * 4
                if len(frame) >= tcp + 4:
                    address = socket.inet_ntoa(frame[offset + 16:offset + 20])
                    port = int.from_bytes(frame[tcp + 2:tcp + 4], 'big')
                    endpoint = address + ':' + str(port)
                    destinations[endpoint] = destinations.get(endpoint, 0) + 1
    return {'recorder': 'native-android-emulator-external-interface-pcap',
            'packets': packets, 'tcpDestinations': destinations}


def network_audit(platform, raw, cases):
    result = android(raw) if platform == 'android' else desktop(raw)
    destinations = result['tcpDestinations']
    direct = next(case for case in cases if case['mode'] == 'direct')
    ssh = next(case for case in cases if case['mode'] == 'ssh')
    if not destinations.get(direct['host'] + ':' + str(direct['httpPort'])):
        raise AssertionError('Native recorder lacks the direct node LAN HTTP connection')
    if not destinations.get(ssh['host'] + ':' + str(ssh['sshPort'])):
        raise AssertionError('Native recorder lacks the SSH node transport connection')
    fallback = destinations.get(ssh['host'] + ':' + str(ssh['httpPort']), 0)
    if fallback:
        raise AssertionError('The browser attempted direct HTTP to the SSH node')
    result.update(directNodeHttpObserved=True, sshNodeSshObserved=True,
                  sshNodeDirectHttpAttempts=0, guestLoopbackObservable=False,
                  scope='Browser external connections; guest ingress, SSH channels and Vite logs are correlated by the node controller')
    return result


def _require(condition, message):
    if not condition:
        raise ValueError('real-node evidence: ' + message)


def _object(value, label):
    _require(isinstance(value, dict), label + ' must be an object')
    return value


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result

    def finite(value):
        raise ValueError('real-node evidence: nonfinite JSON number: ' + value)

    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique, parse_constant=finite)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('real-node evidence: unreadable JSON: ' + str(path)) from error


def _relative(value):
    _require(_text(value) and '\\' not in value and '\0' not in value,
             'invalid public artifact path')
    path = PurePosixPath(value)
    _require(not path.is_absolute() and '..' not in path.parts and
             path.as_posix() == value and value not in ('.', ''),
             'artifact path is absolute, noncanonical or escapes the public root')
    return value


def public_artifact_hashes(output):
    """Hash every public file; never follow cache/private/profile symlinks."""
    output = Path(output)
    _require(output.is_dir() and not output.is_symlink(), 'invalid public artifact root')
    result = {}
    for path in sorted(output.rglob('*')):
        _require(not path.is_symlink(), 'symlink in public artifacts: ' + str(path))
        if path.is_dir():
            continue
        _require(path.is_file(), 'nonregular public artifact: ' + str(path))
        relative = path.relative_to(output).as_posix()
        if relative != 'result.json':
            result[relative] = file_sha256(path)
    return result


def _reference(root, hashes, reference, *, prefix='', nonempty=True):
    reference = _object(reference, 'artifact reference')
    relative = _relative(reference.get('path'))
    name = prefix + relative
    _require(name in hashes and reference.get('sha256') == hashes[name],
             'unbound artifact reference: ' + name)
    path = root / name
    size = path.stat().st_size
    _require(type(reference.get('bytes')) is int and reference['bytes'] == size and
             (not nonempty or size > 0), 'artifact byte size mismatch: ' + name)
    return path


def _stages(rows, required, label, *, window=None):
    _require(isinstance(rows, list) and rows, label + ' lacks executed stages')
    names = set()
    for row in rows:
        row = _object(row, label + ' stage')
        name = row.get('name')
        _require(_text(name) and name not in names and row.get('status') == 'passed',
                 label + ' has failed, duplicate or unexecuted stages')
        names.add(name)
        start, end = row.get('startedAt'), row.get('finishedAt')
        _require(_number(start) and start > 0 and _number(end) and end >= start and
                 _number(row.get('elapsedSeconds')), label + ' has invalid stage timings')
        if window:
            _require(window[0] <= start <= end <= window[1],
                     label + ' stage lies outside its fresh execution')
    _require(required <= names, label + ' lacks required executed stages')


def _screenshot(root, hashes, value, platform):
    value = _object(value, 'native screenshot')
    path = _reference(root, hashes, value, prefix=platform + '/')
    _require(path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), 'screenshot is not a native PNG')
    _require(value.get('kind') == 'native Firefox full-page', 'missing native browser screenshot identity')
    if platform == 'android':
        native = _reference(root, hashes, value.get('nativeScreen'), prefix=platform + '/')
        _require(native.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), 'missing Android native screen')


def _runtime_asset(release, version, commit):
    release = _object(release, 'runtime release manifest')
    _require(release.get('version') == version and release.get('source') == {
        'repository': 'ylang-ylang/portmap', 'tag': 'v' + version, 'commit': commit,
    }, 'runtime manifest version/source identity differs')
    records = release.get('assets')
    _require(isinstance(records, list), 'runtime manifest lacks actual CLI archive')
    records = [row for row in records if isinstance(row, dict) and
               row.get('os') == 'linux' and row.get('arch') == 'amd64']
    _require(len(records) == 1, 'runtime manifest needs exactly one linux-amd64 CLI archive')
    record = records[0]
    name = 'portmap-' + version + '-linux-amd64.tar.gz'
    _require(record.get('filename') == name and _digest(record.get('sha256')) and
             type(record.get('size')) is int and record['size'] > 0 and
             record.get('source_url') ==
             'https://github.com/ylang-ylang/portmap/releases/download/v' + version + '/' + name,
             'runtime CLI archive record is incomplete or noncanonical')
    return record


def _source(value, source, commit, require_candidate):
    value = _object(value, 'harness provenance')
    _require(isinstance(value.get('commit'), str) and
             re.fullmatch(r'[0-9a-f]{40}', value['commit']) and
             type(value.get('dirty')) is bool, 'harness lacks actual Git provenance')
    for field in ('payload_sha256', 'harness_sha256'):
        hashes = value.get(field)
        _require(isinstance(hashes, dict) and hashes and
                 all(_text(name) and _digest(digest) for name, digest in hashes.items()),
                 'harness lacks complete source hashes')
    if source is not None:
        _require(value == source, 'stale proof: harness source differs from the exact checkout')
    if require_candidate:
        _require(value['commit'] == commit and value['dirty'] is False,
                 'publication requires clean exact-source harness provenance')
    return value


def _browser(root, hashes, platform, receipt, inputs, harness):
    prefix = platform + '/'
    _require(_json(root / prefix / 'receipt.json') == receipt,
             platform + ' aggregate differs from retained browser receipt')
    receipt = _object(receipt, platform + ' receipt')
    _require(receipt.get('schemaVersion') == 1 and receipt.get('status') == 'passed' and
             receipt.get('platform') == platform and receipt.get('failures') == [],
             platform + ' did not pass every real browser stage')
    for observed, expected in (('xpi_sha256', 'xpi_sha256'), ('addon_version', 'version'),
                               ('source_commit', 'source_commit'),
                               ('runtime_archive_sha256', 'runtime_archive_sha256')):
        _require(receipt.get(observed) == inputs[expected], platform + ' tested different ' + observed)
    start, end = receipt.get('startedAt'), receipt.get('finishedAt')
    _require(_number(start) and start > 0 and _number(end) and end > start,
             platform + ' lacks fresh execution timing')
    required = {'inputs', 'environment', 'browser-start', 'addon-install', 'browser-close',
                'native-network-audit', 'profile-discard', 'artifact-index'}
    if platform == 'android':
        required |= {'android-boot', 'android-stop'}
    _stages(receipt.get('stages'), required, platform, window=(start, end))
    timings = receipt.get('timings')
    _require(isinstance(timings, dict) and _number(timings.get('totalSeconds')) and
             timings['totalSeconds'] > 0 and all(_number(value) for value in timings.values()),
             platform + ' lacks actual measured timings')
    profile = _object(receipt.get('profile'), platform + ' profile')
    _require(_text(profile.get('id')) and profile.get('fresh') is True and
             _digest(profile.get('nativeProfileIdSha256')) and
             profile.get('initialConnectionsEmpty') is True and profile.get('discarded') is True,
             platform + ' reused or failed to discard a fresh browser profile')
    identity = _object(profile.get('initialManagedIdentity'), 'initial browser identity')
    _require(identity.get('present') is False and identity.get('reference_present') is False,
             platform + ' started with cached browser credentials')
    toolchain = _object(receipt.get('toolchain'), platform + ' toolchain')
    _require(all(_text(toolchain.get(key)) for key in
                 ('seleniumVersion', 'geckodriver', 'browserVersion', 'platform', 'buildId')),
             platform + ' lacks actual native browser/toolchain identity')
    addon_observation = _object(toolchain.get('addon'), platform + ' actual addon observation')
    addon = _object(addon_observation.get('addon_state'), platform + ' actual addon state')
    _require(addon.get('active') is True and addon.get('appDisabled') is False and
             addon.get('temporary') is False, platform + ' addon was not permanently active')
    _require(addon_observation.get('signature_required') is False and
             addon_observation.get('experiments_preference') is True and
             addon_observation.get('browser_version') == toolchain['browserVersion'] and
             addon_observation.get('buildID') == toolchain['buildId'] and
             type(addon_observation.get('processID')) is int and addon_observation['processID'] > 0,
             platform + ' lacks native capability/toolchain admission evidence')
    browser_sources = {name: harness['harness_sha256'].get(name) for name in BROWSER_SOURCE_INPUTS}
    _require(all(_digest(value) for value in browser_sources.values()) and
             receipt.get('harnessFiles') == browser_sources and receipt.get('harness_sha256') ==
             hashlib.sha256(json.dumps(browser_sources, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
             platform + ' observed browser harness differs from source-bound inputs')
    if platform == 'android':
        android_identity = _object(toolchain.get('android'), 'Android toolchain')
        _require(_digest(android_identity.get('apk_sha256')) and android_identity.get('freshAvd') is True,
                 'Android APK identity or fresh native emulator is missing')
    cleanup_name = prefix + 'container-cleanup.json'
    _require(cleanup_name in hashes and prefix + 'launcher.log' in hashes,
             platform + ' lacks raw launcher/container cleanup evidence')
    _require({prefix + 'raw/container-hosts.txt', prefix + 'raw/container-resolv.conf'} <= hashes.keys(),
             platform + ' lacks raw native resolver/host environment observations')
    cleanup = _object(_json(root / cleanup_name), 'owned browser container cleanup')
    _require(cleanup.get('status') == 'passed' and cleanup.get('removed') is True and
             _text(cleanup.get('name')), platform + ' owned browser container cleanup failed')
    manifest = _object(receipt.get('node_manifest'), platform + ' node manifest')
    _require(manifest.get('ready') is True and manifest.get('frozen') is True,
             platform + ' real nodes were not ready and frozen')
    for observed, expected in (('addonSha256', 'xpi_sha256'), ('publishedVersion', 'version'),
                               ('sourceCommit', 'source_commit'),
                               ('publishedCliSha256', 'runtime_archive_sha256')):
        _require(manifest.get(observed) == inputs[expected], 'node manifest has stale ' + observed)
    cases = manifest.get('cases')
    _require(isinstance(cases, list) and len(cases) == 2 and
             {row.get('mode') for row in cases if isinstance(row, dict)} == {'direct', 'ssh'},
             platform + ' lacks independent actual direct and SSH nodes')
    scenarios = receipt.get('scenarios')
    _require(isinstance(scenarios, list) and len(scenarios) == 2 and
             {row.get('mode') for row in scenarios if isinstance(row, dict)} == {'direct', 'ssh'},
             platform + ' has partial or duplicate real-node scenarios')
    declared = receipt.get('artifacts')
    _require(isinstance(declared, list) and declared, platform + ' lacks raw browser evidence')
    observed_artifacts = set()
    for reference in declared:
        path = _reference(root, hashes, reference, prefix=prefix, nonempty=False)
        _require(reference['path'] not in observed_artifacts, 'duplicate browser artifact')
        observed_artifacts.add(reference['path'])
    expected_artifacts = {name[len(prefix):] for name in hashes if
                          any(name.startswith(prefix + directory + '/') for directory in ('raw', 'captures', 'screens'))}
    _require(observed_artifacts == expected_artifacts, platform + ' raw evidence inventory is incomplete')
    network = _object(receipt.get('network'), platform + ' network audit')
    _require(network.get('directNodeHttpObserved') is True and network.get('sshNodeSshObserved') is True and
             type(network.get('sshNodeDirectHttpAttempts')) is int and
             network['sshNodeDirectHttpAttempts'] == 0 and network.get('guestLoopbackObservable') is False,
             platform + ' native connection evidence failed or overclaims guest visibility')
    try:
        recorded_network = network_audit(platform, root / platform / 'raw', cases)
    except (OSError, AssertionError, KeyError, StopIteration) as error:
        raise ValueError('real-node evidence: native browser recording is incomplete') from error
    _require(recorded_network == network and _json(root / platform / 'captures/network-audit.json') == network,
             platform + ' native raw connection recording does not corroborate its receipt')
    nonces = set()
    for scenario in scenarios:
        mode = scenario['mode']
        case = next(row for row in cases if row['mode'] == mode)
        _scenario(root, hashes, platform, scenario, case, window=(start, end))
        nonce = scenario['application']['nonce']
        _require(nonce not in nonces, 'reused application nonce')
        nonces.add(nonce)
    return {'profile': profile['id'], 'native_profile': profile['nativeProfileIdSha256'],
            'nonces': nonces, 'manifest': manifest}


def _canonical_url(value):
    _require(_text(value), 'missing actual source URL')
    parts = urlsplit(value)
    _require(parts.scheme in ('http', 'https') and parts.hostname and
             not parts.username and not parts.password, 'invalid actual source URL')
    host = '[' + parts.hostname + ']' if ':' in parts.hostname else parts.hostname
    port = parts.port
    authority = host if port is None or (parts.scheme, port) in (('http', 80), ('https', 443)) else host + ':' + str(port)
    return urlunsplit((parts.scheme, authority, parts.path or '/', parts.query, parts.fragment))


def _scenario(root, hashes, platform, scenario, case, *, window):
    identifier = platform + '-' + case['mode']
    _require(scenario.get('id') == identifier and scenario.get('status') == 'passed' and
             scenario.get('domain') == case.get('domain') and scenario.get('projectUrl') == case.get('projectUrl'),
             identifier + ' is failed, partial or bound to another node')
    started, finished = scenario.get('startedAt'), scenario.get('finishedAt')
    _require(_number(started) and _number(finished) and
             window[0] <= started <= finished <= window[1] and _number(scenario.get('elapsedSeconds')),
             identifier + ' lacks actual scenario timing')
    _stages(scenario.get('stages'), {'onboarding', 'catalog', 'application', 'resources', 'screenshot'},
            identifier, window=(started, finished))
    _require(case.get('httpPort') == 80 and case.get('endpointPreserveHost') is False and
             case.get('expectedBackendHost') == '127.0.0.1:5173', 'default project Host semantics were replaced')
    url = urlsplit(_canonical_url(case.get('projectUrl')))
    _require(url.scheme == 'http' and url.hostname and url.hostname.endswith('.' + case.get('domain', '')) and
             case['domain'].endswith('.portmap') and not url.username and not url.password,
             'project URL does not retain its actual Portmap hostname')
    original_host = url.netloc
    onboarding = _object(scenario.get('onboarding'), 'actual options onboarding')
    _require(onboarding.get('actualOptionsUi') is True, 'node was not onboarded through actual options')
    saved = _object(onboarding.get('saved'), 'actual saved public connection')
    _require(saved.get('mode') == case['mode'] and saved.get('httpPort') == 80,
             'actual saved route differs from the real gateway')
    if case['mode'] == 'ssh':
        _require(onboarding.get('fingerprint') == case.get('hostKeySha256Hex') and
                 _digest(case.get('hostKeySha256Hex')) and
                 all(onboarding.get(key) is True for key in
                     ('explicitFingerprintComparison', 'oneTimePasswordBootstrap', 'passwordCleared')),
                 'native SSH host pin/bootstrap boundary was not exercised')
        ssh = _object(saved.get('ssh'), 'saved managed SSH connection')
        identity = _object(onboarding.get('managedIdentity'), 'actual managed SSH identity')
        _require(ssh.get('credentialFree') is True and ssh.get('authType') == 'managed' and
                 ssh.get('hostKeySha256Hex') == case['hostKeySha256Hex'] and
                 identity.get('present') is True and type(identity.get('rsaBits')) is int and
                 identity['rsaBits'] >= 3072 and identity.get('localPrivateCopy') is False,
                 'managed SSH authentication evidence is incomplete')
    catalog = _object(scenario.get('catalog'), 'actual Portmap catalog')
    private = case['mode'] == 'ssh'
    boundary = _object(catalog.get('ingressBoundary'), 'catalog ingress boundary')
    _require(_canonical_url(catalog.get('url')) == _canonical_url(case.get('catalogUrl')) and _text(catalog.get('title')) and
             _text(case.get('projectName')) and case['projectName'] in catalog.get('text', '') and
             catalog.get('projectHref') == case['projectUrl'] and catalog.get('projectLinkClicked') is True and
             any(isinstance(link, dict) and link.get('href') in (case['projectUrl'], _canonical_url(case['projectUrl']))
                 for link in catalog.get('links', [])) and
             catalog.get('controlRole') == ('private-ssh' if private else 'public-lan') and
             boundary.get('connectionsStatus') == (200 if private else 404) and
             boundary.get('machinesStatus') == 200 and boundary.get('canManageConnections') is private,
             'real catalog/project click or private/public control-plane boundary is missing')
    _screenshot(root, hashes, catalog.get('screenshot'), platform)
    _require(_json(root / platform / ('captures/catalog-' + case['mode'] + '.json')) ==
             {key: value for key, value in catalog.items() if key not in ('screenshot', 'projectLinkClicked')},
             'catalog receipt differs from retained native browser observation')
    application = _object(scenario.get('application'), 'actual Vite application')
    nonce = application.get('nonce')
    _require(_text(nonce) and re.fullmatch(re.escape('portmap-realnode-' + identifier + '-') + r'[0-9a-f]{32}', nonce),
             'application lacks a fresh scenario-bound nonce')
    _require(_text(case.get('expectedNode')) and application.get('node') == case['expectedNode'] and
             application.get('originalProjectUrl') == case['projectUrl'] and
             application.get('observedSourceHost') == original_host and
             application.get('originalForwardedHost') == original_host and
             application.get('defaultBackendHost') == '127.0.0.1:5173' and
             application.get('endpointPreserveHost') is False,
             'application node/source/default Host binding differs')
    for name in ('before', 'after'):
        document = _object(application.get(name), 'native application document')
        proof = _object(document.get('proof'), 'rendered application proof')
        _require(_canonical_url(document.get('url')) == _canonical_url(case['projectUrl']) and document.get('host') == original_host and
                 proof.get('ready') is True and proof.get('node') == case['expectedNode'] and
                 any(isinstance(image, dict) and image.get('complete') is True and
                     type(image.get('naturalWidth')) is int and image['naturalWidth'] > 0
                     for image in document.get('images', [])), 'actual application JS/image rendering is missing')
    interaction = _object(application.get('interaction'), 'Vite interaction')
    _require(interaction.get('nonce') == nonce and interaction.get('node') == case['expectedNode'] and
             all(interaction.get(key) is True for key in ('cssLoaded', 'jsBehavior', 'staticLoaded')),
             'genuine JavaScript/CSS/static interaction was not exercised')
    for channel in ('post', 'websocket'):
        response = _object(interaction.get(channel), channel + ' nonce roundtrip')
        _require(response.get('nonce') == nonce and response.get('node') == case['expectedNode'] and
                 response.get('host') == '127.0.0.1:5173' and response.get('forwardedHost') == original_host,
                 'HTTP/Vite WebSocket roundtrip or default backend/original forwarded Host differs')
    resources = _object(application.get('resources'), 'actual resource bytes')
    contract = _object(case.get('appContract'), 'genuine Vite app contract')
    expected = contract.get('resources')
    responses = resources.get('responses')
    _require(isinstance(expected, list) and isinstance(responses, list) and len(responses) == len(expected) and
             {'js', 'css', 'image', 'static'} <= {row.get('kind') for row in expected if isinstance(row, dict)},
             'genuine app resource contract is incomplete')
    seen = set()
    for response in responses:
        response = _object(response, 'actual resource response')
        key = (response.get('sourcePath'), response.get('kind'))
        matches = [row for row in expected if (row.get('path'), row.get('kind')) == key]
        _require(len(matches) == 1 and key not in seen, 'duplicate or unknown application resource')
        seen.add(key)
        resource = matches[0]
        location = urlsplit(response.get('url', ''))
        _require(response.get('status') == 200 and location.scheme == url.scheme and
                 location.netloc == original_host and location.path == resource['path'] and
                 parse_qs(location.query).get('browserProofNonce') == [nonce] and
                 _text(response.get('contentType')) and resource.get('contentType') in response['contentType'],
                 'actual browser resource response has wrong URL/status/content type')
        path = _reference(root, hashes, response.get('artifact'), prefix=platform + '/')
        if resource.get('sha256'):
            _require(_digest(resource['sha256']) and file_sha256(path) == resource['sha256'],
                     'actual resource bytes differ from genuine app source')
        else:
            _require(_text(resource.get('contains')) and resource['contains'].encode() in path.read_bytes(),
                     'Vite-transformed JavaScript/CSS bytes lack genuine source content')
    css = _object(resources.get('computedCss'), 'rendered CSS')
    _require(css.get('selector') == contract.get('cssSelector') and
             css.get('property') == contract.get('cssProperty') and
             _text(contract.get('cssExpected')) and
             css.get('expected') == css.get('observed') == contract['cssExpected'],
             'genuine imported CSS did not affect native rendered DOM')
    errors = application.get('console')
    _require(isinstance(errors, list) and all(isinstance(error, dict) and error.get('warning') is True
                                            for error in errors), 'actual application emitted browser script errors')
    _screenshot(root, hashes, application.get('screenshot'), platform)
    captured = _json(root / platform / ('captures/application-' + case['mode'] + '.json'))
    _require(captured == application, 'application receipt differs from retained raw observation')
    _require(_json(root / platform / ('captures/interaction-' + case['mode'] + '.json')) ==
             {'nonce': nonce, 'response': interaction}, 'HTTP/WS nonce differs from retained browser interaction')


def _host_keys(rows):
    _require(isinstance(rows, list) and rows, 'native guest SSH public keys are missing')
    fingerprints = set()
    for row in rows:
        row = _object(row, 'native guest SSH public key')
        fields = row.get('publicKey', '').split()
        _require(len(fields) == 2 and fields[0] == row.get('algorithm'), 'invalid native guest SSH key identity')
        try:
            blob = base64.b64decode(fields[1], validate=True)
        except ValueError as error:
            raise ValueError('real-node evidence: invalid native guest SSH public key bytes') from error
        digest = hashlib.sha256(blob).digest()
        _require(row.get('sha256Hex') == digest.hex() and
                 row.get('sha256Base64') == 'SHA256:' + base64.b64encode(digest).decode().rstrip('='),
                 'native guest host-key fingerprint does not match actual public key bytes')
        fingerprints.add(digest.hex())
    return fingerprints


def _gateway_flow(value):
    flow = _object(value, 'native ingress TCP connection')
    fields = ('source_address', 'source_port', 'destination_address', 'destination_port',
              'initial_sequence', 'syn_observed')
    _require(set(flow) == set(fields) and flow['syn_observed'] is True and
             all(type(flow[field]) is int and 0 < flow[field] <= 65535 for field in ('source_port', 'destination_port')) and
             flow['destination_port'] in (80, 8080, 5173) and
             type(flow['initial_sequence']) is int and 0 <= flow['initial_sequence'] <= 0xffffffff,
             'native ingress lacks a fresh SYN-anchored TCP connection')
    try:
        for field in ('source_address', 'destination_address'):
            _require(isinstance(flow[field], str) and socket.inet_ntoa(socket.inet_aton(flow[field])) == flow[field],
                     'native ingress has a noncanonical IPv4 connection identity')
    except OSError as error:
        raise ValueError('real-node evidence: invalid native ingress IPv4 connection') from error
    return tuple(flow[field] for field in fields)


def _gateway_requests(rows):
    """Accept only complete per-request observations, never joined field fragments."""
    invalid, requests, seen, ranges = set(), [], set(), {}
    for row in rows:
        row = _object(row, 'native ingress observation')
        flow = _gateway_flow(row.get('flow'))
        _require(type(row.get('schemaVersion')) is int and row['schemaVersion'] == 1 and
                 isinstance(row.get('at'), str) and re.fullmatch(
                     r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+00:00', row['at']),
                 'native ingress lacks sanitized capture time/schema')
        common = {'type', 'schemaVersion', 'at', 'flow'}
        if row.get('type') == 'native-gateway-flow-invalid':
            _require(set(row) == common | {'reason'} and row.get('reason') in
                     ('request-framing', 'stream-limit', 'conflicting-tcp-overlap',
                      'header-limit', 'body-limit', 'request-limit'),
                     'invalid native TCP flow lacks sanitized failure metadata')
            invalid.add(flow)
            continue
        _require(row.get('type') == 'native-gateway-http-request' and
                 set(row) == common | {'request', 'method', 'path', 'host', 'nonce'} and
                 row.get('method') == 'POST' and row.get('path') == '/api/echo' and
                 isinstance(row.get('host'), str) and len(row['host']) <= 260 and
                 re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]{1,5})?', row['host']) and
                 isinstance(row.get('nonce'), str) and re.fullmatch(r'portmap-realnode-[A-Za-z0-9_.:-]{1,240}', row['nonce']),
                 'native ingress is not a sanitized complete fixture POST observation')
        request = _object(row.get('request'), 'native ingress HTTP request boundary')
        numbers = ('ordinal', 'start_offset', 'header_bytes', 'body_bytes', 'end_offset')
        _require(set(request) == {*numbers, 'complete', 'framing'} and
                 all(type(request.get(field)) is int for field in numbers) and
                 request.get('complete') is True and request.get('framing') == 'content-length' and
                 0 < request['ordinal'] <= 256 and 0 <= request['start_offset'] < request['end_offset'] <= 256 * 1024 and
                 0 < request['header_bytes'] <= 16 * 1024 and 0 < request['body_bytes'] <= 64 * 1024 and
                 request['end_offset'] == request['start_offset'] + request['header_bytes'] + request['body_bytes'],
                 'native ingress lacks one bounded, completely reconstructed HTTP request')
        identity = (flow, request['ordinal'])
        _require(identity not in seen, 'native ingress reused an HTTP request identity')
        seen.add(identity)
        ranges.setdefault(flow, []).append((request['ordinal'], request['start_offset'], request['end_offset']))
        requests.append((flow, row))
    for sequence in ranges.values():
        last_ordinal, last_end = 0, 0
        for ordinal, start, end in sorted(sequence):
            _require(ordinal > last_ordinal and start >= last_end,
                     'native ingress HTTP request boundaries overlap or cross keepalive requests')
            last_ordinal, last_end = ordinal, end
    return [row for flow, row in requests if flow not in invalid]


def _runtime(root, hashes, runtime, inputs, browser_results, platforms, harness, executable_sha256):
    runtime = _object(runtime, 'real runtime')
    cache = _object(runtime.get('cache'), 'immutable runtime cache')
    _require(_digest(cache.get('cache_key')) and type(cache.get('cache_hit')) is bool and
             _number(cache.get('elapsed_seconds')) and _digest(cache.get('manifest_sha256')),
             'runtime cache lacks verified immutable identity/timing')
    cached = cache.get('artifacts')
    _require(isinstance(cached, list) and cached and
             all(isinstance(row, dict) and _text(row.get('name')) and _digest(row.get('sha256')) and
                 type(row.get('bytes')) is int and row['bytes'] > 0 for row in cached),
             'immutable runtime cache lacks verified byte hashes')
    sources = _object(cache.get('sources'), 'pinned runtime cache sources')
    _require(all(sources.get(key) for key in
                 ('os', 'images', 'ubuntuSnapshot', 'dockerVersion', 'nodeVersion', 'viteVersion', 'sourceFiles')),
             'runtime cache lacks pinned OS/image/dependency/source identities')
    runtime_sources = {name: value for name, value in harness['harness_sha256'].items()
                       if name.startswith('tests/browser/real_node/') and name not in BROWSER_SOURCE_INPUTS and
                       name != 'tests/browser/real_node/__init__.py'}
    _require(runtime_sources and sources['sourceFiles'] == runtime_sources,
             'runtime proof has different or incomplete current harness/app source inputs')
    _require(all(name in runtime_sources for name in CONSTRUCTION_SOURCE_INPUTS),
             'current runtime proof lacks a dependency-construction source input')
    construction_sources = {name: runtime_sources[name] for name in CONSTRUCTION_SOURCE_INPUTS}
    _require(sources.get('constructionFiles') == construction_sources,
             'immutable cache construction inputs differ from the current dependency builder')
    construction_key = hashlib.sha256(json.dumps({'schema': 2, 'constructionFiles': construction_sources},
                                                 sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    _require(cache['cache_key'] == construction_key, 'runtime cache key does not bind the schema-2 construction inputs')
    golden = _object(cache.get('golden'), 'dependency-only cache construction seal')
    _require(golden.get('construction') == 'dependencies-only-no-portmap-no-app-no-consumer-credentials' and
             golden.get('portmapInstalled') is False and golden.get('appInstalled') is False and
             all(type(golden.get(field)) is int and golden[field] == 0
                 for field in ('consumerAccounts', 'containers', 'sshHostKeys')) and
             all(golden.get(field) == sources[field] for field in ('dockerVersion', 'nodeVersion', 'viteVersion')) and
             _digest(golden.get('packagesSha256')) and golden.get('packages') and golden.get('images') and
             all(_text(golden.get(field)) for field in
                 ('dependencyImage', 'dependencyImageId', 'freeBlockSanitization',
                  'sshKeyGenerationPrevented', 'diskSanitization')),
             'cache is not a sealed dependency-only base without Portmap/apps/state/credentials')
    start = _object(runtime.get('start'), 'real runtime start')
    _require(_text(start.get('network')) and start['network'] not in ('host', 'bridge', 'none') and
             'private_manifest' not in start and type(start.get('cache_hit')) is bool and
             start['cache_hit'] is cache['cache_hit'], 'runtime start exposes private data or unowned network')
    evidence = _object(runtime.get('evidence'), 'actual full-node evidence')
    _require(evidence.get('schemaVersion') == 1 and _text(evidence.get('runId')) and
             evidence.get('cacheKey') == cache['cache_key'] and
             evidence.get('publishedVersion') == inputs['version'] and
             evidence.get('sourceCommit') == inputs['source_commit'] and
             evidence.get('publishedCliSha256') == inputs['runtime_archive_sha256'] and
             evidence.get('addonSha256') == inputs['xpi_sha256'],
             'real nodes did not run the exact supplied runtime archive/version/source')
    manifest_name = 'runtime/' + _relative(evidence.get('publicManifest'))
    _require(manifest_name in hashes, 'real node public manifest was not retained')
    manifest = _json(root / manifest_name)
    _require(all(result['manifest'] == manifest for result in browser_results.values()),
             'browsers did not use the same retained fresh full-node manifest')
    _require(manifest.get('runId') == evidence['runId'] and manifest.get('network') == start['network'] and
             manifest.get('cacheKey') == cache['cache_key'], 'real node manifest belongs to another run/cache/network')
    public_path = Path(start.get('public_manifest', ''))
    if public_path.is_absolute():
        _require(public_path == root / manifest_name, 'runtime start references an external public manifest')
    else:
        _require(public_path.as_posix() in (manifest_name, evidence['publicManifest']),
                 'runtime start references another public manifest')
    cases = {case['mode']: case for case in manifest['cases']}
    _require(len({case['expectedNode'] for case in cases.values()}) == 2 and
             len({case['host'] for case in cases.values()}) == 2,
             'direct and SSH scenarios did not use independent real nodes')
    nodes = evidence.get('nodes')
    _require(isinstance(nodes, list) and len(nodes) == 2 and
             {node.get('mode') for node in nodes if isinstance(node, dict)} == {'direct', 'ssh'},
             'missing actual independent real Portmap nodes')
    artifacts = evidence.get('artifacts')
    _require(isinstance(artifacts, list) and artifacts, 'runtime lacks public raw node evidence')
    retained = set()
    for reference in artifacts:
        _reference(root, hashes, reference, prefix='runtime/')
        _require(reference['path'] not in retained, 'duplicate runtime artifact')
        retained.add(reference['path'])
    _require({'node-direct.json', 'node-ssh.json'} <= retained, 'runtime raw node evidence is incomplete')
    machines, boots, containers = set(), set(), set()
    host_keys = set()
    for node in nodes:
        mode = node['mode']
        case = cases[mode]
        _require(node.get('name') == case['expectedNode'] and node.get('domain') == case['domain'] and
                 node.get('physicalHost') == case['host'] and
                 _json(root / ('runtime/node-' + mode + '.json')) == node,
                 'real node identity differs from retained native guest evidence')
        outer = _object(node.get('outerContainer'), 'owned outer container')
        _require(_text(outer.get('id')) and _text(outer.get('image')) and
                 str(outer.get('user', '')).split(':')[0] not in ('', '0', 'root') and
                 outer.get('privileged') is False and outer.get('networkMode') == start['network'] and
                 outer.get('capDrop') == ['ALL'] and 'no-new-privileges:true' in outer.get('securityOpt', []) and
                 outer.get('devices') == ['/dev/kvm'] and outer.get('portBindings') == {} and
                 set(outer.get('mountTargets', [])) == {'/base', '/work', '/inputs'},
                 'real runtime violated owned unprivileged KVM/network/mount boundaries')
        _require(outer['id'] not in containers, 'real nodes share an outer container')
        containers.add(outer['id'])
        guest = _object(node.get('guest'), 'actual guest')
        for field, seen in (('machineId', machines), ('bootId', boots)):
            identity = guest.get(field)
            _require(_text(identity) and identity not in seen, 'real nodes lack independent fresh ' + field)
            seen.add(identity)
        cli = _object(guest.get('cli'), 'actual injected guest CLI')
        _require(cli.get('version') == inputs['version'] and
                 cli.get('archiveSha256') == inputs['runtime_archive_sha256'] and
                 cli.get('executableSha256') == executable_sha256, 'guest executable differs from the retained CLI archive/version')
        engine = _object(guest.get('engine'), 'actual guest container engine')
        _require(_text(engine.get('version')) and _text(engine.get('id')), 'no actual full guest container engine')
        processes = guest.get('processes')
        _require(isinstance(processes, list) and processes and
                 all(isinstance(row, dict) and type(row.get('pid')) is int and row['pid'] > 0 and
                     _text(row.get('name')) for row in processes) and _text(guest.get('listeners')),
                 'no actual guest process/listener observations')
        ssh = _object(guest.get('openssh'), 'actual guest OpenSSH')
        _require(isinstance(ssh.get('hostKeys'), list) and ssh['hostKeys'] and
                 ssh.get('effective', {}).get('allowtcpforwarding') == 'yes',
                 'actual guest SSH host-key/forwarding configuration is missing')
        fingerprints = _host_keys(ssh['hostKeys'])
        _require(case.get('hostKeySha256Hex') in fingerprints and not fingerprints.intersection(host_keys),
                 'independent native guest keys do not match the browser-confirmed host pins')
        host_keys.update(fingerprints)
        if mode == 'ssh':
            _require(isinstance(ssh.get('forwardings'), list) and
                     any('direct-tcpip' in line or 'server_request_direct_tcpip' in line for line in ssh['forwardings']),
                     'native SSH direct-tcpip channel was not observed')
        status = _object(node.get('statusCheck'), 'actual Portmap status')
        _require(type(status.get('exitCode')) is int and status['exitCode'] == 0 and
                 all(status.get(field) is True for field in ('gatewayRunning', 'routesLoaded', 'dnsRunning')) and
                 status.get('hostDnsEnabled') is False and status.get('ingresses'),
                 'actual full Portmap status/ingress/default host-DNS boundary is incomplete')
        _require(status.get('agentRunning') is True and type(status.get('agentPid')) is int and
                 any(row['pid'] == status['agentPid'] for row in processes) and
                 status.get('agentExecutableSha256') == executable_sha256,
                 'actual Portmap agent process does not bind the supplied CLI executable')
        guest_containers = node.get('containers')
        _require(isinstance(guest_containers, list) and guest_containers and
                 all(isinstance(row, dict) and _text(row.get('id')) and _text(row.get('image')) and
                     row.get('state') == 'running' for row in guest_containers) and
                 any(row.get('composeService') == 'frontend' for row in guest_containers) and
                 any(isinstance(row.get('imageReference'), str) and 'traefik' in row['imageReference']
                     for row in guest_containers),
                 'real Portmap gateway/project containers were not observed')
        images = node.get('images')
        _require(isinstance(images, list) and images and
                 all(isinstance(row, dict) and _text(row.get('id')) and
                     isinstance(row.get('references'), list) and all(_text(name) for name in row['references']) and
                     isinstance(row.get('rootfsLayers'), list) and row['rootfsLayers'] for row in images) and
                 {row['image'] for row in guest_containers} <= {row['id'] for row in images},
                 'actual guest image identities/layers do not corroborate running container bytes')
        project = _object(node.get('project'), 'real default project')
        _require(project.get('repoName') == 'vite-app' and project.get('branch') == 'development' and
                 project.get('endpointPreserveHost') is False and project.get('url') == case['projectUrl'] and
                 _digest(project.get('endpointTomlSha256')) and _digest(project.get('composeSha256')),
                 'actual project source/default endpoint identity differs')
        catalog = _object(node.get('catalog'), 'real guest catalog')
        for scope, private in (('public', False), ('private', True)):
            observed = _object(catalog.get(scope), 'real ' + scope + ' catalog')
            _require(observed.get('registryStatus') == 200 and observed.get('machinesStatus') == 200 and
                     observed.get('canManage') is private and
                     observed.get('connectionsStatus') == (200 if private else 404),
                     'guest catalog violates private/public ingress control boundary')
            services = observed.get('services')
            _require(isinstance(services, list) and any(
                isinstance(service, dict) and service.get('repo_name') == 'vite-app' and
                service.get('branch') == 'development' and service.get('compose_service') == 'frontend' and
                any(isinstance(endpoint, dict) and endpoint.get('url') == case['projectUrl']
                    for endpoint in service.get('endpoints', [])) for service in services),
                'actual guest catalog does not advertise the genuine project service')
        app = node.get('appMarkers')
        gateway = node.get('gatewayMarkers')
        _require(isinstance(app, list) and isinstance(gateway, list), 'missing native app/ingress nonce evidence')
        gateway_requests = _gateway_requests(gateway)
        for platform in platforms:
            scenario = next(row for row in _json(root / platform / 'receipt.json')['scenarios'] if row['mode'] == mode)
            nonce, original_host = scenario['application']['nonce'], urlsplit(_canonical_url(case['projectUrl'])).netloc
            for kind in ('vite-http-post-nonce', 'vite-websocket-nonce'):
                _require(any(isinstance(marker, dict) and marker.get('type') == kind and
                             marker.get('nonce') == nonce and marker.get('node') == case['expectedNode'] and
                             marker.get('host') == '127.0.0.1:5173' and marker.get('forwardedHost') == original_host
                             for marker in app), 'native Vite POST/WebSocket logs lack browser nonce/default Host binding')
            _require(any(marker['nonce'] == nonce and marker['host'] == original_host and
                         marker['flow']['destination_port'] in (80, 8080) for marker in gateway_requests),
                     'native gateway ingress lacks a complete same-request browser nonce/original Host binding')
    cleanup = _object(runtime.get('cleanup'), 'owned runtime cleanup')
    baseline = _object(cleanup.get('hostBaseline'), 'host cleanup baseline')
    _require('runtime/cleanup.json' in hashes and _json(root / 'runtime/cleanup.json') == cleanup,
             'cleanup receipt differs from retained raw runtime evidence')
    _require(cleanup.get('status') == 'passed' and cleanup.get('runId') == evidence['runId'] and
             cleanup.get('privateStateRemoved') is True and cleanup.get('errors') == [] and
             baseline.get('unchanged') is True and isinstance(baseline.get('checks'), dict) and
             set(baseline['checks']) == {'files', 'routes', 'interfaces', 'containers', 'resolver', 'sysctl', 'tailscale'} and
             all(value is True for value in baseline['checks'].values()) and
             isinstance(cleanup.get('containersRemoved'), list) and
             containers <= set(cleanup['containersRemoved']) and
             isinstance(cleanup.get('networksRemoved'), list) and start['network'] in cleanup['networksRemoved'],
             'cleanup failed or lacks actual owned-resource removal/unchanged host baseline')


def validate_aggregate(path, *, release, xpi_sha256, version, commit, source=None,
                       require_candidate=False, required_platforms=('desktop', 'android')):
    """Validate a complete fresh real-node run; publication always requires both."""
    path = Path(path).absolute()
    _require(path.name == 'result.json' and path.is_file() and not path.is_symlink(),
             'aggregate must be a regular retained result.json')
    root = path.parent
    proof = _object(_json(path), 'aggregate')
    _require(proof.get('schemaVersion') == 1 and proof.get('kind') == 'portmap-real-node-browser' and
             proof.get('status') == 'passed' and proof.get('error') is None and proof.get('failures') == [],
             'aggregate did not pass the complete real-node run')
    _require(_digest(xpi_sha256) and isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+', version) and
             isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit), 'invalid required input identity')
    selected = tuple(required_platforms)
    _require(selected and len(selected) == len(set(selected)) and set(selected) <= {'desktop', 'android'},
             'invalid required real-node platforms')
    if require_candidate:
        _require(set(selected) == {'desktop', 'android'} and source is not None,
                 'publication requires both platforms and exact source provenance')
    inputs = _object(proof.get('inputs'), 'tested inputs')
    record = _runtime_asset(release, version, commit)
    _require(inputs.get('mode') in ('candidate', 'release') and inputs.get('version') == version and
             inputs.get('source_commit') == commit and inputs.get('xpi_sha256') == xpi_sha256 and
             inputs.get('runtime_archive_sha256') == record['sha256'] and
             _digest(inputs.get('runtime_manifest_sha256')), 'tested input identities differ or CLI hash is stale')
    if require_candidate:
        _require(inputs['mode'] == 'candidate', 'released-artifact proof cannot authorize candidate publication')
    hashes = proof.get('artifacts')
    _require(isinstance(hashes, dict) and hashes and
             all(_relative(name) and _digest(digest) for name, digest in hashes.items()) and
             hashes == public_artifact_hashes(root), 'public artifact hashes/inventory are incomplete or changed')
    _require({'inputs/release.json', 'inputs/addon.xpi', 'inputs/runtime.tar.gz'} <= hashes.keys() and
             hashes['inputs/release.json'] == inputs['runtime_manifest_sha256'] and
             hashes['inputs/addon.xpi'] == xpi_sha256 and
             hashes['inputs/runtime.tar.gz'] == inputs['runtime_archive_sha256'] and
             (root / 'inputs/runtime.tar.gz').stat().st_size == record['size'],
             'retained tested manifest/XPI/CLI archive bytes differ from the proof')
    executable_sha256 = _archive_executable_sha256(root / 'inputs/runtime.tar.gz')
    try:
        with zipfile.ZipFile(root / 'inputs/addon.xpi') as archive:
            addon = json.loads(archive.read('manifest.json'))
        _require(addon.get('version') == version and addon.get('browser_specific_settings', {}).get('gecko', {}).get('id') ==
                 'firefox@portmap.ylang-ylang.github.io' and addon.get('experiment_apis'),
                 'retained tested XPI has different version/extension/capability identity')
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        raise ValueError('real-node evidence: retained tested XPI is invalid') from error
    retained_release = _json(root / 'inputs/release.json')
    _require(_runtime_asset(retained_release, version, commit) == record,
             'retained pre-attachment runtime manifest has different CLI/source identity')
    # Firefox attachment intentionally changes release.json; compare the tested
    # runtime identity, not its pre-attachment byte hash to a published manifest.
    harness = _source(proof.get('harness'), source, commit, require_candidate)
    platforms = _object(proof.get('platforms'), 'browser platform evidence')
    _require(set(platforms) == set(selected), 'aggregate lacks every required actual browser platform')
    results = {}
    for platform in selected:
        _require(platform + '/receipt.json' in hashes, 'missing raw ' + platform + ' browser receipt')
        results[platform] = _browser(root, hashes, platform, platforms[platform], inputs, harness)
    _require(len({result['profile'] for result in results.values()}) == len(selected) and
             len({result['native_profile'] for result in results.values()}) == len(selected) and
             len(set().union(*(result['nonces'] for result in results.values()))) == 2 * len(selected),
             'browser profiles or scenario nonces were reused')
    browser_images = _object(proof.get('browser_images'), 'immutable browser image identities')
    for platform in selected:
        image = _object(browser_images.get(platform), platform + ' browser image')
        _require(image.get('platform') == platform and _digest(image.get('cache_key')) and
                 isinstance(image.get('image_id'), str) and re.fullmatch(r'sha256:[0-9a-f]{64}', image['image_id']) and
                 _digest(image.get('archive_sha256')) and type(image.get('cache_hit')) is bool,
                 'browser image cache lacks verified immutable bytes/identity')
    _runtime(root, hashes, proof.get('runtime'), inputs, results, selected, harness, executable_sha256)
    timings = proof.get('timings_seconds')
    _require(isinstance(timings, dict) and all(_number(value) for value in timings.values()) and
             _number(timings.get('total')) and timings['total'] > 0 and
             {'inputs', 'runtime_prepare', 'browser_prepare', 'nodes_start', 'collect_cleanup', *selected} <= timings.keys(),
             'aggregate lacks complete actual measured stage timings')
    return proof
