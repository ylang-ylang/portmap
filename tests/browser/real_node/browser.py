#!/usr/bin/env python3
"""Exercise a canonical addon against actual full Portmap nodes in a fresh browser."""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import urlsplit, urlunsplit
import uuid
import zipfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import runner
import managed
from selenium import __version__ as selenium_version
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import Select, WebDriverWait

from evidence import artifacts, file_sha256, network_audit


SECRET_FIELDS = ('ssh-onetime-password', 'ssh-password', 'ssh-private-key',
                 'ssh-public-key', 'ssh-passphrase')


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def canonical_url(value):
    parts = urlsplit(value)
    host = parts.hostname or ''
    if ':' in host:
        host = '[' + host + ']'
    default = {'http': 80, 'https': 443}.get(parts.scheme)
    authority = host + (':' + str(parts.port) if parts.port and parts.port != default else '')
    return urlunsplit((parts.scheme, authority, parts.path or '/', parts.query, parts.fragment))


class Gate:
    def __init__(self, args):
        self.args = args
        self.output = args.output.resolve()
        self.raw = self.output / 'raw'
        self.captures = self.output / 'captures'
        self.screens = self.output / 'screens'
        for path in (self.raw, self.captures, self.screens):
            path.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.secrets = []
        self.last_failure_error = None
        self.receipt = {'schemaVersion': 1, 'status': 'failed', 'platform': args.platform,
                        'startedAt': time.time(), 'scenarios': [], 'failures': [], 'stages': [],
                        'timings': {}, 'artifacts': []}

    def write_json(self, path, value):
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')

    def save(self):
        self.write_json(self.output / 'receipt.json', self.receipt)

    def redact(self, value):
        for secret in self.secrets:
            value = value.replace(secret, '[REDACTED]')
        return value

    def artifact(self, path):
        return {'path': path.relative_to(self.output).as_posix(),
                'sha256': file_sha256(path), 'bytes': path.stat().st_size}

    @contextmanager
    def stage(self, name, kind, scenario=None):
        row = {'name': name, 'kind': kind, 'status': 'failed', 'startedAt': time.time()}
        (scenario['stages'] if scenario else self.receipt['stages']).append(row)
        started = time.monotonic()
        try:
            yield
            row['status'] = 'passed'
        except Exception as error:
            self.last_failure_error = error
            failure = {'kind': kind, 'stage': name,
                       'scenario': scenario['id'] if scenario else None,
                       'message': self.redact(type(error).__name__ + ': ' + str(error))}
            self.receipt['failures'].append(failure)
            row['failure'] = failure
            log = self.raw / ('failure-' + str(len(self.receipt['failures'])) + '.txt')
            log.write_text(self.redact(traceback.format_exc()))
            raise
        finally:
            row['finishedAt'] = time.time()
            row['elapsedSeconds'] = time.monotonic() - started
            key = (scenario['id'] + '.' if scenario else '') + name
            self.receipt['timings'][key] = row['elapsedSeconds']
            self.save()


class Captures:
    def screenshot(self, name):
        if not self.capture_allowed(name):
            return None
        if self.driver.current_url == runner.OPTIONS_URL:
            require(self.driver.execute_script('''
              return arguments[0].every(id=>document.getElementById(id)?.value==='');
            ''', list(SECRET_FIELDS)), 'Refusing a credential-bearing editor screenshot')
        require(self.settle_render(), 'Browser did not complete native paint before capture')
        path = self.gate.screens / (name + '.png')
        require(self.driver.get_full_page_screenshot_as_file(str(path)), 'Native full-page screenshot failed')
        result = self.gate.artifact(path)
        result['kind'] = 'native Firefox full-page'
        if self.gate.args.platform == 'android':
            import android
            native = self.gate.screens / (name + '-native.png')
            with native.open('wb') as output:
                android.adb('exec-out', 'screencap', '-p', stdout=output, timeout=10)
            result['nativeScreen'] = self.gate.artifact(native)
        return result


