#!/usr/bin/env python3
"""Nimbus: a live dashboard and control panel for a Paper Minecraft server.

Runs on the Ubuntu machine that hosts the server and serves a web page you open
from any computer on your network. Standard library only, nothing to pip install.

  python3 nimbus.py                 run in the foreground
  sudo python3 nimbus.py --install  run at boot as a systemd service
  python3 nimbus.py --token         print the access token
"""

import argparse
import glob
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCREEN_NAME = "mc"
WORLD_DIRS = ("world", "world_nether", "world_the_end")
HISTORY_LEN = 150  # samples kept for the charts (2 s apart = 5 minutes)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BACKUP_NAME_RE = re.compile(r"^world-\d{8}-\d{6}\.tar\.gz$")

MC_DIR = Path.home() / "minecraft"
TOKEN = ""


# ---------------------------------------------------------------- helpers

def read_text(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path, default=None):
    value = read_text(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def server_properties():
    props = {}
    text = read_text(MC_DIR / "server.properties", "")
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    return props


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def load_token():
    path = Path.home() / ".config" / "nimbus" / "token"
    token = read_text(path)
    if not token:
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(18)
        path.write_text(token + "\n")
        path.chmod(0o600)
    return token


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# ---------------------------------------------------------------- system metrics

def read_cpu_times():
    times = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            parts = line.split()
            values = [int(v) for v in parts[1:9]]
            idle = values[3] + values[4]  # idle + iowait
            times[parts[0]] = (sum(values), idle)
    return times


def read_memory():
    info = {}
    for line in read_text("/proc/meminfo", "").splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0]) * 1024
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {"total": total, "used": total - available,
            "swap_total": swap_total, "swap_used": swap_total - swap_free}


def read_temperatures():
    sensors = []
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read_text(f"{hw}/name", "sensor")
        for inp in sorted(glob.glob(f"{hw}/temp*_input")):
            millideg = read_int(inp)
            if millideg is None or millideg <= 0:
                continue
            label = read_text(inp.replace("_input", "_label"), name)
            sensors.append({"chip": name, "label": label, "c": round(millideg / 1000, 1)})
    if not sensors:
        for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
            millideg = read_int(f"{zone}/temp")
            if millideg and millideg > 0:
                kind = read_text(f"{zone}/type", "zone")
                sensors.append({"chip": kind, "label": kind, "c": round(millideg / 1000, 1)})

    cpu_chips = ("k10temp", "coretemp", "zenpower", "cpu_thermal", "x86_pkg_temp", "acpitz")
    cpu = None
    for chip in cpu_chips:
        matches = [s for s in sensors if s["chip"] == chip]
        if matches:
            preferred = [s for s in matches if s["label"] in ("Tctl", "Tdie", "Package id 0")]
            cpu = preferred[0]["c"] if preferred else max(s["c"] for s in matches)
            break
    if cpu is None and sensors:
        cpu = max(s["c"] for s in sensors)
    hottest = max(sensors, key=lambda s: s["c"]) if sensors else None
    return {"cpu": cpu, "hottest": hottest, "sensors": sensors}


def read_power():
    batteries, on_ac = [], None
    for ps in sorted(glob.glob("/sys/class/power_supply/*")):
        kind = read_text(f"{ps}/type")
        if kind == "Mains":
            online = read_int(f"{ps}/online")
            if online is not None:
                on_ac = bool(online) or bool(on_ac)
        elif kind == "Battery" and read_int(f"{ps}/present", 1):
            now = read_int(f"{ps}/energy_now") or read_int(f"{ps}/charge_now")
            full = read_int(f"{ps}/energy_full") or read_int(f"{ps}/charge_full")
            design = read_int(f"{ps}/energy_full_design") or read_int(f"{ps}/charge_full_design")
            rate = read_int(f"{ps}/power_now") or read_int(f"{ps}/current_now")
            status = read_text(f"{ps}/status", "Unknown")
            hours = None
            if rate and now is not None and full:
                if status == "Discharging":
                    hours = now / rate
                elif status == "Charging":
                    hours = max(full - now, 0) / rate
            watts = read_int(f"{ps}/power_now")
            batteries.append({
                "name": os.path.basename(ps),
                "percent": read_int(f"{ps}/capacity"),
                "status": status,
                "hours": round(hours, 2) if hours else None,
                "health": round(100 * full / design) if full and design else None,
                "watts": round(watts / 1e6, 1) if watts else None,
                "cycles": read_int(f"{ps}/cycle_count"),
            })
    return {"on_ac": on_ac, "batteries": batteries}


def read_net_bytes():
    rx = tx = 0
    for line in read_text("/proc/net/dev", "").splitlines()[2:]:
        iface, _, rest = line.partition(":")
        if iface.strip() == "lo":
            continue
        fields = rest.split()
        rx += int(fields[0])
        tx += int(fields[8])
    return rx, tx


def read_wifi():
    lines = read_text("/proc/net/wireless", "").splitlines()[2:]
    for line in lines:
        iface, _, rest = line.partition(":")
        fields = rest.split()
        try:
            level = float(fields[2].rstrip("."))
        except (IndexError, ValueError):
            continue
        if level > 0:  # some drivers report an unsigned value
            level -= 256
        return {"iface": iface.strip(), "dbm": int(level)}
    return None


# ---------------------------------------------------------------- minecraft process

def find_server_pid():
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().split(b"\0")
        except OSError:
            continue
        if cmd and cmd[0].endswith(b"java") and any(arg.endswith(b"paper.jar") for arg in cmd):
            try:
                if os.path.samefile(f"/proc/{entry}/cwd", MC_DIR):
                    return int(entry)
            except OSError:
                return int(entry)
    return None


