"""Server security: a paired computer is the only kind that may connect; the pairing is approved once on the server, each computer gets its own secret, and the streaming password is random per server."""
import getpass
import hashlib
import hmac
import json
import os
import platform
import secrets
import socket
import threading
import time

# WARNING: RFC 3526 group 14 (a 2048-bit safe prime); both computers agree on a key through it without ever sending the key.
_P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
_G = 2
_BYTES = 256

DEVICES_FILE_NAME = "server_devices.json"
SHARE_SECRET_FILE_NAME = "server_share_secret.json"
ANY_SLOT = "*"


# ============================================================================
# SECTION: Key agreement and sealed messages
# ============================================================================


def dh_keypair():
    """A fresh (private number, public number) pair for one pairing."""
    private = secrets.randbelow(_P - 4) + 2
    return private, pow(_G, private, _P)


def dh_valid_public(public):
    """True for a public number that is a real group member and not a trivial value an attacker could force."""
    return isinstance(public, int) and 2 <= public <= _P - 2


def dh_shared_key(private, other_public):
    """The 32-byte key both computers reach from their own private number and the other's public number."""
    if not dh_valid_public(other_public):
        raise ValueError("bad public key")
    return hashlib.sha256(pow(other_public, private, _P).to_bytes(_BYTES, "big")).digest()


def sas_code(key):
    """The four digits both screens show for one pairing; the owner compares them, which exposes anyone sitting in the middle."""
    return "%04d" % (int.from_bytes(hmac.new(key, b"retro-tv-sas", hashlib.sha256).digest()[:4], "big") % 10000)


def _keystream(key, nonce, length):
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(key, b"stream" + nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def seal(key, plaintext):
    """Hides and signs a short message with a 32-byte key; returns hex text."""
    nonce = secrets.token_bytes(16)
    body = bytes(a ^ b for a, b in zip(plaintext, _keystream(key, nonce, len(plaintext))))
    tag = hmac.new(key, b"tag" + nonce + body, hashlib.sha256).digest()
    return (nonce + body + tag).hex()


def unseal(key, text):
    """The message sealed with this key, or None when it was changed or sealed with another key."""
    try:
        raw = bytes.fromhex(str(text))
    except ValueError:
        return None
    if len(raw) < 48:
        return None
    nonce, body, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(key, b"tag" + nonce + body, hashlib.sha256).digest()):
        return None
    return bytes(a ^ b for a, b in zip(body, _keystream(key, nonce, len(body))))


def new_nonce():
    return secrets.token_bytes(16)


def auth_mac(secret, nonce):
    """The answer to a login challenge: proves the computer holds its secret without sending it."""
    return hmac.new(secret, b"retro-tv-auth" + nonce, hashlib.sha256).hexdigest()


def check_auth(secret, nonce, mac):
    return hmac.compare_digest(auth_mac(secret, nonce), str(mac))


def session_key(secret, nonce):
    """The key the server seals the streaming sign-in with for one connection."""
    return hmac.new(secret, b"retro-tv-session" + nonce, hashlib.sha256).digest()


# ============================================================================
# SECTION: Stores
# ============================================================================


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class PairedDevices:
    """The computers the owner approved: {device id: {"secret", "name", "info", "slots", "paired_at"}}, one small file read fresh each time so the tray and the program agree."""

    def __init__(self, base_dir, clock=time.time):
        self._path = os.path.join(base_dir, "logs", DEVICES_FILE_NAME)
        self._clock = clock
        self._lock = threading.Lock()

    def _all(self):
        return _read_json(self._path)

    def secret_for(self, device_id):
        rec = self._all().get(str(device_id))
        try:
            return bytes.fromhex(rec["secret"]) if rec else None
        except (KeyError, ValueError, TypeError):
            return None

    def allows(self, device_id, slot_id):
        """True when this computer may read this profile (its slot id, or any when it was approved for any)."""
        rec = self._all().get(str(device_id))
        if not rec:
            return False
        slots = rec.get("slots") or []
        return ANY_SLOT in slots or str(slot_id) in slots

    def slots_for(self, device_id):
        return list((self._all().get(str(device_id)) or {}).get("slots") or [])

    def approve(self, device_id, info, slot_id):
        """Adds the profile to the computer (making its secret the first time) and returns its secret."""
        with self._lock:
            data = self._all()
            rec = data.get(str(device_id))
            if not rec:
                rec = {"secret": secrets.token_bytes(32).hex(), "name": str(info.get("host", "")), "info": dict(info),
                       "slots": [], "paired_at": self._clock()}
            rec["info"] = dict(info)
            slot = ANY_SLOT if slot_id in (None, "") else str(slot_id)
            if slot not in rec["slots"]:
                rec["slots"].append(slot)
            data[str(device_id)] = rec
            _write_json(self._path, data)
            return bytes.fromhex(rec["secret"])

    def remove(self, device_id):
        with self._lock:
            data = self._all()
            if data.pop(str(device_id), None) is not None:
                _write_json(self._path, data)
                return True
        return False

    def listing(self):
        """[(device id, name, slots)] for every approved computer."""
        return [(k, v.get("name", ""), list(v.get("slots") or [])) for k, v in sorted(self._all().items())]


