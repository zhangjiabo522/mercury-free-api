import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tiktoken
from tiktoken.load import load_tiktoken_bpe

from protocol import ProxyError


class TokenCounter:
    def __init__(self):
        ranks = load_tiktoken_bpe(str(Path(__file__).parent / "assets" / "cl100k_base.tiktoken"), expected_hash="223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7")
        self.encoding = tiktoken.Encoding(name="proxy_cl100k_base", pat_str=r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s", mergeable_ranks=ranks, special_tokens={})

    def count(self, text):
        return len(self.encoding.encode_ordinary(text)) if text else 0

    def messages(self, messages):
        return 3 + sum(3 + self.count(message.role) + self.count(message.text()) for message in messages)


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


class Store:
    def __init__(self, directory, api_key, timezone="Asia/Shanghai"):
        self.directory = None if directory == ":memory:" else Path(directory)
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.directory / "proxy.sqlite3") if self.directory else ":memory:", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.timezone = ZoneInfo(timezone)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, created_at REAL NOT NULL, endpoint TEXT NOT NULL,
                model TEXT NOT NULL, status TEXT NOT NULL, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, reasoning_tokens INTEGER NOT NULL,
                latency_ms REAL NOT NULL, first_token_ms REAL, error_code TEXT,
                stream INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS requests_time ON requests(created_at);
            CREATE TABLE IF NOT EXISTS responses (
                id TEXT PRIMARY KEY, created_at REAL NOT NULL, body TEXT NOT NULL, history TEXT NOT NULL
            );
        """)
        if self.directory and os.name != "nt":
            os.chmod(self.directory, 0o700)
            os.chmod(self.directory / "proxy.sqlite3", 0o600)
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('api_key', ?)", (api_key,))
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('session_secret', ?)", (secrets.token_hex(32),))
            if self.get("admin_hash") is None:
                password = os.getenv("ADMIN_PASSWORD") or secrets.token_urlsafe(18)
                salt = secrets.token_hex(16)
                self.set("admin_salt", salt)
                self.set("admin_hash", password_hash(password, salt))
                if self.directory:
                    self.write_private("admin-password.txt", password)
                    print(f"控制台初始管理密码已保存至：{self.directory / 'admin-password.txt'}")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('reasoning_effort', 'medium')")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('web_search', 'false')")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('upstream_timeout', ?)", (os.getenv("UPSTREAM_TIMEOUT", "180"),))
        if self.directory:
            self.write_private("proxy-key.txt", self.get("api_key"))

    def write_private(self, name, value):
        path = self.directory / name
        temporary = self.directory / (name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        temporary.replace(path)

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set(self, key, value):
        with self.lock:
            self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def set_key(self, key):
        with self.lock:
            if self.directory:
                self.write_private("proxy-key.txt", key)
            self.set("api_key", key)

    def check_password(self, password):
        return hmac.compare_digest(password_hash(password, self.get("admin_salt")), self.get("admin_hash"))

    def session(self):
        payload = base64.urlsafe_b64encode(f"{int(time.time()) + 43200}:{secrets.token_hex(16)}".encode()).decode().rstrip("=")
        signature = hmac.new(self.get("session_secret").encode(), payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + signature

    def check_session(self, value):
        try:
            payload, signature = value.split(".", 1)
            expected = hmac.new(self.get("session_secret").encode(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                return False
            decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
            return int(decoded.split(":", 1)[0]) > time.time()
        except (ValueError, UnicodeError):
            return False

    def record(self, entry):
        with self.lock:
            columns = tuple(entry)
            self.db.execute(f"INSERT INTO requests ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(entry.values()))

    def overview(self, selected_date=None, status="all", offset=0):
        try:
            day = date.fromisoformat(selected_date) if selected_date else datetime.now(self.timezone).date()
        except ValueError as exc:
            raise ProxyError("日期格式不正确", 400, "invalid_date") from exc
        start = datetime.combine(day, datetime.min.time(), self.timezone).timestamp()
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), self.timezone).timestamp()
        with self.lock:
            rows = [dict(row) for row in self.db.execute("SELECT * FROM requests WHERE created_at>=? AND created_at<? ORDER BY created_at DESC", (start, end)).fetchall()]
        successes = [row for row in rows if row["status"] == "success"]
        first = [row["first_token_ms"] for row in successes if row["first_token_ms"] is not None]
        hourly = [{"hour": hour, "input_tokens": 0, "output_tokens": 0, "requests": 0} for hour in range(24)]
        for row in rows:
            bucket = hourly[datetime.fromtimestamp(row["created_at"], self.timezone).hour]
            bucket["input_tokens"] += row["input_tokens"]
            bucket["output_tokens"] += row["output_tokens"]
            bucket["requests"] += 1
        filtered = rows if status == "all" else [row for row in rows if row["status"] == status]
        return {
            "date": day.isoformat(), "timezone": str(self.timezone), "estimated": True, "tokenizer": "cl100k_base",
            "summary": {"requests": len(rows), "success": len(successes), "errors": sum(row["status"] == "error" for row in rows), "cancelled": sum(row["status"] == "cancelled" for row in rows), "input_tokens": sum(row["input_tokens"] for row in rows), "output_tokens": sum(row["output_tokens"] for row in rows), "reasoning_tokens": sum(row["reasoning_tokens"] for row in rows), "avg_latency_ms": sum(row["latency_ms"] for row in successes) / len(successes) if successes else None, "avg_first_token_ms": sum(first) / len(first) if first else None},
            "hourly": hourly, "requests": filtered[offset:offset + 20], "total": len(filtered), "offset": offset,
        }

    def save_response(self, body, history):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?)", (body["id"], body["created_at"], json.dumps(body, ensure_ascii=False), json.dumps(history, ensure_ascii=False)))

    def response(self, response_id):
        with self.lock:
            row = self.db.execute("SELECT body,history FROM responses WHERE id=?", (response_id,)).fetchone()
        if row is None:
            raise ProxyError("找不到此响应，可能没有保存或已经删除", 404, "response_not_found")
        return json.loads(row[0]), json.loads(row[1])

    def delete_response(self, response_id):
        with self.lock:
            result = self.db.execute("DELETE FROM responses WHERE id=?", (response_id,))
            if not result.rowcount:
                raise ProxyError("找不到此响应", 404, "response_not_found")

    def close(self):
        self.db.close()
