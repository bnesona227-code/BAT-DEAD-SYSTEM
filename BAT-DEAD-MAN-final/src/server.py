#!/usr/bin/env python3
"""
BAT-DEAD-MAN v1.1 // Autonomous Dead Man's Switch & Emergency Data Vault
"""

import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import platform
import secrets
import subprocess
import threading
import time
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent
VAULT_DIR = BASE_DIR / "vault_safe"
CONFIG_FILE = BASE_DIR / "deadman_config.json"

VAULT_DIR.mkdir(parents=True, exist_ok=True)
state_lock = threading.Lock()

DEFAULT_CONFIG = {
    "pin_salt": "",
    "pin_hash": "",
    "interval_seconds": 3600,
    "lock_screen_on_trigger": False,
    "armed": False,
    "triggered": False,
    "deadline_ts": 0.0,
    "logs": [],
}


def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(data)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


config: Dict[str, Any] = load_config()


def save_config() -> None:
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def add_log(msg: str, level: str = "INFO") -> None:
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] [{level}] {msg}"
    print(entry)
    config["logs"].insert(0, entry)
    config["logs"] = config["logs"][:60]


def hash_pin(pin: str, salt_hex: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), 120_000)
    return dk.hex()


def verify_pin(pin: str) -> bool:
    if not config["pin_hash"] or not config["pin_salt"]:
        return False
    candidate = hash_pin(pin, config["pin_salt"])
    return hmac.compare_digest(candidate, config["pin_hash"])


def derive_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        blk = hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        out.extend(blk)
        counter += 1
    return bytes(out[:length])


def secure_shred_file(filepath: Path) -> None:
    try:
        size = filepath.stat().st_size
        if size > 0:
            with open(filepath, "r+b") as f:
                f.seek(0)
                f.write(os.urandom(size))
                f.flush()
                os.fsync(f.fileno())
                f.seek(0)
                f.write(b"\x00" * size)
                f.flush()
                os.fsync(f.fileno())
        filepath.unlink()
    except Exception as e:
        add_log(f"Ошибка затирания {filepath.name}: {e}", "ERR")


def encrypt_and_shred_file(filepath: Path, master_key: bytes) -> None:
    if filepath.suffix == ".batenc" or not filepath.is_file():
        return
    try:
        raw = filepath.read_bytes()
        nonce = secrets.token_bytes(16)
        stream = derive_keystream(master_key, nonce, len(raw))
        cipher = bytes(a ^ b for a, b in zip(raw, stream))
        mac = hmac.new(master_key, nonce + cipher, hashlib.sha256).digest()

        enc_path = filepath.with_name(filepath.name + ".batenc")
        enc_path.write_bytes(b"BATENC01" + nonce + mac + cipher)
        secure_shred_file(filepath)
        add_log(f"Зашифрован и затёрт оригинал: {filepath.name} -> {enc_path.name}", "SEC")
    except Exception as e:
        add_log(f"Ошибка шифрования {filepath.name}: {e}", "ERR")


def decrypt_file(enc_path: Path, master_key: bytes) -> bool:
    if not enc_path.name.endswith(".batenc") or not enc_path.is_file():
        return False
    try:
        blob = enc_path.read_bytes()
        if len(blob) < 56 or blob[:8] != b"BATENC01":
            add_log(f"Неверный формат заголовка: {enc_path.name}", "ERR")
            return False
        nonce = blob[8:24]
        mac = blob[24:56]
        cipher = blob[56:]

        expected_mac = hmac.new(master_key, nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected_mac):
            add_log(f"Ошибка HMAC (неверный PIN): {enc_path.name}", "ERR")
            return False

        stream = derive_keystream(master_key, nonce, len(cipher))
        plain = bytes(a ^ b for a, b in zip(cipher, stream))

        orig_path = enc_path.with_name(enc_path.name[:-7])
        orig_path.write_bytes(plain)
        enc_path.unlink()
        add_log(f"Расшифрован и восстановлен: {orig_path.name}", "OK")
        return True
    except Exception as e:
        add_log(f"Ошибка расшифровки {enc_path.name}: {e}", "ERR")
        return False


def lock_os_screen() -> None:
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            subprocess.Popen(["pmset", "displaysleepnow"])
            add_log("Экран macOS заблокирован (displaysleepnow).", "SEC")
        elif sys_name == "Windows":
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
            add_log("Рабочая станция Windows заблокирована.", "SEC")
        else:
            subprocess.Popen(["loginctl", "lock-session"])
    except Exception as e:
        add_log(f"Не удалось заблокировать экран ОС: {e}", "WARN")


