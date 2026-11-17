/* Disposable Nightly probe. libssh2 owns SSH, JS owns only raw socket I/O.
 * All buffers crossing the ABI are bytes, never embind std::string. */
#include <emscripten.h>
#include <libssh2.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

EM_JS(int, wire_send, (int id, const void *data, int size), {
  return Module.io.send(id, HEAPU8.subarray(data, data + size));
});
EM_JS(int, wire_recv, (int id, void *data, int size), {
  return Module.io.recv(id, HEAPU8.subarray(data, data + size));
});
EM_JS(int, secure_random, (unsigned char *data, int size), {
  try {
    for (let at = 0; at < size; at += 65536)
      globalThis.crypto.getRandomValues(HEAPU8.subarray(data + at, data + Math.min(size, at + 65536)));
    return 0;
  } catch (_) { return -1; }
});
int mbedtls_hardware_poll(void *context, unsigned char *output, size_t len, size_t *olen) {
  (void)context;
  if (secure_random(output, (int)len)) { *olen = 0; return -1; }
  *olen = len;
  return 0;
}
static ssize_t send_cb(libssh2_socket_t socket, const void *data, size_t length,
                       int flags, void **abstract) {
  (void)socket; (void)flags;
  int rc = wire_send((int)(intptr_t)*abstract, data, (int)length);
  return rc == -2 ? -EAGAIN : rc;
}
static ssize_t recv_cb(libssh2_socket_t socket, void *data, size_t length,
                       int flags, void **abstract) {
  (void)socket; (void)flags;
  int rc = wire_recv((int)(intptr_t)*abstract, data, (int)length);
  return rc == -2 ? -EAGAIN : rc;
}
EMSCRIPTEN_KEEPALIVE LIBSSH2_SESSION *probe_create(int id) {
  if (libssh2_init(0)) return NULL;
  LIBSSH2_SESSION *s = libssh2_session_init_ex(NULL, NULL, NULL, (void *)(intptr_t)id);
  if (!s) return NULL;
  libssh2_session_set_blocking(s, 0);
  libssh2_session_callback_set2(s, LIBSSH2_CALLBACK_SEND, (libssh2_cb_generic *)send_cb);
  libssh2_session_callback_set2(s, LIBSSH2_CALLBACK_RECV, (libssh2_cb_generic *)recv_cb);
  const char *kex = "ecdh-sha2-nistp256,ecdh-sha2-nistp384,ecdh-sha2-nistp521,diffie-hellman-group14-sha256";
  const char *host = "ecdsa-sha2-nistp256,ecdsa-sha2-nistp384,ecdsa-sha2-nistp521,rsa-sha2-512,rsa-sha2-256";
  if (libssh2_session_method_pref(s, LIBSSH2_METHOD_KEX, kex) ||
      libssh2_session_method_pref(s, LIBSSH2_METHOD_HOSTKEY, host) ||
      libssh2_session_method_pref(s, LIBSSH2_METHOD_CRYPT_CS, "aes256-ctr,aes128-ctr") ||
      libssh2_session_method_pref(s, LIBSSH2_METHOD_CRYPT_SC, "aes256-ctr,aes128-ctr") ||
      libssh2_session_method_pref(s, LIBSSH2_METHOD_MAC_CS, "hmac-sha2-256,hmac-sha2-512") ||
      libssh2_session_method_pref(s, LIBSSH2_METHOD_MAC_SC, "hmac-sha2-256,hmac-sha2-512")) {
    libssh2_session_free(s);
    return NULL;
  }
  return s;
}
EMSCRIPTEN_KEEPALIVE int probe_handshake(LIBSSH2_SESSION *s) { return libssh2_session_handshake(s, 0); }
EMSCRIPTEN_KEEPALIVE const char *probe_fingerprint(LIBSSH2_SESSION *s) {
  return libssh2_hostkey_hash(s, LIBSSH2_HOSTKEY_HASH_SHA256);
}
EMSCRIPTEN_KEEPALIVE int probe_password(LIBSSH2_SESSION *s, const char *user, const char *pass) {
  return libssh2_userauth_password_ex(s, user, (unsigned int)strlen(user), pass, (unsigned int)strlen(pass), NULL);
}
EMSCRIPTEN_KEEPALIVE int probe_key(LIBSSH2_SESSION *s, const char *user, const char *pub, const char *priv, const char *pass) {
  return libssh2_userauth_publickey_frommemory(s, user, strlen(user), pub, strlen(pub), priv, strlen(priv), pass);
}
EMSCRIPTEN_KEEPALIVE LIBSSH2_CHANNEL *probe_open(LIBSSH2_SESSION *s, const char *host, int port) {
  return libssh2_channel_direct_tcpip_ex(s, host, port, "127.0.0.1", 0);
}
EMSCRIPTEN_KEEPALIVE LIBSSH2_CHANNEL *probe_open_session(LIBSSH2_SESSION *s) {
  return libssh2_channel_open_session(s);
}
/* This ABI is private to ssh.js; only its fixed authorized_keys installer uses exec. */
EMSCRIPTEN_KEEPALIVE int probe_exec(LIBSSH2_CHANNEL *c, const char *command) {
  return libssh2_channel_exec(c, command);
}
EMSCRIPTEN_KEEPALIVE int probe_read_stderr(LIBSSH2_CHANNEL *c, char *buf, int len) {
  return (int)libssh2_channel_read_stderr(c, buf, len);
}
EMSCRIPTEN_KEEPALIVE int probe_send_eof(LIBSSH2_CHANNEL *c) { return libssh2_channel_send_eof(c); }
EMSCRIPTEN_KEEPALIVE int probe_exit_status(LIBSSH2_CHANNEL *c) { return libssh2_channel_get_exit_status(c); }
EMSCRIPTEN_KEEPALIVE int probe_exit_signal(LIBSSH2_CHANNEL *c) {
  size_t length = 0;
  int result = libssh2_channel_get_exit_signal(c, NULL, &length, NULL, NULL, NULL, NULL);
  return result ? result : length != 0;
}
EMSCRIPTEN_KEEPALIVE int probe_errno(LIBSSH2_SESSION *s) { return libssh2_session_last_errno(s); }
EMSCRIPTEN_KEEPALIVE int probe_read(LIBSSH2_CHANNEL *c, char *buf, int len) { return (int)libssh2_channel_read(c, buf, len); }
EMSCRIPTEN_KEEPALIVE int probe_write(LIBSSH2_CHANNEL *c, const char *buf, int len) { return (int)libssh2_channel_write(c, buf, len); }
EMSCRIPTEN_KEEPALIVE int probe_eof(LIBSSH2_CHANNEL *c) { return libssh2_channel_eof(c); }
EMSCRIPTEN_KEEPALIVE int probe_close_channel(LIBSSH2_CHANNEL *c) { return libssh2_channel_close(c); }
EMSCRIPTEN_KEEPALIVE int probe_free_channel(LIBSSH2_CHANNEL *c) { return libssh2_channel_free(c); }
/* Private ABI compiled only with LIBSSH2_BROWSER_CALLBACK_IO. */
extern int _libssh2_browser_probe_abort(LIBSSH2_SESSION *);
extern int _libssh2_browser_probe_read_cancel_safe(LIBSSH2_CHANNEL *);
EMSCRIPTEN_KEEPALIVE int probe_abort_free(LIBSSH2_SESSION *s) { return _libssh2_browser_probe_abort(s); }
EMSCRIPTEN_KEEPALIVE int probe_read_cancel_safe(LIBSSH2_CHANNEL *c) { return _libssh2_browser_probe_read_cancel_safe(c); }
