"""QR UI integration using CLI PNGs, actual file inputs and native camera tracks.

Android camera frames come from the emulator's supported imagefile backend;
this is a synthetic owned scene, not physical-camera validation. Desktop uses
Firefox's native fake video source only for track lifecycle, never as QR proof.
No navigator/mediaDevices/getUserMedia function is replaced by this harness.
"""
import base64
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait

import runner


DIRECT_DOMAIN = 'qr-direct.portmap'
HTTP_ONLY_DOMAIN = 'qr-http-only.portmap'
SSH_DOMAIN = 'qr-ssh.portmap'
CAMERA_DOMAIN = 'qr-camera.portmap'


def prepare(proof):
    """Run the real read-only CLI before Android opens its imagefile camera."""
    from portmap.connection_qr import qr_png
    directory = runner.ARTIFACTS / 'qr'
    directory.mkdir(exist_ok=True)
    metadata = json.loads(Path('/meta/b/client.json').read_text())
    addresses = {letter: socket.gethostbyname('node-' + letter) for letter in ('a', 'b')}
    fixtures = {}
    for label, domain, mode in [('direct', DIRECT_DOMAIN, 'direct'), ('http-only', HTTP_ONLY_DOMAIN, 'direct'),
                                ('ssh', SSH_DOMAIN, 'ssh'), ('camera', CAMERA_DOMAIN, 'direct')]:
        target = 'b' if mode == 'ssh' else 'a'
        png = directory / (label + '.png')
        command = [sys.executable, '-c', 'from portmap.cli import main; raise SystemExit(main())',
                   'connection-qr', '--name', 'Owned QR ' + label, '--domain', domain,
                   '--mode', mode, '--host', addresses[target], '--http-port', '8080',
                   '--png', str(png), '--wire', '--no-terminal']
        command += ['--no-https'] if label == 'http-only' else ['--https-port', '8443']
        if mode == 'ssh':
            command += ['--ssh-port', str(metadata['sshPort']), '--ssh-username', metadata['username'],
                        '--ssh-target-host', '127.0.0.1', '--ssh-host-key-sha256', metadata['hostKeySha256Hex']]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        wire = result.stdout.strip()
        runner.require(wire.startswith('portmap-connect:v1:'), 'Actual CLI did not emit the connection wire')
        (directory / (label + '.wire')).write_text(wire + '\n')
        fixtures[label] = {'command': command, 'png_sha256': hashlib.sha256(png.read_bytes()).hexdigest(),
                           'wire_sha256': hashlib.sha256(wire.encode()).hexdigest(), 'domain': domain}
    def wire(raw):
        return 'portmap-connect:v1:' + base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')
    valid = json.loads(base64.urlsafe_b64decode((directory / 'direct.wire').read_text().strip().split(':', 2)[2] + '==='))
    compact = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    vectors = {
        'malformed': 'portmap-connect:v1:not-base64!',
        'foreign-url': 'http://node-a:8080/qr-must-not-fetch-' + uuid.uuid4().hex,
        'secret': wire(compact(dict(valid, password='owned-negative-secret-not-an-auth-grant'))),
        'version': (directory / 'direct.wire').read_text().strip().replace(':v1:', ':v2:', 1),
        'duplicate': wire(compact(valid).replace('"domain":', '"domain":"other.portmap","domain":', 1)),
        'unknown-field': wire(compact(dict(valid, enabled=True))),
        'overlap': wire(compact(dict(valid, domain='child.a.portmap'))),
    }
    from camera_scene import padded_cli_png
    camera_scene = padded_cli_png((directory / 'camera.png').read_bytes())
    (directory / 'camera-scene.png').write_bytes(camera_scene)
    fixtures['camera']['synthetic_padded_scene_sha256'] = hashlib.sha256(camera_scene).hexdigest()
    for label, text in vectors.items():
        (directory / (label + '.png')).write_bytes(qr_png(text))
    for kind in ('private', 'public'):
        (directory / ('stale-' + kind + '.txt')).write_text('owned-stale-' + kind + '-file')
    proof['qr_fixtures'] = fixtures
    proof['qr_camera_boundary'] = {'physical_camera': False, 'media_api_overridden': False,
                                   'android_source': 'supported emulator -camera-back imagefile:<CLI PNG>',
                                   'desktop_source': 'Firefox native fake camera; lifecycle only, not QR-scene proof'}