class Desktop(Captures, runner.Browser):
    def __init__(self, profile, gate):
        self.gate = gate
        super().__init__(profile, False)

    def start(self):
        options = self.configuration()
        options.binary_location = '/opt/firefox/firefox'
        options.add_argument('-profile')
        options.add_argument(str(self.profile))
        options.add_argument('-headless')
        os.environ['PORTMAP_BROWSER_RAW_DIR'] = str(self.gate.raw)
        self.driver = webdriver.Firefox(options=options, service=Service(
            str(HERE / 'traced-geckodriver.sh'),
            service_args=['--allow-system-access', '--log', 'info'],
            log_output=str(self.gate.raw / 'geckodriver.log')))
        self.driver.set_page_load_timeout(30)
        self.driver.set_script_timeout(45)
        self.driver.set_window_size(1440, 1100)


def android_browser(profile, gate):
    import android

    class Android(Captures, android.AndroidBrowser):
        def __init__(self):
            self.gate = gate
            super().__init__(profile, False)

        def close(self):
            if self.driver:
                try:
                    with (gate.raw / 'android-logcat.txt').open('wb') as output:
                        android.adb('logcat', '-d', '-v', 'threadtime', stdout=output, timeout=20)
                finally:
                    # This gate never retains/restarts profiles. Reuse the
                    # ordinary close path, not fixture profile-retention logic.
                    runner.Browser.close(self)

    return Android()


def secret_editor_empty(browser):
    return browser.driver.execute_script('''
      return arguments[0].every(id=>document.getElementById(id).value==='');
    ''', list(SECRET_FIELDS))


def saved_public(browser, domain):
    result = browser.driver.execute_async_script('''
      const [domain,done]=arguments;
      browser.storage.local.get('config').then(({config})=>{
        const c=config?.connections.find(c=>c.domain===domain);
        done(c?{id:c.id,domain:c.domain,mode:c.mode,host:c.host,httpPort:c.httpPort,httpsPort:c.httpsPort,
          ssh:c.ssh?{username:c.ssh.username,port:c.ssh.port,targetHost:c.ssh.targetHost,
            hostKeySha256Hex:c.ssh.hostKeySha256Hex,authType:c.ssh.auth?.type,
            credentialFree:JSON.stringify(c.ssh.auth)===JSON.stringify({type:'managed'})}:null}:null);
      }).catch(()=>done({error:'Cannot observe public endpoint metadata'}));
    ''', domain)
    require(result and 'error' not in result, 'No actual saved endpoint metadata')
    return result


