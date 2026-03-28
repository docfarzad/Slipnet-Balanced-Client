import os
import random
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import END, LEFT, RIGHT, VERTICAL, Y, BOTH, X, filedialog, messagebox, ttk

import requests

BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DNSTT_EXEC = os.path.join(BASE_DIR, "slipnet.exe")
TEST_URL = "https://1.1.1.1/cdn-cgi/trace"
GOOD_TEST_URL = "https://httpbin.org/bytes/51200"
DEFAULT_SLIPNET_URL = ""

SOCKS_PUBLIC_BIND = "0.0.0.0:1080"
HTTP_PUBLIC_BIND = "0.0.0.0:8080"
DEFAULT_WORKERS = 20
WORKER_BASE_PORT = 12000
TEST_WORKER_BASE_PORT = 20000
BACKEND_BASE_PORT = 14000
HTTP_TIMEOUT = 16
DNSTT_STARTUP_TIMEOUT = 3.0
DNSTT_STARTUP_POLL_INTERVAL = 0.1
BACKEND_CONNECT_TIMEOUT = 3
PROCESS_STOP_TIMEOUT = 3
RELAY_SELECT_TIMEOUT = 1.0
RELAY_READ_CHUNK = 65535
HTTP_HEADER_MAX_BYTES = 65536
HTTP_HEADER_READ_CHUNK = 4096
HTTP_BODY_READ_CHUNK = 8192
EWMA_ALPHA = 0.2
FAILURE_THRESHOLD = 3
BASE_COOLDOWN_SECONDS = 8.0
MAX_COOLDOWN_SECONDS = 60.0
RECOVERY_SLOW_START_SECONDS = 12.0
INFLIGHT_PENALTY_SECONDS = 0.03
P2C_SAMPLE_SIZE = 2


def get_local_ipv4():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def load_unique_resolvers(path):
    seen = set()
    unique = []
    with open(path, "r", encoding="utf-8", errors="ignore") as file_obj:
        for raw in file_obj:
            item = raw.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            unique.append(item)
    return unique


def stop_process(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=PROCESS_STOP_TIMEOUT)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def wait_for_local_port(port, timeout=DNSTT_STARTUP_TIMEOUT, interval=DNSTT_STARTUP_POLL_INTERVAL):
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(interval)
        try:
            sock.connect(("127.0.0.1", port))
            sock.close()
            return True
        except OSError:
            sock.close()
            time.sleep(interval)
    return False


def start_dnstt(resolver, listen_port, slipnet_url):
    cmd = [
        DNSTT_EXEC,
        "--dns",
        resolver,
        "--query-size",
        "50",
        "--port",
        str(listen_port),
        slipnet_url,
    ]

    popen_kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }

    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        popen_kwargs["startupinfo"] = startupinfo
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    return subprocess.Popen(cmd, **popen_kwargs)



def proxy_request_latency(local_port, test_url):
    proxy = f"socks5h://127.0.0.1:{local_port}"
    proxies = {"http": proxy, "https": proxy}
    try:
        started = time.perf_counter()
        response = requests.get(test_url, proxies=proxies, timeout=HTTP_TIMEOUT)
        if response.status_code != 200:
            return None
        return time.perf_counter() - started
    except requests.RequestException:
        return None


def test_resolver_once(resolver, local_port, slipnet_url, test_url=TEST_URL):
    proc = start_dnstt(resolver, local_port, slipnet_url)
    try:
        if not wait_for_local_port(local_port):
            return None
        return proxy_request_latency(local_port, test_url)
    finally:
        stop_process(proc)


class ResolverManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.scan_cancel = threading.Event()
        self.is_scanning = False
        self.scan_executor = None
        self.loaded_resolvers = []
        self.good_resolvers = []
        self.good_set = set()
        self.latency = {}
        self.last_test_ok = {}
        self.test_fail_count = {}
        self.selected = set()
        self.active_pool = []
        self.backend_ports = {}
        self.backend_procs = {}
        self.ewma_latency = {}
        self.consecutive_failures = {}
        self.cooldown_until = {}
        self.recovered_at = {}
        self.backend_inflight = {}
        self.next_backend_port = BACKEND_BASE_PORT
        self.scanned_count = 0
        self.socks_server = None
        self.http_server = None
        self.local_ip = get_local_ipv4()
        self.slipnet_url = DEFAULT_SLIPNET_URL

    def set_slipnet_url(self, slipnet_url):
        with self.lock:
            self.slipnet_url = slipnet_url

    def set_loaded_resolvers(self, resolvers):
        with self.lock:
            self.loaded_resolvers = list(resolvers)
            self.good_resolvers = []
            self.good_set.clear()
            self.latency.clear()
            self.ewma_latency.clear()
            self.last_test_ok.clear()
            self.test_fail_count.clear()
            self.consecutive_failures.clear()
            self.cooldown_until.clear()
            self.recovered_at.clear()
            self.backend_inflight.clear()
            self.selected.clear()
            self.scanned_count = 0

    def scan_resolvers(self, workers, on_progress, on_good, on_done):
        with self.lock:
            if self.is_scanning:
                return False
            self.is_scanning = True
            self.scan_cancel.clear()
            self.scan_executor = None
            input_resolvers = list(self.loaded_resolvers)
            self.scanned_count = 0
            self.good_resolvers = []
            self.good_set.clear()
            self.latency.clear()
            self.ewma_latency.clear()
            self.last_test_ok.clear()
            self.test_fail_count.clear()
            self.consecutive_failures.clear()
            self.cooldown_until.clear()
            self.recovered_at.clear()
            self.backend_inflight.clear()
            self.selected.clear()

        def runner():
            executor = None
            futures = {}
            try:
                max_workers = max(1, int(workers))
                executor = ThreadPoolExecutor(max_workers=max_workers)
                with self.lock:
                    self.scan_executor = executor
                for idx, resolver in enumerate(input_resolvers):
                    if self.scan_cancel.is_set():
                        break
                    port = WORKER_BASE_PORT + idx
                    with self.lock:
                        slipnet_url = self.slipnet_url
                    futures[executor.submit(test_resolver_once, resolver, port, slipnet_url)] = resolver
                for future in as_completed(futures):
                    if self.scan_cancel.is_set():
                        for pending in futures:
                            pending.cancel()
                        break
                    resolver = futures[future]
                    latency = None
                    try:
                        latency = future.result()
                    except Exception:
                        latency = None
                    is_new_good = False
                    if latency is not None:
                        with self.lock:
                            if resolver not in self.good_set:
                                self.good_set.add(resolver)
                                self.good_resolvers.append(resolver)
                                self.latency[resolver] = latency
                                self.ewma_latency[resolver] = latency
                                self.last_test_ok[resolver] = True
                                self.test_fail_count[resolver] = 0
                                self.consecutive_failures[resolver] = 0
                                self.cooldown_until[resolver] = 0.0
                                self.recovered_at[resolver] = 0.0
                                self.backend_inflight[resolver] = 0
                                is_new_good = True
                    with self.lock:
                        self.scanned_count += 1
                        scanned = self.scanned_count
                        good_count = len(self.good_resolvers)
                    on_progress(scanned, len(input_resolvers), good_count)
                    if is_new_good:
                        on_good(resolver)
            finally:
                if executor is not None:
                    executor.shutdown(wait=False, cancel_futures=True)
                with self.lock:
                    self.is_scanning = False
                    self.scan_executor = None
                on_done()

        threading.Thread(target=runner, daemon=True).start()
        return True

    def stop_scan(self):
        self.scan_cancel.set()
        with self.lock:
            executor = self.scan_executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def ensure_backend_for(self, resolver):
        with self.lock:
            if resolver in self.backend_ports:
                return self.backend_ports[resolver]
            port = self.next_backend_port
            self.next_backend_port += 1
            self.backend_ports[resolver] = port
        with self.lock:
            slipnet_url = self.slipnet_url
        proc = start_dnstt(resolver, port, slipnet_url)
        if not wait_for_local_port(port):
            stop_process(proc)
            with self.lock:
                self.backend_ports.pop(resolver, None)
            return None
        with self.lock:
            self.backend_procs[resolver] = proc
        return port

    def activate_selected_pool(self):
        with self.lock:
            picked = [r for r in self.good_resolvers if r in self.selected]
        active = []
        for resolver in picked:
            port = self.ensure_backend_for(resolver)
            if port is not None:
                active.append(resolver)
        with self.lock:
            self.active_pool = active
        return active

    def _base_score(self, resolver):
        ewma = self.ewma_latency.get(resolver)
        if ewma is None:
            ewma = self.latency.get(resolver, float("inf"))
        inflight = self.backend_inflight.get(resolver, 0)
        return ewma + (INFLIGHT_PENALTY_SECONDS * inflight)

    def _effective_score(self, resolver, now):
        score = self._base_score(resolver)
        recovered = self.recovered_at.get(resolver, 0.0)
        if recovered and now > recovered:
            elapsed = now - recovered
            if elapsed < RECOVERY_SLOW_START_SECONDS:
                ramp = max(0.15, elapsed / RECOVERY_SLOW_START_SECONDS)
                score = score / ramp
        return score

    def choose_active_backend(self, excluded):
        with self.lock:
            now = time.time()
            candidates = [
                r
                for r in self.active_pool
                if r not in excluded and now >= self.cooldown_until.get(r, 0.0)
            ]
            if not candidates:
                return None, None
            if len(candidates) <= P2C_SAMPLE_SIZE:
                sampled = candidates
            else:
                sampled = random.sample(candidates, P2C_SAMPLE_SIZE)
            resolver = min(sampled, key=lambda item: self._effective_score(item, now))
            port = self.backend_ports.get(resolver)
            return resolver, port

    def mark_backend_failure(self, resolver):
        with self.lock:
            self.last_test_ok[resolver] = False
            fail_total = self.test_fail_count.get(resolver, 0) + 1
            self.test_fail_count[resolver] = fail_total
            consecutive = self.consecutive_failures.get(resolver, 0) + 1
            self.consecutive_failures[resolver] = consecutive
            if consecutive >= FAILURE_THRESHOLD:
                exp = consecutive - FAILURE_THRESHOLD
                cooldown = min(MAX_COOLDOWN_SECONDS, BASE_COOLDOWN_SECONDS * (2 ** exp))
                self.cooldown_until[resolver] = time.time() + cooldown

    def mark_backend_success(self, resolver, latency_value=None):
        with self.lock:
            self.last_test_ok[resolver] = True
            self.test_fail_count[resolver] = 0
            was_in_cooldown = time.time() < self.cooldown_until.get(resolver, 0.0)
            self.consecutive_failures[resolver] = 0
            self.cooldown_until[resolver] = 0.0
            if was_in_cooldown:
                self.recovered_at[resolver] = time.time()
            if latency_value is not None:
                self.latency[resolver] = latency_value
                previous = self.ewma_latency.get(resolver, latency_value)
                self.ewma_latency[resolver] = (EWMA_ALPHA * latency_value) + ((1.0 - EWMA_ALPHA) * previous)

    def acquire_backend(self, resolver):
        with self.lock:
            self.backend_inflight[resolver] = self.backend_inflight.get(resolver, 0) + 1

    def release_backend(self, resolver):
        with self.lock:
            current = self.backend_inflight.get(resolver, 0)
            if current <= 1:
                self.backend_inflight[resolver] = 0
            else:
                self.backend_inflight[resolver] = current - 1

    def test_good_resolvers(self, on_item, on_done):
        with self.lock:
            items = list(self.good_resolvers)
            for resolver in items:
                self.last_test_ok[resolver] = None

        def runner():
            with ThreadPoolExecutor(max_workers=min(max(1, DEFAULT_WORKERS), max(1, len(items)))) as executor:
                futures = {}
                for idx, resolver in enumerate(items):
                    port = TEST_WORKER_BASE_PORT + idx
                    with self.lock:
                        slipnet_url = self.slipnet_url
                    futures[executor.submit(test_resolver_once, resolver, port, slipnet_url, GOOD_TEST_URL)] = resolver
                for future in as_completed(futures):
                    resolver = futures[future]
                    latency = None
                    try:
                        latency = future.result()
                    except Exception:
                        latency = None
                    if latency is None:
                        self.mark_backend_failure(resolver)
                        on_item(resolver, False)
                    else:
                        self.mark_backend_success(resolver, latency)
                        on_item(resolver, True)
            on_done()

        threading.Thread(target=runner, daemon=True).start()

    def stop_backends(self):
        with self.lock:
            procs = list(self.backend_procs.values())
            self.backend_procs = {}
            self.backend_ports = {}
            self.active_pool = []
            self.backend_inflight = {}
            self.next_backend_port = BACKEND_BASE_PORT
        for proc in procs:
            stop_process(proc)

    def stop_all(self):
        self.stop_scan()
        if self.socks_server is not None:
            self.socks_server.shutdown()
            self.socks_server.server_close()
            self.socks_server = None
        if self.http_server is not None:
            self.http_server.shutdown()
            self.http_server.server_close()
            self.http_server = None
        self.stop_backends()