def exercise(browser, case, proof):
    driver = browser.driver
    directory = runner.ARTIFACTS / 'qr'
    metadata = json.loads(Path('/meta/b/client.json').read_text())
    android = proof['platform'] == 'android-firefox'

    def config_digest():
        value = driver.execute_async_script('''const done=arguments[0];(async()=>{
          const {config}=await browser.storage.local.get('config');
          const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(config)));
          done(Array.from(new Uint8Array(digest),x=>x.toString(16).padStart(2,'0')).join(''));
        })().catch(()=>done(null));''')
        runner.require(value is not None, 'Cannot observe saved config digest')
        return value

    def origin_counts():
        return {origin: len(runner.records(origin)) for origin in runner.ORIGINS}

    def state():
        return driver.find_element(By.ID, 'qr-status').get_attribute('data-state')

    def wait_state(*states):
        WebDriverWait(driver, 20).until(lambda _: state() in states)
        return state()

    def open_panel():
        driver.find_element(By.ID, 'qr-open').click()
        WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID, 'qr-panel').is_displayed())

    def choose_file(field, filename):
        if android:
            import android as android_harness
            # Raw Download paths are not readable by scoped-storage Fenix
            # without a native chooser grant. Stage only owned fixture bytes in
            # this disposable app's external files; no storage permission added.
            remote_directory = '/sdcard/Android/data/' + android_harness.PACKAGE + '/files/portmap-qr'
            android_harness.adb('shell', 'mkdir', '-p', remote_directory, capture_output=True)
            remote = remote_directory + '/' + filename.name
            android_harness.adb('push', str(filename), remote, capture_output=True)
            filename = remote
        driver.find_element(By.ID, field).send_keys(str(filename))

    def image(label):
        choose_file('qr-image', directory / (label + '.png'))
        wait_state('preview', 'error')

    def no_stream():
        runner.require(driver.execute_script('return document.getElementById("qr-video").srcObject===null'), 'Camera retained stream after stop')

    def seed_draft():
        browser.enter('name', 'owned-unsaved-qr-draft')
        browser.enter('domain', 'unsaved-qr.portmap')

    def unchanged(digest, counts):
        runner.require(config_digest() == digest, 'QR changed configuration before confirmation')
        runner.require(origin_counts() == counts, 'Inert QR operation sent a receiver request')

    def cli_receipt():
        for label, fixture in proof['qr_fixtures'].items():
            runner.require(hashlib.sha256((directory / (label + '.png')).read_bytes()).hexdigest() == fixture['png_sha256'], 'CLI PNG changed before chooser')
        return proof['qr_fixtures']
    case('qr-cli-produces-owned-fixtures', cli_receipt, fatal=True)

    def cancel_preview():
        browser.options()
        seed_draft()
        digest, counts = config_digest(), origin_counts()
        open_panel()
        image('direct')
        runner.require(state() == 'preview', 'Valid CLI image did not decode')
        runner.require(DIRECT_DOMAIN in driver.find_element(By.ID, 'qr-preview').text, 'Preview omitted destination')
        no_stream()
        browser.screenshot('qr-image-preview')
        unchanged(digest, counts)
        driver.find_element(By.ID, 'qr-cancel').click()
        runner.require(not driver.find_element(By.ID, 'qr-panel').is_displayed(), 'Cancel left scanner open')
        runner.require(driver.find_element(By.ID, 'name').get_attribute('value') == 'owned-unsaved-qr-draft', 'Cancel destroyed unrelated draft')
        unchanged(digest, counts)
    case('qr-image-preview-cancel-inert', cancel_preview, fatal=True)

    def direct_confirm():
        open_panel()
        image('direct')
        digest, counts = config_digest(), origin_counts()
        unchanged(digest, counts)
        driver.find_element(By.ID, 'qr-confirm').click()
        WebDriverWait(driver, 15).until(lambda _: len(browser.cards(DIRECT_DOMAIN)) == 1)
        runner.require(driver.find_element(By.ID, 'name').get_attribute('value') == 'owned-unsaved-qr-draft', 'Direct import destroyed unrelated draft')
        runner.require(browser.cards(DIRECT_DOMAIN)[0].get_attribute('data-mode') == 'direct', 'QR direct import selected wrong mode')
        imported = browser.saved_connection(DIRECT_DOMAIN)
        runner.require(imported and imported['httpPort'] == 8080 and imported['httpsPort'] == 8443,
                       'Direct QR import replaced custom ports with new-form defaults')
        return browser.page('http://' + DIRECT_DOMAIN + '/qr-direct-' + uuid.uuid4().hex, 'node-a', loopback=False)
    case('qr-image-direct-confirm-route', direct_confirm, fatal=True)

    def ports_reedit(domain, https, screenshot):
        browser.action(domain, 'edit')
        runner.require(driver.find_element(By.ID, 'advanced-settings').get_property('open') is False,
                       'Editing a QR connection unexpectedly opened Advanced')
        browser.advanced()
        runner.require(driver.find_element(By.ID, 'http-port').get_attribute('value') == '8080' and
                       driver.find_element(By.ID, 'https-port').get_attribute('value') == ('' if https is None else str(https)),
                       'QR ports were not retained in the real editor')
        browser.screenshot(screenshot)
        browser.advanced(False)
        browser.enter('name', 'Renamed QR direct endpoint')
        driver.find_element(By.ID, 'save').click()
        WebDriverWait(driver, 15).until(lambda _: driver.find_element(By.ID, 'name').get_attribute('value') == '')
        imported = browser.saved_connection(domain)
        runner.require(imported and imported['httpPort'] == 8080 and imported['httpsPort'] == https,
                       'Saving an unchanged QR endpoint replaced its imported ports')
        return imported
    case('qr-imported-custom-ports-retained-through-edit',
         lambda: ports_reedit(DIRECT_DOMAIN, 8443, 'qr-imported-custom-ports-reedit'), fatal=True)

    def http_only_confirm():
        browser.options()
        open_panel()
        image('http-only')
        runner.require(state() == 'preview', 'HTTP-only CLI QR did not decode')
        driver.find_element(By.ID, 'qr-confirm').click()
        WebDriverWait(driver, 15).until(lambda _: len(browser.cards(HTTP_ONLY_DOMAIN)) == 1)
        imported = browser.saved_connection(HTTP_ONLY_DOMAIN)
        runner.require(imported and imported['httpPort'] == 8080 and imported['httpsPort'] is None,
                       'HTTP-only QR import invented a default HTTPS port')
        edited = ports_reedit(HTTP_ONLY_DOMAIN, None, 'qr-imported-null-https-reedit')
        runner.require(edited['id'] == imported['id'], 'HTTP-only QR re-edit replaced the saved identity')
        route = browser.page('http://' + HTTP_ONLY_DOMAIN + '/qr-http-only-' + uuid.uuid4().hex, 'node-a', loopback=False)
        browser.denied(HTTP_ONLY_DOMAIN, 'qr-null-https', scheme='https')
        return {'saved': edited, 'http_route': route, 'https_blocked': True}
    case('qr-imported-null-https-retained-through-edit-and-route', http_only_confirm, fatal=True)

    def ssh_confirm():
        browser.action('b.portmap', 'edit')
        browser.ssh_advanced()
        runner.require(driver.execute_script('''
          return ["ssh-password","ssh-private-key","ssh-public-key","ssh-passphrase"]
            .every(id=>document.getElementById(id).value==="");
        '''), 'Editing stored SSH credentials exposed private material in the DOM')
        browser.sensitive_input = True
        Select(driver.find_element(By.ID, 'ssh-auth-type')).select_by_value('key')
        replacement = driver.find_element(By.ID, 'replace-credentials')
        if replacement.is_displayed() and not replacement.is_selected():
            replacement.click()
        for kind in ('private', 'public'):
            choose_file('ssh-' + kind + '-key-file', directory / ('stale-' + kind + '.txt'))
            WebDriverWait(driver, 10).until(lambda d, kind=kind: d.execute_script('''
              return document.getElementById(arguments[0]).value===arguments[1];
            ''', 'ssh-' + kind + '-key', 'owned-stale-' + kind + '-file'))
        browser.enter('ssh-passphrase', 'owned-stale-passphrase')
        Select(driver.find_element(By.ID, 'ssh-auth-type')).select_by_value('password')
        browser.enter('ssh-password', 'owned-stale-password')
        digest, counts = config_digest(), origin_counts()
        open_panel()
        image('ssh')
        runner.require(state() == 'preview', 'SSH CLI image did not decode')
        preview = driver.find_element(By.ID, 'qr-preview').text
        runner.require(SSH_DOMAIN in preview and metadata['hostKeySha256Hex'] in preview, 'SSH preview omitted domain or fingerprint')
        unchanged(digest, counts)
        driver.find_element(By.ID, 'qr-confirm').click()
        WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID, 'domain').get_attribute('value') == SSH_DOMAIN)
        runner.require(driver.find_element(By.ID, 'connection-id').get_attribute('value') == '', 'SSH QR retained old edit identity')
        runner.require(driver.execute_script('''
          return ["ssh-password","ssh-private-key","ssh-public-key","ssh-passphrase"]
            .every(id=>document.getElementById(id).value==="");
        '''), 'SSH QR retained old authentication material')
        runner.require(driver.execute_script('return Array.from(document.querySelectorAll("input[type=file]")).every(x=>x.files.length===0)'), 'SSH QR retained file inputs')
        runner.require(driver.find_element(By.ID, 'ssh-username').get_attribute('value') == metadata['username'], 'SSH username differs from public QR')
        runner.require(driver.find_element(By.ID, 'ssh-target-host').get_attribute('value') == '127.0.0.1', 'SSH target differs from QR')
        runner.require(driver.find_element(By.ID, 'advanced-settings').get_property('open') is False and
                       driver.find_element(By.ID, 'http-port').get_attribute('value') == '8080' and
                       driver.find_element(By.ID, 'https-port').get_attribute('value') == '8443',
                       'SSH QR public draft replaced imported custom ports')
        runner.require(driver.find_element(By.ID, 'ssh-auth-type').get_attribute('value') == 'managed' and
                       not driver.find_element(By.ID, 'save').is_enabled(),
                       'Public SSH QR bypassed the default browser-key trust confirmation')
        unchanged(digest, counts)
        browser.sensitive_input = False
        driver.execute_script('document.getElementById("connection-form").scrollIntoView({behavior:"instant",block:"start"})')
        browser.screenshot('qr-ssh-public-draft')
        driver.execute_script('document.getElementById("ssh-fields").scrollIntoView({behavior:"instant",block:"start"})')
        browser.screenshot('qr-ssh-trust-and-empty-auth')
        # Imported endpoints must no longer belong to name autofill.
        host = driver.find_element(By.ID, 'host').get_attribute('value')
        browser.enter('name', 'renamed-public-ssh-import')
        runner.require(driver.find_element(By.ID, 'host').get_attribute('value') == host and driver.find_element(By.ID, 'domain').get_attribute('value') == SSH_DOMAIN, 'Imported endpoints retained autofill ownership')
        browser.ssh_advanced()
        Select(driver.find_element(By.ID, 'ssh-auth-type')).select_by_value('password')
        browser.sensitive_input = True
        browser.enter('ssh-password', metadata['password'])
        driver.find_element(By.ID, 'save').click()
        WebDriverWait(driver, 15).until(lambda _: len(browser.cards(SSH_DOMAIN)) == 1)
        browser.sensitive_input = False
        runner.require(len(browser.cards('b.portmap')) == 1, 'SSH import overwrote existing record')
        imported = browser.saved_connection(SSH_DOMAIN)
        runner.require(imported and imported['httpPort'] == 8080 and imported['httpsPort'] == 8443,
                       'SSH QR save replaced imported custom ports')
        browser.action(SSH_DOMAIN, 'edit')
        runner.require(driver.find_element(By.ID, 'advanced-settings').get_property('open') is False and
                       driver.find_element(By.ID, 'http-port').get_attribute('value') == '8080' and
                       driver.find_element(By.ID, 'https-port').get_attribute('value') == '8443',
                       'SSH QR re-edit replaced imported custom ports')
        driver.find_element(By.ID, 'cancel-edit').click()
        return browser.page('http://' + SSH_DOMAIN + '/qr-ssh-' + uuid.uuid4().hex, 'node-b', loopback=True)
    case('qr-ssh-public-fill-clears-auth-and-routes', ssh_confirm, fatal=True)

    def reject(label):
        browser.options()
        seed_draft()
        digest, counts = config_digest(), origin_counts()
        open_panel()
        image(label)
        runner.require(state() == 'error', 'Unsafe QR was not rejected: ' + label)
        if label == 'overlap':
            runner.require(not driver.find_element(By.ID, 'qr-confirm').is_enabled(), 'Overlapping QR remained confirmable')
        else:
            runner.require(not driver.find_element(By.ID, 'qr-preview').is_displayed(), 'Unsafe QR displayed a confirmable preview')
        no_stream()
        driver.find_element(By.ID, 'qr-cancel').click()
        runner.require(driver.find_element(By.ID, 'name').get_attribute('value') == 'owned-unsaved-qr-draft', 'Rejected QR destroyed draft')
        unchanged(digest, counts)
        return {'config_unchanged': True, 'receiver_counts_unchanged': True, 'remote_fetch': False}
    for label in ('malformed', 'foreign-url', 'secret', 'version', 'duplicate', 'unknown-field', 'overlap'):
        case('qr-reject-' + label, lambda label=label: reject(label), fatal=True)

    def watch_tracks():
        driver.execute_script('''window.qrObservedTracks=[];
          document.getElementById('qr-video').addEventListener('loadedmetadata',()=>{
            const s=document.getElementById('qr-video').srcObject;
            if(s)window.qrObservedTracks.push(...s.getTracks());
          });''')

    def tracks_ended():
        result = driver.execute_script('return window.qrObservedTracks.map(t=>({kind:t.kind,state:t.readyState}))')
        runner.require(result and all(t['kind'] == 'video' and t['state'] == 'ended' for t in result), 'Native video tracks not observed and stopped')
        no_stream()
        return result

    def camera_cancel():
        browser.options()
        digest, counts = config_digest(), origin_counts()
        open_panel()
        # Cancel through the real button as soon as the real camera stream has
        # metadata, before decoding can immediately stop a valid QR scene.
        driver.execute_script('''
          window.qrCancelEvidence=null;
          const video=document.getElementById('qr-video');
          video.addEventListener('loadedmetadata',()=>{
            const tracks=video.srcObject.getTracks();
            const before=tracks.map(t=>({kind:t.kind,state:t.readyState}));
            document.getElementById('qr-cancel').click();
            window.qrCancelEvidence={before,after:tracks.map(t=>({kind:t.kind,state:t.readyState}))};
          },{once:true});
        ''')
        driver.find_element(By.ID, 'qr-camera').click()
        WebDriverWait(driver, 30).until(lambda d: d.execute_script('return window.qrCancelEvidence!==null'))
        observed = driver.execute_script('return window.qrCancelEvidence')
        runner.require(observed['before'] and all(t['kind'] == 'video' and t['state'] == 'live' for t in observed['before']), 'Cancel did not observe live native video')
        runner.require(all(t['state'] == 'ended' for t in observed['after']), 'Cancel did not stop actual camera tracks')
        runner.require(not driver.find_element(By.ID, 'qr-panel').is_displayed(), 'Camera cancel left scanner open')
        no_stream()
        unchanged(digest, counts)
        return {'media_api_overridden': False, 'cancelled': True, 'synthetic_scene': True, **observed}
    case('qr-camera-track-cancel-lifecycle', camera_cancel, fatal=True)

    def native_camera():
        browser.options()
        digest, counts = config_digest(), origin_counts()
        open_panel()
        watch_tracks()
        driver.find_element(By.ID, 'qr-camera').click()
        if android:
            wait_state('preview', 'error')
            runner.require(state() == 'preview', 'Native emulator imagefile camera did not decode QR')
            runner.require(CAMERA_DOMAIN in driver.find_element(By.ID, 'qr-preview').text, 'Camera decoded wrong destination')
            browser.screenshot('qr-camera-preview')
            tracks = tracks_ended()
            unchanged(digest, counts)
            driver.find_element(By.ID, 'qr-confirm').click()
            WebDriverWait(driver, 15).until(lambda _: len(browser.cards(CAMERA_DOMAIN)) == 1)
            route = browser.page('http://' + CAMERA_DOMAIN + '/qr-camera-' + uuid.uuid4().hex, 'node-a', loopback=False)
            return {'tracks': tracks, 'route': route, 'source': 'emulator-imagefile CLI PNG', 'synthetic_scene': True, 'physical_camera': False, 'media_api_overridden': False}
        wait_state('scanning', 'error')
        runner.require(state() == 'scanning', 'Firefox native fake camera did not start')
        WebDriverWait(driver, 10).until(lambda d: d.execute_script('return window.qrObservedTracks.length>0'))
        runner.require(driver.execute_script('return window.qrObservedTracks.every(t=>t.readyState==="live")'), 'Fake camera track was not live')
        driver.find_element(By.ID, 'qr-cancel').click()
        tracks = tracks_ended()
        unchanged(digest, counts)
        return {'tracks': tracks, 'source': 'Firefox native fake source; no QR scene', 'synthetic_scene': True, 'physical_camera': False, 'media_api_overridden': False}
    case('qr-camera-native-synthetic-scene-route' if android else 'qr-camera-native-fake-lifecycle', native_camera, fatal=True)

    # Do not leave extra routes to change the existing persistence/deletion cases.
    for domain in (DIRECT_DOMAIN, HTTP_ONLY_DOMAIN, SSH_DOMAIN, CAMERA_DOMAIN) if android else (DIRECT_DOMAIN, HTTP_ONLY_DOMAIN, SSH_DOMAIN):
        browser.action(domain, 'delete')