def execute_deadman_protocol(reason: str) -> None:
    with state_lock:
        if config["triggered"]:
            return
        config["triggered"] = True
        config["armed"] = False
        add_log(f"АКТИВАЦИЯ ПРОТОКОЛА DEAD-MAN ({reason})!", "ALERT")

        master_key = hashlib.sha256(
            (config["pin_hash"] + config["pin_salt"]).encode("utf-8")
        ).digest()

        files = [p for p in VAULT_DIR.rglob("*") if p.is_file() and not p.name.startswith(".")]
        if not files:
            add_log("В папке vault_safe нет файлов для шифрования.", "WARN")
        for f in files:
            encrypt_and_shred_file(f, master_key)

        if config.get("lock_screen_on_trigger"):
            lock_os_screen()

        save_config()


def watchdog_loop() -> None:
    while True:
        time.sleep(0.4)
        should_trigger = False
        with state_lock:
            if config["armed"] and not config["triggered"]:
                if time.time() >= config["deadline_ts"]:
                    should_trigger = True
        if should_trigger:
            execute_deadman_protocol("ТАЙМЕР ИСТЁК (00:00:00)")


def list_vault_files() -> List[Dict[str, Any]]:
    items = []
    for p in sorted(VAULT_DIR.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            st = p.stat()
            items.append(
                {
                    "name": str(p.relative_to(VAULT_DIR)),
                    "size": st.st_size,
                    "encrypted": p.suffix == ".batenc",
                    "mtime": time.strftime("%d.%m %H:%M", time.localtime(st.st_mtime)),
                }
            )
    return items


class DeadManHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = (Path(__file__).parent / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/status":
            with state_lock:
                remaining = max(0, int(config["deadline_ts"] - time.time())) if config["armed"] else config["interval_seconds"]
                payload = {
                    "has_pin": bool(config["pin_hash"]),
                    "armed": config["armed"],
                    "triggered": config["triggered"],
                    "interval_seconds": config["interval_seconds"],
                    "remaining_seconds": remaining,
                    "lock_screen_on_trigger": config["lock_screen_on_trigger"],
                    "vault_path": str(VAULT_DIR),
                    "files": list_vault_files(),
                    "logs": config["logs"],
                }
            self._send_json(payload)
        else:
            self._send_json({"detail": "Not Found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        data = self._read_json()

        if parsed.path == "/api/setup":
            pin = str(data.get("pin") or "").strip()
            interval = int(data.get("interval_seconds") or 3600)
            lock_screen = bool(data.get("lock_screen_on_trigger"))
            if len(pin) < 4:
                self._send_json({"detail": "PIN-код должен содержать минимум 4 символа"}, status=400)
                return
            with state_lock:
                if bool(config["pin_hash"]):
                    old_pin = str(data.get("old_pin") or "").strip()
                    if not verify_pin(old_pin):
                        self._send_json({"detail": "Неверный текущий PIN-код"}, status=403)
                        return
                salt = secrets.token_hex(16)
                config["pin_salt"] = salt
                config["pin_hash"] = hash_pin(pin, salt)
                config["interval_seconds"] = max(10, interval)
                config["lock_screen_on_trigger"] = lock_screen
                add_log(f"Конфигурация обновлена (таймер: {config['interval_seconds']} сек).", "INFO")
                save_config()
            self._send_json({"status": "ok"})

        elif parsed.path == "/api/arm":
            pin = str(data.get("pin") or "").strip()
            with state_lock:
                if not verify_pin(pin):
                    add_log("Неудачная попытка активации (неверный PIN).", "WARN")
                    save_config()
                    self._send_json({"detail": "Неверный PIN-код"}, status=403)
                    return
                if config["triggered"]:
                    self._send_json({"detail": "Сначала расшифруйте сейф после тревоги!"}, status=400)
                    return
                config["armed"] = True
                config["deadline_ts"] = time.time() + config["interval_seconds"]
                add_log(f"ТАЙМЕР ЗАПУЩЕН на {config['interval_seconds']} сек.", "ARMED")
                save_config()
            self._send_json({"status": "ok"})

        elif parsed.path == "/api/checkin":
            pin = str(data.get("pin") or "").strip()
            with state_lock:
                if not verify_pin(pin):
                    add_log("Ошибка чек-ина: введён неверный PIN!", "WARN")
                    save_config()
                    self._send_json({"detail": "Неверный PIN-код"}, status=403)
                    return
                if not config["armed"]:
                    self._send_json({"detail": "Таймер сейчас не активен"}, status=400)
                    return
                config["deadline_ts"] = time.time() + config["interval_seconds"]
                add_log("ЧЕК-ИН ПОДТВЕРЖДЁН. Таймер сброшен.", "OK")
                save_config()
            self._send_json({"status": "ok"})

        elif parsed.path == "/api/disarm":
            pin = str(data.get("pin") or "").strip()
            with state_lock:
                if not verify_pin(pin):
                    add_log("Неудачная попытка снятия с охраны (неверный PIN).", "WARN")
                    save_config()
                    self._send_json({"detail": "Неверный PIN-код"}, status=403)
                    return
                config["armed"] = False
                config["deadline_ts"] = 0.0
                add_log("Система снята с дежурства (DISARMED).", "INFO")
                save_config()
            self._send_json({"status": "ok"})

        elif parsed.path == "/api/panic":
            execute_deadman_protocol("РУЧНАЯ КНОПКА ТРЕВОГИ (PANIC)")
            self._send_json({"status": "triggered"})

        elif parsed.path == "/api/recover":
            pin = str(data.get("pin") or "").strip()
            with state_lock:
                if not verify_pin(pin):
                    add_log("Попытка расшифровки с неверным PIN!", "WARN")
                    save_config()
                    self._send_json({"detail": "Неверный PIN-код"}, status=403)
                    return
                master_key = hashlib.sha256(
                    (config["pin_hash"] + config["pin_salt"]).encode("utf-8")
                ).digest()
                enc_files = [p for p in VAULT_DIR.rglob("*.batenc") if p.is_file()]
                ok_count = sum(1 for f in enc_files if decrypt_file(f, master_key))
                config["triggered"] = False
                config["armed"] = False
                add_log(f"Восстановление завершено (расшифровано файлов: {ok_count}).", "OK")
                save_config()
            self._send_json({"status": "ok"})

        elif parsed.path == "/api/upload":
            filename = os.path.basename(str(data.get("filename") or "file.bin"))
            b64_data = str(data.get("content_b64") or "")
            if not filename or not b64_data:
                self._send_json({"detail": "Файл пуст"}, status=400)
                return
            try:
                raw = base64.b64decode(b64_data)
                dest = VAULT_DIR / filename
                dest.write_bytes(raw)
                with state_lock:
                    add_log(f"Файл помещён в сейф: {filename} ({len(raw)} байт).", "INFO")
                    save_config()
                self._send_json({"status": "ok"})
            except Exception as e:
                self._send_json({"detail": f"Ошибка загрузки: {e}"}, status=400)

        elif parsed.path == "/api/shred-one":
            filename = os.path.basename(str(data.get("filename") or ""))
            target = VAULT_DIR / filename
            if target.exists() and target.is_file():
                secure_shred_file(target)
                with state_lock:
                    add_log(f"Файл безвозвратно затёрт шредером: {filename}", "SEC")
                    save_config()
            self._send_json({"status": "ok"})

        elif parsed.path == "/api/open-vault":
            sys_name = platform.system()
            try:
                if sys_name == "Darwin":
                    subprocess.Popen(["open", str(VAULT_DIR)])
                elif sys_name == "Windows":
                    subprocess.Popen(["explorer", str(VAULT_DIR)])
                else:
                    subprocess.Popen(["xdg-open", str(VAULT_DIR)])
            except Exception as e:
                print(e)
            self._send_json({"status": "ok"})

        else:
            self._send_json({"detail": "Not Found"}, status=404)


if __name__ == "__main__":
    if not any(p for p in VAULT_DIR.iterdir() if not p.name.startswith(".")):
        sample = VAULT_DIR / "secret_notes.txt"
        sample.write_text(
            "CONFIDENTIAL // BAT-DEAD-MAN TEST PAYLOAD\n"
            "Если таймер дойдёт до 00:00:00, этот файл будет зашифрован в .batenc, а оригинал затёрт.\n",
            encoding="utf-8",
        )

    t = threading.Thread(target=watchdog_loop, daemon=True)
    t.start()

    print("=" * 64)
    print(" BAT-DEAD-MAN v1.1 // EMERGENCY DEAD MAN'S SWITCH")
    print("=" * 64)
    print(f"[INFO] Vault Dir : {VAULT_DIR}")
    print("[INFO] Web UI    : http://127.0.0.1:8080")
    print("[INFO] Press Ctrl+C to stop.\n")

    server = ThreadingHTTPServer(("127.0.0.1", 8080), DeadManHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...")
        server.server_close()
