"""Actual AOSP emulator + official Firefox Android; never desktop viewport mode."""
import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import xml.etree.ElementTree as ET

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import runner

SERIAL = "emulator-5554"
CHANNEL = os.environ.get("ANDROID_FIREFOX_CHANNEL", "release")
PACKAGE = "org.mozilla.fenix" if CHANNEL == "nightly" else "org.mozilla.firefox"
PROCESSES = []


def adb(*args, **kwargs):
    kwargs.setdefault("check", True)
    return subprocess.run(["adb", "-s", SERIAL, *args], **kwargs)


def cleanup():
    for process in reversed(PROCESSES):
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


atexit.register(cleanup)


def prepare(args, proof):
    runner.require(CHANNEL == ("release" if args.require_signed else "nightly"), "Signed release mode requires official release APK; explicit development mode requires pinned official Nightly APK")
    runner.require(os.environ.get("ACCEPT_ANDROID_SDK_LICENSE") == "1", "Set ACCEPT_ANDROID_SDK_LICENSE=1 only after accepting Google's SDK license")
    runner.require(Path("/dev/kvm").exists(), "Android service requires only --device /dev/kvm; no privileged workaround is used")
    runner.ARTIFACTS.mkdir(exist_ok=True)
    proof["android_apk_sha256"] = hashlib.sha256(Path("/opt/android/firefox.apk").read_bytes()).hexdigest()
    proof["android_package"] = PACKAGE
    proof["android_browser_channel"] = CHANNEL
    subprocess.run(["emulator", "-accel-check"], check=True, stdout=(runner.ARTIFACTS / "android-acceleration.txt").open("w"), stderr=subprocess.STDOUT)
    avds = Path(os.environ["ANDROID_AVD_HOME"])
    avd = avds / "portmap.avd"
    avd.mkdir(parents=True, exist_ok=True)
    (avds / "portmap.ini").write_text("avd.ini.encoding=UTF-8\npath=" + str(avd) + "\ntarget=android-30\n")
    (avd / "config.ini").write_text("AvdId=portmap\navd.ini.encoding=UTF-8\nabi.type=x86_64\nhw.cpu.arch=x86_64\nhw.cpu.ncore=2\nhw.ramSize=2048\nhw.gpu.enabled=yes\nhw.gpu.mode=swiftshader_indirect\nhw.lcd.width=1080\nhw.lcd.height=1920\nhw.lcd.density=420\nimage.sysdir.1=system-images/android-30/default/x86_64/\ntag.id=default\ntag.display=Default\ndisk.dataPartition.size=3G\n")
    subprocess.run(["adb", "start-server"], check=True)
    log = (runner.ARTIFACTS / "android-emulator.log").open("w")
    process = subprocess.Popen(["emulator", "-avd", "portmap", "-port", "5554", "-accel", "on", "-no-window", "-no-audio", "-no-snapshot", "-no-boot-anim", "-gpu", "swiftshader_indirect", "-dns-server", socket.gethostbyname("dns"), "-tcpdump", str(runner.ARTIFACTS / "android-network.pcap")], stdout=log, stderr=subprocess.STDOUT)
    PROCESSES.append(process)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        runner.require(process.poll() is None, "Actual Android emulator exited; see android-emulator.log")
        boot = subprocess.run(["adb", "-s", SERIAL, "shell", "getprop", "sys.boot_completed"], capture_output=True, text=True, timeout=10)
        if boot.returncode == 0 and boot.stdout.strip() == "1":
            break
        time.sleep(2)
    else:
        raise RuntimeError("Actual Android emulator did not boot within 300 seconds")
    # `adb root` restarts adbd and can close its own transport. Preserve the
    # command outcome, then require the actual reconnected daemon to be root.
    root = adb("root", check=False, capture_output=True, text=True, timeout=30)
    adb("wait-for-device", timeout=30)
    identity = adb("shell", "id", "-u", capture_output=True, text=True, timeout=10)
    (runner.ARTIFACTS / "android-root-transition.txt").write_text("request_exit=" + str(root.returncode) + "\n" + root.stdout + root.stderr + "actual_uid=" + identity.stdout)
    runner.require(identity.stdout.strip() == "0", "Reconnected native AOSP daemon is not root; see android-root-transition.txt")
    # Use ADB's documented push-then-Package-Manager path, not the streamed
    # package pipe that failed on the API30 guest. Failure still aborts the run.
    with (runner.ARTIFACTS / "android-install.log").open("w") as install_log:
        adb("install", "--no-streaming", "-r", "/opt/android/firefox.apk", timeout=120, stdout=install_log, stderr=subprocess.STDOUT)
    adb("shell", "input", "keyevent", "82")
    adb("shell", "getprop", stdout=(runner.ARTIFACTS / "android-properties.txt").open("w"))
    proof["android_sdk"] = {"api": 30, "system_image_revision": 11, "emulator": "37.2.13", "platform_tools": "37.0.1"}