def read_proc_ticks(pid):
    try:
        stat = read_text(f"/proc/{pid}/stat")
        fields = stat[stat.rindex(")") + 2:].split()
        return int(fields[11]) + int(fields[12]), int(fields[19])  # utime+stime, starttime
    except (AttributeError, ValueError, IndexError):
        return None


def read_proc_rss(pid):
    for line in read_text(f"/proc/{pid}/status", "").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


def screen_session_exists():
    try:
        out = subprocess.run(["screen", "-ls"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return re.search(rf"\d+\.{re.escape(SCREEN_NAME)}\s", out) is not None


def send_console(command):
    if not screen_session_exists():
        raise RuntimeError("The server console isn't available (no screen session named 'mc').")
    subprocess.run(["screen", "-S", SCREEN_NAME, "-p", "0", "-X", "stuff", command + "\r"],
                   check=True, timeout=5)


# ---------------------------------------------------------------- server list ping

def _varint(n):
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(stream):
    value = shift = 0
    while True:
        b = stream.read(1)
        if not b:
            raise OSError("connection closed")
        value |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return value
        shift += 7
        if shift > 35:
            raise OSError("bad varint")


def ping_server(port, host="127.0.0.1"):
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.settimeout(3)
        host_bytes = host.encode()
        handshake = (b"\x00" + _varint(-1) + _varint(len(host_bytes)) + host_bytes
                     + struct.pack(">H", port) + _varint(1))
        sock.sendall(_varint(len(handshake)) + handshake + b"\x01\x00")
        stream = sock.makefile("rb")
        _read_varint(stream)  # packet length
        _read_varint(stream)  # packet id
        length = _read_varint(stream)
        data = stream.read(length)
    status = json.loads(data)
    desc = status.get("description", "")
    if isinstance(desc, dict):
        parts = [desc.get("text", "")] + [x.get("text", "") for x in desc.get("extra", []) if isinstance(x, dict)]
        desc = "".join(parts)
    players = status.get("players", {})
    return {
        "version": status.get("version", {}).get("name"),
        "motd": re.sub(r"§.", "", desc or ""),
        "online": players.get("online", 0),
        "max": players.get("max", 0),
        "names": sorted(p.get("name", "?") for p in players.get("sample", []) or []),
    }


# ---------------------------------------------------------------- sampler

class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot = {}
        self.history = {"cpu": [], "temp": [], "ram": [], "mc_cpu": []}
        self.slow = {"world_bytes": None, "backup_bytes": None}
        self.ping = None
        self.stopping_since = None
        self.task = {"name": None, "message": "", "ok": True}

    def run(self):
        prev_cpu = read_cpu_times()
        prev_net = read_net_bytes()
        prev_proc = None
        prev_t = time.monotonic()
        last_slow = last_ping = 0.0
        clk = os.sysconf("SC_CLK_TCK")
        ncpu = os.cpu_count() or 1
        while True:
            time.sleep(2)
            now = time.monotonic()
            dt = max(now - prev_t, 1e-3)

            cpu = read_cpu_times()
            usage = {}
            for name, (total, idle) in cpu.items():
                p_total, p_idle = prev_cpu.get(name, (total, idle))
                d_total = total - p_total
                usage[name] = round(100 * (1 - (idle - p_idle) / d_total), 1) if d_total > 0 else 0.0
            cores = [usage[k] for k in sorted((k for k in usage if k != "cpu"), key=lambda k: int(k[3:]))]

            rx, tx = read_net_bytes()
            net = {"rx_rate": max(rx - prev_net[0], 0) / dt, "tx_rate": max(tx - prev_net[1], 0) / dt,
                   "rx_total": rx, "tx_total": tx, "wifi": read_wifi()}

            pid = find_server_pid()
            proc = None
            if pid:
                ticks = read_proc_ticks(pid)
                if ticks:
                    sys_uptime = float(read_text("/proc/uptime", "0").split()[0])
                    cpu_pct = None
                    if prev_proc and prev_proc[0] == pid:
                        cpu_pct = round(100 * (ticks[0] - prev_proc[1]) / clk / dt / ncpu, 1)
                    proc = {"pid": pid, "cpu": cpu_pct, "rss": read_proc_rss(pid),
                            "uptime": round(sys_uptime - ticks[1] / clk)}
                    prev_proc = (pid, ticks[0])
            else:
                prev_proc = None

            if now - last_ping >= 4 or (pid and self.ping is None):
                last_ping = now
                port = int(server_properties().get("server-port", "25565") or 25565)
                try:
                    self.ping = ping_server(port) if pid else None
                except (OSError, ValueError):
                    self.ping = None

            if now - last_slow >= 60:
                last_slow = now
                self.slow = {
                    "world_bytes": sum(dir_size(MC_DIR / d) for d in WORLD_DIRS if (MC_DIR / d).is_dir()),
                    "backup_bytes": dir_size(MC_DIR / "backups") if (MC_DIR / "backups").is_dir() else 0,
                }

            if pid is None:
                self.stopping_since = None
                state = "stopped"
            elif self.stopping_since:
                state = "stopping"
            elif self.ping:
                state = "running"
            else:
                state = "starting"

            mem = read_memory()
            temps = read_temperatures()
            disk = shutil.disk_usage(MC_DIR if MC_DIR.exists() else "/")
            snapshot = {
                "time": time.time(),
                "hostname": socket.gethostname(),
                "uptime": float(read_text("/proc/uptime", "0").split()[0]),
                "load": os.getloadavg(),
                "cpu": {"total": usage.get("cpu", 0.0), "cores": cores, "count": ncpu},
                "memory": mem,
                "temps": temps,
                "power": read_power(),
                "disk": {"total": disk.total, "used": disk.used, "free": disk.free, **self.slow},
                "net": net,
                "server": {"state": state, "process": proc, "ping": self.ping,
                           "console": screen_session_exists() if pid else False,
                           "dir": str(MC_DIR)},
            }
            with self.lock:
                for key, value in (("cpu", usage.get("cpu", 0.0)), ("temp", temps["cpu"]),
                                   ("ram", round(100 * mem["used"] / mem["total"], 1) if mem["total"] else 0),
                                   ("mc_cpu", proc["cpu"] if proc and proc["cpu"] is not None else 0)):
                    series = self.history[key]
                    series.append(value)
                    del series[:-HISTORY_LEN]
                self.snapshot = snapshot
            prev_cpu, prev_net, prev_t = cpu, (rx, tx), now

    def status(self):
        with self.lock:
            return {**self.snapshot, "history": {k: list(v) for k, v in self.history.items()},
                    "task": dict(self.task)}

    # -------- actions

    def _set_task(self, name, message, ok=True):
        with self.lock:
            self.task = {"name": name, "message": message, "ok": ok}

    def busy(self):
        with self.lock:
            return self.task["name"] is not None

    def start_server(self):
        if find_server_pid():
            raise RuntimeError("The server is already running.")
        script = MC_DIR / "start.sh"
        if not script.exists():
            raise RuntimeError(f"{script} not found.")
        if screen_session_exists():
            raise RuntimeError("A screen session named 'mc' already exists. Close it first (screen -r mc, then exit).")
        subprocess.run(["screen", "-dmS", SCREEN_NAME, str(script)], cwd=MC_DIR, check=True, timeout=10)

    def stop_server(self):
        if not find_server_pid():
            raise RuntimeError("The server isn't running.")
        send_console("stop")
        self.stopping_since = time.time()

    def _wait_for_exit(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not find_server_pid():
                return True
            time.sleep(1)
        return False

    def restart_server(self):
        if self.busy():
            raise RuntimeError("Another task is still running.")
        self.stop_server()

        def worker():
            self._set_task("restart", "Stopping the server…")
            try:
                if not self._wait_for_exit(120):
                    raise RuntimeError("The server didn't stop within 2 minutes.")
                time.sleep(2)  # let screen close its session
                self._set_task("restart", "Starting the server…")
                self.start_server()
                self._set_task(None, "Server restarted.")
            except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard
                self._set_task(None, f"Restart failed: {exc}", ok=False)

        threading.Thread(target=worker, daemon=True).start()

    def force_kill(self):
        pid = find_server_pid()
        if not pid:
            raise RuntimeError("The server isn't running.")
        os.kill(pid, 9)

    def backup(self):
        if self.busy():
            raise RuntimeError("Another task is still running.")
        worlds = [d for d in WORLD_DIRS if (MC_DIR / d).is_dir()]
        if not worlds:
            raise RuntimeError("No world folder found.")

        def worker():
            running = find_server_pid() is not None and screen_session_exists()
            log = MC_DIR / "logs" / "latest.log"
            try:
                if running:
                    self._set_task("backup", "Saving the world…")
                    offset = log.stat().st_size if log.exists() else 0
                    send_console("save-off")
                    send_console("save-all flush")
                    deadline = time.time() + 120
                    while time.time() < deadline:
                        time.sleep(1)
                        with open(log, "rb") as f:
                            f.seek(offset)
                            if b"Saved the game" in f.read():
                                break
                self._set_task("backup", "Compressing the world…")
                backups = MC_DIR / "backups"
                backups.mkdir(exist_ok=True)
                target = backups / time.strftime("world-%Y%m%d-%H%M%S.tar.gz")
                partial = target.with_suffix(".partial")
                with tarfile.open(partial, "w:gz", compresslevel=4) as tar:
                    for d in worlds:
                        tar.add(MC_DIR / d, arcname=d, filter=lambda ti: None if ti.name.endswith("session.lock") else ti)
                partial.rename(target)
                self.slow["backup_bytes"] = dir_size(backups)
                self._set_task(None, f"Backup saved: {target.name}")
            except Exception as exc:  # noqa: BLE001 - surfaced to the dashboard
                self._set_task(None, f"Backup failed: {exc}", ok=False)
            finally:
                if running:
                    try:
                        send_console("save-on")
                    except Exception:  # noqa: BLE001
                        pass

        threading.Thread(target=worker, daemon=True).start()


def list_backups():
    folder = MC_DIR / "backups"
    if not folder.is_dir():
        return []
    items = []
    for path in folder.iterdir():
        if BACKUP_NAME_RE.match(path.name):
            st = path.stat()
            items.append({"name": path.name, "size": st.st_size, "time": st.st_mtime})
    return sorted(items, key=lambda b: b["time"], reverse=True)


def read_console(offset):
    log = MC_DIR / "logs" / "latest.log"
    try:
        size = log.stat().st_size
    except OSError:
        return {"offset": 0, "text": "", "reset": True}
    reset = offset < 0 or offset > size
    if reset:
        offset = max(0, size - 24000)
    with open(log, "rb") as f:
        f.seek(offset)
        data = f.read(min(size - offset, 200000))
    if reset and offset > 0:
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else data
    return {"offset": offset + len(data) if not reset else size, "reset": reset,
            "text": ANSI_RE.sub("", data.decode("utf-8", "replace"))}


# ---------------------------------------------------------------- http

MONITOR = Monitor()


class Handler(BaseHTTPRequestHandler):
    server_version = "Nimbus"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self, query):
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        supplied = supplied or query.get("token", [""])[0]
        return hmac.compare_digest(supplied.encode(), TOKEN.encode())

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(min(length, 65536)))
        except ValueError:
            return {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if not self._authorized(query):
            return self._send(401, {"error": "Wrong or missing access token."})
        if url.path == "/api/status":
            return self._send(200, MONITOR.status())
        if url.path == "/api/console":
            return self._send(200, read_console(int(query.get("offset", ["-1"])[0] or -1)))
        if url.path == "/api/backups":
            return self._send(200, list_backups())
        if url.path == "/api/backups/download":
            name = query.get("name", [""])[0]
            path = MC_DIR / "backups" / name
            if not BACKUP_NAME_RE.match(name) or not path.is_file():
                return self._send(404, {"error": "No such backup."})
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return None
        return self._send(404, {"error": "Not found."})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not self._authorized(query):
            return self._send(401, {"error": "Wrong or missing access token."})
        body = self._body()
        try:
            if url.path == "/api/server/start":
                MONITOR.start_server()
                msg = "Starting the server…"
            elif url.path == "/api/server/stop":
                MONITOR.stop_server()
                msg = "Stopping the server…"
            elif url.path == "/api/server/restart":
                MONITOR.restart_server()
                msg = "Restarting the server…"
            elif url.path == "/api/server/kill":
                MONITOR.force_kill()
                msg = "Server process killed."
            elif url.path == "/api/command":
                command = str(body.get("command", "")).strip().lstrip("/")
                if not command or "\n" in command or "\r" in command:
                    raise RuntimeError("Enter a single command.")
                send_console(command)
                msg = f"Sent: {command}"
            elif url.path == "/api/backup":
                MONITOR.backup()
                msg = "Backup started…"
            elif url.path == "/api/backups/delete":
                name = str(body.get("name", ""))
                path = MC_DIR / "backups" / name
                if not BACKUP_NAME_RE.match(name) or not path.is_file():
                    raise RuntimeError("No such backup.")
                path.unlink()
                msg = f"Deleted {name}"
            else:
                return self._send(404, {"error": "Not found."})
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(200, {"message": msg})


# ---------------------------------------------------------------- install

def install(port):
    if os.geteuid() != 0:
        sys.exit("Run the installer with sudo:  sudo python3 nimbus.py --install")
    user = os.environ.get("SUDO_USER")
    if not user or user == "root":
        sys.exit("Run it with sudo from your normal account, not as root.")
    if not shutil.which("screen"):
        subprocess.run(["apt-get", "install", "-y", "screen"], check=True)
    home = Path(os.path.expanduser(f"~{user}"))
    script = Path(__file__).resolve()
    unit = f"""[Unit]
Description=Nimbus Minecraft dashboard
After=network-online.target
Wants=network-online.target

[Service]
User={user}
WorkingDirectory={home}
ExecStart={sys.executable} {script} --mc-dir {MC_DIR} --port {port}
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""
    Path("/etc/systemd/system/nimbus.service").write_text(unit)
    token = subprocess.run(["sudo", "-H", "-u", user, sys.executable, str(script), "--token"],
                           capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "nimbus"], check=True)
    subprocess.run(["systemctl", "restart", "nimbus"], check=True)
    ufw = subprocess.run(["ufw", "status"], capture_output=True, text=True) if shutil.which("ufw") else None
    if ufw and "Status: active" in ufw.stdout:
        subprocess.run(["ufw", "allow", f"{port}/tcp"], check=True)
    print(f"\nNimbus is running and will start on boot.\n"
          f"  Open:  http://{lan_ip()}:{port}\n"
          f"  Token: {token}\n")


def main():
    global MC_DIR, TOKEN
    parser = argparse.ArgumentParser(description="Minecraft server dashboard")
    parser.add_argument("--mc-dir", default=str(Path.home() / "minecraft"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--install", action="store_true", help="install as a systemd service (use sudo)")
    parser.add_argument("--token", action="store_true", help="print the access token and exit")
    args = parser.parse_args()

    if args.install:
        user = os.environ.get("SUDO_USER")
        if user and args.mc_dir == str(Path.home() / "minecraft"):
            args.mc_dir = os.path.expanduser(f"~{user}/minecraft")
        MC_DIR = Path(args.mc_dir)
        return install(args.port)

    MC_DIR = Path(args.mc_dir).expanduser().resolve()
    TOKEN = load_token()
    if args.token:
        print(TOKEN)
        return None

    threading.Thread(target=MONITOR.run, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Nimbus on http://{lan_ip()}:{args.port}  (token: {TOKEN})", flush=True)
    server.serve_forever()
    return None


# ---------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nimbus</title>
<style>
:root {
  --bg: #f4f5f7; --card: #ffffff; --text: #1d2330; --muted: #677085; --line: #e3e6ec;
  --accent: #2f6fde; --good: #1f9d55; --warn: #c98a0c; --bad: #d64545; --track: #e9ecf1;
  --console-bg: #11151c; --console-text: #d6dbe4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1218; --card: #171b23; --text: #e6e9ef; --muted: #8b93a5; --line: #262c37;
    --accent: #5b95f5; --good: #3ccf7e; --warn: #f0b429; --bad: #f06565; --track: #232934;
    --console-bg: #0b0e13; --console-text: #cfd5df;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { display: flex; align-items: center; gap: 12px; padding: 14px 20px;
  border-bottom: 1px solid var(--line); background: var(--card); position: sticky; top: 0; z-index: 2; }
header h1 { font-size: 17px; margin: 0; letter-spacing: .2px; }
header .host { color: var(--muted); }
header .spacer { flex: 1; }
main { padding: 16px 20px 40px; max-width: 1400px; margin: 0 auto; }
.alerts { display: flex; flex-direction: column; gap: 8px; margin-bottom: 14px; }
.alert { padding: 10px 14px; border-radius: 8px; border: 1px solid var(--bad);
  color: var(--bad); background: color-mix(in srgb, var(--bad) 10%, transparent); font-weight: 500; }
.alert.warn { border-color: var(--warn); color: var(--warn); background: color-mix(in srgb, var(--warn) 10%, transparent); }
.grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; min-width: 0; }
.card h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .8px; color: var(--muted);
  margin: 0 0 12px; font-weight: 600; }
.wide { grid-column: 1 / -1; }
.big { font-size: 30px; font-weight: 650; font-variant-numeric: tabular-nums; line-height: 1.1; }
.sub { color: var(--muted); font-size: 13px; }
.row { display: flex; justify-content: space-between; gap: 10px; padding: 5px 0;
  border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
.row:last-child { border-bottom: 0; }
.row span:first-child { color: var(--muted); }
.bar { height: 8px; background: var(--track); border-radius: 4px; overflow: hidden; margin: 6px 0 10px; }
.bar > div { height: 100%; background: var(--accent); border-radius: 4px; transition: width .4s; }
.split { display: flex; gap: 18px; align-items: flex-end; flex-wrap: wrap; }
canvas.spark { width: 100%; height: 46px; display: block; margin-top: 8px; }
.cores { display: grid; grid-template-columns: repeat(auto-fill, minmax(26px, 1fr)); gap: 3px; margin-top: 10px; }
.core { height: 26px; border-radius: 3px; background: var(--track); position: relative; overflow: hidden; }
.core > div { position: absolute; bottom: 0; left: 0; right: 0; background: var(--accent); opacity: .85; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 600; border: 1px solid var(--line); }
.pill::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
.s-running { color: var(--good); } .s-stopped { color: var(--bad); }
.s-starting, .s-stopping { color: var(--warn); }
.btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
button { font: inherit; padding: 7px 14px; border-radius: 7px; border: 1px solid var(--line);
  background: var(--card); color: var(--text); cursor: pointer; }
button:hover:not(:disabled) { border-color: var(--accent); }
button:disabled { opacity: .45; cursor: default; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button.danger { color: var(--bad); }
button.small { padding: 3px 9px; font-size: 12px; }
.console { background: var(--console-bg); color: var(--console-text); border-radius: 8px; padding: 10px 12px;
  height: 340px; overflow: auto; font: 12px/1.5 ui-monospace, Consolas, "Cascadia Mono", monospace;
  white-space: pre-wrap; word-break: break-word; }
.console .w { color: #f0b429; } .console .e { color: #f06565; } .console .c { color: #7fb4ff; }
form.cmd { display: flex; gap: 8px; margin-top: 10px; }
input[type=text], input[type=password] { flex: 1; min-width: 0; font: inherit; padding: 8px 10px; border-radius: 7px;
  border: 1px solid var(--line); background: var(--bg); color: var(--text); }
.quick { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.list .item { display: flex; align-items: center; gap: 8px; padding: 7px 0; border-bottom: 1px solid var(--line); }
.list .item:last-child { border-bottom: 0; }
.list .item .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty { color: var(--muted); padding: 6px 0; }
#toast { position: fixed; right: 18px; bottom: 18px; padding: 10px 14px; border-radius: 8px; background: var(--text);
  color: var(--bg); opacity: 0; transform: translateY(8px); transition: .25s; pointer-events: none; max-width: 420px; }
#toast.show { opacity: 1; transform: none; }
#toast.err { background: var(--bad); color: #fff; }
#login { max-width: 380px; margin: 12vh auto; }
#login p { color: var(--muted); }
details summary { cursor: pointer; color: var(--muted); margin-top: 8px; }
@media (max-width: 560px) { header, main { padding-left: 16px; padding-right: 16px; } .big { font-size: 26px; } }
</style>
</head>
<body>
<header>
  <h1>Nimbus</h1><span class="host" id="host"></span>
  <span class="spacer"></span>
  <span class="sub" id="updated"></span>
</header>

<div id="login" class="card" hidden>
  <h2>Access token</h2>
  <p>Paste the token shown when Nimbus was installed. Run <code>python3 nimbus.py --token</code> on the server to see it again.</p>
  <form id="loginForm" class="cmd"><input type="password" id="tokenInput" placeholder="Token" autocomplete="off"><button class="primary">Connect</button></form>
</div>

<main id="app" hidden>
  <div class="alerts" id="alerts"></div>
  <div class="grid">
    <section class="card">
      <h2>Minecraft server</h2>
      <div class="split"><span class="pill" id="mcState">…</span><span class="sub" id="mcVersion"></span></div>
      <div class="big" style="margin-top:12px" id="mcPlayers">–</div>
      <div class="sub" id="mcMotd"></div>
      <div style="margin-top:10px">
        <div class="row"><span>Server CPU</span><span id="mcCpu">–</span></div>
        <div class="row"><span>Server memory</span><span id="mcRam">–</span></div>
        <div class="row"><span>Server uptime</span><span id="mcUptime">–</span></div>
      </div>
      <canvas class="spark" id="sparkMc"></canvas>
      <div class="btns">
        <button class="primary" id="btnStart">Start</button>
        <button id="btnStop">Stop</button>
        <button id="btnRestart">Restart</button>
        <button class="danger" id="btnKill" title="Only if the server is frozen">Force kill</button>
      </div>
      <div class="sub" id="task" style="margin-top:10px"></div>
    </section>

    <section class="card">
      <h2>Processor</h2>
      <div class="split">
        <div><div class="big" id="cpuPct">–</div><div class="sub">usage</div></div>
        <div><div class="big" id="cpuTemp">–</div><div class="sub">temperature</div></div>
      </div>
      <canvas class="spark" id="sparkCpu"></canvas>
      <div class="cores" id="cores"></div>
      <div class="row" style="margin-top:10px"><span>Load (1 / 5 / 15 min)</span><span id="load">–</span></div>
      <details><summary>All sensors</summary><div id="sensors"></div></details>
    </section>

    <section class="card">
      <h2>Memory &amp; storage</h2>
      <div class="row"><span>RAM</span><span id="ramText">–</span></div>
      <div class="bar"><div id="ramBar"></div></div>
      <div class="row"><span>Swap</span><span id="swapText">–</span></div>
      <div class="row"><span>Disk</span><span id="diskText">–</span></div>
      <div class="bar"><div id="diskBar"></div></div>
      <div class="row"><span>World size</span><span id="worldSize">–</span></div>
      <div class="row"><span>Backups</span><span id="backupSize">–</span></div>
    </section>

    <section class="card">
      <h2>Power</h2>
      <div class="split">
        <div><div class="big" id="batPct">–</div><div class="sub" id="batStatus"></div></div>
      </div>
      <div class="bar"><div id="batBar"></div></div>
      <div class="row"><span>Charger</span><span id="ac">–</span></div>
      <div class="row"><span>Time left</span><span id="batTime">–</span></div>
      <div class="row"><span>Power draw</span><span id="batWatts">–</span></div>
      <div class="row"><span>Battery health</span><span id="batHealth">–</span></div>
    </section>

    <section class="card">
      <h2>Network &amp; system</h2>
      <div class="row"><span>Download</span><span id="rx">–</span></div>
      <div class="row"><span>Upload</span><span id="tx">–</span></div>
      <div class="row"><span>Wi-Fi signal</span><span id="wifi">–</span></div>
      <div class="row"><span>Computer uptime</span><span id="uptime">–</span></div>
      <div class="row"><span>CPU cores</span><span id="ncpu">–</span></div>
    </section>

    <section class="card">
      <h2>Players online</h2>
      <div class="list" id="players"></div>
      <form class="cmd" id="playerForm"><input type="text" id="playerName" placeholder="Player name"><button type="button" data-p="whitelist add">Whitelist</button><button type="button" data-p="op">Op</button></form>
    </section>

    <section class="card wide">
      <h2>Console</h2>
      <div class="console" id="console"></div>
      <form class="cmd" id="cmdForm"><input type="text" id="cmdInput" placeholder="Type a server command, e.g. say hello" autocomplete="off"><button class="primary">Send</button></form>
      <div class="quick">
        <button class="small" data-cmd="list">List players</button>
        <button class="small" data-cmd="mspt">Tick times</button>
        <button class="small" data-cmd="save-all">Save world</button>
        <button class="small" data-cmd="time set day">Make it day</button>
        <button class="small" data-cmd="weather clear">Clear weather</button>
        <button class="small" data-cmd="difficulty normal">Normal difficulty</button>
        <button class="small" data-cmd="whitelist on">Whitelist on</button>
        <button class="small" data-cmd="whitelist off">Whitelist off</button>
        <button class="small" data-cmd="chunky progress">Chunky progress</button>
      </div>
    </section>

    <section class="card wide">
      <h2>Backups</h2>
      <div class="btns" style="margin-top:0;margin-bottom:10px"><button class="primary" id="btnBackup">Back up now</button><span class="sub" style="align-self:center">Saves the world, then archives it to <code>minecraft/backups</code>.</span></div>
      <div class="list" id="backups"></div>
    </section>
  </div>
</main>
<div id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let token = '';
try { token = localStorage.getItem('nimbus-token') || ''; } catch (e) {}
if (location.hash.startsWith('#token=')) {
  token = decodeURIComponent(location.hash.slice(7));
  try { localStorage.setItem('nimbus-token', token); } catch (e) {}
  history.replaceState(null, '', location.pathname);
}
let state = null, consoleOffset = -1, lastBackupFetch = 0;

function fmtBytes(b) {
  if (b == null) return '–';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (b >= 100 || i === 0 ? b.toFixed(0) : b.toFixed(1)) + ' ' + u[i];
}
function fmtDur(s) {
  if (s == null) return '–';
  s = Math.floor(s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}
function esc(t) { return String(t).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
function toast(msg, err) {
  const t = $('toast'); t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.className = '', 3500);
}
function setBar(id, pct) {
  const el = $(id); el.style.width = Math.max(0, Math.min(100, pct || 0)) + '%';
  el.style.background = pct >= 90 ? css('--bad') : pct >= 75 ? css('--warn') : css('--accent');
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' } });
  if (res.status === 401) { showLogin(); throw new Error('unauthorized'); }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
async function act(path, body, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  try { const r = await api(path, { method: 'POST', body: JSON.stringify(body || {}) }); toast(r.message); pollStatus(); }
  catch (e) { if (e.message !== 'unauthorized') toast(e.message, true); }
}
function sendCmd(cmd) { return act('/api/command', { command: cmd }); }

function spark(id, data, max, color) {
  const c = $(id), dpr = window.devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
  if (!w) return;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, w, h);
  const pts = data.filter(v => v != null);
  if (pts.length < 2) return;
  const top = Math.max(max, ...pts), n = 150, step = w / (n - 1), off = n - data.length;
  g.beginPath();
  data.forEach((v, i) => { const x = (i + off) * step, y = h - 2 - ((v || 0) / top) * (h - 4); i ? g.lineTo(x, y) : g.moveTo(x, y); });
  g.strokeStyle = color; g.lineWidth = 1.5; g.stroke();
  g.lineTo((data.length - 1 + off) * step, h); g.lineTo(off * step, h); g.closePath();
  g.globalAlpha = .12; g.fillStyle = color; g.fill();
}

function render(s) {
  state = s;
  $('host').textContent = s.hostname || '';
  $('updated').textContent = 'Updated ' + new Date(s.time * 1000).toLocaleTimeString();

  const srv = s.server, st = srv.state, ping = srv.ping, proc = srv.process;
  const pill = $('mcState'); pill.className = 'pill s-' + st; pill.textContent = st[0].toUpperCase() + st.slice(1);
  $('mcVersion').textContent = ping && ping.version ? ping.version : '';
  $('mcPlayers').textContent = ping ? `${ping.online} / ${ping.max} players` : (st === 'stopped' ? 'Offline' : '–');
  $('mcMotd').textContent = ping ? ping.motd : '';
  $('mcCpu').textContent = proc && proc.cpu != null ? proc.cpu + '%' : '–';
  $('mcRam').textContent = proc ? fmtBytes(proc.rss) : '–';
  $('mcUptime').textContent = proc ? fmtDur(proc.uptime) : '–';
  const busy = !!(s.task && s.task.name);
  $('btnStart').disabled = st !== 'stopped' || busy;
  $('btnStop').disabled = st === 'stopped' || st === 'stopping' || !srv.console || busy;
  $('btnRestart').disabled = st !== 'running' || !srv.console || busy;
  $('btnKill').disabled = st === 'stopped';
  $('btnBackup').disabled = busy;
  const task = $('task');
  task.textContent = s.task && s.task.message ? s.task.message : (st !== 'stopped' && !srv.console ? 'Console not available: start the server from here (or in a screen named "mc") to control it.' : '');
  task.style.color = s.task && !s.task.ok ? css('--bad') : '';

  const cpu = s.cpu, t = s.temps;
  $('cpuPct').textContent = cpu.total.toFixed(0) + '%';
  $('cpuTemp').textContent = t.cpu != null ? t.cpu.toFixed(0) + '°C' : 'n/a';
  $('cpuTemp').style.color = t.cpu >= 90 ? css('--bad') : t.cpu >= 80 ? css('--warn') : '';
  $('cores').innerHTML = cpu.cores.map((v, i) => `<div class="core" title="Core ${i}: ${v}%"><div style="height:${v}%"></div></div>`).join('');
  $('load').textContent = s.load.map(v => v.toFixed(2)).join(' / ');
  $('sensors').innerHTML = t.sensors.length ? t.sensors.map(x => `<div class="row"><span>${esc(x.chip)} · ${esc(x.label)}</span><span>${x.c}°C</span></div>`).join('') : '<div class="empty">No sensors found</div>';

  const m = s.memory, ramPct = m.total ? 100 * m.used / m.total : 0;
  $('ramText').textContent = `${fmtBytes(m.used)} / ${fmtBytes(m.total)} (${ramPct.toFixed(0)}%)`; setBar('ramBar', ramPct);
  $('swapText').textContent = m.swap_total ? `${fmtBytes(m.swap_used)} / ${fmtBytes(m.swap_total)}` : 'none';
  const d = s.disk, diskPct = 100 * d.used / d.total;
  $('diskText').textContent = `${fmtBytes(d.used)} / ${fmtBytes(d.total)} (${fmtBytes(d.free)} free)`; setBar('diskBar', diskPct);
  $('worldSize').textContent = fmtBytes(d.world_bytes);
  $('backupSize').textContent = fmtBytes(d.backup_bytes);

  const p = s.power, b = p.batteries[0];
  if (b) {
    $('batPct').textContent = b.percent != null ? b.percent + '%' : '–';
    $('batStatus').textContent = b.status;
    setBar('batBar', b.percent); $('batBar').style.background = b.percent <= 20 ? css('--bad') : css('--good');
    $('batTime').textContent = b.hours ? fmtDur(b.hours * 3600) + (b.status === 'Charging' ? ' to full' : ' remaining') : '–';
    $('batWatts').textContent = b.watts != null ? b.watts + ' W' : '–';
    $('batHealth').textContent = b.health != null ? b.health + '%' + (b.cycles ? ` · ${b.cycles} cycles` : '') : '–';
  } else {
    $('batPct').textContent = 'No battery'; $('batStatus').textContent = ''; setBar('batBar', 0);
  }
  $('ac').textContent = p.on_ac == null ? '–' : p.on_ac ? 'Plugged in' : 'Unplugged';
  $('ac').style.color = p.on_ac === false ? css('--bad') : '';

  const n = s.net;
  $('rx').textContent = fmtBytes(n.rx_rate) + '/s · ' + fmtBytes(n.rx_total) + ' total';
  $('tx').textContent = fmtBytes(n.tx_rate) + '/s · ' + fmtBytes(n.tx_total) + ' total';
  if (n.wifi) {
    const q = n.wifi.dbm >= -55 ? 'excellent' : n.wifi.dbm >= -67 ? 'good' : n.wifi.dbm >= -75 ? 'fair' : 'weak';
    $('wifi').textContent = `${n.wifi.dbm} dBm (${q})`; $('wifi').style.color = q === 'weak' ? css('--bad') : '';
  } else $('wifi').textContent = 'wired / n/a';
  $('uptime').textContent = fmtDur(s.uptime);
  $('ncpu').textContent = cpu.count;

  const names = ping ? ping.names : [];
  $('players').innerHTML = names.length ? names.map(nm => `<div class="item"><span class="name">${esc(nm)}</span>
      <button class="small" data-pc="gamemode survival ${esc(nm)}">Survival</button>
      <button class="small" data-pc="gamemode creative ${esc(nm)}">Creative</button>
      <button class="small danger" data-pc="kick ${esc(nm)}" data-confirm="Kick ${esc(nm)}?">Kick</button></div>`).join('')
    : `<div class="empty">${st === 'running' ? 'Nobody online' : 'Server offline'}</div>`;

  const alerts = [];
  if (p.on_ac === false) alerts.push(['bad', `Charger unplugged: the laptop is running on battery${b && b.percent != null ? ' (' + b.percent + '%)' : ''}.`]);
  if (b && b.percent != null && b.percent <= 20 && b.status === 'Discharging') alerts.push(['bad', 'Battery low: the server will shut off when it runs out.']);
  if (t.cpu >= 90) alerts.push(['bad', `CPU is very hot (${t.cpu}°C). Check the vents and fans.`]);
  else if (t.cpu >= 80) alerts.push(['warn', `CPU is running warm (${t.cpu}°C).`]);
  if (ramPct >= 92) alerts.push(['warn', 'RAM is almost full.']);
  if (diskPct >= 90) alerts.push(['bad', `Disk is ${diskPct.toFixed(0)}% full. Delete old backups.`]);
  if (n.wifi && n.wifi.dbm < -75) alerts.push(['warn', 'Wi-Fi signal is weak, so players may lag.']);
  $('alerts').innerHTML = alerts.map(([k, msg]) => `<div class="alert ${k === 'warn' ? 'warn' : ''}">${esc(msg)}</div>`).join('');

  spark('sparkCpu', s.history.cpu, 100, css('--accent'));
  spark('sparkMc', s.history.mc_cpu, 25, css('--good'));

  if (Date.now() - lastBackupFetch > 15000 || (renderBackups.wasBusy && !busy)) { lastBackupFetch = Date.now(); loadBackups(); }
  renderBackups.wasBusy = busy;
}

async function loadBackups() {
  try { renderBackups(await api('/api/backups')); } catch (e) {}
}
function renderBackups(list) {
  $('backups').innerHTML = list.length ? list.map(b => `<div class="item"><span class="name">${esc(b.name)}</span>
      <span class="sub">${fmtBytes(b.size)} · ${new Date(b.time * 1000).toLocaleString()}</span>
      <a href="/api/backups/download?name=${encodeURIComponent(b.name)}&token=${encodeURIComponent(token)}"><button class="small">Download</button></a>
      <button class="small danger" data-del="${esc(b.name)}">Delete</button></div>`).join('')
    : '<div class="empty">No backups yet</div>';
}

async function pollStatus() {
  try { const s = await api('/api/status'); if (s.time) render(s); } catch (e) {}
}
async function pollConsole() {
  try {
    const r = await api('/api/console?offset=' + consoleOffset);
    const box = $('console'), atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    if (r.reset) box.innerHTML = '';
    if (r.text) {
      const html = esc(r.text).split('\n').map(l => /WARN/.test(l) ? `<span class="w">${l}</span>` : /ERROR|SEVERE/.test(l) ? `<span class="e">${l}</span>` : /issued server command|^&gt; /.test(l) ? `<span class="c">${l}</span>` : l).join('\n');
      box.insertAdjacentHTML('beforeend', html);
      while (box.childNodes.length > 4000) box.removeChild(box.firstChild);
      if (atBottom || r.reset) box.scrollTop = box.scrollHeight;
    }
    consoleOffset = r.offset;
  } catch (e) {}
}

function showLogin() { $('app').hidden = true; $('login').hidden = false; $('tokenInput').focus(); }
let timers = [];
function startApp() {
  $('login').hidden = true; $('app').hidden = false;
  timers.forEach(clearInterval);
  pollStatus(); pollConsole(); loadBackups();
  timers = [setInterval(pollStatus, 2000), setInterval(pollConsole, 1000)];
}

$('loginForm').onsubmit = async e => {
  e.preventDefault(); token = $('tokenInput').value.trim();
  try { await api('/api/status'); try { localStorage.setItem('nimbus-token', token); } catch (e) {} startApp(); }
  catch (err) { toast('That token didn\'t work.', true); }
};
$('btnStart').onclick = () => act('/api/server/start');
$('btnStop').onclick = () => act('/api/server/stop', null, 'Stop the server? Players will be disconnected.');
$('btnRestart').onclick = () => act('/api/server/restart', null, 'Restart the server? Players will be disconnected.');
$('btnKill').onclick = () => act('/api/server/kill', null, 'Force kill the server process? Anything not saved will be lost. Only use this if the server is frozen.');
$('btnBackup').onclick = () => act('/api/backup');
$('cmdForm').onsubmit = e => { e.preventDefault(); const v = $('cmdInput').value.trim(); if (v) { sendCmd(v); $('cmdInput').value = ''; } };
document.addEventListener('click', e => {
  const q = e.target.closest('[data-cmd]'); if (q) return sendCmd(q.dataset.cmd);
  const pc = e.target.closest('[data-pc]'); if (pc) { if (pc.dataset.confirm && !confirm(pc.dataset.confirm)) return; return sendCmd(pc.dataset.pc); }
  const pp = e.target.closest('[data-p]'); if (pp) { const nm = $('playerName').value.trim(); if (!nm) return toast('Type a player name first.', true); return sendCmd(pp.dataset.p + ' ' + nm); }
  const del = e.target.closest('[data-del]');
  if (del) act('/api/backups/delete', { name: del.dataset.del }, `Delete ${del.dataset.del}? This can't be undone.`).then(loadBackups);
});
window.addEventListener('resize', () => state && render(state));

if (token) api('/api/status').then(startApp).catch(e => { if (e.message !== 'unauthorized') startApp(); }); else showLogin();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