def configure(browser, case, password):
    browser.options()
    browser.enter('name', 'Real node ' + case['mode'])
    browser.enter('domain', case['domain'])
    Select(browser.driver.find_element(By.ID, 'mode')).select_by_value(case['mode'])
    browser.enter('host', case['host'])
    browser.enter('http-port', case['httpPort'])
    browser.enter('https-port', '')
    browser.advanced(False)
    result = {'actualOptionsUi': True}
    if case['mode'] == 'ssh':
        browser.enter('ssh-username', case['sshUsername'])
        browser.enter('ssh-port', case['sshPort'])
        browser.enter('ssh-target-host', '127.0.0.1')
        browser.enter('ssh-host-key', '')
        browser.ssh_advanced(False)
        require(browser.driver.find_element(By.ID, 'ssh-auth-type').get_attribute('value') == 'managed',
                'New SSH connection did not default to the browser key')
        require(not browser.driver.find_element(By.ID, 'save').is_enabled(),
                'SSH save was enabled before explicit fingerprint confirmation')
        browser.driver.find_element(By.ID, 'inspect-host').click()
        WebDriverWait(browser.driver, 40).until(lambda d: d.find_element(By.ID, 'host-confirmation').is_displayed())
        fingerprint = browser.driver.find_element(By.ID, 'detected-fingerprint').text.strip()
        require(fingerprint == case['hostKeySha256Hex'], 'Displayed fingerprint differs from the actual guest host key')
        require(not browser.driver.find_element(By.ID, 'host-ack').is_selected() and
                not browser.driver.find_element(By.ID, 'confirm-host').is_enabled(),
                'Host inspection silently acknowledged or trusted the SSH server')
        browser.driver.find_element(By.ID, 'host-ack').click()
        browser.driver.find_element(By.ID, 'confirm-host').click()
        WebDriverWait(browser.driver, 20).until(lambda d: d.find_element(By.ID, 'install-managed-key').is_enabled())
        section = browser.driver.find_element(By.ID, 'setup-password-section')
        if not section.get_property('open'):
            section.find_element(By.CSS_SELECTOR, 'summary').click()
        browser.sensitive_input = True
        browser.enter('ssh-onetime-password', password)
        browser.driver.find_element(By.ID, 'install-managed-key').click()
        result.update(fingerprint=fingerprint, explicitFingerprintComparison=True, oneTimePasswordBootstrap=True)
    else:
        browser.driver.find_element(By.ID, 'save').click()
    WebDriverWait(browser.driver, 60).until(lambda d:
        d.find_element(By.ID, 'name').get_attribute('value') == '' and len(browser.cards(case['domain'])) == 1)
    require(secret_editor_empty(browser), 'Successful onboarding retained credentials in the editor')
    browser.sensitive_input = False
    saved = saved_public(browser, case['domain'])
    require(saved['mode'] == case['mode'] and saved['httpPort'] == 80, 'Saved route differs from the actual node endpoint')
    if case['mode'] == 'ssh':
        require(saved['ssh']['credentialFree'] and saved['ssh']['authType'] == 'managed',
                'Managed SSH persisted per-node authentication material')
        require(saved['ssh']['hostKeySha256Hex'] == case['hostKeySha256Hex'], 'Saved host pin differs from the acknowledged fingerprint')
        identity = managed.identity(browser)
        require(identity['present'] and identity['rsaBits'] >= 3072 and not identity['localPrivateCopy'],
                'Actual browser-managed SSH identity metadata is invalid')
        result.update(passwordCleared=True, managedIdentity=identity)
    result['saved'] = saved
    return result


def select_project(browser, before, url):
    def selected(driver):
        for handle in driver.window_handles:
            if handle in before:
                continue
            driver.switch_to.window(handle)
            if canonical_url(driver.current_url) == canonical_url(url):
                return True
        return False
    WebDriverWait(browser.driver, 30).until(selected)
    WebDriverWait(browser.driver, 20).until(lambda d: d.execute_script('return document.visibilityState') == 'visible')


def catalog(browser, case, gate):
    browser.action(case['domain'], 'open')
    browser.focus_page(canonical_url(case['catalogUrl']))
    require(canonical_url(browser.driver.current_url) == canonical_url(case['catalogUrl']), 'Catalog URL was replaced')
    links = WebDriverWait(browser.driver, 45).until(lambda d:
        d.find_elements(By.CSS_SELECTOR, 'a[href="' + case['projectUrl'] + '"]'))
    document = browser.driver.execute_script('''
      return {url:location.href,title:document.title,text:document.body.innerText,
        links:Array.from(document.querySelectorAll('a[href]')).map(a=>({text:a.innerText,href:a.href}))};
    ''')
    require(case['projectName'] in document['text'], 'Actual catalog does not list the genuine development project')
    boundary = browser.driver.execute_async_script('''
      const done=arguments[0];(async()=>{
        const connections=await fetch('/api/connections',{cache:'no-store',signal:AbortSignal.timeout(15000)});
        const machines=await fetch('/api/machines',{cache:'no-store',signal:AbortSignal.timeout(15000)});
        const data=await machines.json();
        done({connectionsStatus:connections.status,machinesStatus:machines.status,
          canManageConnections:data.can_manage_connections});
      })().catch(e=>done({error:String(e)}));
    ''')
    private = case['mode'] == 'ssh'
    require(boundary.get('connectionsStatus') == (200 if private else 404),
            'Actual connection controls contradict the selected private/public ingress')
    require(boundary.get('machinesStatus') == 200 and boundary.get('canManageConnections') is private,
            'Actual machine catalog scope contradicts the selected transport')
    document.update(controlRole='private-ssh' if private else 'public-lan', ingressBoundary=boundary,
                    projectHref=case['projectUrl'])
    gate.write_json(gate.captures / ('catalog-' + case['mode'] + '.json'), document)
    document['screenshot'] = browser.screenshot('portmap-realnode-' + gate.args.platform + '-' + case['mode'] + '-catalog')
    before = set(browser.driver.window_handles)
    links[0].click()
    if not (set(browser.driver.window_handles) == before and
            canonical_url(browser.driver.current_url) == canonical_url(case['projectUrl'])):
        select_project(browser, before, case['projectUrl'])
    require(canonical_url(browser.driver.current_url) == canonical_url(case['projectUrl']),
            'Real catalog click changed the project hostname')
    document['projectLinkClicked'] = True
    return document


