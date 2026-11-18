"""Actual Firefox UI, native networking and real OpenSSH fixtures; no API mocks."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import traceback
import uuid
import zipfile

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import Select, WebDriverWait

EXTENSION_ID = 'firefox@portmap.ylang-ylang.github.io'
EXTENSION_UUID = '181307fa-a3eb-420c-8fd9-a232d676672a'
OPTIONS_URL = 'moz-extension://' + EXTENSION_UUID + '/options.html'
ARTIFACTS = Path('/artifacts')
ORIGINS = ('node-a', 'node-b', 'unrelated', 'trap')


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def records(origin, path='/requests'):
    connection = http.client.HTTPConnection(origin, 8080, timeout=5)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        require(response.status == 200, 'Fixture evidence unavailable')
        return json.loads(response.read())
    finally:
        connection.close()


class Browser:
    experiments_enabled = True

    def __init__(self, profile, signed=False):
        self.profile, self.signed, self.driver = profile, signed, None
        self.sensitive_input = False
        self.start()

    def configuration(self):
        options = Options()
        options.set_capability('acceptInsecureCerts', False)
        options.set_preference('xpinstall.signatures.required', self.signed)
        options.set_preference('extensions.experiments.enabled', self.experiments_enabled)
        options.set_preference('extensions.webextensions.uuids', json.dumps({EXTENSION_ID: EXTENSION_UUID}))
        options.set_preference('network.trr.mode', 5)
        options.set_preference('network.proxy.no_proxies_on', '')
        options.set_preference('browser.shell.checkDefaultBrowser', False)
        options.set_preference('browser.startup.page', 0)
        options.set_preference('datareporting.policy.dataSubmissionEnabled', False)
        options.set_preference('toolkit.telemetry.enabled', False)
        options.set_preference('signon.rememberSignons', False)
        options.set_preference('marionette.log.level', 'Info')
        # Browser-native synthetic camera; no JS media API replacement and no
        # host camera device. Android explicitly selects its emulator backend.
        options.set_preference('media.navigator.permission.disabled', True)
        options.set_preference('media.navigator.streams.fake', True)
        return options

    def start(self):
        options = self.configuration()
        options.binary_location = '/opt/firefox/firefox'
        options.add_argument('-profile')
        options.add_argument(str(self.profile))
        options.add_argument('-headless')
        self.driver = webdriver.Firefox(options=options, service=Service(
            '/app/tests/browser/traced-geckodriver.sh', service_args=['--allow-system-access', '--log', 'info'],
            log_output=str(ARTIFACTS / 'geckodriver.log')))
        self.driver.set_page_load_timeout(20)
        self.driver.set_script_timeout(40)

    def restart(self, first_url=None):
        self.driver.quit()
        self.driver = None
        self.start()
        if first_url:
            self.driver.get(first_url)

    def close(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    def capture_allowed(self, name):
        if self.sensitive_input:
            (ARTIFACTS / (name + '-capture-skipped.json')).write_text(json.dumps({
                'reason':'Credential editor may contain private material; screenshot intentionally omitted'}))
            return False
        return True

    def settle_render(self):
        return self.driver.execute_async_script('''
          const done=arguments[arguments.length-1];
          const timer=setTimeout(()=>done(false),2000);
          const paint=()=>requestAnimationFrame(()=>requestAnimationFrame(()=>{
            clearTimeout(timer);done(true);
          }));
          Promise.resolve(document.fonts?.ready).then(paint,paint);
        ''')

    def confirm_dialog(self):
        self.driver.switch_to.alert.accept()

    def screenshot(self, name):
        if self.capture_allowed(name):
            self.settle_render()
            self.driver.save_screenshot(str(ARTIFACTS / (name + '.png')))

    def assert_network_paths(self):
        import network_evidence
        self.close()
        network_evidence.desktop(ARTIFACTS)

    def options(self):
        self.driver.get(OPTIONS_URL)
        self.wait_options()

    def wait_options(self):
        WebDriverWait(self.driver, 15).until(lambda driver:
            driver.find_element(By.ID, 'status').get_attribute('data-state') in ('ready', 'unsupported', 'error'))
        require(self.driver.execute_script('return document.visibilityState') == 'visible', 'Options are not native-visible')

    def focus_page(self, url):
        def visible(driver):
            matches = []
            for handle in driver.window_handles:
                driver.switch_to.window(handle)
                if driver.current_url == url and driver.execute_script('return document.visibilityState') == 'visible':
                    matches.append(handle)
            if len(matches) != 1:
                return False
            driver.switch_to.window(matches[0])
            return True
        WebDriverWait(self.driver, 20).until(visible)

    def cards(self, domain):
        return self.driver.find_elements(By.CSS_SELECTOR, 'article[data-domain="' + domain + '"]')

    def action(self, domain, action):
        self.options()
        cards = self.cards(domain)
        require(len(cards) == 1, 'Expected one connection for ' + domain)
        cards[0].find_element(By.CSS_SELECTOR, '[data-action="' + action + '"]').click()
        if action == 'delete':
            self.confirm_dialog()
            WebDriverWait(self.driver, 10).until(lambda _: not self.cards(domain))
        elif action == 'toggle':
            WebDriverWait(self.driver, 10).until(lambda d: d.find_element(By.ID, 'status').get_attribute('data-state') == 'ready')

    def advanced(self, opened=True):
        details = self.driver.find_element(By.ID, 'advanced-settings')
        if details.get_property('open') is not opened:
            details.find_element(By.CSS_SELECTOR, 'summary').click()
        WebDriverWait(self.driver, 10).until(lambda _: details.get_property('open') is opened)

    def ssh_advanced(self, opened=True):
        details = self.driver.find_element(By.ID, 'ssh-advanced-settings')
        if details.get_property('open') is not opened:
            details.find_element(By.CSS_SELECTOR, 'summary').click()
        WebDriverWait(self.driver, 10).until(lambda _: details.get_property('open') is opened)

    def saved_connection(self, domain):
        # Observe only public endpoint metadata, never stored authentication.
        result = self.driver.execute_async_script('''
          const [domain,done]=arguments;
          browser.storage.local.get('config').then(({config})=>{
            const entry=config?.connections.find(entry=>entry.domain===domain);
            done({connection:entry?{id:entry.id,domain:entry.domain,mode:entry.mode,
              httpPort:entry.httpPort,httpsPort:entry.httpsPort}:null});
          }).catch(error=>done({error:String(error)}));
        ''', domain)
        require('connection' in result, 'Cannot observe persisted endpoint metadata')
        return result['connection']

    def enter(self, field, value):
        if field in ('http-port', 'https-port'):
            self.advanced()
        if field in ('ssh-port', 'ssh-target-host', 'ssh-host-key', 'ssh-password',
                     'ssh-private-key', 'ssh-public-key', 'ssh-passphrase'):
            self.ssh_advanced()
        element = self.driver.find_element(By.ID, field)
        element.clear()
        if value:
            element.send_keys(str(value))

    def save(self, domain, host, *, mode='direct', auth='password', metadata=None, fault=None, edit=False):
        self.sensitive_input = self.sensitive_input or mode == 'ssh'
        if edit:
            self.action(domain, 'edit')
        else:
            self.options()
        self.enter('name', domain.removesuffix('.portmap'))
        self.enter('domain', domain)
        Select(self.driver.find_element(By.ID, 'mode')).select_by_value(mode)
        self.enter('host', host)
        self.enter('http-port', 18081 if fault == 'direct-port' else 8080)
        self.enter('https-port', 8443)
        if mode == 'ssh':
            self.ssh_advanced()
            for field, value in (('ssh-username', metadata['username']),
                                 ('ssh-port', 18081 if fault == 'ssh-port' else metadata['sshPort']),
                                 ('ssh-target-host', '127.0.0.1'),
                                 ('ssh-host-key', '0' * 64 if fault == 'pin' else metadata['hostKeySha256Hex'])):
                self.enter(field, value)
            Select(self.driver.find_element(By.ID, 'ssh-auth-type')).select_by_value(auth)
            replacement = self.driver.find_element(By.ID, 'replace-credentials')
            if replacement.is_displayed() and not replacement.is_selected():
                replacement.click()
            if auth == 'password':
                self.enter('ssh-password', 'invalid-' + metadata['password'] if fault == 'password' else metadata['password'])
            else:
                self.enter('ssh-private-key', metadata['privateKey'])
                self.enter('ssh-public-key', metadata['publicKey'])
                self.enter('ssh-passphrase', metadata.get('passphrase', ''))
        self.driver.find_element(By.ID, 'save').click()
        WebDriverWait(self.driver, 15).until(lambda d:
            d.find_element(By.ID, 'name').get_attribute('value') == '' and len(self.cards(domain)) == 1)
        require(self.cards(domain)[0].get_attribute('data-mode') == mode, 'Wrong saved transport mode')
        self.sensitive_input = False

    def page(self, url, node, *, loopback=None, navigate=True):
        if navigate:
            self.driver.get(url)
        require(self.driver.current_url == url, 'Original address replaced')
        require(self.driver.execute_script('return document.visibilityState') == 'visible', 'App loaded in hidden context')
        evidence = json.loads(self.driver.find_element(By.ID, 'evidence').text)
        require(evidence['node'] == node, 'Wrong receiver node')
        require(not evidence['authorization'] and not evidence['proxyAuthorization'], 'Proxy credential leaked to origin')
        if loopback is not None:
            require(evidence['peerLoopback'] is loopback, 'Receiver peer contradicts selected transport')
        WebDriverWait(self.driver, 10).until(lambda d: d.execute_script('return window.fixtureAsset') == node)
        require(self.driver.execute_script("return getComputedStyle(document.getElementById('asset-proof')).color") == 'rgb(1, 2, 3)', 'Real stylesheet did not load')
        return evidence

    def denied(self, host, label, scheme='http'):
        self.driver.get('about:blank')
        marker = '/denied-' + label + '-' + uuid.uuid4().hex
        try:
            self.driver.get(scheme + '://' + host + marker)
        except (TimeoutException, WebDriverException):
            try:
                self.driver.execute_script('window.stop()')
            except WebDriverException:
                pass
        require(not self.driver.find_elements(By.ID, 'evidence'), 'Blocked request rendered an origin document')
        for origin in ORIGINS:
            require(not any(marker in row['path'] for row in records(origin)), 'Blocked request reached ' + origin)

    def fetch(self, path, method='GET', body=None, headers=None):
        result = self.driver.execute_async_script('''
          const [path, method, body, headers, done] = arguments;
          fetch(path,{method,body,headers,cache:'no-store',signal:AbortSignal.timeout(15000)})
            .then(async r=>done({status:r.status,data:await r.json()})).catch(e=>done({error:String(e)}));
        ''', path, method, body, headers or {})
        require(result.get('status') == 200, 'Actual page fetch failed')
        return result['data']

    def websocket(self, scheme, host):
        result = self.driver.execute_async_script('''
          const [url,done]=arguments; const ws=new WebSocket(url);
          const timer=setTimeout(()=>{ws.close();done({error:'timeout'});},15000);
          ws.onopen=()=>ws.send('portmap-real-websocket-✓');
          ws.onerror=()=>{clearTimeout(timer);done({error:'websocket'});};
          ws.onmessage=e=>{clearTimeout(timer);done(JSON.parse(e.data));ws.close();};
        ''', scheme + '://' + host + '/ws')
        require(result.get('message') == 'portmap-real-websocket-✓', 'WebSocket payload corrupted')
        require(not result['proxyAuthorization'], 'Proxy credential leaked in WebSocket upgrade')
        return result


def advanced_ports(browser, case, host):
    """Exercise native details disclosure, form validation and saved endpoints."""
    driver = browser.driver

    def value(field):
        return driver.find_element(By.ID, field).get_attribute('value')

    def closed(http='80', https='443'):
        require(driver.find_element(By.ID, 'advanced-settings').get_property('open') is False,
                'Advanced settings unexpectedly opened')
        require(not driver.find_element(By.ID, 'http-port').is_displayed() and
                not driver.find_element(By.ID, 'https-port').is_displayed(), 'Collapsed ports remain visible')
        require(value('http-port') == http and value('https-port') == https,
                'Collapsed editor lost its endpoint values')

    def screenshot(name):
        # Keep the disclosure and native validation target inside the viewport.
        driver.execute_script('document.getElementById("advanced-settings").scrollIntoView({behavior:"instant",block:"center"})')
        browser.screenshot(name)

    def begin(label):
        browser.options()
        driver.refresh()
        browser.wait_options()
        closed()
        # Reuse the fixture's real *.a.portmap certificate for HTTPS routing.
        domain = 'ports-' + label + '.a.portmap'
        browser.enter('name', 'ports-' + label)
        browser.enter('domain', domain)
        browser.enter('host', host)
        return domain

    def save(domain):
        driver.find_element(By.ID, 'save').click()
        WebDriverWait(driver, 15).until(lambda _: value('name') == '' and len(browser.cards(domain)) == 1)
        closed()
        return browser.saved_connection(domain)

    def persisted(entry, http, https):
        require(entry and entry['httpPort'] == http and entry['httpsPort'] == https,
                'Persisted ports differ from the actual form submission')
        return entry

    def defaults():
        domain = begin('defaults')
        screenshot('advanced-defaults-collapsed')
        original = persisted(save(domain), 80, 443)
        browser.action(domain, 'edit')
        closed()
        browser.enter('name', 'Renamed default endpoint')
        closed()
        renamed = persisted(save(domain), 80, 443)
        require(renamed['id'] == original['id'], 'Editing default ports replaced the saved identity')
        browser.action(domain, 'delete')
        return {'saved': renamed, 'advanced_closed_at_save': True, 'ports_touched': False}
    case('advanced-default-ports-save-closed-and-reedit', defaults, fatal=True)

    def custom():
        domain = begin('custom')
        browser.enter('http-port', '8080')
        browser.enter('https-port', '8443')
        require(driver.find_element(By.ID, 'http-port').is_displayed(), 'User interaction did not reveal ports')
        screenshot('advanced-custom-ports-expanded')
        browser.advanced(False)
        original = persisted(save(domain), 8080, 8443)
        browser.action(domain, 'edit')
        closed('8080', '8443')
        browser.advanced()
        require(value('http-port') == '8080' and value('https-port') == '8443', 'Edit replaced custom ports with defaults')
        screenshot('advanced-custom-ports-reedit')
        browser.advanced(False)
        browser.enter('name', 'Renamed custom endpoint')
        renamed = persisted(save(domain), 8080, 8443)
        require(renamed['id'] == original['id'], 'Editing custom ports replaced the saved identity')
        route = browser.page('http://' + domain + '/advanced-custom-' + uuid.uuid4().hex, 'node-a', loopback=False)
        tls_route = browser.page('https://' + domain + '/advanced-custom-tls-' + uuid.uuid4().hex, 'node-a', loopback=False)
        browser.action(domain, 'delete')
        return {'saved': renamed, 'http_route': route, 'https_route': tls_route}
    case('advanced-custom-ports-save-reedit-and-route', custom, fatal=True)

    def no_https():
        domain = begin('no-https')
        browser.enter('http-port', '8080')
        browser.enter('https-port', '')
        screenshot('advanced-https-disabled')
        browser.advanced(False)
        original = persisted(save(domain), 8080, None)
        browser.action(domain, 'edit')
        closed('8080', '')
        browser.enter('name', 'Renamed HTTP-only endpoint')
        renamed = persisted(save(domain), 8080, None)
        require(renamed['id'] == original['id'], 'Editing HTTP-only ports replaced the saved identity')
        route = browser.page('http://' + domain + '/advanced-no-https-' + uuid.uuid4().hex, 'node-a', loopback=False)
        browser.denied(domain, 'advanced-disabled-https', scheme='https')
        browser.action(domain, 'delete')
        return {'saved': renamed, 'http_route': route, 'https_blocked': True}
    case('advanced-null-https-preserved-through-edit-and-route', no_https, fatal=True)

    def reset():
        domain = begin('reset')
        browser.enter('http-port', '8123')
        browser.enter('https-port', '')
        original = persisted(save(domain), 8123, None)
        browser.action(domain, 'edit')
        closed('8123', '')
        browser.enter('http-port', '9191')
        driver.find_element(By.ID, 'cancel-edit').click()
        closed()
        require(value('name') == '' and browser.saved_connection(domain) == original,
                'Cancel changed saved ports or failed to clear the new editor')
        browser.action(domain, 'edit')
        browser.advanced()
        driver.find_element(By.ID, 'reset').click()
        browser.confirm_dialog()
        WebDriverWait(driver, 15).until(lambda _: not browser.cards(domain) and value('name') == '')
        closed()
        require(browser.saved_connection(domain) is None, 'Reset retained the saved connection')
        screenshot('advanced-reset-restores-defaults')
        return {'cancel_restored_defaults': True, 'reset_restored_defaults': True, 'saved_connection_removed': True}
    case('advanced-cancel-and-reset-restore-new-defaults', reset, fatal=True)

    def invalid_hidden():
        observations = []
        for label, field, invalid in (('http-low', 'http-port', '0'),
                                      ('http-required', 'http-port', ''),
                                      ('https-high', 'https-port', '65536')):
            domain = begin('invalid-' + label)
            browser.enter(field, invalid)
            browser.advanced(False)
            require(not driver.find_element(By.ID, field).is_displayed(), 'Invalid field was not hidden before Save')
            driver.execute_script('''
              window.portValidationEvents={invalid:[],submits:0};
              document.getElementById('connection-form').addEventListener('invalid',event=>{
                window.portValidationEvents.invalid.push(event.target.id);
              },true);
              document.getElementById('connection-form').addEventListener('submit',()=>{
                window.portValidationEvents.submits++;
              },true);
            ''')
            driver.find_element(By.ID, 'save').click()
            WebDriverWait(driver, 10).until(lambda _:
                driver.find_element(By.ID, 'advanced-settings').get_property('open') is True and
                driver.find_element(By.ID, field).is_displayed())
            observed = driver.execute_script('''
              const field=document.getElementById(arguments[0]);
              return {...window.portValidationEvents,active:document.activeElement.id,
                valid:field.validity.valid,visible:field.getClientRects().length>0,
                validationMessage:field.validationMessage};
            ''', field)
            require(field in observed['invalid'] and observed['submits'] == 0 and not observed['valid'] and
                    observed['visible'] and observed['validationMessage'], 'Native invalid-port validation was bypassed')
            require(not browser.cards(domain) and browser.saved_connection(domain) is None and value('name'),
                    'Invalid hidden port saved a connection or erased the draft')
            screenshot('advanced-invalid-' + label + '-revealed')
            # Android can reveal a native-invalid field without auto-focusing
            # it. Record that difference; require the user can tap and repair it.
            corrected = '8080' if field == 'http-port' else '8443'
            driver.find_element(By.ID, field).click()
            browser.enter(field, corrected)
            require(value(field) == corrected and driver.execute_script(
                'return document.getElementById(arguments[0]).validity.valid', field),
                'Real user input did not correct the revealed invalid port')
            screenshot('advanced-invalid-' + label + '-corrected')
            saved = persisted(save(domain), 8080 if field == 'http-port' else 80,
                              8443 if field == 'https-port' else 443)
            submits = driver.execute_script('return window.portValidationEvents.submits')
            require(submits == 1, 'Corrected port did not produce exactly one native form submission')
            observations.append({'field': field, 'input': invalid, **observed, 'saved': False,
                                 'correction': {'input': corrected, 'submits': submits, 'saved': saved}})
            browser.action(domain, 'delete')
        browser.options()
        driver.refresh()
        browser.wait_options()
        return observations
    case('advanced-invalid-hidden-ports-expand-block-and-allow-correction', invalid_hidden, fatal=True)


def observe(browser):
    global OPTIONS_URL
    browser.driver.set_context('chrome')
    try:
        result = browser.driver.execute_async_script('''
          const [id,done]=arguments;
          (async()=>{
            const {AddonManager}=ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
            const a=await AddonManager.getAddonByID(id);
            const uuid=JSON.parse(Services.prefs.getStringPref('extensions.webextensions.uuids','{}'))[id];
            done({addon_state:a&&{active:a.isActive,appDisabled:a.appDisabled,temporary:a.temporarilyInstalled,signedState:a.signedState},
              options_url:uuid&&'moz-extension://'+uuid+'/options.html',processID:Services.appinfo.processID,
              browser_version:Services.appinfo.version,buildID:Services.appinfo.appBuildID,
              signature_required:Services.prefs.getBoolPref('xpinstall.signatures.required'),
              experiments_preference:Services.prefs.getBoolPref('extensions.experiments.enabled')});
          })().catch(e=>done({error:String(e)}));
        ''', EXTENSION_ID)
    finally:
        browser.driver.set_context('content')
    require(result.get('addon_state', {}).get('active') is True, 'Actual experimental addon not active')
    require(result['addon_state']['temporary'] is False, 'Install was temporary')
    require(result.get('options_url'), 'No native options URL')
    OPTIONS_URL = result['options_url']
    return result


def profile_at(directory):
    directory.mkdir()
    subprocess.run(['certutil','-N','--empty-password','-d','sql:' + str(directory)], check=True)
    subprocess.run(['certutil','-A','-n','Isolated fixture CA','-t','C,,','-i','/certs/ca.pem','-d','sql:' + str(directory)], check=True)
    return directory


def run(args, proof, browser_factory):
    browser = None
    def save_proof():
        (ARTIFACTS / 'result.json').write_text(json.dumps(proof, indent=2))
    def case(name, action, fatal=False):
        row = {'name':name,'status':'failed','started_at':time.time()}
        proof['cases'].append(row)
        try:
            value = action()
            if value is not None:
                proof.setdefault('observations', {})[name] = value
            row['status'] = 'passed'
        except Exception:
            row['error'] = traceback.format_exc()
            if browser and browser.driver:
                try:
                    browser.screenshot(name + '-failed')
                    row['current_url'] = browser.driver.current_url
                    if row['current_url'] == OPTIONS_URL:
                        # Only the application's fixed validation message, never
                        # editor values, whole DOM or connection snapshots.
                        messages = browser.driver.find_elements(By.ID, 'error')
                        if messages:
                            row['ui_error'] = messages[0].text[:300]
                except WebDriverException:
                    pass
            if fatal:
                raise
        finally:
            row['finished_at'] = time.time()
            save_proof()
        return row['status'] == 'passed'

    with tempfile.TemporaryDirectory(prefix='portmap-browser-capability-') as directory:
        work = Path(directory)
        xpi = Path(args.xpi) if args.xpi else work / 'experimental.xpi'
        if not args.xpi:
            subprocess.run(['python','/app/tools/build_firefox_extension.py','build','--output',str(xpi)], check=True)
        subprocess.run(['python','/app/tools/build_firefox_extension.py','validate','--xpi',str(xpi)], check=True)
        proof['extension_sha256'] = hashlib.sha256(xpi.read_bytes()).hexdigest()
        shutil.copyfile(xpi, ARTIFACTS / 'extension-under-test.xpi')
        with zipfile.ZipFile(xpi) as archive:
            proof['extension_version'] = json.loads(archive.read('manifest.json'))['version']
        metadata = {letter:json.loads(Path('/meta', letter, 'client.json').read_text()) for letter in ('a','b')}
        addresses = {letter:socket.gethostbyname('node-' + letter) for letter in ('a','b')}
        proof['targets'] = {letter:{'host':addresses[letter],'sshPort':2222,'httpPort':8080,'httpsPort':8443} for letter in addresses}
        try:
            browser_factory.experiments_enabled = False
            browser = browser_factory(profile_at(work / 'unsupported-profile'), False)
            browser.driver.install_addon(str(xpi), temporary=False)
            proof['unsupported_install'] = observe(browser)
            def unavailable():
                browser.options()
                require(browser.driver.find_element(By.ID,'status').get_attribute('data-state') == 'unsupported', 'Missing APIs did not show unsupported state')
                require(browser.driver.find_element(By.ID,'capability-guidance').is_displayed(), 'Missing compatible-browser guidance')
                require(not browser.driver.find_element(By.ID,'mode').is_enabled() and not browser.driver.find_element(By.ID,'save').is_enabled(), 'Unsupported connection controls enabled')
                require(not browser.driver.find_element(By.ID, 'qr-open').is_enabled(), 'Unsupported capability allowed QR onboarding')
                browser.screenshot('unsupported-capability')
                browser.denied('a.portmap','capability-unavailable')
                browser.page('http://unrelated:8080/unsupported-ordinary','unrelated')
            case('actual-missing-capability-blocks-both-modes', unavailable, fatal=True)
            browser.close()
            browser = None
            browser_factory.experiments_enabled = True
            browser = browser_factory(profile_at(work / 'supported-profile'), False)
            proof['browser_version'] = browser.driver.capabilities['browserVersion']
            require(browser.driver.capabilities['acceptInsecureCerts'] is False, 'TLS validation bypassed')
            browser.driver.install_addon(str(xpi), temporary=False)
            admitted = observe(browser)
            proof.update(addon_state=admitted['addon_state'], signature_required=admitted['signature_required'], experimental=True)
            proof['supported_install'] = admitted
            def capability_ready():
                browser.options()
                ready = browser.driver.find_element(By.ID,'status').get_attribute('data-state') == 'ready'
                enabled = browser.driver.find_element(By.ID,'mode').is_enabled()
                require(ready and enabled, 'Actual native capability self-check did not enable editor')
                proof['capability_available'] = True
                browser.screenshot('capability-ready')
            case('actual-native-self-check-enables-both-modes', capability_ready, fatal=True)
            case('unconfigured-fails-closed', lambda: browser.denied('a.portmap','unconfigured'))
            advanced_ports(browser, case, addresses['a'])
            import autofill
            autofill.exercise(browser, case)
            case('save-independent-direct-and-ssh-nodes', lambda: (
                browser.save('a.portmap',addresses['a']),
                browser.save('b.portmap',addresses['b'],mode='ssh',metadata=metadata['b'])), fatal=True)
            def open_from_card():
                browser.action('a.portmap', 'open')
                browser.focus_page('http://a.portmap/')
                result = browser.page('http://a.portmap/', 'node-a', loopback=False, navigate=False)
                browser.screenshot('open-node-from-options')
                return result
            case('open-node-from-real-options-control', open_from_card, fatal=True)

            def application(domain, node, ssh, scheme='http'):
                label = domain.split('.')[0] + '-' + ('ssh' if ssh else 'direct') + '-' + scheme
                row = browser.page(scheme + '://' + domain + '/application-' + uuid.uuid4().hex,node,loopback=ssh)
                echo = browser.fetch('/echo','POST','portmap-body-✓',{'Authorization':'Bearer fixture-origin-token'})
                require(echo['body'] == 'portmap-body-✓' and echo['originAuthorizationCorrect'] and echo['peerLoopback'] is ssh, 'POST/origin auth/path mismatch')
                ws = browser.websocket('wss' if scheme == 'https' else 'ws',domain)
                require(ws['peerLoopback'] is ssh, 'WebSocket used wrong transport')
                gate = browser.driver.execute_async_script("const done=arguments[0];fetch('/ssh-only').then(r=>done(r.status)).catch(()=>done(0));")
                require(gate == (200 if ssh else 403), 'SSH-only gate contradicts transport')
                browser.screenshot(label)
                return {'page':row,'post':echo,'websocket':ws,'ssh_only_status':gate}

            def binary():
                result = browser.driver.execute_async_script('''
                  const done=arguments[0];(async()=>{
                    const bytes=Uint8Array.from({length:131072},(_,i)=>i%256);
                    const response=await fetch('/binary-echo',{method:'POST',body:bytes});
                    const got=new Uint8Array(await response.arrayBuffer());
                    const rows=await Promise.all(Array.from({length:4},(_,i)=>fetch('/echo?parallel='+i).then(r=>r.json())));
                    const binaryWs=await new Promise((resolve,reject)=>{
                      const ws=new WebSocket(location.origin.replace(/^http/,'ws')+'/ws');ws.binaryType='arraybuffer';
                      const timer=setTimeout(()=>{ws.close();reject(new Error('timeout'));},15000);
                      ws.onopen=()=>ws.send(bytes.subarray(0,32768));ws.onerror=()=>{clearTimeout(timer);reject(new Error('WebSocket failed'));};
                      ws.onmessage=e=>{clearTimeout(timer);const data=new Uint8Array(e.data);ws.close();resolve(data.length===32768&&data.every((v,i)=>v===i%256));};
                    });
                    done({status:response.status,binary:got.length===bytes.length&&got.every((v,i)=>v===i%256),rows,binaryWs});
                  })().catch(e=>done({error:String(e)}));
                ''')
                require(result.get('status') == 200 and result.get('binary') is True and result.get('binaryWs') is True, 'Binary transfer failed')
                require([row['path'] for row in result['rows']] == ['/echo?parallel=' + str(i) for i in range(4)], 'Concurrent channels crossed')
                return result

            for domain,node,ssh in [('a.portmap','node-a',False),('b.portmap','node-b',True)]:
                case(domain + '-http-app',lambda d=domain,n=node,s=ssh: application(d,n,s),fatal=True)
                case(domain + '-binary-concurrency',binary)
                case(domain + '-https-wss',lambda d=domain,n=node,s=ssh: application(d,n,s,'https'))
                case(domain + '-descendant',lambda d=domain,n=node,s=ssh: application('app.'+d,n,s))
            case('ssh-key-configuration',lambda: browser.save('b.portmap',addresses['b'],mode='ssh',auth='key',metadata=metadata['b'],edit=True),fatal=True)
            case('ssh-key-http-app',lambda: application('b.portmap','node-b',True),fatal=True)
            case('ssh-key-binary-concurrency',binary)
            case('ssh-key-https-wss',lambda: application('b.portmap','node-b',True,'https'))
            case('unknown-namespace-rejected',lambda: browser.denied('unknown.portmap','unknown'))

            def bad_connection(fault):
                browser.save('b.portmap',addresses['b'],mode='ssh',metadata=metadata['b'],fault=fault,edit=True)
                browser.denied('b.portmap',fault)
                browser.page('http://a.portmap/other-node-remains-' + fault,'node-a',loopback=False)
                browser.page('http://unrelated:8080/ordinary-after-' + fault,'unrelated')
            for fault in ('password','pin','ssh-port'):
                case('ssh-reject-' + fault,lambda f=fault: bad_connection(f))
            browser.save('b.portmap',addresses['b'],mode='ssh',auth='key',metadata=metadata['b'],edit=True)
            def unreachable_direct():
                browser.save('a.portmap', addresses['a'], fault='direct-port', edit=True)
                browser.denied('a.portmap', 'direct-port')
                application('b.portmap', 'node-b', True)
                browser.save('a.portmap', addresses['a'], edit=True)
            case('unreachable-direct-keeps-other-node', unreachable_direct)
            def lost_session():
                application('b.portmap','node-b',True)
                gate = uuid.uuid4().hex
                (ARTIFACTS/'control-request.json').write_text(json.dumps({'gate':gate,'action':'terminate-ssh','service':'node-b'}))
                deadline = time.monotonic()+60
                while time.monotonic()<deadline:
                    path=ARTIFACTS/'control-response.json'
                    if path.exists():
                        response=json.loads(path.read_text())
                        if response.get('gate')==gate:
                            require(response.get('terminated',0)>0,'No live owned SSH process terminated')
                            break
                    time.sleep(.1)
                else:
                    raise AssertionError('Owned SSH interruption controller timed out')
                browser.denied('b.portmap','active-session-loss')
                browser.page('http://a.portmap/independent-after-ssh-loss','node-a',loopback=False)
                return response
            case('active-ssh-loss-no-direct-fallback',lost_session)
            # Explicitly reapply after a failed session: this is not an auto-reconnect claim.
            browser.save('b.portmap',addresses['b'],mode='ssh',auth='key',metadata=metadata['b'],edit=True)
            def disabled():
                browser.action('a.portmap','toggle')
                browser.denied('a.portmap','disabled')
                application('b.portmap','node-b',True)
                browser.action('a.portmap','toggle')
            case('disabled-node-isolated',disabled)
            def unavailable_update():
                browser.options()
                browser.enter('name','unsaved-draft')
                browser.driver.find_element(By.ID,'check-update').click()
                WebDriverWait(browser.driver,40).until(lambda d:d.find_element(By.ID,'update-status').get_attribute('data-state')=='error')
                require(browser.driver.find_element(By.ID,'name').get_attribute('value')=='unsaved-draft','Update failure erased draft')
                require(len(browser.cards('a.portmap'))==1 and len(browser.cards('b.portmap'))==1,'Update check changed saved nodes')
            case('update-feed-offline-preserves-state',unavailable_update)
            before = observe(browser)
            browser.restart()
            after = observe(browser)
            require(before['processID'] != after['processID'],'Native browser did not restart')
            proof['restart']={'before':before,'after':after,'reinstalled':False}
            case('restart-direct-config-and-page',lambda: application('a.portmap','node-a',False))
            case('restart-ssh-key-config-and-page',lambda: application('b.portmap','node-b',True))
            import qr
            qr.exercise(browser, case, proof)
            case('delete-node-revokes-route',lambda:(browser.action('a.portmap','delete'),browser.denied('a.portmap','deleted')))
            import managed
            managed.exercise(browser, case, proof, metadata['b'], addresses['b'])
            proof['origin_requests']={origin:records(origin) for origin in ORIGINS}
            require(not proof['origin_requests']['trap'],'Claimed traffic reached DNS fallback trap')
            proof['dns_diagnostics']=records('dns','/queries')
            save_proof()
            if case('actual-network-path-evidence', browser.assert_network_paths):
                proof['network_audit'] = json.loads((ARTIFACTS / 'network-audit.json').read_text())
            require(all(row['status']=='passed' for row in proof['cases']),'Actual browser cases failed')
            proof['status']='passed'
        finally:
            if browser:
                browser.close()
            save_proof()


def main(*, platform='desktop-firefox', browser_factory=Browser, prepare=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--development',action='store_true',required=True)
    parser.add_argument('--xpi')
    args=parser.parse_args()
    args.require_signed=False
    os.umask(0o077)
    ARTIFACTS.mkdir(exist_ok=True)
    proof={'status':'failed','platform':platform,'source_commit':os.environ.get('BROWSER_SOURCE_COMMIT',''),
           'signature_required':False,'experimental':True,'capability_available':False,'cases':[]}
    try:
        provenance = json.loads((ARTIFACTS / 'source-provenance.json').read_text())
        proof.update(provenance)
        proof['source_commit'] = provenance['source_provenance']['commit']
        proof['source_commit_label'] = 'checkout-base-with-uncommitted-changes' if provenance['source_provenance']['dirty'] else 'clean-checkout'
        import qr
        qr.prepare(proof)
        if prepare:
            prepare(args,proof)
        run(args,proof,browser_factory)
    except Exception:
        proof['status']='failed'
        proof['error']=traceback.format_exc()
    finally:
        (ARTIFACTS/'result.json').write_text(json.dumps(proof,indent=2))
        print(json.dumps({'status':proof['status'],'cases':proof['cases']},indent=2),flush=True)
    return 0 if proof['status']=='passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