def share_password(base_dir):
    """The streaming account's password: made once per server from random bytes, kept in the server's logs folder, never in the program."""
    path = os.path.join(base_dir, "logs", SHARE_SECRET_FILE_NAME)
    data = _read_json(path)
    password = str(data.get("password", ""))
    if len(password) < 20:
        password = secrets.token_urlsafe(24)
        _write_json(path, {"password": password, "applied": False})
    return password


def share_password_applied(base_dir):
    """True once the streaming account on this computer holds the current random password."""
    path = os.path.join(base_dir, "logs", SHARE_SECRET_FILE_NAME)
    return bool(_read_json(path).get("applied"))


def mark_share_password_applied(base_dir):
    path = os.path.join(base_dir, "logs", SHARE_SECRET_FILE_NAME)
    data = _read_json(path)
    if data.get("password"):
        data["applied"] = True
        _write_json(path, data)


def clear_share_password_applied(base_dir):
    """Forgets that the streaming account holds the password (the account was removed), so the next sharing step sets it up again."""
    path = os.path.join(base_dir, "logs", SHARE_SECRET_FILE_NAME)
    data = _read_json(path)
    if data.get("password"):
        data["applied"] = False
        _write_json(path, data)


class AuthLimiter:
    """Locks an address out for a while after too many failed logins or ignored pairing questions."""

    def __init__(self, max_fails=5, window_s=600.0, clock=time.monotonic):
        self._max, self._window, self._clock = int(max_fails), float(window_s), clock
        self._fails = {}
        self._lock = threading.Lock()

    def note_fail(self, address):
        with self._lock:
            now = self._clock()
            held = [t for t in self._fails.get(address, []) if now - t < self._window]
            held.append(now)
            self._fails[address] = held

    def blocked(self, address):
        with self._lock:
            now = self._clock()
            held = [t for t in self._fails.get(address, []) if now - t < self._window]
            self._fails[address] = held
            return len(held) >= self._max


# ============================================================================
# SECTION: This computer's description (shown to the owner on the approval question)
# ============================================================================


def _cpu_name():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return " ".join(str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).split())
    except Exception:
        return " ".join((platform.processor() or platform.machine() or "unknown CPU").split())


def _ram_gb():
    try:
        if os.name == "nt":
            import ctypes

            class _Mem(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("ptotal", ctypes.c_ulonglong), ("pavail", ctypes.c_ulonglong),
                            ("vtotal", ctypes.c_ulonglong), ("vavail", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]
            mem = _Mem()
            mem.length = ctypes.sizeof(_Mem)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            return int(round(mem.total / 1073741824.0))
        return int(round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1073741824.0))
    except Exception:
        return 0


def machine_info():
    """What the server shows the owner about a computer asking to join: its name, the Windows user, its processor and memory."""
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    return {"host": socket.gethostname(), "user": user, "cpu": _cpu_name(), "ram_gb": _ram_gb(),
            "os": " ".join((platform.system(), platform.release()))}


def describe(info, slot_name=""):
    """The text of the approval question, one fact per line."""
    info = info or {}
    ram = int(info.get("ram_gb") or 0)
    lines = ["%s wants to watch your server." % (str(info.get("host", "") or "A computer"))]
    if slot_name:
        lines.append("Profile asked for: %s" % slot_name)
    lines.append("Windows user: %s" % (info.get("user") or "unknown"))
    lines.append("Processor: %s" % (info.get("cpu") or "unknown"))
    lines.append("Memory: %s" % ("%d GB" % ram if ram else "unknown"))
    return lines


# ============================================================================
# SECTION: The owner's question (shown on the server computer)
# ============================================================================


def ask_owner(info, slot_name, code, shares=False):
    """Shows the owner a Yes/No box (No is the default, always on top) naming the computer, its user, its processor and memory, the profile asked for and the four digits the other screen shows; True only on Yes."""
    if os.name != "nt":
        return False
    import ctypes
    text = "\n".join(describe(info, slot_name)) + "\n\nThe other screen shows this code: %s\n\nLet this computer watch it?" % code
    flags = 0x4 | 0x20 | 0x100 | 0x10000 | 0x40000
    return ctypes.windll.user32.MessageBoxW(0, text, "Retro TV server", flags) == 6