def resources(browser, case, nonce, gate):
    contract = case['appContract']
    require({'js', 'css', 'image', 'static'} <= {row['kind'] for row in contract['resources']},
            'The genuine app contract lacks JS/CSS/image/static content assertions')
    observations = []
    for index, resource in enumerate(contract['resources']):
        response = browser.driver.execute_async_script('''
          const [path,nonce,done]=arguments;(async()=>{
            const url=new URL(path,location.href);url.searchParams.set('browserProofNonce',nonce);
            const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(15000)});
            const bytes=new Uint8Array(await response.arrayBuffer());
            let binary='';for(const byte of bytes) binary+=String.fromCharCode(byte);
            done({status:response.status,url:response.url,contentType:response.headers.get('content-type'),bytes:btoa(binary)});
          })().catch(e=>done({error:String(e)}));
        ''', resource['path'], nonce)
        require(response.get('status') == 200, 'Actual project resource failed: ' + resource['path'])
        data = base64.b64decode(response.pop('bytes'), validate=True)
        digest = hashlib.sha256(data).hexdigest()
        if resource.get('sha256'):
            require(digest == resource['sha256'], 'Resource bytes differ from the real guest project source: ' + resource['path'])
        else:
            require(resource.get('contains') and resource['contains'].encode() in data,
                    'Vite transformed resource lacks its genuine source content: ' + resource['path'])
        require(resource['contentType'] in response['contentType'], 'Incorrect actual project resource content type')
        path = gate.captures / ('resource-' + case['mode'] + '-' + str(index) + '-' + resource['kind'] + '.bin')
        path.write_bytes(data)
        observations.append({**response, 'sourcePath': resource['path'], 'kind': resource['kind'],
                             'artifact': gate.artifact(path)})
    css = browser.driver.execute_script('''
      const node=document.querySelector(arguments[0]);
      return node?getComputedStyle(node).getPropertyValue(arguments[1]).trim():null;
    ''', contract['cssSelector'], contract['cssProperty'])
    require(css == contract['cssExpected'], 'Real imported CSS did not affect rendered application DOM')
    return {'responses': observations, 'computedCss': {'selector': contract['cssSelector'],
            'property': contract['cssProperty'], 'expected': contract['cssExpected'], 'observed': css}}


def document_state(browser):
    return browser.driver.execute_script('''
      return {url:location.href,host:location.host,title:document.title,text:document.body.innerText,
        proof:{ready:window.realPortmapProof.ready,node:window.realPortmapProof.node},
        images:Array.from(document.images).map(i=>({src:i.currentSrc,complete:i.complete,naturalWidth:i.naturalWidth,naturalHeight:i.naturalHeight})),
        resources:performance.getEntriesByType('resource').map(e=>({name:e.name,initiatorType:e.initiatorType,
          transferSize:e.transferSize,decodedBodySize:e.decodedBodySize}))};
    ''')


def console_errors(browser, url):
    browser.driver.set_context('chrome')
    try:
        return browser.driver.execute_script('''
          const base=arguments[0];
          return Services.console.getMessageArray().map(x=>{
            try {const e=x.QueryInterface(Ci.nsIScriptError);return {message:e.errorMessage,source:e.sourceName,
              line:e.lineNumber,warning:!!(e.flags & Ci.nsIScriptError.warningFlag)};}
            catch(_){return null;}
          }).filter(e=>e && e.source.startsWith(base));
        ''', canonical_url(url))
    finally:
        browser.driver.set_context('content')