class AndroidBrowser(runner.Browser):
    def start(self, native_url=None):
        options = self.configuration()
        arguments = ["--allow-system-access", "--log", "debug"]
        if getattr(self, "native_profile", None):
            # Reattach to the SAME native profile and app data. Another Android
            # NewSession would run geckodriver's unconditional pm clear, which
            # is not a browser restart and invalidates the persistence test.
            adb("shell", "am", "set-debug-app", "--persistent", PACKAGE)
            adb("forward", "tcp:2829", "tcp:2829")
            resolved = adb("shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.VIEW", "-d", "http://a.portmap/", "-p", PACKAGE, capture_output=True, text=True)
            activity = resolved.stdout.strip().splitlines()[-1]
            runner.require(activity.startswith(PACKAGE + "/"), "Cannot resolve actual installed Firefox URL activity")
            startup_url = native_url or "about:blank"
            adb("shell", "am", "start", "-W", "-n", activity, "-a", "android.intent.action.VIEW", "-d", startup_url, "--ez", "automationtest", "true", stdout=(runner.ARTIFACTS / "android-restart-launch.txt").open("w"))
            # The public connect-existing mode speaks Marionette over the
            # explicit ADB forward. It cannot accept androidPackage in 0.36;
            # the actual remote browser must still report platformName Android.
            arguments += ["--connect-existing", "--marionette-port", "2829"]
        else:
            options.profile = str(self.profile)
            options.enable_mobile(android_package=PACKAGE, device_serial=SERIAL)
            options.mobile_options["androidIntentArguments"] = [
                "-a", "android.intent.action.VIEW", "-d", "about:blank",
                "--ez", "automationtest", "true",
            ]
            options.mobile_options["env"] = {"MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1"}
        self.driver = webdriver.Firefox(options=options, service=Service("/usr/local/bin/geckodriver", service_args=arguments, log_output=str(runner.ARTIFACTS / "geckodriver.log")))
        self.driver.set_page_load_timeout(20)
        self.driver.set_script_timeout(15)
        runner.require(self.driver.capabilities.get("platformName", "").lower() == "android", "Driver did not connect to real Android Firefox")
        if native_url:
            # Bind Marionette to the exact tab opened by the actual native VIEW
            # intent, not an arbitrary restored, potentially detached GeckoView.
            # Never open extension UI or repeat the first native navigation.
            observed = {"expected_url": startup_url, "initial_handle": self.driver.current_window_handle, "contexts": []}
            if native_url:
                with (runner.ARTIFACTS / "android-first-native-navigation.png").open("wb") as image:
                    adb("exec-out", "screencap", "-p", stdout=image)
            def launched_context(driver):
                observed["contexts"] = []
                for handle in driver.window_handles:
                    driver.switch_to.window(handle)
                    url = driver.current_url
                    observed["contexts"].append({"handle": handle, "url": url})
                    if url == startup_url:
                        observed["selected_handle"] = handle
                        return True
                return False
            try:
                WebDriverWait(self.driver, 15).until(launched_context)
            finally:
                (runner.ARTIFACTS / ("android-restart-contexts-" + str(time.time_ns()) + ".json")).write_text(json.dumps(observed, indent=2))
                if native_url:
                    origins = {name: runner.records(name) for name in runner.ORIGINS}
                    (runner.ARTIFACTS / "android-first-native-navigation-origins.json").write_text(json.dumps(origins, indent=2))

    def native_tap(self, labels, last=False):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            adb("shell", "uiautomator", "dump", "/sdcard/portmap-ui.xml", stdout=subprocess.DEVNULL)
            xml = adb("exec-out", "cat", "/sdcard/portmap-ui.xml", capture_output=True, text=True).stdout
            (runner.ARTIFACTS / "android-ui.xml").write_text(xml)
            nodes = list(ET.fromstring(xml).iter("node"))
            install_notice = any("portmap connections" in element.get("text", "").lower() and "added" in element.get("text", "").lower() for element in nodes)
            for element in reversed(nodes) if last else nodes:
                text = (element.get("text", "") + " " + element.get("content-desc", "")).lower()
                matches = element.get("resource-id", "").endswith(":id/confirm_button") if install_notice else any(label.lower() in text for label in labels)
                if matches:
                    bounds = [int(value) for value in re.findall(r"\d+", element.get("bounds", ""))]
                    if len(bounds) == 4 and element.get("enabled") == "true":
                        adb("shell", "input", "tap", str((bounds[0] + bounds[2]) // 2), str((bounds[1] + bounds[3]) // 2))
                        if install_notice:
                            break  # Dismiss the actual install sheet before finding the requested control.
                        return
            time.sleep(1)
        with (runner.ARTIFACTS / "android-native.png").open("wb") as image:
            adb("exec-out", "screencap", "-p", stdout=image)
        raise RuntimeError("Actual Android UI control not found: " + repr(labels))

    def options(self):
        # Marionette forbids direct moz-extension navigation on Android. Enter
        # through Firefox's actual native extension menu and browser_action.
        self.native_tap(("More options", "Menu"))
        self.native_tap(("Extensions", "Add-ons"))
        # The menu summary repeats this title; the final match is the actual
        # extension action row, not the expandable Extensions section header.
        self.native_tap(("Portmap connections",), last=True)
        def options_context(driver):
            for handle in driver.window_handles:
                driver.switch_to.window(handle)
                if driver.current_url == runner.OPTIONS_URL:
                    return driver.find_element(By.ID, "status").get_attribute("data-state") == "ready"
            return False
        WebDriverWait(self.driver, 15).until(options_context)

    def assert_no_admission_connections(self):
        import network_evidence
        self.close()
        adb("emu", "kill", capture_output=True)
        for process in PROCESSES:
            runner.require(process.wait(timeout=30) == 0, "Native emulator recorder did not close cleanly")
        PROCESSES.clear()
        network_evidence.android(runner.ARTIFACTS)

    def save(self, domain, host, user, password, port=18080, edit=False):
        # The real guest reaches the isolated Docker network through emulator
        # user-mode NAT, just as its unrelated-origin browsing already does.
        super().save(domain, socket.gethostbyname(host), user, password, port=port, edit=edit)
        with (runner.ARTIFACTS / "android-options-configured.png").open("wb") as image:
            adb("exec-out", "screencap", "-p", stdout=image)

    def snapshot_native_profile(self):
        # Stop native process before copying native disk state; never re-add the
        # extension or replay connection configuration to make persistence pass.
        adb("shell", "am", "force-stop", PACKAGE)
        reported = self.driver.capabilities.get("moz:profile", "")
        exists = adb("shell", "test", "-d", reported, check=False, capture_output=True).returncode if reported.startswith(("/data/", "/sdcard/")) else 1
        if exists == 0:
            remote = reported
        else:
            found = adb("shell", "find", "/data/local/tmp", "/sdcard/Android/data/" + PACKAGE + "/files", "-name", "prefs.js", check=False, capture_output=True, text=True)
            candidates = [str(Path(line).parent) for line in found.stdout.splitlines() if line.endswith("/prefs.js")]
            runner.require(len(candidates) == 1, "Cannot uniquely locate native Android profile for restart; no storage reseeding fallback")
            remote = candidates[0]
        self.native_profile = remote
        snapshot = runner.ARTIFACTS / ("android-native-profile-" + str(time.time_ns()))
        snapshot.mkdir()
        adb("pull", remote + "/.", str(snapshot), timeout=60, stdout=subprocess.DEVNULL)
        return snapshot

    def close(self):
        if self.driver:
            with (runner.ARTIFACTS / "android-logcat.txt").open("wb") as output:
                adb("logcat", "-d", "-v", "threadtime", stdout=output)
            with (runner.ARTIFACTS / "android-final-screen.png").open("wb") as output:
                adb("exec-out", "screencap", "-p", stdout=output)
            self.snapshot_native_profile()
            try:
                super().close()
            except WebDriverException:
                self.driver = None  # The native process was explicitly stopped.

    def restart(self, first_url=None):
        first_restart = not getattr(self, "retained_native_session", False)
        self.snapshot_native_profile()
        config = "/data/local/tmp/" + PACKAGE + "-geckoview-config.yaml"
        backup = "/data/local/tmp/portmap-retained-native-profile"
        if first_restart:
            # AndroidHandler.drop deletes its managed profile and config. Keep
            # their native owners/modes inside this disposable guest; do not
            # round-trip SQLite/storage files through a host ZIP and ADB push.
            adb("shell", "cp", "-a", self.native_profile, backup)
            adb("shell", "cp", "-a", config, backup + ".yaml")
        try:
            self.driver.quit()
        except WebDriverException:
            pass  # Native app was explicitly stopped above.
        self.driver = None
        if first_restart:
            adb("shell", "mkdir", "-p", str(Path(self.native_profile).parent))
            adb("shell", "cp", "-a", backup, self.native_profile)
            adb("shell", "cp", "-a", backup + ".yaml", config)
            adb("shell", "rm", "-rf", backup, backup + ".yaml")
            self.retained_native_session = True
        self.start(native_url=first_url)


if __name__ == "__main__":
    raise SystemExit(runner.main(platform="android-firefox", browser_factory=AndroidBrowser, prepare=prepare))
