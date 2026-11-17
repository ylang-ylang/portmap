"""Isolated Android camera prerequisite, never imports or installs Portmap.

Uses an owned emulator imagefile camera and genuine Firefox getUserMedia. The
scene is synthetic, not a physical camera. No host camera devices are mounted.
Run only a copied/frozen version, with /dev/kvm and an empty /artifacts mount.
"""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import socketserver
import struct
import subprocess
import threading
import time
import zlib

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait


def synthetic_png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    rows = b''.join(b'\0' + b''.join(bytes((240, 20, 20) if not (160 < x < 480 and 160 < y < 480) else (0, 0, 0)) for x in range(640)) for y in range(640))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 640, 640, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')


PAGE = b'''<!doctype html><meta charset=utf-8><button id=start>Start synthetic camera</button><video id=video autoplay muted playsinline></video><script>
start.onclick=async()=>{try{window.stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:'environment'},width:{ideal:640},height:{ideal:480}}});video.srcObject=stream;await video.play();window.ready=true;}catch(e){window.error=e.name;}};
</script>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', default='/artifacts')
    args = parser.parse_args()
    out = Path(args.artifacts)
    out.mkdir(parents=True, exist_ok=True)
    package = 'org.mozilla.fenix' if os.environ.get('ANDROID_FIREFOX_CHANNEL') == 'nightly' else 'org.mozilla.firefox'
    receipt = {'status': 'failed', 'synthetic_scene': True, 'physical_camera': False,
               'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'media_api_overridden': False, 'backend': 'emulator-imagefile'}
    processes, driver, server = [], None, None
    def adb(*values, **kwargs):
        kwargs.setdefault('check', True)
        return subprocess.run(['adb', '-s', 'emulator-5554', *values], **kwargs)
    try:
        image = out / 'synthetic-camera.png'
        image.write_bytes(synthetic_png())
        receipt['scene_sha256'] = hashlib.sha256(image.read_bytes()).hexdigest()
        subprocess.run(['emulator', '-help-camera-back'], check=True, stdout=(out / 'camera-back-help.txt').open('w'), stderr=subprocess.STDOUT)
        avds = Path(os.environ['ANDROID_AVD_HOME'])
        avd = avds / 'camera-probe.avd'
        avd.mkdir(parents=True, exist_ok=True)
        (avds / 'camera-probe.ini').write_text('avd.ini.encoding=UTF-8\npath=' + str(avd) + '\ntarget=android-30\n')
        (avd / 'config.ini').write_text('AvdId=camera-probe\navd.ini.encoding=UTF-8\nabi.type=x86_64\nhw.cpu.arch=x86_64\nhw.cpu.ncore=2\nhw.ramSize=2048\nhw.gpu.enabled=yes\nhw.gpu.mode=swiftshader_indirect\nhw.lcd.width=1080\nhw.lcd.height=1920\nhw.lcd.density=420\nimage.sysdir.1=system-images/android-30/default/x86_64/\ntag.id=default\ntag.display=Default\ndisk.dataPartition.size=3G\n')
        subprocess.run(['adb', 'start-server'], check=True)
        command = ['emulator', '-avd', 'camera-probe', '-port', '5554', '-accel', 'on', '-no-window', '-no-audio', '-no-snapshot', '-no-boot-anim', '-gpu', 'swiftshader_indirect', '-camera-back', 'imagefile:' + str(image), '-camera-front', 'none']
        receipt['emulator_command'] = command
        process = subprocess.Popen(command, stdout=(out / 'emulator.log').open('w'), stderr=subprocess.STDOUT)
        processes.append(process)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Owned emulator exited: inspect emulator.log')
            boot = subprocess.run(['adb', '-s', 'emulator-5554', 'shell', 'getprop', 'sys.boot_completed'], capture_output=True, text=True, timeout=10)
            if boot.returncode == 0 and boot.stdout.strip() == '1':
                break
            time.sleep(2)
        else:
            raise RuntimeError('Owned emulator boot deadline exceeded')
        adb('root', check=False, capture_output=True)
        adb('wait-for-device', timeout=30)
        adb('install', '--no-streaming', '-r', '/opt/android/firefox.apk', timeout=120, stdout=(out / 'install.log').open('w'), stderr=subprocess.STDOUT)
        adb('shell', 'input', 'keyevent', '82')
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(PAGE)
            def log_message(self, *unused):
                pass
        server = socketserver.TCPServer(('127.0.0.1', 8765), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        adb('reverse', 'tcp:8765', 'tcp:8765')
        options = Options()
        options.enable_mobile(android_package=package, device_serial='emulator-5554')
        options.mobile_options['androidIntentArguments'] = ['-a', 'android.intent.action.VIEW', '-d', 'http://localhost:8765/', '--ez', 'automationtest', 'true']
        options.set_preference('media.navigator.permission.disabled', True)
        options.set_preference('media.navigator.streams.fake', False)
        options.set_preference('remote.log.level', 'Info')
        driver = webdriver.Firefox(options=options, service=Service('/usr/local/bin/geckodriver', log_output=str(out / 'geckodriver.log')))
        driver.set_script_timeout(45)
        adb('shell', 'pm', 'grant', package, 'android.permission.CAMERA')
        driver.get('http://localhost:8765/')
        driver.find_element(By.ID, 'start').click()
        WebDriverWait(driver, 45).until(lambda d: d.execute_script('return window.ready || window.error'))
        result = driver.execute_async_script('''const done=arguments[0];(async()=>{
          if(window.error){done({error:window.error});return;}
          await new Promise(r=>setTimeout(r,1200));
          const c=document.createElement('canvas');c.width=video.videoWidth;c.height=video.videoHeight;
          const ctx=c.getContext('2d');ctx.drawImage(video,0,0);const p=ctx.getImageData(0,0,c.width,c.height).data;
          let red=0,black=0;for(let i=0;i<p.length;i+=4){if(p[i]>150&&p[i+1]<90&&p[i+2]<90)red++;if(p[i]<60&&p[i+1]<60&&p[i+2]<60)black++;}
          const tracks=stream.getTracks();const before=tracks.map(t=>({kind:t.kind,state:t.readyState,settings:t.getSettings()}));
          tracks.forEach(t=>t.stop());video.srcObject=null;
          done({width:c.width,height:c.height,redFraction:red/(p.length/4),blackFraction:black/(p.length/4),before,after:tracks.map(t=>t.readyState),secureContext:isSecureContext});
        })().catch(e=>done({error:e.name}));''')
        receipt['capture'] = result
        if not (result.get('redFraction', 0) > .2 and result.get('blackFraction', 0) > .05 and result.get('after') == ['ended']):
            raise RuntimeError('Native camera did not deliver owned synthetic scene or stop track')
        receipt['browser_version'] = driver.capabilities['browserVersion']
        receipt['status'] = 'passed'
    except Exception as error:
        receipt['error'] = str(error)
        raise
    finally:
        if driver:
            driver.quit()
        if server:
            server.shutdown()
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        (out / 'camera-prerequisite.json').write_text(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