MANAGER = ResolverManager()


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise OSError("connection closed")
        data += chunk
    return data


def _relay_bidirectional(left, right):
    sockets = [left, right]
    for entry in sockets:
        entry.setblocking(False)
    while True:
        readable, _, errored = select.select(sockets, [], sockets, RELAY_SELECT_TIMEOUT)
        if errored:
            return
        if not readable:
            continue
        for src in readable:
            dst = right if src is left else left
            try:
                chunk = src.recv(RELAY_READ_CHUNK)
            except OSError:
                return
            if not chunk:
                return
            try:
                dst.sendall(chunk)
            except OSError:
                return


def _encode_socks5_address(host):
    try:
        packed = socket.inet_pton(socket.AF_INET, host)
        return b"\x01" + packed
    except OSError:
        pass
    try:
        packed = socket.inet_pton(socket.AF_INET6, host)
        return b"\x04" + packed
    except OSError:
        pass
    host_bytes = host.encode("idna")
    if len(host_bytes) > 255:
        raise ValueError("hostname too long")
    return b"\x03" + bytes([len(host_bytes)]) + host_bytes


def _build_socks5_connect_request(host, port):
    addr = _encode_socks5_address(host)
    return b"\x05\x01\x00" + addr + port.to_bytes(2, "big")


