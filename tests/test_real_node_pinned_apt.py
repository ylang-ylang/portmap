"""Pinned APT blob verification using real Debian archives and isolated command edges."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import ssl
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit

import pytest

from tests.browser.real_node import base_bootstrap


SNAPSHOT = '20261118T000000Z'
PACKAGE = 'fixture-pinned'
VERSION = '1:2.3.4-0ubuntu1~24.04.1'
MARKER = 'PORTMAP_REAL_NODE_PACKAGE_TRANSPORT '
PLAN_KEYS = {'package', 'version', 'architecture', 'filename', 'primary', 'sha256', 'bytes'}
TRANSPORT_KEYS = PLAN_KEYS | {'provider', 'url', 'primaryHttpStatus', 'alternateAttempts'}


@pytest.fixture(autouse=True)
def closed_command_edges(monkeypatch):
    """Never let a missing test double execute host APT or contact a package server."""
    real_run = subprocess.run
    real_check_output = subprocess.check_output

    def guard_run(argv, *args, **kwargs):
        if argv and Path(argv[0]).name in ('apt-get', 'curl'):
            pytest.fail('Unexpected package command: ' + repr(argv))
        return real_run(argv, *args, **kwargs)

    def guard_output(argv, *args, **kwargs):
        if argv and Path(argv[0]).name == 'apt-get':
            pytest.fail('Unexpected host apt-get call: ' + repr(argv))
        return real_check_output(argv, *args, **kwargs)

    def no_network(url, maximum_size):
        pytest.fail('Unexpected package network call: ' + url)

    monkeypatch.setattr(subprocess, 'run', guard_run)
    monkeypatch.setattr(subprocess, 'check_output', guard_output)
    monkeypatch.setattr(base_bootstrap, '_open_package', no_network)


@pytest.fixture
def deb_factory(tmp_path):
    binary = shutil.which('dpkg-deb')
    if binary is None:
        pytest.skip('Real pinned-package verification fixtures require the dpkg-deb binary')
    built = {}

    def build(*, package=PACKAGE, version=VERSION, architecture='amd64'):
        identity = (package, version, architecture)
        if identity in built:
            return built[identity]
        root = tmp_path / ('deb-source-' + str(len(built)))
        control = root / 'DEBIAN'
        control.mkdir(parents=True)
        (control / 'control').write_text(
            f'Package: {package}\nVersion: {version}\nArchitecture: {architecture}\n'
            'Maintainer: Pinned APT Test <fixture@example.invalid>\n'
            'Description: Real tiny archive for immutable package transport verification\n')
        data = root / 'usr/share' / package / 'fixture.txt'
        data.parent.mkdir(parents=True)
        data.write_text('A real archive, not a mocked dpkg-deb result.\n')
        archive = tmp_path / ('fixture-' + str(len(built)) + '.deb')
        subprocess.run([binary, '--build', '--root-owner-group', str(root), str(archive)],
                       check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        body = archive.read_bytes()
        filename = f'{package}_{version.replace(":", "%3a")}_{architecture}.deb'
        remote_version = version.split(':', 1)[-1]
        remote_filename = f'{package}_{remote_version}_{architecture}.deb'
        primary = (f'https://snapshot.ubuntu.com/ubuntu/{SNAPSHOT}/pool/main/'
                   f'{package[0]}/{package}/{remote_filename}')
        row = {'package': package, 'version': version, 'architecture': architecture,
               'filename': filename, 'primary': primary,
               'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
        built[identity] = {'row': row, 'body': body, 'archive': archive}
        return built[identity]

    return build


@pytest.fixture
def deb(deb_factory):
    return deb_factory()


def uri_line(row, *, uri=None, filename=None, size=None, hash_field=None):
    return (f"'{row['primary'] if uri is None else uri}' "
            f"{row['filename'] if filename is None else filename} "
            f"{row['bytes'] if size is None else size} "
            f"{'SHA256:' + row['sha256'] if hash_field is None else hash_field}")


def alternate_url(row):
    path = unquote(urlsplit(row['primary']).path)
    pool = path.split('/pool/', 1)[1]
    return 'https://archive.ubuntu.com/ubuntu/pool/' + quote(pool, safe='/+~')


class PackageResponse(io.BytesIO):
    def __init__(self, body, url, *, status=200, headers=None, target=None):
        super().__init__(body)
        self.status = status
        self.url = url
        self.headers = {'Content-Length': str(len(body))} if headers is None else headers
        self.target = target
        self.delivered = 0
        self.read_sizes = []
        self.published_during_read = []

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.target is not None:
            self.published_during_read.append(self.target.exists())
        value = super().read(size)
        self.delivered += len(value)
        return value


def http_error(url, status):
    return HTTPError(url, status, 'fixture package HTTP failure', {}, io.BytesIO(b'Unavailable'))


def transport(row, *, status=200, alternate=False):
    return dict(row, provider='archive.ubuntu.com' if alternate else 'snapshot.ubuntu.com',
                url=alternate_url(row) if alternate else row['primary'],
                primaryHttpStatus=status, alternateAttempts=int(alternate))


def test_apt_print_uris_parses_real_archive_epoch_and_ignores_progress(deb):
    row = deb['row']
    text = ('Reading package lists... Done\nBuilding dependency tree... Done\n'
            'The following NEW packages will be installed:\n  fixture-pinned\n'
            + uri_line(row) + '\nNeed to get 1 archive.\n')
    assert base_bootstrap.package_plan(text, SNAPSHOT) == [row]
    assert set(row) == PLAN_KEYS
    assert '%3a' in row['filename']
    assert '1%3a' not in row['primary']


def test_uri_escapes_preserve_plus_tilde_and_epoch_identity(deb_factory):
    fixture = deb_factory(version='1:2.3.4+dfsg-0ubuntu1~24.04.1')
    row = fixture['row']
    encoded = row['primary'].replace('+', '%2b').replace('~', '%7e')
    result = base_bootstrap.package_plan(uri_line(row, uri=encoded), SNAPSHOT)
    assert len(result) == 1
    assert result[0]['package'] == row['package']
    assert result[0]['version'] == row['version']
    assert result[0]['architecture'] == 'amd64'
    assert result[0]['filename'] == row['filename']
    assert unquote(result[0]['primary']) == row['primary']
    assert result[0]['sha256'] == row['sha256']
    assert result[0]['bytes'] == row['bytes']
    assert '+' in unquote(urlsplit(result[0]['primary']).path)


def test_architecture_all_is_a_valid_planned_package(deb_factory):
    fixture = deb_factory(architecture='all')
    assert base_bootstrap.package_plan(uri_line(fixture['row']), SNAPSHOT) == [fixture['row']]


@pytest.mark.parametrize('snapshot', [
    '', 'latest', '20261118', '2026-11-18T000000Z', '20261118T000000',
    '20261118T000000Z/../20261118T000000Z',
])
def test_snapshot_must_be_an_exact_pinned_timestamp(deb, snapshot):
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.package_plan(uri_line(deb['row']), snapshot)


@pytest.mark.parametrize('hash_field', [
    '', 'MD5Sum:' + 'a' * 32, 'MD5:' + 'a' * 32, 'SHA1:' + 'a' * 40,
    'SHA256:', 'SHA256:' + 'a' * 63, 'SHA256:' + 'a' * 65, 'SHA256:' + 'g' * 64,
])
def test_plan_requires_a_complete_sha256_from_apt(deb, hash_field):
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.package_plan(uri_line(deb['row'], hash_field=hash_field), SNAPSHOT)


@pytest.mark.parametrize('size', ['', '0', '-1', '1.5', 'NaN', 'not-a-size'])
def test_plan_rejects_missing_or_invalid_signed_index_size(deb, size):
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.package_plan(uri_line(deb['row'], size=size), SNAPSHOT)


@pytest.mark.parametrize('mutation', [
    'http', 'archive', 'unapproved-host', 'host-suffix', 'credentials', 'port', 'query',
    'fragment', 'other-snapshot', 'not-pool', 'traversal', 'encoded-traversal',
    'dot-segment', 'encoded-separator', 'double-encoded-traversal', 'backslash',
    'not-deb', 'wrong-remote-package', 'wrong-remote-version', 'wrong-remote-architecture',
])
def test_plan_rejects_unapproved_or_ambiguous_snapshot_uris(deb, mutation):
    row = deb['row']
    primary = row['primary']
    bad = {
        'http': primary.replace('https:', 'http:', 1),
        'archive': primary.replace('snapshot.ubuntu.com', 'archive.ubuntu.com'),
        'unapproved-host': primary.replace('snapshot.ubuntu.com', 'packages.example.invalid'),
        'host-suffix': primary.replace('snapshot.ubuntu.com', 'snapshot.ubuntu.com.invalid'),
        'credentials': primary.replace('https://', 'https://user:secret@', 1),
        'port': primary.replace('snapshot.ubuntu.com', 'snapshot.ubuntu.com:443'),
        'query': primary + '?download=1',
        'fragment': primary + '#package',
        'other-snapshot': primary.replace(SNAPSHOT, '20261117T000000Z'),
        'not-pool': primary.replace('/pool/', '/dists/'),
        'traversal': primary.replace('/pool/main/', '/pool/../main/'),
        'encoded-traversal': primary.replace('/pool/main/', '/pool/%2e%2e/main/'),
        'dot-segment': primary.replace('/pool/main/', '/pool/./main/'),
        'encoded-separator': primary.replace('/pool/main/', '/pool/main%2f../'),
        'double-encoded-traversal': primary.replace('/pool/main/', '/pool/%252e%252e/main/'),
        'backslash': primary.replace('/pool/main/', '/pool/main\\../'),
        'not-deb': primary.removesuffix('.deb') + '.tar.gz',
        'wrong-remote-package': primary.rsplit('/', 1)[0] + '/other_2.3.4-0ubuntu1~24.04.1_amd64.deb',
        'wrong-remote-version': primary.replace('2.3.4-0ubuntu1', '2.3.5-0ubuntu1'),
        'wrong-remote-architecture': primary.replace('_amd64.deb', '_arm64.deb'),
    }[mutation]
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.package_plan(uri_line(row, uri=bad), SNAPSHOT)


@pytest.mark.parametrize('filename', [
    '../fixture-pinned_1%3a2.3.4-0ubuntu1~24.04.1_amd64.deb',
    '/tmp/fixture-pinned_1%3a2.3.4-0ubuntu1~24.04.1_amd64.deb',
    'subdir/fixture-pinned_1%3a2.3.4-0ubuntu1~24.04.1_amd64.deb',
    r'..\fixture-pinned_1%3a2.3.4-0ubuntu1~24.04.1_amd64.deb',
    'fixture-pinned%2fescape_1%3a2.3.4-0ubuntu1~24.04.1_amd64.deb',
    'fixture-pinned_1%253a2.3.4-0ubuntu1~24.04.1_amd64.deb',
    '.', '..', 'fixture-pinned.deb', 'fixture-pinned_2.3.4_amd64.tar.gz',
])
def test_destination_is_one_unambiguous_debian_archive_basename(deb, filename):
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.package_plan(uri_line(deb['row'], filename=filename), SNAPSHOT)


@pytest.mark.parametrize('architecture', ['arm64', 'i386', 'source', 'AMD64'])
def test_plan_rejects_unsupported_architecture(deb, architecture):
    row = dict(deb['row'])
    row['filename'] = row['filename'].replace('_amd64.deb', f'_{architecture}.deb')
    row['primary'] = row['primary'].replace('_amd64.deb', f'_{architecture}.deb')
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.package_plan(uri_line(row), SNAPSHOT)


@pytest.mark.parametrize('conflict', ['duplicate', 'hash', 'size', 'uri', 'package-version'])
def test_plan_rejects_duplicate_or_conflicting_package_names(deb, conflict):
    first = deb['row']
    second = dict(first)
    if conflict == 'hash':
        second['sha256'] = '0' * 64
    elif conflict == 'size':
        second['bytes'] += 1
    elif conflict == 'uri':
        second['primary'] = second['primary'].replace('/pool/main/', '/pool/universe/')
    elif conflict == 'package-version':
        second['filename'] = second['filename'].replace('2.3.4', '2.3.5')
        second['primary'] = second['primary'].replace('2.3.4', '2.3.5')
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.package_plan(uri_line(first) + '\n' + uri_line(second), SNAPSHOT)


def test_malformed_quoted_uri_is_not_silently_ignored(deb):
    line = uri_line(deb['row'])
    malformed = [line.replace("' ", ' ', 1), line.split("' ", 1)[0] + "'", line + ' extra-token']
    for text in malformed:
        with pytest.raises(base_bootstrap.PackageBlobError):
            base_bootstrap.package_plan(text, SNAPSHOT)


@pytest.mark.parametrize('headers', [None, {}, {'Content-Encoding': 'identity'}])
@pytest.mark.parametrize('encoded_response_url', [False, True])
def test_fetch_publishes_only_the_real_verified_deb(
        monkeypatch, tmp_path, deb, headers, encoded_response_url):
    row = deb['row']
    archives = tmp_path / 'archives'
    archives.mkdir()
    destination = archives / row['filename']
    response_url = row['primary'].replace('~', '%7E') if encoded_response_url else row['primary']
    response = PackageResponse(deb['body'], response_url, headers=headers, target=destination)
    attempts = []

    def open_package(url, maximum_size):
        assert maximum_size == row['bytes']
        attempts.append(url)
        return response

    monkeypatch.setattr(base_bootstrap, '_open_package', open_package)
    base_bootstrap._fetch_package(row['primary'], row, destination)
    assert attempts == [row['primary']]
    assert destination.read_bytes() == deb['body']
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == row['sha256']
    assert sorted(path.name for path in archives.iterdir()) == [row['filename']]
    assert response.closed
    assert not any(response.published_during_read)


@pytest.mark.parametrize('failure', [
    'truncated', 'oversized', 'tampered', 'sha256', 'signed-size-small', 'signed-size-large',
    'not-deb', 'package', 'version', 'architecture', 'length-mismatch', 'length-nonnumeric',
    'content-encoding', 'response-206', 'redirect-host', 'redirect-path', 'redirect-http',
    'redirect-credentials', 'redirect-port', 'redirect-query', 'redirect-fragment',
])
def test_verification_or_header_failure_never_publishes_or_uses_archive_fallback(
        monkeypatch, tmp_path, deb, deb_factory, failure):
    row = dict(deb['row'])
    body = deb['body']
    headers = {}
    status = 200
    response_url = row['primary']
    if failure == 'truncated':
        body = body[:-1]
    elif failure == 'oversized':
        body += b'excess bytes' * 65536
    elif failure == 'tampered':
        body = bytes([body[0] ^ 1]) + body[1:]
    elif failure == 'sha256':
        row['sha256'] = '0' * 64
    elif failure == 'signed-size-small':
        row['bytes'] -= 1
    elif failure == 'signed-size-large':
        row['bytes'] += 1
    elif failure == 'not-deb':
        body = b'Not a Debian archive, despite a matching signed hash and size.\n'
        row.update(sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
    elif failure in ('package', 'version', 'architecture'):
        changes = {'package': {'package': 'fixture-impostor'},
                   'version': {'version': '1:2.3.5-0ubuntu1~24.04.1'},
                   'architecture': {'architecture': 'all'}}[failure]
        body = deb_factory(**changes)['body']
        row.update(sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
    elif failure == 'length-mismatch':
        headers = {'Content-Length': str(len(body) + 1)}
    elif failure == 'length-nonnumeric':
        headers = {'Content-Length': 'not-decimal'}
    elif failure == 'content-encoding':
        headers = {'Content-Encoding': 'gzip'}
    elif failure == 'response-206':
        status = 206
    elif failure == 'redirect-host':
        response_url = row['primary'].replace('snapshot.ubuntu.com', 'archive.ubuntu.com')
    elif failure == 'redirect-path':
        response_url = row['primary'].replace('2.3.4', '2.3.5')
    elif failure == 'redirect-http':
        response_url = row['primary'].replace('https:', 'http:', 1)
    elif failure == 'redirect-credentials':
        response_url = row['primary'].replace('https://', 'https://user:secret@', 1)
    elif failure == 'redirect-port':
        response_url = row['primary'].replace('snapshot.ubuntu.com', 'snapshot.ubuntu.com:443')
    elif failure == 'redirect-query':
        response_url = row['primary'] + '?download=1'
    elif failure == 'redirect-fragment':
        response_url = row['primary'] + '#package'
    archives = tmp_path / 'archives'
    archives.mkdir()
    destination = archives / row['filename']
    response = PackageResponse(body, response_url, status=status, headers=headers, target=destination)
    attempts = []

    def open_package(url, maximum_size):
        assert maximum_size == row['bytes']
        attempts.append(url)
        return response

    monkeypatch.setattr(base_bootstrap, '_open_package', open_package)
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.prefetch_packages([row], archives)
    assert attempts == [row['primary']]
    assert list(archives.iterdir()) == []
    assert not any(response.published_during_read)
    assert response.closed
    if failure == 'oversized':
        assert response.delivered <= row['bytes'] + 1


def test_fetch_preserves_the_original_http_error_for_availability_classification(monkeypatch, tmp_path, deb):
    row = deb['row']
    original = http_error(row['primary'], 502)

    def open_package(url, maximum_size):
        assert maximum_size == row['bytes']
        assert url == row['primary']
        raise original

    monkeypatch.setattr(base_bootstrap, '_open_package', open_package)
    destination = tmp_path / row['filename']
    with pytest.raises(HTTPError) as caught:
        base_bootstrap._fetch_package(row['primary'], row, destination)
    assert caught.value is original
    assert not destination.exists()


def test_primary_success_has_exact_public_provenance_and_one_get(monkeypatch, tmp_path, deb):
    row = deb['row']
    attempts = []

    def open_package(url, maximum_size):
        assert maximum_size == row['bytes']
        attempts.append(url)
        return PackageResponse(deb['body'], url)

    monkeypatch.setattr(base_bootstrap, '_open_package', open_package)
    archives = tmp_path / 'archives'
    archives.mkdir()
    result = base_bootstrap.prefetch_packages([row], archives)
    assert result == [transport(row)]
    assert set(result[0]) == TRANSPORT_KEYS
    assert attempts == [row['primary']]
    assert (archives / row['filename']).read_bytes() == deb['body']
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize('status', [404, 410, 500, 502, 503, 599])
def test_only_unavailable_primary_uses_one_exact_official_alternate_with_provenance(
        monkeypatch, tmp_path, deb, capsys, status):
    row = deb['row']
    expected_alternate = alternate_url(row)
    original = http_error(row['primary'], status)
    attempts = []

    def open_package(url, maximum_size):
        assert maximum_size == row['bytes']
        attempts.append(url)
        if len(attempts) == 1:
            assert url == row['primary']
            raise original
        assert len(attempts) == 2
        assert url == expected_alternate
        return PackageResponse(deb['body'], url)

    monkeypatch.setattr(base_bootstrap, '_open_package', open_package)
    archives = tmp_path / 'archives'
    archives.mkdir()
    result = base_bootstrap.prefetch_packages([row], archives)
    assert attempts == [row['primary'], expected_alternate]
    assert result == [transport(row, status=status, alternate=True)]
    assert set(result[0]) == TRANSPORT_KEYS
    assert result[0]['primary'] == original.url
    assert result[0]['primaryHttpStatus'] == original.code
    assert (archives / row['filename']).read_bytes() == deb['body']
    markers = [json.loads(line[len(MARKER):]) for line in capsys.readouterr().out.splitlines()
               if line.startswith(MARKER)]
    assert markers == result
    assert str(tmp_path) not in json.dumps(result)


def test_encoded_plus_and_tilde_keep_the_same_official_pool_object_on_alternate(
        monkeypatch, tmp_path, deb_factory):
    fixture = deb_factory(version='1:2.3.4+dfsg-0ubuntu1~24.04.1')
    row = dict(fixture['row'])
    row['primary'] = row['primary'].replace('+', '%2B').replace('~', '%7E')
    attempts = []

    def open_package(url, maximum_size):
        assert maximum_size == row['bytes']
        attempts.append(url)
        if len(attempts) == 1:
            raise http_error(url, 502)
        parts = urlsplit(url)
        assert parts.scheme == 'https'
        assert parts.netloc == 'archive.ubuntu.com'
        assert not parts.query and not parts.fragment
        assert unquote(parts.path) == urlsplit(fixture['row']['primary']).path.replace('/' + SNAPSHOT, '', 1)
        return PackageResponse(fixture['body'], url)

    monkeypatch.setattr(base_bootstrap, '_open_package', open_package)
    archives = tmp_path / 'archives'
    archives.mkdir()
    result = base_bootstrap.prefetch_packages([row], archives)
    assert len(attempts) == 2
    assert result[0]['version'] == fixture['row']['version']
    assert result[0]['primaryHttpStatus'] == 502
    assert result[0]['alternateAttempts'] == 1
    assert (archives / row['filename']).read_bytes() == fixture['body']


@pytest.mark.parametrize('failure', [404, 502, 'tls', 'hash'])
def test_alternate_failure_never_causes_a_second_alternate_attempt(monkeypatch, tmp_path, deb, failure):
    row = deb['row']
    attempts = []

    def open_package(url, maximum_size):
        assert maximum_size == row['bytes']
        attempts.append(url)
        if len(attempts) == 1:
            raise http_error(url, 502)
        assert url == alternate_url(row)
        if isinstance(failure, int):
            raise http_error(url, failure)
        if failure == 'tls':
            raise URLError(ssl.SSLCertVerificationError('fixture certificate verification failed'))
        return PackageResponse(bytes([deb['body'][0] ^ 1]) + deb['body'][1:], url)

    monkeypatch.setattr(base_bootstrap, '_open_package', open_package)
    archives = tmp_path / 'archives'
    archives.mkdir()
    with pytest.raises((base_bootstrap.PackageBlobError, HTTPError)):
        base_bootstrap.prefetch_packages([row], archives)
    assert attempts == [row['primary'], alternate_url(row)]
    assert list(archives.iterdir()) == []


@pytest.mark.parametrize('failure', [
    400, 401, 403, 301, 302, 303, 307, 308, 429, 600, 'tls', 'network',
])
def test_auth_redirect_tls_and_other_nonavailability_failures_never_fall_back(
        monkeypatch, tmp_path, deb, failure):
    row = deb['row']
    attempts = []

    def open_package(url, maximum_size):
        assert maximum_size == row['bytes']
        attempts.append(url)
        if isinstance(failure, int):
            raise http_error(url, failure)
        if failure == 'tls':
            raise URLError(ssl.SSLCertVerificationError('fixture certificate verification failed'))
        raise URLError('fixture connection refused')

    monkeypatch.setattr(base_bootstrap, '_open_package', open_package)
    archives = tmp_path / 'archives'
    archives.mkdir()
    expected = HTTPError if isinstance(failure, int) else base_bootstrap.PackageBlobError
    with pytest.raises(expected):
        base_bootstrap.prefetch_packages([row], archives)
    assert attempts == [row['primary']]
    assert list(archives.iterdir()) == []


def apt_options(command):
    result = {}
    arguments = iter(command[1:])
    for argument in arguments:
        if argument in ('-o', '--option'):
            value = next(arguments)
        elif argument.startswith('-o'):
            value = argument[2:]
        elif argument.startswith('--option='):
            value = argument.split('=', 1)[1]
        else:
            continue
        key, value = value.split('=', 1)
        key = key.casefold()
        assert key not in result, 'APT option must not have conflicting repeated values: ' + key
        result[key] = value
    return result


def assert_secure_apt_options(options):
    assert options['acquire::forcehash'] == 'SHA256'
    for key in ('acquire::allowinsecurerepositories', 'acquire::allowweakrepositories',
                'acquire::allowdowngradetoinsecurerepositories', 'apt::get::allowunauthenticated'):
        assert options[key].casefold() in ('false', '0', 'no')
    for key in ('acquire::https::verify-peer', 'acquire::https::verify-host'):
        assert options[key].casefold() in ('true', '1', 'yes')


@pytest.fixture
def isolated_private_caches(monkeypatch, tmp_path):
    real_temporary_directory = base_bootstrap.tempfile.TemporaryDirectory
    roots = []

    def temporary_directory(*args, **kwargs):
        assert Path(kwargs['dir']) == Path('/var/cache')
        kwargs['dir'] = str(tmp_path)
        directory = real_temporary_directory(*args, **kwargs)
        roots.append(Path(directory.name))
        return directory

    monkeypatch.setattr(base_bootstrap.tempfile, 'TemporaryDirectory', temporary_directory)
    return roots


class AptBoundary:
    def __init__(self, monkeypatch, fixtures, roots, *, fail=None):
        self.fixtures = fixtures
        self.rows = [fixture['row'] for fixture in fixtures]
        self.roots = roots
        self.fail = fail
        self.commands = []
        self.events = []
        self.cache = None
        self.real_run = subprocess.run
        monkeypatch.setattr(subprocess, 'run', self.run)
        monkeypatch.setattr(base_bootstrap, '_open_package', self.open_package)

    def run(self, argv, *args, **kwargs):
        if not argv or Path(argv[0]).name != 'apt-get':
            return self.real_run(argv, *args, **kwargs)
        command = list(argv)
        self.commands.append((command, kwargs))
        assert 'update' not in command, 'The original signed snapshot metadata must be reused'
        assert 'install' in command
        assert kwargs['env']['LC_ALL'] == 'C'
        assert kwargs['env']['DEBIAN_FRONTEND'] == 'noninteractive'
        options = apt_options(command)
        assert_secure_apt_options(options)
        cache = Path(options['dir::cache::archives'])
        assert cache.is_dir()
        assert any(root == cache or root in cache.parents for root in self.roots)
        if '--print-uris' in command:
            assert len(self.commands) == 1
            assert '--download-only' in command
            assert '--no-install-recommends' in command
            assert '--no-download' not in command
            assert not list(cache.glob('*.deb')), 'The plan needs a fresh cache, not host package hits'
            self.cache = cache
            self.events.append('plan')
            if self.fail == 'plan':
                raise subprocess.CalledProcessError(100, command, stderr='fixture plan failure')
            return subprocess.CompletedProcess(command, 0,
                                               stdout='\n'.join(uri_line(row) for row in self.rows) + '\n')
        assert len(self.commands) == 2
        assert '--no-download' in command
        assert '--download-only' not in command
        assert '--no-install-recommends' in command
        assert '-y' in command or '--yes' in command
        assert cache == self.cache
        assert options == apt_options(self.commands[0][0])
        assert self.events == ['plan', *[row['primary'] for row in self.rows]]
        for fixture in self.fixtures:
            row = fixture['row']
            assert (cache / row['filename']).read_bytes() == fixture['body']
            assert f"{row['package']}={row['version']}" in command
        self.events.append('install')
        if self.fail == 'install':
            raise subprocess.CalledProcessError(100, command, stderr='fixture install failure')
        return subprocess.CompletedProcess(command, 0, stdout='fixture packages installed\n')

    def open_package(self, url, maximum_size):
        assert self.cache is not None
        assert self.cache.is_dir()
        self.events.append(url)
        if self.fail == 'download':
            raise URLError(ssl.SSLCertVerificationError('fixture certificate verification failed'))
        fixture = next(fixture for fixture in self.fixtures if fixture['row']['primary'] == url)
        target = self.cache / fixture['row']['filename']
        assert not target.exists()
        return PackageResponse(fixture['body'], url, target=target)


def test_install_reuses_one_signed_plan_and_private_cache_pins_every_package_then_cleans_up(
        monkeypatch, deb, deb_factory, isolated_private_caches):
    dependency = deb_factory(package='fixture-dependency', version='3.4.5-1', architecture='all')
    edges = AptBoundary(monkeypatch, [deb, dependency], isolated_private_caches)
    requests = [f'{PACKAGE}={VERSION}', 'already-installed=4.5-1']
    specification = {'ubuntuSnapshot': SNAPSHOT, 'guestPackages': list(requests)}
    result = base_bootstrap.install_guest_packages(specification)
    assert result == [transport(deb['row']), transport(dependency['row'])]
    assert specification['guestPackages'] == requests
    assert len(edges.commands) == 2
    for command, _ in edges.commands:
        assert all(request in command for request in requests)
    assert not any('fixture-dependency=3.4.5-1' == argument for argument in edges.commands[0][0])
    assert edges.events == ['plan', deb['row']['primary'], dependency['row']['primary'], 'install']
    assert isolated_private_caches and all(not root.exists() for root in isolated_private_caches)
    assert edges.cache is not None and not edges.cache.exists()
    assert all(set(row) == TRANSPORT_KEYS for row in result)
    assert all(str(root) not in json.dumps(result) for root in isolated_private_caches)


def test_explicit_requested_version_mismatch_fails_before_any_blob_fetch_and_cleans_cache(
        monkeypatch, deb, isolated_private_caches):
    edges = AptBoundary(monkeypatch, [deb], isolated_private_caches)
    specification = {'ubuntuSnapshot': SNAPSHOT,
                     'guestPackages': [f'{PACKAGE}=1:2.3.5-0ubuntu1~24.04.1']}
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.install_guest_packages(specification)
    assert len(edges.commands) == 1
    assert edges.events == ['plan']
    assert isolated_private_caches and all(not root.exists() for root in isolated_private_caches)


@pytest.mark.parametrize('failure', ['plan', 'download', 'install'])
def test_private_cache_is_removed_on_plan_prefetch_and_install_failures(
        monkeypatch, deb, isolated_private_caches, failure):
    edges = AptBoundary(monkeypatch, [deb], isolated_private_caches, fail=failure)
    specification = {'ubuntuSnapshot': SNAPSHOT, 'guestPackages': [f'{PACKAGE}={VERSION}']}
    expected = base_bootstrap.PackageBlobError if failure == 'download' else subprocess.CalledProcessError
    with pytest.raises(expected):
        base_bootstrap.install_guest_packages(specification)
    assert len(edges.commands) == (2 if failure == 'install' else 1)
    assert not any('update' in command for command, _ in edges.commands)
    assert isolated_private_caches and all(not root.exists() for root in isolated_private_caches)
    if failure != 'install':
        assert 'install' not in edges.events


def test_one_unverified_dependency_prevents_the_entire_no_download_install(
        monkeypatch, deb, deb_factory, isolated_private_caches):
    dependency = deb_factory(package='fixture-dependency', version='3.4.5-1', architecture='all')
    unverified = dict(dependency, row=dict(dependency['row'], sha256='0' * 64))
    edges = AptBoundary(monkeypatch, [deb, unverified], isolated_private_caches)
    specification = {'ubuntuSnapshot': SNAPSHOT, 'guestPackages': [f'{PACKAGE}={VERSION}']}
    with pytest.raises(base_bootstrap.PackageBlobError):
        base_bootstrap.install_guest_packages(specification)
    assert len(edges.commands) == 1
    assert edges.events == ['plan', deb['row']['primary'], dependency['row']['primary']]
    assert 'install' not in edges.events
    assert isolated_private_caches and all(not root.exists() for root in isolated_private_caches)


def test_already_installed_explicit_version_is_retained_even_when_the_uri_plan_is_empty(
        monkeypatch, isolated_private_caches):
    edges = AptBoundary(monkeypatch, [], isolated_private_caches)
    request = f'{PACKAGE}={VERSION}'
    specification = {'ubuntuSnapshot': SNAPSHOT, 'guestPackages': [request]}
    assert base_bootstrap.install_guest_packages(specification) == []
    assert len(edges.commands) == 2
    assert all(request in command for command, _ in edges.commands)
    assert edges.events == ['plan', 'install']
    assert isolated_private_caches and all(not root.exists() for root in isolated_private_caches)