def application(browser, case, gate, scenario):
    contract = case['appContract']
    nonce = 'portmap-realnode-' + scenario['id'] + '-' + uuid.uuid4().hex
    WebDriverWait(browser.driver, 45).until(lambda d:
        d.execute_script('return !!(' + contract['readyExpression'] + ');'))
    before = document_state(browser)
    original_host = urlsplit(canonical_url(case['projectUrl'])).netloc
    require(before['host'] == original_host and before['proof']['node'] == case['expectedNode'],
            'Application URL or actual independent node identity differs from the catalog project')
    require(any(image['complete'] and image['naturalWidth'] > 0 for image in before['images']),
            'Actual application image did not render')
    control = browser.driver.find_element(By.CSS_SELECTOR, contract['buttonSelector'])
    old_text = control.text
    control.click()
    WebDriverWait(browser.driver, 10).until(lambda d:
        d.find_element(By.CSS_SELECTOR, contract['buttonSelector']).text != old_text)
    result = browser.driver.execute_async_script('''
      const [nonce,done]=arguments;
      Promise.resolve(window.realPortmapProof.exercise(nonce)).then(done).catch(e=>done({error:String(e)}));
    ''', nonce)
    gate.write_json(gate.captures / ('interaction-' + case['mode'] + '.json'), {'nonce': nonce, 'response': result})
    require(isinstance(result, dict) and 'error' not in result, 'Actual Vite application interaction failed: ' + str(result))
    require(result.get('nonce') == nonce and result.get('node') == case['expectedNode'], 'Actual application nonce/node differs')
    require(result.get('post', {}).get('nonce') == nonce and result.get('websocket', {}).get('nonce') == nonce,
            'Actual HTTP POST or genuine Vite HMR WebSocket did not return the bidirectional nonce')
    backend_host = case['expectedBackendHost']
    require(case['endpointPreserveHost'] is False and backend_host == '127.0.0.1:5173',
            'Required DEFAULT Portmap endpoint configuration was replaced')
    for channel in ('post', 'websocket'):
        require(result[channel].get('host') == backend_host, 'Unexpected documented default backend Host on ' + channel)
        require(result[channel].get('forwardedHost') == original_host, 'Original project Host was not forwarded by actual Traefik on ' + channel)
        require(result[channel].get('node') == case['expectedNode'], 'Actual response came from the wrong independent node')
    require(result.get('cssLoaded') is True and result.get('jsBehavior') is True and result.get('staticLoaded') is True,
            'Real application CSS, JavaScript behavior or static/image content failed')
    with gate.stage('resources', 'resource', scenario):
        resource_proof = resources(browser, case, nonce, gate)
    errors = console_errors(browser, case['projectUrl'])
    require(not [error for error in errors if not error['warning']], 'Actual application emitted browser script errors')
    with gate.stage('screenshot', 'capture', scenario):
        screenshot = browser.screenshot('portmap-realnode-' + scenario['id'])
    observation = {'nonce': nonce, 'before': before, 'after': document_state(browser), 'interaction': result,
                   'originalProjectUrl': case['projectUrl'], 'observedSourceHost': original_host,
                   'originalForwardedHost': original_host, 'defaultBackendHost': backend_host,
                   'endpointPreserveHost': False, 'node': case['expectedNode'],
                   'resources': resource_proof, 'console': errors, 'screenshot': screenshot}
    gate.write_json(gate.captures / ('application-' + case['mode'] + '.json'), observation)
    return observation