def _recv_socks5_reply(sock):
    head = _recv_exact(sock, 4)
    atyp = head[3]
    if atyp == 1:
        tail = _recv_exact(sock, 6)
    elif atyp == 3:
        ln = _recv_exact(sock, 1)[0]
        tail = bytes([ln]) + _recv_exact(sock, ln + 2)
    elif atyp == 4:
        tail = _recv_exact(sock, 18)
    else:
        raise OSError("invalid socks reply")
    return head + tail


def _connect_backend_via_socks_request(connect_request):
    attempted = set()
    while True:
        resolver, port = MANAGER.choose_active_backend(attempted)
        if resolver is None or port is None:
            return None, None, None
        attempted.add(resolver)
        try:
            MANAGER.acquire_backend(resolver)
            sock = socket.create_connection(("127.0.0.1", port), timeout=BACKEND_CONNECT_TIMEOUT)
            sock.sendall(b"\x05\x01\x00")
            if _recv_exact(sock, 2) != b"\x05\x00":
                sock.close()
                MANAGER.release_backend(resolver)
                continue
            sock.sendall(connect_request)
            reply = _recv_socks5_reply(sock)
            if reply[1] != 0:
                sock.close()
                MANAGER.release_backend(resolver)
                continue
            MANAGER.mark_backend_success(resolver)
            return sock, resolver, reply
        except OSError:
            MANAGER.mark_backend_failure(resolver)
            MANAGER.release_backend(resolver)
            continue


def _connect_backend_to_target(host, port):
    request = _build_socks5_connect_request(host, port)
    backend_sock, resolver, _ = _connect_backend_via_socks_request(request)
    return backend_sock, resolver