def run(args):
    os.umask(0o077)
    gate = Gate(args)
    runner.ARTIFACTS = gate.raw
    browser = runtime = profile = None
    try:
        with gate.stage('inputs', 'input'):
            manifest = json.loads(args.public_manifest.read_text())
            private = json.loads(args.private_manifest.read_text())
            gate.secrets = [private[platform]['ssh']['password'] for platform in ('desktop', 'android')]
            require(manifest.get('ready') is True and manifest.get('frozen') is True, 'Actual full nodes are not explicitly ready/frozen')
            require(len(manifest['cases']) == 2 and {case['mode'] for case in manifest['cases']} == {'direct', 'ssh'},
                    'Both actual independent direct and SSH nodes are required')
            digest = file_sha256(args.xpi)
            require(digest == manifest['addonSha256'], 'Canonical XPI hash differs from the actual node manifest')
            with zipfile.ZipFile(args.xpi) as archive:
                addon_version = json.loads(archive.read('manifest.json'))['version']
            require(addon_version == manifest['publishedVersion'], 'Addon and actual CLI runtime versions differ')
            gate.receipt.update(xpi_sha256=digest, addon_version=addon_version,
                                source_commit=manifest['sourceCommit'],
                                runtime_archive_sha256=manifest['publishedCliSha256'], node_manifest=manifest)
            for case in manifest['cases']:
                required = ('domain', 'catalogUrl', 'projectUrl', 'projectName', 'host', 'httpPort',
                            'sshPort', 'sshUsername', 'hostKeySha256Hex', 'expectedBackendHost',
                            'endpointPreserveHost', 'appContract', 'expectedNode')
                require(all(field in case for field in required), 'The actual node public case contract is incomplete')
                require(case['httpPort'] == 80, 'The real gateway must use its actual published HTTP80 endpoint')
                require(case['endpointPreserveHost'] is False and case['expectedBackendHost'] == '127.0.0.1:5173',
                        'The required default project endpoint was replaced')
        with gate.stage('environment', 'environment'):
            os.environ['MOZ_REMOTE_ALLOW_SYSTEM_ACCESS'] = '1'
            for key, default in (('HOME', '/tmp/real-node-home'), ('XDG_RUNTIME_DIR', '/tmp/real-node-runtime')):
                os.environ.setdefault(key, default)
                Path(os.environ[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
            (gate.raw / 'container-hosts.txt').write_text(Path('/etc/hosts').read_text())
            (gate.raw / 'container-resolv.conf').write_text(Path('/etc/resolv.conf').read_text())
            gate.receipt['toolchain'] = {'seleniumVersion': selenium_version,
                'geckodriver': subprocess.check_output(['/usr/local/bin/geckodriver', '--version'], text=True).strip()}
            sources = [HERE / name for name in ('browser.py', 'android_runtime.py', 'evidence.py', 'traced-geckodriver.sh')]
            sources += [HERE.parent / name for name in ('runner.py', 'android.py', 'managed.py')]
            gate.receipt['harnessFiles'] = {
                source.relative_to(HERE.parents[2]).as_posix(): file_sha256(source) for source in sources}
            encoded = json.dumps(gate.receipt['harnessFiles'], sort_keys=True, separators=(',', ':')).encode()
            gate.receipt['harness_sha256'] = hashlib.sha256(encoded).hexdigest()
        if args.platform == 'android':
            with gate.stage('android-boot', 'environment'):
                from android_runtime import AndroidRuntime
                runtime = AndroidRuntime(gate.raw)
                gate.receipt['toolchain']['android'] = runtime.prepare()
        with gate.stage('browser-start', 'environment'):
            profile_id = uuid.uuid4().hex
            profile = tempfile.TemporaryDirectory(prefix='real-node-private-profile-' + profile_id + '-')
            browser = android_browser(Path(profile.name), gate) if args.platform == 'android' else Desktop(Path(profile.name), gate)
            capabilities = browser.driver.capabilities
            require(capabilities['acceptInsecureCerts'] is False, 'Browser TLS validation was bypassed')
            gate.receipt['toolchain'].update(browserVersion=capabilities['browserVersion'],
                platform=capabilities['platformName'], buildId=capabilities.get('moz:buildID'))
            gate.receipt['profile'] = {'id': profile_id, 'fresh': True,
                'nativeProfileIdSha256': hashlib.sha256(capabilities.get('moz:profile', profile.name).encode()).hexdigest()}
        with gate.stage('addon-install', 'addon'):
            browser.driver.install_addon(str(args.xpi), temporary=False)
            gate.receipt['toolchain']['addon'] = runner.observe(browser)
            browser.options()
            require(browser.driver.find_element(By.ID, 'status').get_attribute('data-state') == 'ready',
                    'Actual addon native capability gate is not ready')
            require(not browser.driver.find_elements(By.CSS_SELECTOR, '#connections article'), 'Browser profile already contains saved connections')
            initial_identity = managed.identity(browser)
            require(not initial_identity['present'] and not initial_identity['reference_present'], 'Browser profile already contains a managed SSH identity')
            gate.receipt['profile'].update(initialConnectionsEmpty=True, initialManagedIdentity=initial_identity)
        for case in manifest['cases']:
            scenario = {'id': args.platform + '-' + case['mode'], 'mode': case['mode'], 'status': 'failed',
                        'domain': case['domain'], 'projectUrl': case['projectUrl'], 'stages': [], 'startedAt': time.time()}
            gate.receipt['scenarios'].append(scenario)
            started = time.monotonic()
            try:
                with gate.stage('onboarding', 'onboarding', scenario):
                    scenario['onboarding'] = configure(browser, case, private[args.platform]['ssh']['password'])
                with gate.stage('catalog', 'catalog', scenario):
                    scenario['catalog'] = catalog(browser, case, gate)
                with gate.stage('application', 'application', scenario):
                    scenario['application'] = application(browser, case, gate, scenario)
                scenario['status'] = 'passed'
            except Exception:
                # The typed stage failure is retained. Attempt the independent
                # other node too, but never turn a partial run into a pass.
                if browser.driver:
                    try:
                        with gate.stage('failure-capture', 'capture', scenario):
                            if browser.driver.current_url == runner.OPTIONS_URL:
                                # Fixed application validation text only; never
                                # serialize the editor or any input values.
                                scenario['uiError'] = gate.redact(browser.driver.find_element(By.ID, 'error').text[:500])
                            elif not browser.sensitive_input:
                                scenario['failureScreenshot'] = browser.screenshot('portmap-realnode-' + scenario['id'] + '-failed')
                    except Exception:
                        pass  # The capture stage records its own failure.
            finally:
                scenario['finishedAt'] = time.time()
                scenario['elapsedSeconds'] = time.monotonic() - started
                gate.save()
    except Exception as error:
        if gate.last_failure_error is not error:
            try:
                with gate.stage('runner', 'internal'):
                    raise
            except Exception:
                pass  # The unexpected failure is now typed and retained.
    finally:
        if browser:
            try:
                with gate.stage('browser-close', 'cleanup'):
                    browser.close()
            except Exception:
                pass
        if runtime:
            try:
                with gate.stage('android-stop', 'cleanup'):
                    runtime.stop()
            except Exception:
                pass
        if profile:
            try:
                with gate.stage('profile-discard', 'cleanup'):
                    profile.cleanup()
                    if 'profile' in gate.receipt:
                        gate.receipt['profile']['discarded'] = True
            except Exception:
                pass
        if gate.receipt.get('node_manifest'):
            try:
                with gate.stage('native-network-audit', 'network'):
                    audit = network_audit(args.platform, gate.raw, gate.receipt['node_manifest']['cases'])
                    gate.receipt['network'] = audit
                    gate.write_json(gate.captures / 'network-audit.json', audit)
            except Exception:
                pass
        try:
            with gate.stage('artifact-index', 'artifact'):
                gate.receipt['artifacts'] = artifacts(gate.output)
        except Exception:
            pass
        gate.receipt['finishedAt'] = time.time()
        gate.receipt['timings']['totalSeconds'] = time.monotonic() - gate.started
        passed = (len(gate.receipt['scenarios']) == 2 and not gate.receipt['failures'] and
                  all(row['status'] == 'passed' for row in gate.receipt['scenarios']))
        gate.receipt['status'] = 'passed' if passed else 'failed'
        gate.save()
        print(json.dumps({'schemaVersion': 1, 'platform': args.platform,
                          'status': gate.receipt['status'], 'receipt': str(gate.output / 'receipt.json'),
                          'failures': gate.receipt['failures'], 'timings': gate.receipt['timings']}, indent=2), flush=True)
    return 0 if gate.receipt['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('desktop', 'android'), required=True)
    parser.add_argument('--xpi', type=Path, required=True)
    parser.add_argument('--public-manifest', type=Path, required=True)
    parser.add_argument('--private-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