def _recv_http_request_head(sock):
    data = b""
    while b"\r\n\r\n" not in data and len(data) < HTTP_HEADER_MAX_BYTES:
        chunk = sock.recv(HTTP_HEADER_READ_CHUNK)
        if not chunk:
            return None
        data += chunk
    if b"\r\n\r\n" not in data:
        return None
    head, rest = data.split(b"\r\n\r\n", 1)
    return head + b"\r\n\r\n", rest


def _send_http_error(sock, code, reason):
    body = f"{code} {reason}\n".encode("ascii", "ignore")
    header = (
        f"HTTP/1.1 {code} {reason}\r\n"
        "Connection: close\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii", "ignore")
    sock.sendall(header + body)


def _handle_client_socks5(client_sock):
    greeting = _recv_exact(client_sock, 2)
    if greeting[0] != 5:
        return
    _recv_exact(client_sock, greeting[1])
    client_sock.sendall(b"\x05\x00")
    req_head = _recv_exact(client_sock, 4)
    ver, cmd, _, atyp = req_head
    if ver != 5 or cmd != 1:
        client_sock.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    if atyp == 1:
        addr_part = _recv_exact(client_sock, 4)
    elif atyp == 3:
        ln = _recv_exact(client_sock, 1)[0]
        addr_part = bytes([ln]) + _recv_exact(client_sock, ln)
    elif atyp == 4:
        addr_part = _recv_exact(client_sock, 16)
    else:
        client_sock.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    port_part = _recv_exact(client_sock, 2)
    backend_sock, resolver, reply = _connect_backend_via_socks_request(req_head + addr_part + port_part)
    if backend_sock is None:
        client_sock.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    try:
        client_sock.sendall(reply)
        _relay_bidirectional(client_sock, backend_sock)
    finally:
        try:
            backend_sock.close()
        except Exception:
            pass
        MANAGER.release_backend(resolver)


def _handle_client_http(client_sock):
    parsed = _recv_http_request_head(client_sock)
    if parsed is None:
        return
    head_bytes, buffered_body = parsed
    try:
        head_text = head_bytes.decode("iso-8859-1")
    except UnicodeDecodeError:
        _send_http_error(client_sock, 400, "Bad Request")
        return
    lines = head_text.split("\r\n")
    try:
        method, target, version = lines[0].split(" ", 2)
    except ValueError:
        _send_http_error(client_sock, 400, "Bad Request")
        return
    headers = []
    host_header = None
    content_length = 0
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.strip()
        val = value.strip()
        low = key.lower()
        if low == "host":
            host_header = val
        if low == "content-length":
            try:
                content_length = max(0, int(val))
            except ValueError:
                content_length = 0
        if low in ("proxy-connection", "proxy-authenticate", "proxy-authorization"):
            continue
        headers.append((key, val))
    if method.upper() == "CONNECT":
        if ":" in target:
            host, port_text = target.rsplit(":", 1)
            try:
                port = int(port_text)
            except ValueError:
                _send_http_error(client_sock, 400, "Bad CONNECT Target")
                return
        else:
            host, port = target, 443
        backend_sock, resolver = _connect_backend_to_target(host.strip("[]"), port)
        if backend_sock is None:
            _send_http_error(client_sock, 502, "Bad Gateway")
            return
        try:
            client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _relay_bidirectional(client_sock, backend_sock)
        finally:
            try:
                backend_sock.close()
            except Exception:
                pass
            MANAGER.release_backend(resolver)
        return
    parsed_url = urllib.parse.urlsplit(target)
    if parsed_url.scheme and parsed_url.hostname:
        target_host = parsed_url.hostname
        target_port = parsed_url.port or (443 if parsed_url.scheme.lower() == "https" else 80)
        path = urllib.parse.urlunsplit(("", "", parsed_url.path or "/", parsed_url.query, ""))
    else:
        if not host_header:
            _send_http_error(client_sock, 400, "Missing Host")
            return
        if ":" in host_header:
            host_part, port_part = host_header.rsplit(":", 1)
            target_host = host_part.strip("[]")
            try:
                target_port = int(port_part)
            except ValueError:
                target_port = 80
        else:
            target_host = host_header.strip("[]")
            target_port = 80
        path = target if target else "/"
    if not path.startswith("/"):
        path = "/" + path
    backend_sock, resolver = _connect_backend_to_target(target_host, target_port)
    if backend_sock is None:
        _send_http_error(client_sock, 502, "Bad Gateway")
        return
    if not any(key.lower() == "host" for key, _ in headers):
        headers.append(("Host", host_header or target_host))
    headers.append(("Connection", "close"))
    rewritten = (
        f"{method} {path} {version}\r\n"
        + "".join(f"{key}: {value}\r\n" for key, value in headers)
        + "\r\n"
    ).encode("iso-8859-1")
    try:
        backend_sock.sendall(rewritten)
        if content_length > 0:
            body = buffered_body
            while len(body) < content_length:
                chunk = client_sock.recv(min(HTTP_BODY_READ_CHUNK, content_length - len(body)))
                if not chunk:
                    break
                body += chunk
            if body:
                backend_sock.sendall(body[:content_length])
        _relay_bidirectional(client_sock, backend_sock)
    finally:
        try:
            backend_sock.close()
        except Exception:
            pass
        MANAGER.release_backend(resolver)


class SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            _handle_client_socks5(self.request)
        except Exception:
            pass
        finally:
            try:
                self.request.close()
            except Exception:
                pass


class HttpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            _handle_client_http(self.request)
        except Exception:
            pass
        finally:
            try:
                self.request.close()
            except Exception:
                pass


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _parse_bind(bind_text):
    host, port_text = bind_text.rsplit(":", 1)
    return host, int(port_text)


def start_proxy_servers():
    if MANAGER.socks_server is None:
        host, port = _parse_bind(SOCKS_PUBLIC_BIND)
        MANAGER.socks_server = ThreadingTCPServer((host, port), SocksHandler)
        threading.Thread(target=MANAGER.socks_server.serve_forever, daemon=True).start()
    if MANAGER.http_server is None:
        host, port = _parse_bind(HTTP_PUBLIC_BIND)
        MANAGER.http_server = ThreadingTCPServer((host, port), HttpHandler)
        threading.Thread(target=MANAGER.http_server.serve_forever, daemon=True).start()


class ResolverFinderUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Slipnet Balanced Client")
        self.path_var = tk.StringVar()
        self.worker_var = tk.StringVar(value=str(DEFAULT_WORKERS))
        self.slipnet_var = tk.StringVar(value=DEFAULT_SLIPNET_URL)
        self.stats_var = tk.StringVar(value="Loaded: 0 | Scanned: 0/0 | Good: 0")
        self.proxy_var = tk.StringVar(value="No active pool yet.")
        self.unique_total = 0
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_ui()

    def _build(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=X)
        ttk.Button(top, text="Browse IP List", command=self._browse_file).pack(side=LEFT)
        ttk.Entry(top, textvariable=self.path_var).pack(side=LEFT, fill=X, expand=True, padx=6)
        ttk.Label(top, text="Workers").pack(side=LEFT, padx=(6, 3))
        ttk.Entry(top, width=6, textvariable=self.worker_var).pack(side=LEFT)
        self.start_btn = ttk.Button(top, text="Start Scan", command=self._start_scan)
        self.start_btn.pack(side=LEFT, padx=6)
        self.stop_btn = ttk.Button(top, text="Stop Scan", command=self._stop_scan)
        self.stop_btn.pack(side=LEFT, padx=3)

        slipnet_row = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        slipnet_row.pack(fill=X)
        ttk.Label(slipnet_row, text="Slipnet connection string").pack(side=LEFT)
        ttk.Entry(slipnet_row, textvariable=self.slipnet_var).pack(side=LEFT, fill=X, expand=True, padx=(6, 0))

        mid = ttk.Panedwindow(self.root, orient="horizontal")
        mid.pack(fill=BOTH, expand=True, padx=8, pady=4)

        left_frame = ttk.Frame(mid, padding=4)
        right_frame = ttk.Frame(mid, padding=4)
        mid.add(left_frame, weight=1)
        mid.add(right_frame, weight=2)

        ttk.Label(left_frame, text="Unique IPs").pack(anchor="w")
        self.ip_list = tk.Listbox(left_frame, height=20)
        ip_scroll = ttk.Scrollbar(left_frame, orient=VERTICAL, command=self.ip_list.yview)
        self.ip_list.configure(yscrollcommand=ip_scroll.set)
        self.ip_list.pack(side=LEFT, fill=BOTH, expand=True)
        ip_scroll.pack(side=RIGHT, fill=Y)

        controls = ttk.Frame(right_frame)
        controls.pack(fill=X, pady=(0, 6))
        ttk.Button(controls, text="Activate Selected", command=self._activate_selected).pack(side=LEFT)
        ttk.Button(controls, text="Verify Good Resolvers' Quality", command=self._test_good).pack(side=LEFT, padx=6)

        columns = ("select", "resolver", "latency", "status", "active")
        self.good_tree = ttk.Treeview(right_frame, columns=columns, show="headings", height=18)
        self.good_tree.heading("select", text="Pick")
        self.good_tree.heading("resolver", text="Resolver")
        self.good_tree.heading("latency", text="Latency")
        self.good_tree.heading("status", text="Last Test")
        self.good_tree.heading("active", text="In Pool")
        self.good_tree.column("select", width=50, anchor="center")
        self.good_tree.column("resolver", width=220)
        self.good_tree.column("latency", width=90, anchor="center")
        self.good_tree.column("status", width=90, anchor="center")
        self.good_tree.column("active", width=70, anchor="center")
        self.good_tree.pack(fill=BOTH, expand=True)
        self.good_tree.bind("<Button-1>", self._on_tree_click)

        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(fill=X)
        ttk.Label(bottom, textvariable=self.stats_var).pack(anchor="w")
        ttk.Label(bottom, textvariable=self.proxy_var).pack(anchor="w", pady=(4, 0))

    def _browse_file(self):
        MANAGER.stop_scan()
        path = filedialog.askopenfilename(
            title="Select resolver IP list",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            resolvers = load_unique_resolvers(path)
        except Exception as exc:
            messagebox.showerror("Load Failed", f"Could not load file:\n{exc}")
            return
        self.path_var.set(path)
        self.unique_total = len(resolvers)
        self.ip_list.delete(0, END)
        for item in resolvers:
            self.ip_list.insert(END, item)
        MANAGER.set_loaded_resolvers(resolvers)
        self._rebuild_good_table()
        self._refresh_stats()

    def _start_scan(self):
        if not os.path.exists(DNSTT_EXEC):
            messagebox.showerror("Missing slipnet", f"Executable not found:\n{DNSTT_EXEC}")
            return
        try:
            workers = max(1, int(self.worker_var.get().strip()))
        except Exception:
            messagebox.showerror("Invalid workers", "Workers must be an integer.")
            return
        slipnet_url = self.slipnet_var.get().strip()
        if not slipnet_url:
            messagebox.showerror("Missing Slipnet", "Slipnet string cannot be empty.")
            return
        MANAGER.set_slipnet_url(slipnet_url)
        if self.unique_total == 0:
            messagebox.showwarning("No Input", "Load an IP list first.")
            return
        started = MANAGER.scan_resolvers(
            workers=workers,
            on_progress=lambda scanned, total, good: self.root.after(
                0, self._on_scan_progress, scanned, total, good
            ),
            on_good=lambda resolver: self.root.after(0, self._on_new_good, resolver),
            on_done=lambda: self.root.after(0, self._on_scan_done),
        )
        if not started:
            messagebox.showinfo("Scan Running", "A scan is already in progress.")

    def _stop_scan(self):
        MANAGER.stop_scan()

    def _on_scan_progress(self, scanned, total, good):
        self.stats_var.set(f"Loaded: {self.unique_total} | Scanned: {scanned}/{total} | Good: {good}")

    def _on_new_good(self, resolver):
        self._upsert_good_row(resolver)
        self._refresh_stats()

    def _on_scan_done(self):
        self._refresh_stats()

    def _activate_selected(self):
        try:
            start_proxy_servers()
        except Exception as exc:
            messagebox.showerror("Proxy Start Failed", str(exc))
            return
        active = MANAGER.activate_selected_pool()
        self._refresh_good_rows()
        if not active:
            self.proxy_var.set("No selected resolver could be activated.")
            return
        socks_port = SOCKS_PUBLIC_BIND.rsplit(":", 1)[1]
        http_port = HTTP_PUBLIC_BIND.rsplit(":", 1)[1]
        self.proxy_var.set(
            f"Active pool: {len(active)} resolver(s) | SOCKS5: {MANAGER.local_ip}:{socks_port} | HTTP: {MANAGER.local_ip}:{http_port}"
        )

    def _test_good(self):
        with MANAGER.lock:
            total = len(MANAGER.good_resolvers)
        if total == 0:
            messagebox.showinfo("No Good Resolvers", "Run scan first.")
            return
        self.proxy_var.set("Testing good resolvers...")
        MANAGER.test_good_resolvers(
            on_item=lambda resolver, ok: self.root.after(0, self._on_test_result, resolver, ok),
            on_done=lambda: self.root.after(0, self._on_test_done),
        )
        self._refresh_good_rows()

    def _on_test_result(self, resolver, ok):
        self._upsert_good_row(resolver)
        if not ok:
            self.proxy_var.set(f"Marked failed: {resolver}")

    def _on_test_done(self):
        self._refresh_good_rows()
        self.proxy_var.set("Good resolver test finished. Failures are marked, not removed.")

    def _row_values(self, resolver):
        with MANAGER.lock:
            selected = resolver in MANAGER.selected
            latency = MANAGER.latency.get(resolver)
            ok = MANAGER.last_test_ok.get(resolver, True)
            active = resolver in MANAGER.active_pool
        if ok is None:
            status = "PENDING"
        else:
            status = "OK" if ok else "FAIL"
        return (
            "[x]" if selected else "[ ]",
            resolver,
            f"{latency:.3f}s" if latency is not None else "-",
            status,
            "Yes" if active else "No",
        )

    def _upsert_good_row(self, resolver):
        item_id = resolver
        values = self._row_values(resolver)
        if self.good_tree.exists(item_id):
            self.good_tree.item(item_id, values=values)
        else:
            self.good_tree.insert("", END, iid=item_id, values=values)

    def _refresh_good_rows(self):
        with MANAGER.lock:
            resolvers = list(MANAGER.good_resolvers)
            latency_map = dict(MANAGER.latency)
        resolvers.sort(key=lambda item: latency_map.get(item, float("inf")))
        existing = set(self.good_tree.get_children())
        for resolver in resolvers:
            self._upsert_good_row(resolver)
            self.good_tree.move(resolver, "", "end")
            existing.discard(resolver)
        for stale in existing:
            self.good_tree.delete(stale)

    def _rebuild_good_table(self):
        for item in self.good_tree.get_children():
            self.good_tree.delete(item)
        self._refresh_good_rows()

    def _on_tree_click(self, event):
        region = self.good_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        column = self.good_tree.identify_column(event.x)
        row = self.good_tree.identify_row(event.y)
        if not row or column != "#1":
            return
        with MANAGER.lock:
            if row in MANAGER.selected:
                MANAGER.selected.remove(row)
            else:
                MANAGER.selected.add(row)
        self._upsert_good_row(row)

    def _refresh_stats(self):
        with MANAGER.lock:
            scanned = MANAGER.scanned_count
            good = len(MANAGER.good_resolvers)
            total = len(MANAGER.loaded_resolvers)
        self.stats_var.set(f"Loaded: {total} | Scanned: {scanned}/{total} | Good: {good}")

    def _refresh_ui(self):
        with MANAGER.lock:
            scanning = MANAGER.is_scanning
        self.start_btn.configure(state="disabled" if scanning else "normal")
        self.stop_btn.configure(state="normal" if scanning else "disabled")
        self._refresh_stats()
        self._refresh_good_rows()
        self.root.after(1000, self._refresh_ui)

    def _on_close(self):
        MANAGER.stop_all()
        self.root.destroy()


def main():
    root = tk.Tk()
    ResolverFinderUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
